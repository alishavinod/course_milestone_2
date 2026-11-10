import os, re, json, sqlite3, secrets, hashlib, asyncio
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from pydantic import BaseModel

DB='/app/data/huddle.db'
NODE=int(os.getenv('NODE_ID','0'))
app=FastAPI()

def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=10,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c
def init():
    c=db()
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,UNIQUE(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS dms(user1 INTEGER,user2 INTEGER,channel_id INTEGER,UNIQUE(user1,user2));
    '''); c.commit(); c.close()
init()
def userobj(r):
    return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(req):
    h=req.headers.get('authorization','')
    if not h.lower().startswith('bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close()
    return r
def uby(c,uid): return c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
def msgjson(c,r):
    a=uby(c,r['author_id']); reps=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    rx=[]
    for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
        rx.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]})
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':reps,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':rx,'mentions':[]}
def emit(c,ch,typ,payload):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']
    c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(ch,n,typ,json.dumps(payload)))
    return n
def channel(c,i): return c.execute('SELECT * FROM channels WHERE id=?',(i,)).fetchone()
def chanjson(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def member(c,ch,uid): return c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch,uid)).fetchone()
def wsrole(c,w,uid): 
    r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,uid)).fetchone()
    return r['role'] if r else None
def bad(code,msg): return JSONResponse({'error':msg},status_code=code)

@app.get('/')
async def root(): return FileResponse('/app/static/index.html')
@app.get('/static/{path:path}')
async def static(path): return FileResponse('/app/static/'+path)
@app.get('/api/health')
async def health(): return {'status':'ok','node_id':NODE}

@app.post('/api/auth/register')
async def register(req:Request):
    x=await req.json(); name=x.get('username',''); pw=x.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',name) or len(pw)<8:return bad(400,'invalid registration')
    c=db()
    try:
        cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(name,hashlib.sha256(pw.encode()).hexdigest(),x.get('display_name') or name)); uid=cur.lastrowid
        t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit()
    except sqlite3.IntegrityError: c.close(); return bad(409,'duplicate username')
    r=uby(c,uid); c.close(); return JSONResponse({'user':userobj(r),'token':t},201)
@app.post('/api/auth/login')
async def login(req:Request):
    x=await req.json(); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(x.get('username'),hashlib.sha256(x.get('password','').encode()).hexdigest())).fetchone()
    if not r:c.close();return bad(401,'invalid credentials')
    t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return {'user':userobj(r),'token':t}
@app.get('/api/auth/me')
async def me(req:Request):
    r=auth(req)
    return {'user':userobj(r)} if r else bad(401,'unauthorized')

@app.post('/api/workspaces')
async def create_ws(req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    x=await req.json()
    if not re.fullmatch(r'[a-z0-9-]{2,32}',x.get('slug','')):return bad(400,'invalid slug')
    c=db()
    try:
        cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(x['slug'],x.get('name',''),u['id'])); wid=cur.lastrowid
        c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); cc=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=cc.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
    except sqlite3.IntegrityError:c.close();return bad(409,'duplicate workspace')
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=channel(c,cid);c.close()
    return JSONResponse({'workspace':dict(w),'general_channel':chanjson(ch)},201)
@app.get('/api/workspaces')
async def workspaces(req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return {'workspaces':[dict(x) for x in rows]}
@app.get('/api/workspaces/{slug}')
async def getws(slug,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:c.close();return bad(404,'not found')
    rows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)).fetchall();c.close();return {'workspace':dict(w),'channels':[chanjson(x) for x in rows],'read_state':{}}
@app.post('/api/workspaces/{slug}/channels')
async def create_ch(slug,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    x=await req.json();c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();role=wsrole(c,w['id'],u['id']) if w else None
    if not w:return bad(404,'not found')
    if role not in ('owner','admin') and x.get('is_private'):c.close();return bad(403,'forbidden')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',x.get('name','')):c.close();return bad(400,'invalid name')
    if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],x['name'])).fetchone(): c.close(); return bad(409,'duplicate channel')
    try: cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],x['name'],int(bool(x.get('is_private'))),x.get('topic','')));cid=cur.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
    except sqlite3.IntegrityError:c.close();return bad(409,'duplicate channel')
    r=channel(c,cid);c.close();return JSONResponse({'channel':chanjson(r)},201)
@app.post('/api/channels/{cid}/join')
async def join(cid:int,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    c=db();ch=channel(c,cid)
    if not ch:c.close();return bad(404,'not found')
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); role=wsrole(c,w['id'],u['id'])
    if ch['is_private'] and not role:c.close();return bad(403,'private channel')
    if not role:c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
    r=channel(c,cid);c.close();return {'channel':chanjson(r)}
@app.post('/api/channels/{cid}/leave')
async def leave(cid:int,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    c=db();c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id']));c.commit();r=channel(c,cid);c.close()
    return {'channel':chanjson(r)} if r else bad(404,'not found')
@app.get('/api/channels/{cid}/members')
async def chmembers(cid:int,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    c=db(); rows=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall();c.close();return {'members':[userobj(x) for x in rows]}
@app.patch('/api/channels/{cid}')
async def patchch(cid:int,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    x=await req.json();c=db();ch=channel(c,cid)
    if not ch:c.close();return bad(404,'not found')
    if not member(c,cid,u['id']) and wsrole(c,ch['workspace_id'],u['id'])!='owner':c.close();return bad(403,'forbidden')
    if len(x.get('topic',''))>250:c.close();return bad(400,'topic')
    c.execute('UPDATE channels SET topic=? WHERE id=?',(x.get('topic',ch['topic']),cid));c.commit();r=channel(c,cid);c.close();return {'channel':chanjson(r)}
@app.get('/api/channels/{cid}/messages')
async def messages(cid:int,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    c=db()
    if not member(c,cid,u['id']):c.close();return bad(403,'not a member')
    try: limit=min(200,max(1,int(req.query_params.get('limit','50'))))
    except: c.close();return bad(400,'limit')
    rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT ?',(cid,limit)).fetchall();out=[msgjson(c,x) for x in rows];c.close();return {'messages':out,'next_cursor':None}
async def add_message(cid,req,parent=None):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    x=await req.json();body=x.get('body','')
    if not isinstance(body,str) or not body.strip():return bad(400,'empty body')
    c=db();ch=channel(c,cid)
    if not ch:c.close();return bad(404,'not found')
    if not member(c,cid,u['id']):c.close();return bad(403,'not a member')
    if ch['is_archived']:c.close();return bad(423,'archived')
    cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,parent or x.get('parent_id'),now()));mid=cur.lastrowid
    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); typ='message.reply' if r['parent_id'] else 'message.created'; payload={'message':msgjson(c,r)};emit(c,cid,typ,payload);c.commit();out=msgjson(c,r);c.close();return JSONResponse({'message':out},201)
@app.post('/api/channels/{cid}/messages')
async def postmsg(cid:int,req:Request): return await add_message(cid,req)
@app.get('/api/messages/{mid}/replies')
async def replies(mid:int,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    c=db();p=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not p:c.close();return bad(404,'not found')
    rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,)).fetchall();out=[msgjson(c,x) for x in rows];c.close();return {'replies':out,'next_cursor':None}
@app.patch('/api/messages/{mid}')
async def edit(mid:int,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    x=await req.json();c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:c.close();return bad(404,'not found')
    if r['deleted']:c.close();return bad(409,'deleted')
    if r['author_id']!=u['id']:c.close();return bad(403,'forbidden')
    if not x.get('body','').strip():c.close();return bad(400,'empty')
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(x['body'],now(),mid));r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();emit(c,r['channel_id'],'message.updated',{'message':msgjson(c,r)});c.commit();out=msgjson(c,r);c.close();return {'message':out}
@app.delete('/api/messages/{mid}')
async def delete(mid:int,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:c.close();return bad(404,'not found')
    role=wsrole(c,channel(c,r['channel_id'])['workspace_id'],u['id'])
    if r['author_id']!=u['id'] and role!='owner':c.close();return bad(403,'forbidden')
    if not r['deleted']:
        c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,));emit(c,r['channel_id'],'message.deleted',{'message_id':mid});c.commit()
    c.close();return {'deleted':True}
@app.post('/api/messages/{mid}/reactions')
async def react(mid:int,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    x=await req.json();c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:c.close();return bad(404,'not found')
    c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],x.get('emoji','')));c.commit()
    rr=c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall();out=[{'emoji':z['emoji'],'count':z['n'],'user_ids':[int(i) for i in z['ids'].split(',')]} for z in rr];emit(c,r['channel_id'],'reaction.added',{'message_id':mid,'reactions':out});c.commit();c.close();return {'reactions':out}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
async def unreact(mid:int,emoji:str,req:Request):
    u=auth(req)
    if not u:return bad(401,'unauthorized')
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));c.commit();c.close();return {'reactions':[]}
@app.post('/api/dms')
async def dm(req:Request):
    u=auth(req); 
    if not u:return bad(401,'unauthorized')
    x=await req.json();v=int(x['user_id']);a,b=sorted((u['id'],v));c=db();r=c.execute('SELECT channel_id FROM dms WHERE user1=? AND user2=?',(a,b)).fetchone()
    if r:c.close();return {'channel_id':r['channel_id']}
    cur=c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(0,f'dm-{a}-{b}'));cid=cur.lastrowid;c.execute('INSERT INTO dms VALUES(?,?,?)',(a,b,cid));c.execute('INSERT INTO channel_members VALUES(?,?),(?,?)',(cid,a,cid,b));c.commit();c.close();return {'channel_id':cid}
_ws_clients=[]
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
    token=ws.query_params.get('token','')
    c=db(); u=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone();c.close()
    if not u: await ws.close(code=1008); return
    await ws.accept(); subscriptions=set(); last={}
    _ws_clients.append((ws,subscriptions,last))
    async def poll():
        while True:
            await asyncio.sleep(.08)
            c=db()
            for ch in list(subscriptions):
                since=last.get(ch,0)
                rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall()
                for r in rows:
                    p=json.loads(r['payload']); frame={'type':r['type'],'event_id':r['event_id'],'channel_id':ch,**p}
                    await ws.send_json(frame); last[ch]=r['event_id']
            c.close()
    task=asyncio.create_task(poll())
    try:
        while True:
            x=await ws.receive_json(); typ=x.get('type'); ch=int(x.get('channel_id',0))
            if typ=='subscribe':
                subscriptions.add(ch);c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n'];c.close();last[ch]=head
                await ws.send_json({'type':'subscribed','channel_id':ch,'head_event_id':head})
            elif typ=='resume':
                subscriptions.add(ch); since=int(x.get('since_event_id',0));c=db()
                rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall()
                head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n'];c.close()
                for r in rows:
                    p=json.loads(r['payload']);await ws.send_json({'type':r['type'],'event_id':r['event_id'],'channel_id':ch,**p})
                last[ch]=head;await ws.send_json({'type':'resumed','channel_id':ch,'head_event_id':head})
    except (WebSocketDisconnect,Exception): pass
    finally:
        task.cancel()
        if (ws,subscriptions,last) in _ws_clients:_ws_clients.remove((ws,subscriptions,last))
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8000+NODE)
