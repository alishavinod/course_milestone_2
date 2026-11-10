import os, sqlite3, json, secrets, hashlib, re, asyncio, time, datetime, urllib.parse, mimetypes
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import JSONResponse, Response, HTMLResponse
from starlette.websockets import WebSocketState
from zoneinfo import ZoneInfo

DB='/app/data/huddle.db'; NODE=int(os.getenv('NODE_ID','0')); PORT=int(os.getenv('PORT','8000'))
app=FastAPI()
def db():
    c=sqlite3.connect(DB,timeout=20); c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); return c
def init():
    for attempt in range(30):
        try:
            c=db(); c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB,attached INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
    CREATE TABLE IF NOT EXISTS groupsx(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,users TEXT,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,mentions INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT);
    CREATE TABLE IF NOT EXISTS presence(conn TEXT PRIMARY KEY,user_id INTEGER,seen REAL);
    '''); c.commit(); c.close(); return
        except sqlite3.OperationalError:
            try: c.close()
            except: pass
            time.sleep(.2)
    raise RuntimeError('database initialization failed')
init()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def err(code,msg='bad request'): return JSONResponse({'error':msg},status_code=code)
def userj(r):
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji']}
def token_user(req):
    h=req.headers.get('authorization','')
    if not h.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close(); return r
def need(req):
    u=token_user(req)
    if not u: raise Exception('401')
    return u
async def body(req):
    try: return await req.json()
    except: return {}
def wsj(r):
    return {'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':r['join_mode']}
def chj(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['archived'])}
def reactions(mid):
    c=db(); rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY emoji,user_id',(mid,)).fetchall(); c.close()
    out={}
    for r in rows: out.setdefault(r['emoji'],[]).append(r['user_id'])
    return [{'emoji':e,'count':len(ids),'user_ids':ids} for e,ids in out.items()]
def files_for(mid):
    c=db(); x=c.execute('SELECT f.* FROM files f JOIN messages m ON instr(","||m.body||",",",")=0 WHERE 0',(mid,)).fetchall(); c.close(); return []
def mj(r):
    c=db(); au=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    rr=c.execute('SELECT f.* FROM files f WHERE f.attached=?',(r['id'],)).fetchall(); c.close()
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userj(au),'body':r['body'],'parent_id':r['parent_id'],'reply_count':count_replies(r['id']),'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[fj(x) for x in rr],'reactions':reactions(r['id']),'mentions':resolve_mentions(r['body'],r['channel_id'],r['author_id'])}
def count_replies(mid):
    c=db(); n=c.execute('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(mid,)).fetchone()[0]; c.close(); return n
def fj(r): return {k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def emit(cid,typ,payload):
    c=db(); n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
    c.execute('INSERT INTO events VALUES(?,?,?,?)',(cid,n,typ,json.dumps(payload))); c.commit(); c.close(); return n
def member(cid,uid):
    c=db(); r=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone(); c.close(); return bool(r)
def channel(cid):
    c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return r
def resolve_mentions(text,cid,author):
    c=db(); ch=c.execute('SELECT workspace_id FROM channels WHERE id=?',(cid,)).fetchone(); ids=set()
    for name in re.findall(r'(?<!\w)@([A-Za-z0-9_]+)',text):
        r=c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
        if r and r[0]!=author and c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,r[0])).fetchone(): ids.add(r[0])
    if re.search(r'(?<!\w)@here\b',text):
        for r in c.execute('SELECT DISTINCT user_id FROM presence WHERE seen>?',(time.time()-3,)):
            if r[0]!=author and c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,r[0])).fetchone(): ids.add(r[0])
    for h in re.findall(r'(?<!\w)@([A-Za-z0-9_-]+)',text):
        g=c.execute('SELECT users FROM groupsx WHERE workspace_id=? AND handle=?',(ch[0],h)).fetchone()
        if g:
            for uid in json.loads(g[0]):
                if uid!=author and c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone(): ids.add(uid)
    c.close(); return list(ids)
@app.get('/api/health')
async def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register')
async def register(req:Request):
    b=await body(req); un=b.get('username'); pw=b.get('password','')
    if not isinstance(un,str) or not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return err(400)
    c=db()
    try:
        cur=c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un)); uid=cur.lastrowid
        t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); return JSONResponse({'user':userj(u),'token':t},status_code=201)
    except sqlite3.IntegrityError:return err(409)
    finally:c.close()
@app.post('/api/auth/login')
async def login(req:Request):
    b=await body(req); c=db(); u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(str(b.get('password','')).encode()).hexdigest())).fetchone()
    if not u:c.close(); return err(401)
    t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id'])); c.commit(); c.close(); return {'user':userj(u),'token':t}
@app.get('/api/auth/me')
async def me(req:Request):
    u=token_user(req)
    return {'user':userj(u)} if u else err(401)
@app.get('/api/users/{uid}')
async def get_user(uid:int,req:Request):
    if not token_user(req):return err(401)
    c=db();u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close()
    return {'user':userj(u)} if u else err(404)
@app.patch('/api/users/me')
async def patch_me(req:Request):
    u=token_user(req)
    if not u:return err(401)
    b=await body(req); allowed={'display_name':80,'status_text':100,'status_emoji':32,'avatar_url':1024,'timezone':255}
    if 'timezone' in b:
        try: ZoneInfo(b['timezone'])
        except:return err(400)
    for k,n in allowed.items():
        if k in b and (not isinstance(b[k],str) or len(b[k])>n):return err(400)
    c=db(); vals={k:b[k] for k in allowed if k in b}; 
    if vals:c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),u['id']))
    c.commit(); nu=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); c.execute('INSERT INTO notices(kind,payload) VALUES(?,?)',('user.updated',json.dumps({'type':'user.updated','user':userj(nu)})));c.commit();c.close();return {'user':userj(nu)}
@app.post('/api/workspaces')
async def create_ws(req:Request):
    u=token_user(req)
    if not u:return err(401)
    b=await body(req); slug=b.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not b.get('name'):return err(400)
    c=db()
    try:
        wid=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b['name'],u['id'])).lastrowid;c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'))
        cid=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')).lastrowid;c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();return JSONResponse({'workspace':wsj(w),'general_channel':chj(ch)},status_code=201)
    except sqlite3.IntegrityError:return err(409)
    finally:c.close()
@app.get('/api/workspaces')
async def list_ws(req:Request):
    u=token_user(req)
    if not u:return err(401)
    c=db();rs=c.execute('SELECT w.* FROM workspaces w JOIN wm m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return {'workspaces':[wsj(x) for x in rs]}
def getws(slug):
    c=db();r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();c.close();return r
@app.get('/api/workspaces/{slug}')
async def ws_detail(slug:str,req:Request):
    u=token_user(req);w=getws(slug)
    if not u:return err(401)
    if not w:return err(404)
    c=db(); rs=c.execute('SELECT ch.* FROM channels ch JOIN cm ON cm.channel_id=ch.id WHERE ch.workspace_id=? AND cm.user_id=? AND ch.archived=0',(w['id'],u['id'])).fetchall();c.close();return {'workspace':wsj(w),'channels':[chj(x) for x in rs],'read_state':[]}
@app.post('/api/workspaces/{slug}/channels')
async def create_channel(slug:str,req:Request):
    u=token_user(req);w=getws(slug)
    if not u:return err(401)
    if not w:return err(404)
    c=db();role=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not role:return err(403)
    b=await body(req); name=b.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or (b.get('topic') and len(b['topic'])>250):return err(400)
    if b.get('is_private') and role[0] not in ('owner','admin'):return err(403)
    try:
        cid=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic',''))).lastrowid;c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();return JSONResponse({'channel':chj(r)},status_code=201)
    except sqlite3.IntegrityError:return err(409)
    finally:c.close()
@app.post('/api/channels/{cid}/join')
async def join(cid:int,req:Request):
    u=token_user(req);ch=channel(cid)
    if not u:return err(401)
    if not ch:return err(404)
    c=db(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if ch['is_private']:c.close();return err(403)
    c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(w['id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(cid,u['id']));c.commit();c.close();return {'channel':chj(ch)}
@app.get('/api/channels/{cid}/members')
async def members(cid:int,req:Request):
    if not token_user(req):return err(401)
    c=db();rs=c.execute('SELECT u.* FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)).fetchall();c.close();return {'members':[userj(x) for x in rs]}
def postmsg(cid,uid,text,parent=None,fileids=None):
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return None,404
    if ch['archived'] and not (text.startswith('/unarchive') and not text.startswith('//')):return None,423
    if not member(cid,uid):return None,403
    if text.startswith('/') and not text.startswith('//'):
        p=text.split(' ',1);cmd=p[0];arg=p[1] if len(p)>1 else ''
        if cmd=='/me' and not arg:return None,400
        if cmd=='/shrug':text=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
        elif cmd=='/topic':
            if not arg:return None,400
            c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,cid));c.commit();return {'message':None,'channel':chj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())},201
        elif cmd=='/invite':
            m=re.match(r'@([\w]+)$',arg); x=c.execute('SELECT * FROM users WHERE username=?',(m.group(1) if m else '',)).fetchone() if m else None
            if not x:return None,400
            c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(cid,x['id']));c.commit();return {'message':None,'invited_user':userj(x)},201
        elif cmd in ('/archive','/unarchive'):
            w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            if w['owner_id']!=uid:return None,403
            c.execute('UPDATE channels SET archived=? WHERE id=?',(cmd=='/archive',cid));c.commit();return {'message':None,'channel':chj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())},201
        elif cmd not in ('/me',):return None,400
    if not text:return None,400
    if fileids:
        for fid in fileids:
            f=c.execute('SELECT * FROM files WHERE id=? AND uploader_id=? AND attached=0',(fid,uid)).fetchone()
            if not f:return None,400
    t=now();mid=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,uid,text,parent,t)).lastrowid
    if fileids:
        for fid in fileids:c.execute('UPDATE files SET attached=? WHERE id=?',(mid,fid))
    mids=resolve_mentions(text,cid,uid)
    for target in mids:
        c.execute('INSERT INTO reads(user_id,channel_id,last_read,mentions) VALUES(?,?,0,1) ON CONFLICT(user_id,channel_id) DO UPDATE SET mentions=mentions+1',(target,cid))
    c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); ev=emit(cid,'message.reply' if parent else 'message.created',mj(r));c.close();return mj(r),201
@app.post('/api/channels/{cid}/messages')
async def addmsg(cid:int,req:Request):
    u=token_user(req)
    if not u:return err(401)
    b=await body(req); r,s=postmsg(cid,u['id'],b.get('body',''),b.get('parent_id'),b.get('file_ids'))
    if s!=201:return err(s)
    if isinstance(r,dict) and ('channel' in r or 'invited_user' in r): return JSONResponse(r,status_code=201)
    return JSONResponse({'message':r},status_code=201)
@app.get('/api/channels/{cid}/messages')
async def listmsg(cid:int,req:Request):
    u=token_user(req)
    if not u:return err(401)
    c=db();rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT 200',(cid,)).fetchall();c.close();return {'messages':[mj(x) for x in rs],'next_cursor':None}
@app.get('/api/messages/{mid}/replies')
async def replies(mid:int,req:Request):
    if not token_user(req):return err(401)
    c=db();rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,)).fetchall();c.close();return {'replies':[mj(x) for x in rs],'next_cursor':None}
@app.patch('/api/messages/{mid}')
async def edit(mid:int,req:Request):
    u=token_user(req)
    if not u:return err(401)
    b=await body(req);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:c.close();return err(404)
    if r['author_id']!=u['id']:c.close();return err(403)
    if not b.get('body'):c.close();return err(400)
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),mid));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();c.close();emit(r['channel_id'],'message.updated',mj(r));return {'message':mj(r)}
@app.delete('/api/messages/{mid}')
async def delete(mid:int,req:Request):
    u=token_user(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:c.close();return {'deleted':True}
    w=c.execute('SELECT owner_id FROM workspaces WHERE id=(SELECT workspace_id FROM channels WHERE id=?)',(r['channel_id'],)).fetchone()
    if r['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']):c.close();return err(403)
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,));c.commit();c.close();emit(r['channel_id'],'message.deleted',{'id':mid});return {'deleted':True}
@app.post('/api/messages/{mid}/reactions')
async def addreact(mid:int,req:Request):
    u=token_user(req)
    if not u:return err(401)
    b=await body(req)
    if not b.get('emoji'):return err(400)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
    if not r:c.close();return err(404)
    c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],b['emoji']));c.commit();c.close();rs=reactions(mid);emit(r['channel_id'],'reaction.added',{'message_id':mid,'reactions':rs});return {'reactions':rs}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
async def delreact(mid:int,emoji:str,req:Request):
    u=token_user(req)
    if not u:return err(401)
    emoji=urllib.parse.unquote(emoji);c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:c.close();return err(404)
    c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));c.commit();c.close();rs=reactions(mid);emit(r['channel_id'],'reaction.removed',{'message_id':mid,'reactions':rs});return {'reactions':rs}
@app.post('/api/dms')
async def dm(req:Request):
    u=token_user(req)
    if not u:return err(401)
    b=await body(req); other=b.get('user_id')
    if other is None:return err(400)
    c=db();x=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
    if not x:c.close();return err(404)
    a,bb=sorted((u['id'],other));r=c.execute('SELECT channel_id FROM dms WHERE a=? AND b=?',(a,bb)).fetchone()
    if r:c.close();return {'channel_id':r[0]}
    cid=c.execute('INSERT INTO channels(name,is_dm) VALUES(?,1)',(f'dm-{a}-{bb}',)).lastrowid;c.execute('INSERT INTO dms VALUES(?,?,?)',(a,bb,cid));c.execute('INSERT INTO cm VALUES(?,?),(?,?)',(cid,a,cid,bb));c.commit();c.close();return {'channel_id':cid}
@app.post('/api/files')
async def upload(req:Request,file:UploadFile=File(None)):
    u=token_user(req)
    if not u:return err(401)
    if not file:return err(400)
    data=await file.read()
    if len(data)>10485760:return err(413)
    c=db();fid=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],file.filename,file.content_type or 'application/octet-stream',len(data),now(),data)).lastrowid;c.commit();r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close();return JSONResponse({'file':fj(r)},status_code=201)
@app.get('/api/files/{fid}')
async def fileinfo(fid:int,req:Request):
    if not token_user(req):return err(401)
    c=db();r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close();return {'file':fj(r)} if r else err(404)
@app.get('/api/files/{fid}/download')
async def download(fid:int,req:Request):
    if not token_user(req):return err(401)
    c=db();r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close()
    if not r:return err(404)
    return Response(r['data'],media_type=r['content_type'],headers={'Content-Disposition':f'attachment; filename="{r["filename"]}"'})
@app.get('/api/workspaces/{slug}/groups')
async def groups(slug:str,req:Request):
    u=token_user(req);w=getws(slug)
    if not u:return err(401)
    if not w:return err(404)
    c=db();ok=c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not ok:c.close();return err(403)
    rs=c.execute('SELECT * FROM groupsx WHERE workspace_id=?',(w['id'],)).fetchall();c.close();return {'groups':[gj(x) for x in rs]}
def gj(r):return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['users'])}
@app.post('/api/workspaces/{slug}/groups')
async def groupadd(slug:str,req:Request):
    u=token_user(req);w=getws(slug)
    if not u:return err(401)
    if not w:return err(404)
    c=db();ok=c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not ok:c.close();return err(403)
    b=await body(req)
    if not b.get('handle'):c.close();return err(400)
    if c.execute('SELECT 1 FROM groupsx WHERE workspace_id=? AND handle=?',(w['id'],b['handle'])).fetchone():c.close();return err(409)
    ids=b.get('user_ids',[]);c.execute('INSERT INTO groupsx(workspace_id,handle,name,creator_id,users) VALUES(?,?,?,?,?)',(w['id'],b['handle'],b.get('name',''),u['id'],json.dumps(ids)));c.commit();r=c.execute('SELECT * FROM groupsx WHERE id=last_insert_rowid()').fetchone();c.close();return JSONResponse({'group':gj(r)},status_code=201)
@app.patch('/api/workspaces/{slug}/groups/{handle}')
async def groupedit(slug:str,handle:str,req:Request):
    u=token_user(req);w=getws(slug)
    if not u:return err(401)
    if not w:return err(404)
    c=db();r=c.execute('SELECT * FROM groupsx WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
    if not r:return err(404)
    role=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if r['creator_id']!=u['id'] and (not role or role[0]!='owner'):c.close();return err(403)
    b=await body(req);c.execute('UPDATE groupsx SET name=?,users=? WHERE id=?',(b.get('name',r['name']),json.dumps(b.get('user_ids',json.loads(r['users']))),r['id']));c.commit();r=c.execute('SELECT * FROM groupsx WHERE id=?',(r['id'],)).fetchone();c.close();return {'group':gj(r)}
@app.delete('/api/workspaces/{slug}/groups/{handle}')
async def groupdel(slug:str,handle:str,req:Request):
    u=token_user(req);w=getws(slug)
    if not u:return err(401)
    if not w:return err(404)
    c=db();r=c.execute('SELECT * FROM groupsx WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
    if r:
        role=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
        if r['creator_id']!=u['id'] and (not role or role[0]!='owner'):c.close();return err(403)
        c.execute('DELETE FROM groupsx WHERE id=?',(r['id'],));c.commit()
    c.close();return {'deleted':True}
@app.get('/api/channels/{cid}/read')
async def read(cid:int,req:Request):
    u=token_user(req)
    if not u:return err(401)
    c=db();r=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone();c.close()
    return {'read_state':{'channel_id':cid,'last_read_event_id':r['last_read'] if r else 0,'unread_count':0,'mention_count':r['mentions'] if r else 0}}
@app.post('/api/channels/{cid}/read')
async def mark_read(cid:int,req:Request):
    u=token_user(req)
    if not u:return err(401)
    b=await body(req); n=b.get('last_read_event_id',0)
    if not isinstance(n,int):return err(400)
    c=db(); old=c.execute('SELECT last_read,mentions FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
    val=max(n,old['last_read'] if old else 0)
    c.execute('INSERT INTO reads(user_id,channel_id,last_read,mentions) VALUES(?,?,?,0) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read=?',(u['id'],cid,val,val));c.commit();r=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone();c.close()
    return {'read_state':{'channel_id':cid,'last_read_event_id':r['last_read'],'unread_count':0,'mention_count':r['mentions']}}
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
    token=ws.query_params.get('token',''); c=db();u=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()
    if not u: await ws.close(code=4401); return
    uid=u[0]; await ws.accept(); ident=secrets.token_hex(12); c.execute('INSERT INTO presence VALUES(?,?,?)',(ident,uid,time.time()));c.commit(); c.close()
    channels=set(); last={}; notice=0
    try:
        while True:
            try:
                msg=await asyncio.wait_for(ws.receive_json(),timeout=.25)
                typ=msg.get('type'); cid=msg.get('channel_id')
                if typ in ('subscribe','resume') and cid is not None:
                    channels.add(int(cid)); cc=db(); head=cc.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0];cc.close()
                    if typ=='subscribe': await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
                    else:
                        since=int(msg.get('since_event_id',0));cc=db(); evs=cc.execute('SELECT event_id,type,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall();cc.close()
                        for e in evs:
                            p=json.loads(e['payload']); p.update({'type':e['type'],'event_id':e['event_id'],'channel_id':cid}); await ws.send_json(p)
                        await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
            except asyncio.TimeoutError: pass
            c=db(); c.execute('UPDATE presence SET seen=? WHERE conn=?',(time.time(),ident))
            if channels:
                q=','.join('?'*len(channels)); rows=c.execute(f"SELECT * FROM events WHERE channel_id IN ({q}) ORDER BY channel_id,event_id",tuple(channels)).fetchall()
                for e in rows:
                    if e['event_id']<=last.get(e['channel_id'],0):continue
                    last[e['channel_id']]=e['event_id'];p=json.loads(e['payload']);p.update({'type':e['type'],'event_id':e['event_id'],'channel_id':e['channel_id']});await ws.send_json(p)
            ns=c.execute('SELECT id,kind,payload FROM notices WHERE id>? ORDER BY id',(notice,)).fetchall()
            for n in ns: notice=n['id']; await ws.send_json(json.loads(n['payload']))
            c.commit();c.close()
    except (WebSocketDisconnect,Exception):
        c=db();
        try: c.execute('DELETE FROM presence WHERE conn=?',(ident,)); c.commit()
        except sqlite3.OperationalError: pass
        c.close()
@app.get('/')
async def root():
    return HTMLResponse(open('/app/static/index.html').read())

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='warning')
