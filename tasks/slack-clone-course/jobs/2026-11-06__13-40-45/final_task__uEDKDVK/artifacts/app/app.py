import os, sqlite3, secrets, hashlib, re, json, asyncio, datetime
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

DB='/app/data/huddle.db'
NODE=int(os.getenv('NODE_ID','0'))
app=FastAPI()
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])

def db():
    c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); return c
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def init():
    c=db()
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created TEXT,edited TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,UNIQUE(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,content BLOB,created TEXT);''')
    c.commit(); c.close()
init()
def user_obj(r):
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}
def auth(req):
    h=req.headers.get('authorization','')
    if not h.startswith('Bearer '): raise HTTPException(401)
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close()
    if not r: raise HTTPException(401)
    return r
def token_user(t):
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def ws_for(c, wid, uid):
    return c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(wid,uid)).fetchone()
def channel_access(c,ch,uid):
    return c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch,uid)).fetchone()
def ch_obj(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':False,'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def msg_obj(c,r):
    u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    reactions=[]
    for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
        reactions.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]})
    replies=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_obj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created'],'edited_at':r['edited'],'files':[],'reactions':reactions,'mentions':[]}
def emit(c,ch,typ,payload):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']
    c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(ch,n,typ,json.dumps(payload)))
    return n
async def body(req):
    try:return await req.json()
    except: raise HTTPException(400)
@app.get('/')
async def root(): return FileResponse('/app/static/index.html')
@app.get('/api/health')
async def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(req:Request):
    b=await body(req); un=b.get('username',''); pw=b.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise HTTPException(400)
    c=db()
    try:
        c.execute('INSERT INTO users(username,password,display_name,created) VALUES(?,?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un,now()))
        uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit()
    except sqlite3.IntegrityError: c.close(); raise HTTPException(409)
    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return {'user':user_obj(r),'token':t}
@app.post('/api/auth/login')
async def login(req:Request):
    b=await body(req); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest())).fetchone()
    if not r:c.close();raise HTTPException(401)
    t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return {'user':user_obj(r),'token':t}
@app.get('/api/auth/me')
async def me(req:Request): return {'user':user_obj(auth(req))}
@app.post('/api/workspaces',status_code=201)
async def create_ws(req:Request):
    u=auth(req);b=await body(req)
    if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):raise HTTPException(400)
    c=db()
    try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b['name'],u['id']))
    except sqlite3.IntegrityError:c.close();raise HTTPException(409)
    wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return {'workspace':{'id':wid,'slug':w['slug'],'name':w['name'],'owner_id':u['id'],'join_mode':'open'},'general_channel':ch_obj(ch)}
@app.get('/api/workspaces')
async def list_ws(req:Request):
    u=auth(req);c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return {'workspaces':[{'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':'open'} for r in rows]}
@app.get('/api/workspaces/{slug}')
async def get_ws(slug:str,req:Request):
    u=auth(req);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: c.close();raise HTTPException(404)
    rows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)).fetchall();c.close();return {'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':'open'},'channels':[ch_obj(x) for x in rows],'read_state':{}}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def create_channel(slug:str,req:Request):
    u=auth(req);b=await body(req);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w or not ws_for(c,w['id'],u['id']):c.close();raise HTTPException(403)
    if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')):c.close();raise HTTPException(400)
    try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic','')))
    except sqlite3.IntegrityError:c.close();raise HTTPException(409)
    cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return {'channel':ch_obj(r)}
@app.post('/api/channels/{cid}/join')
async def join(cid:int,req:Request):
    u=auth(req);c=db();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not r:c.close();raise HTTPException(404)
    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(r['workspace_id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();c.close();return {'channel':ch_obj(r)}
@app.get('/api/channels/{cid}/messages')
async def messages(cid:int,req:Request):
    u=auth(req); c=db()
    if not channel_access(c,cid,u['id']): c.close(); raise HTTPException(403)
    rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC',(cid,)).fetchall()
    out=[msg_obj(c,r) for r in rows]; c.close(); return {'messages':out[:200],'next_cursor':None}
@app.post('/api/channels/{cid}/messages',status_code=201)
async def post(cid:int,req:Request):
    u=auth(req); b=await body(req); text=b.get('body','')
    if not isinstance(text,str) or not text.strip(): raise HTTPException(400)
    c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch: c.close(); raise HTTPException(404)
    if not channel_access(c,cid,u['id']): c.close(); raise HTTPException(403)
    t=now(); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created) VALUES(?,?,?,?,?)',(cid,u['id'],text,b.get('parent_id'),t))
    mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); obj=msg_obj(c,r)
    typ='message.reply' if b.get('parent_id') else 'message.created'; eid=emit(c,cid,typ,{'message':obj}); c.commit(); c.close()
    return {'message':obj}
@app.patch('/api/messages/{mid}')
async def edit(mid:int,req:Request):
    u=auth(req);b=await body(req);text=b.get('body','')
    if not isinstance(text,str) or not text.strip(): raise HTTPException(400)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r: c.close();raise HTTPException(404)
    if r['deleted']: c.close();raise HTTPException(409)
    if r['author_id']!=u['id']: c.close();raise HTTPException(403)
    t=now();c.execute('UPDATE messages SET body=?,edited=? WHERE id=?',(text,t,mid));r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();obj=msg_obj(c,r);emit(c,r['channel_id'],'message.updated',{'message':obj});c.commit();c.close();return {'message':obj}
@app.delete('/api/messages/{mid}')
async def delete(mid:int,req:Request):
    u=auth(req);c=db();r=c.execute('SELECT m.*,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(mid,)).fetchone()
    if not r:c.close();raise HTTPException(404)
    if r['author_id']!=u['id'] and r['owner_id']!=u['id']:c.close();raise HTTPException(403)
    if not r['deleted']:
        c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,));emit(c,r['channel_id'],'message.deleted',{'message_id':mid});c.commit()
    c.close();return {'deleted':True}
@app.get('/api/messages/{mid}/replies')
async def replies(mid:int,req:Request):
    u=auth(req);c=db();p=c.execute('SELECT channel_id FROM messages WHERE id=?',(mid,)).fetchone()
    if not p or not channel_access(c,p['channel_id'],u['id']):c.close();raise HTTPException(404)
    rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,)).fetchall();out=[msg_obj(c,x) for x in rows];c.close();return {'replies':out,'next_cursor':None}
@app.post('/api/messages/{mid}/reactions')
async def add_reaction(mid:int,req:Request):
    u=auth(req);b=await body(req);em=b.get('emoji','')
    if not em:raise HTTPException(400)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
    if not r:c.close();raise HTTPException(404)
    if not channel_access(c,r['channel_id'],u['id']):c.close();raise HTTPException(403)
    c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],em)); obj=msg_obj(c,r); emit(c,r['channel_id'],'reaction.added',{'message':obj});c.commit();c.close();return {'reactions':obj['reactions']}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
async def remove_reaction(mid:int,emoji:str,req:Request):
    u=auth(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:c.close();raise HTTPException(404)
    c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));obj=msg_obj(c,r);emit(c,r['channel_id'],'reaction.removed',{'message':obj});c.commit();c.close();return {'reactions':obj['reactions']}
@app.get('/api/channels/{cid}/members')
async def channel_members(cid:int,req:Request):
    auth(req);c=db();rows=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall();c.close();return {'members':[user_obj(x) for x in rows]}
@app.post('/api/dms')
async def dms(req:Request):
    u=auth(req);b=await body(req);c=db();other=c.execute('SELECT id FROM users WHERE id=?',(b.get('user_id'),)).fetchone()
    if not other:c.close();raise HTTPException(404)
    # deterministic DM workspace/channel
    slug='dm-'+str(min(u['id'],other['id']))+'-'+str(max(u['id'],other['id']))
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:
        c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,'DM',u['id']));wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'member'));c.execute('INSERT INTO members VALUES(?,?,?)',(wid,other['id'],'member'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'direct'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,other['id']));c.commit()
    else:cid=c.execute('SELECT id FROM channels WHERE workspace_id=?',(w['id'],)).fetchone()['id']
    c.close();return {'channel_id':cid}
@app.get('/api/users/{uid}')
async def get_user(uid:int,req:Request):
    auth(req);c=db();r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close()
    if not r:raise HTTPException(404)
    return {'user':user_obj(r)}
@app.get('/api/search')
async def search(req:Request):
    u=auth(req);q=req.query_params.get('q','');c=db();rows=c.execute('SELECT m.* FROM messages m JOIN channel_members cm ON cm.channel_id=m.channel_id WHERE cm.user_id=? AND m.deleted=0 AND m.body LIKE ? ORDER BY m.id DESC',(u['id'],'%'+q+'%')).fetchall();out=[msg_obj(c,x) for x in rows];c.close();return {'results':out,'next_cursor':None}
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
    t=ws.query_params.get('token','');u=token_user(t)
    if not u: await ws.close(code=1008);return
    await ws.accept(); subscriptions={}
    try:
        while True:
            try:
                data=await asyncio.wait_for(ws.receive_json(),timeout=.25)
                if data.get('type') in ('subscribe','resume'):
                    cid=int(data['channel_id']);c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n'];since=int(data.get('since_event_id',head))
                    if data['type']=='resume':
                        for x in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)):
                            p=json.loads(x['payload']);await ws.send_json({'type':x['type'],'event_id':x['event_id'],'channel_id':cid,**p})
                        await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
                    else: await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
                    subscriptions[cid]=head;c.close()
            except asyncio.TimeoutError: pass
            c=db()
            for cid,last in list(subscriptions.items()):
                for x in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)):
                    p=json.loads(x['payload']);await ws.send_json({'type':x['type'],'event_id':x['event_id'],'channel_id':cid,**p});subscriptions[cid]=x['event_id']
            c.close()
    except (WebSocketDisconnect,Exception): return
from fastapi.staticfiles import StaticFiles
app.mount('/static',StaticFiles(directory='/app/static'),name='static')
@app.patch('/api/channels/{cid}')
async def update_channel(cid:int,req:Request):
    u=auth(req); b=await body(req); c=db()
    r=c.execute('SELECT ch.*,w.owner_id FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(cid,)).fetchone()
    if not r: c.close(); raise HTTPException(404)
    if not channel_access(c,cid,u['id']) and r['owner_id']!=u['id']:
        c.close(); raise HTTPException(403)
    topic=b.get('topic','')
    if not isinstance(topic,str) or len(topic)>250: c.close(); raise HTTPException(400)
    c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,cid)); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.commit(); c.close()
    return {'channel':ch_obj(r)}
