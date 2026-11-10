import os, re, json, uuid, sqlite3, hashlib, secrets, asyncio, mimetypes
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, FileResponse, HTMLResponse
from fastapi.exceptions import RequestValidationError

DB='/app/data/huddle.db'
STORE='/app/data/files'
Path(STORE).mkdir(parents=True,exist_ok=True)
NODE=int(os.getenv('NODE_ID','0'))
app=FastAPI()
connections=[]
def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB, timeout=30, check_same_thread=False, isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=30000'); c.execute('PRAGMA synchronous=NORMAL')
    return c
def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT,attached INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
    CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
    CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,mention_count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    ''')
    c.commit(); c.close()
init()
def js(row):
    return dict(row) if row else None
def user_json(u):
    return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def error(msg,status):
    return JSONResponse({'error':msg},status_code=status)
async def body(request):
    try:return await request.json()
    except:return {}
def auth(request):
    h=request.headers.get('authorization','')
    if not h.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close()
    return r
def require(request):
    u=auth(request)
    if not u: raise PermissionError
    return u
def channel_ok(c,u,cid):
    ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return None
    if ch['is_dm']:
        if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():return None
    elif not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():return None
    return ch
def reactions(c,mid):
    out=[]
    for r in c.execute('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)):
        ids=[x[0] for x in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(mid,r['emoji']))]
        out.append({'emoji':r['emoji'],'count':r['n'],'user_ids':ids})
    return out
def file_json(r):
    return {k:r[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def msg_json(c,r):
    a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    fs=[file_json(x) for x in c.execute('SELECT * FROM files WHERE id IN (SELECT id FROM files WHERE attached=1 AND id IN (SELECT id FROM files WHERE id IN (SELECT id FROM files))) AND id IN (SELECT id FROM files WHERE id IN (SELECT id FROM files))')]
    # attachment lookup is normalized by message_files when available
    try: fs=[file_json(x) for x in c.execute('SELECT f.* FROM files f JOIN message_files m ON m.file_id=f.id WHERE m.message_id=?',(r['id'],))]
    except: pass
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_json(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':reactions(c,r['id']),'mentions':[]}
def emit(c,ch,typ,payload):
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
    c.execute('INSERT INTO events VALUES(?,?,?,?)',(ch,n,typ,json.dumps(payload)))
    c.commit()
    frame={'type':typ,'event_id':n,'channel_id':ch,**payload}
    for w in list(connections):
        if w.get('channel_id')==ch:
            try: asyncio.create_task(w['ws'].send_json(frame))
            except: pass
    return n
def mentions(c,body,ch,author):
    ids=[]
    for name in re.findall(r'(?<![\w@])@([A-Za-z0-9_-]+)',body or ''):
        if name in ('channel','here'):
            if name=='channel': rows=c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(ch,)).fetchall()
            else: rows=c.execute('SELECT DISTINCT p.user_id FROM presence p JOIN channel_members cm ON cm.user_id=p.user_id WHERE cm.channel_id=? AND p.seen>?',(ch,datetime.now().timestamp()-3)).fetchall()
            ids += [x[0] for x in rows]
        else:
            u=c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
            if u and c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch,u[0])).fetchone(): ids.append(u[0])
            g=c.execute('SELECT id FROM groups_ WHERE handle=? AND workspace_id=(SELECT workspace_id FROM channels WHERE id=?)',(name,ch)).fetchone()
            if g: ids += [x[0] for x in c.execute('SELECT gm.user_id FROM group_members gm JOIN channel_members cm ON cm.user_id=gm.user_id WHERE gm.group_id=? AND cm.channel_id=?',(g[0],ch))]
    return sorted(set(x for x in ids if x!=author))
@app.exception_handler(RequestValidationError)
async def validation(request,exc): return error('bad request',400)
@app.exception_handler(PermissionError)
async def denied(request,exc): return error('unauthorized',401)
@app.get('/api/health')
async def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register')
async def register(request:Request):
    b=await body(request); un=b.get('username',''); pw=b.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return error('invalid',400)
    c=db()
    try:
        c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un)); c.commit()
    except sqlite3.IntegrityError:return error('duplicate',409)
    u=c.execute('SELECT * FROM users WHERE username=?',(un,)).fetchone(); t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id'],)); c.commit(); c.close()
    return JSONResponse({'user':user_json(u),'token':t},201)
@app.post('/api/auth/login')
async def login(request:Request):
    b=await body(request); c=db(); u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(str(b.get('password','')).encode()).hexdigest())).fetchone()
    if not u:c.close();return error('invalid',401)
    t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id']));c.commit();c.close();return {'user':user_json(u),'token':t}
@app.get('/api/auth/me')
async def me(request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    return {'user':user_json(u)}
@app.get('/api/users/{uid}')
async def getuser(uid:int,request:Request):
    if not auth(request):return error('unauthorized',401)
    c=db();u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close()
    return {'user':user_json(u)} if u else error('not found',404)
@app.patch('/api/users/me')
async def profile(request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    b=await body(request); allowed={'display_name':80,'status_text':100,'status_emoji':32,'avatar_url':1024}
    for k,n in allowed.items():
        if k in b and (not isinstance(b[k],str) or len(b[k])>n):return error('invalid',400)
    if 'timezone' in b:
        try: ZoneInfo(b['timezone'])
        except:return error('invalid timezone',400)
    c=db(); vals={k:b[k] for k in ('display_name','timezone','avatar_url','status_text','status_emoji') if k in b}
    if vals:c.execute('UPDATE users SET '+','.join(f'{k}=?' for k in vals)+' WHERE id=?',(*vals.values(),u['id']))
    u=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
    if vals:
        c.execute('INSERT INTO profile_events(payload,created_at) VALUES(?,?)',(json.dumps({'type':'user.updated','user':user_json(u)}),datetime.now().timestamp()))
    c.commit();c.close();return {'user':user_json(u)}
@app.post('/api/workspaces')
async def workspace(request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    b=await body(request);slug=b.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not b.get('name'):return error('invalid',400)
    c=db()
    try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b['name'],u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); ch=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(ch,u['id']));c.commit()
    except sqlite3.IntegrityError:c.close();return error('duplicate',409)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();cc=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone();c.close()
    return JSONResponse({'workspace':js(w),'general_channel':js(cc)},201)
@app.get('/api/workspaces')
async def workspaces(request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db();x=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return {'workspaces':[js(a) for a in x]}
@app.get('/api/workspaces/{slug}')
async def workspace_detail(slug:str,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:c.close();return error('not found',404)
    cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],)).fetchall();c.close();return {'workspace':js(w),'channels':[js(x) for x in cs],'read_state':{}}
@app.post('/api/workspaces/{slug}/channels')
async def create_channel(slug:str,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    b=await body(request);c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:c.close();return error('not found',404)
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():c.close();return error('forbidden',403)
    name=b.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(b.get('topic',''))>250:return error('invalid',400)
    try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
    except sqlite3.IntegrityError:c.close();return error('duplicate',409)
    x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return JSONResponse({'channel':js(x)},201)
@app.get('/api/channels/{cid}/members')
async def channel_members(cid:int,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db();ch=channel_ok(c,u,cid)
    if not ch:c.close();return error('not found',404)
    rows=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall()
    c.close();return {'members':[user_json(x) for x in rows]}

@app.post('/api/channels/{cid}/join')
async def join(cid:int,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:c.close();return error('not found',404)
    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();c.close();return {'channel':js(ch)}
@app.post('/api/channels/{cid}/messages')
async def post(cid:int,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    b=await body(request);c=db();ch=channel_ok(c,u,cid)
    if not ch:c.close();return error('not found',404)
    text=b.get('body','')
    if not isinstance(text,str) or not text.strip():c.close();return error('invalid',400)
    if ch['is_archived'] and text != '/unarchive': c.close(); return error('archived',423)
    if text.startswith('/') and not text.startswith('//'):
        if text.startswith('/shrug'): text=(text[6:].strip()+' ¯\\_(ツ)_/¯').strip()
        elif text == '/me' or (text.startswith('/me ') and not text[4:].strip()): c.close(); return error('invalid',400)
        elif text.startswith('/topic '):
            topic=text[7:].strip()
            if not topic or len(topic)>250: c.close(); return error('invalid',400)
            c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,cid)); c.commit()
            fresh=dict(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()); fresh['is_archived']=bool(fresh['is_archived']); c.close()
            return JSONResponse({'message':None,'channel':fresh},201)
        elif text.startswith('/invite '):
            name=text[8:].strip().lstrip('@')
            invited=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
            if not invited: c.close(); return error('not found',404)
            c.execute('INSERT OR IGNORE INTO members(workspace_id,user_id,role) VALUES(?,?,?)',(ch['workspace_id'],invited['id'],'member'))
            c.execute('INSERT OR IGNORE INTO channel_members(channel_id,user_id) VALUES(?,?)',(cid,invited['id'])); c.commit(); c.close()
            return JSONResponse({'message':None,'invited_user':user_json(invited)},201)
        elif text in ('/archive','/unarchive'):
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            if not w or w['owner_id'] != u['id']: c.close(); return error('forbidden',403)
            archived=1 if text=='/archive' else 0
            c.execute('UPDATE channels SET is_archived=? WHERE id=?',(archived,cid)); c.commit()
            fresh=dict(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()); fresh['is_archived']=bool(fresh['is_archived']); c.close()
            return JSONResponse({'message':None,'channel':fresh},201)
        elif text.startswith('/me ') or text.startswith('//'): pass
        else:c.close();return error('invalid',400)
    c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],text,b.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    ids=b.get('file_ids',[])
    for fid in ids:
        f=c.execute('SELECT * FROM files WHERE id=? AND uploader_id=? AND attached=0',(fid,u['id'])).fetchone()
        if not f:c.rollback();c.close();return error('invalid',400)
        c.execute('INSERT INTO message_files VALUES(?,?)',(mid,fid));c.execute('UPDATE files SET attached=1 WHERE id=?',(fid,))
    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); payload={'message':msg_json(c,r)}; payload['message']['mentions']=mentions(c,text,cid,u['id']); [(c.execute('INSERT OR IGNORE INTO reads(user_id,channel_id) VALUES(?,?)',(x,cid)), c.execute('UPDATE reads SET mention_count=mention_count+1 WHERE user_id=? AND channel_id=?',(x,cid))) for x in payload['message']['mentions']]; c.commit(); emit(c,cid,'message.reply' if b.get('parent_id') else 'message.created',payload);c.close();return JSONResponse(payload,201)
def ensure_extra():
    c=db(); c.executescript('''
    CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id));
    CREATE TABLE IF NOT EXISTS presence(connection_id TEXT PRIMARY KEY,user_id INTEGER,seen REAL);
    CREATE TABLE IF NOT EXISTS profile_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT,created_at REAL);
    '''); c.commit(); c.close()
ensure_extra()

@app.get('/api/channels/{cid}/messages')
async def list_messages(cid:int,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db(); ch=channel_ok(c,u,cid)
    if not ch:c.close();return error('not found',404)
    try: limit=int(request.query_params.get('limit','50'))
    except: limit=0
    if limit<1 or limit>200:return error('invalid',400)
    cur=request.query_params.get('cursor')
    if cur and not cur.isdigit():return error('invalid cursor',400)
    q='SELECT * FROM messages WHERE channel_id=? AND deleted=0'
    args=[cid]
    if cur:q+=' AND id<?';args.append(int(cur))
    q+=' ORDER BY id DESC LIMIT ?';args.append(limit+1)
    rows=c.execute(q,args).fetchall(); more=len(rows)>limit; rows=rows[:limit]
    out=[msg_json(c,r) for r in rows]; c.close()
    return {'messages':out,'next_cursor':str(rows[-1]['id']) if more and rows else None}
@app.get('/api/messages/{mid}/replies')
async def replies(mid:int,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db(); parent=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not parent or not channel_ok(c,u,parent['channel_id']):c.close();return error('not found',404)
    rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,)).fetchall();c.close()
    c2=db(); out=[msg_json(c2,r) for r in rows];c2.close();return {'replies':out,'next_cursor':None}
@app.patch('/api/messages/{mid}')
async def edit(mid:int,request:Request):
    u=auth(request); 
    if not u:return error('unauthorized',401)
    b=await body(request); text=b.get('body')
    if not isinstance(text,str) or not text.strip():return error('invalid',400)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:return error('not found',404)
    if r['deleted']:return error('deleted',409)
    if r['author_id']!=u['id']:return error('forbidden',403)
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(text,now(),mid));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();p={'message':msg_json(c,r)};emit(c,r['channel_id'],'message.updated',p);c.close();return p
@app.delete('/api/messages/{mid}')
async def delete(mid:int,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:c.close();return error('not found',404)
    if r['author_id']!=u['id']:return error('forbidden',403)
    if not r['deleted']:
        c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,));c.commit();emit(c,r['channel_id'],'message.deleted',{'message_id':mid})
    c.close();return {'deleted':True}
def add_reaction(message,user,emoji):
    c=db();c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(message['id'],user['id'],emoji));c.commit();out=reactions(c,message['id']);emit(c,message['channel_id'],'reaction.added',{'message_id':message['id'],'reactions':out});c.close();return out
def remove_reaction(message,user,emoji):
    c=db();c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(message['id'],user['id'],emoji));c.commit();out=reactions(c,message['id']);emit(c,message['channel_id'],'reaction.removed',{'message_id':message['id'],'reactions':out});c.close();return out
@app.post('/api/messages/{mid}/reactions')
async def reaction(mid:int,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    b=await body(request);emoji=b.get('emoji')
    if not emoji:return error('invalid',400)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
    if not r:c.close();return error('not found',404)
    if not channel_ok(c,u,r['channel_id']):c.close();return error('forbidden',403)
    c.close();return {'reactions':add_reaction(r,u,emoji)}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
async def unreaction(mid:int,emoji:str,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:c.close();return error('not found',404)
    c.close();return {'reactions':remove_reaction(r,u,emoji)}
def get_or_create_dm(user_a,user_b):
    a,b=sorted((user_a['id'],user_b['id']));c=db();r=c.execute('SELECT channel_id FROM dms WHERE a=? AND b=?',(a,b)).fetchone()
    if r:c.close();return r[0]
    c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(NULL,?,1)',(f'dm-{a}-{b}',));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.execute('INSERT INTO dms VALUES(?,?,?)',(a,b,cid));c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,a));c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,b));c.commit();c.close();return cid
@app.post('/api/dms')
async def dm(request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    b=await body(request)
    if 'user_id' not in b:return error('invalid',400)
    c=db();v=c.execute('SELECT * FROM users WHERE id=?',(b['user_id'],)).fetchone();c.close()
    if not v:return error('not found',404)
    return {'channel_id':get_or_create_dm(u,v)}
@app.post('/api/files')
async def upload(request:Request,file:UploadFile=File(None)):
    u=auth(request)
    if not u:return error('unauthorized',401)
    if file is None:return error('missing file',400)
    data=await file.read()
    if len(data)>10485760:return error('too large',413)
    fid=secrets.randbelow(10**12);path=f'{STORE}/{fid}';open(path,'wb').write(data)
    c=db();ct=file.content_type or 'application/octet-stream';c.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,0)',(fid,u['id'],file.filename or 'file',ct,len(data),now(),path));c.commit();r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close();return JSONResponse({'file':file_json(r)},201)
@app.get('/api/files/{fid}')
async def file_details(fid:int,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db();r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close()
    return {'file':file_json(r)} if r else error('not found',404)
@app.get('/api/files/{fid}/download')
async def download(fid:int,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db();r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close()
    if not r:return error('not found',404)
    return FileResponse(r['path'],media_type=r['content_type'],filename=r['filename'])
def group_retry(fn):
    import time
    for attempt in range(8):
        try: return fn()
        except sqlite3.OperationalError as e:
            if 'locked' not in str(e).lower() or attempt == 7: raise
            time.sleep(0.05 * (attempt + 1))

def group_to_json(c,g):
    ids=[x[0] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],))]
    return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}
@app.get('/api/workspaces/{slug}/groups')
async def groups(slug:str,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:return error('not found',404)
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return error('forbidden',403)
    gs=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall();out=[group_to_json(c,g) for g in gs];c.close();return {'groups':out}
@app.post('/api/workspaces/{slug}/groups')
async def create_group(slug:str,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    b=await body(request);handle=b.get('handle','')
    if not handle or not re.fullmatch(r'[A-Za-z0-9_-]+',handle):return error('invalid',400)
    c=db();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:return error('not found',404)
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return error('forbidden',403)
    try:
        c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],handle,b.get('name',''),u['id']));gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    except sqlite3.IntegrityError:return error('duplicate',409)
    except sqlite3.OperationalError as e:
        if 'locked' in str(e).lower():
            import time
            for _ in range(8):
                time.sleep(0.1)
                try:
                    c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],handle,b.get('name',''),u['id']));gid=c.execute('SELECT last_insert_rowid()').fetchone()[0];break
                except sqlite3.IntegrityError:return error('duplicate',409)
            else:return error('busy',503)
        raise
    for uid in b.get('user_ids',[]):
        if c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,uid))
    c.commit();g=c.execute('SELECT * FROM groups_ WHERE id=?',(gid,)).fetchone();out=group_to_json(c,g);c.close();return JSONResponse({'group':out},201)
@app.patch('/api/workspaces/{slug}/groups/{handle}')
async def update_group(slug:str,handle:str,request:Request):
    u=auth(request); 
    if not u:return error('unauthorized',401)
    b=await body(request);c=db();g=c.execute('SELECT g.*,w.owner_id FROM groups_ g JOIN workspaces w ON w.id=g.workspace_id WHERE w.slug=? AND g.handle=?',(slug,handle)).fetchone()
    if not g:return error('not found',404)
    if g['creator_id']!=u['id'] and g['owner_id']!=u['id']:return error('forbidden',403)
    c.execute('UPDATE groups_ SET name=? WHERE id=?',(b.get('name',g['name']),g['id']));c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
    for x in b.get('user_ids',[]):c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],x))
    c.commit();g=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone();out=group_to_json(c,g);c.close();return {'group':out}
@app.delete('/api/workspaces/{slug}/groups/{handle}')
async def delete_group(slug:str,handle:str,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db();g=c.execute('SELECT g.*,w.owner_id FROM groups_ g JOIN workspaces w ON w.id=g.workspace_id WHERE w.slug=? AND g.handle=?',(slug,handle)).fetchone()
    if g and g['creator_id']!=u['id'] and g['owner_id']!=u['id']:return error('forbidden',403)
    if g:c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));c.commit()
    c.close();return {'deleted':True}
@app.get('/api/channels/{cid}/read')
async def read_state(cid:int,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    c=db();r=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
    if not r:c.execute('INSERT OR IGNORE INTO reads(user_id,channel_id) VALUES(?,?)',(u['id'],cid));c.commit();r=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
    head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0];out={'channel_id':cid,'last_read_event_id':r['last_read_event_id'],'unread_count':max(0,head-r['last_read_event_id']),'mention_count':r['mention_count']};c.close();return {'read_state':out}
@app.post('/api/channels/{cid}/read')
async def mark_read(cid:int,request:Request):
    u=auth(request)
    if not u:return error('unauthorized',401)
    b=await body(request);n=int(b.get('last_read_event_id',0));c=db();c.execute('INSERT OR IGNORE INTO reads(user_id,channel_id) VALUES(?,?)',(u['id'],cid));c.execute('UPDATE reads SET last_read_event_id=MAX(last_read_event_id,?) WHERE user_id=? AND channel_id=?',(n,u['id'],cid));c.commit();r=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone();c.close();return {'read_state':dict(r)}
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
    token=ws.query_params.get('token','');c=db();u=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone();c.close()
    if not u:await ws.close(code=4401);return
    await ws.accept(); ident=secrets.token_hex(8); item={'ws':ws,'user_id':u[0],'channel_id':None};connections.append(item)
    c=db();c.execute('INSERT OR REPLACE INTO presence VALUES(?,?,?)',(ident,u[0],datetime.now().timestamp()));c.commit();c.close()
    try:
        last_profile=0
        while True:
            try:
                x=await asyncio.wait_for(ws.receive_json(), timeout=0.2)
            except asyncio.TimeoutError:
                c=db(); rows=c.execute('SELECT id,payload FROM profile_events WHERE id>? ORDER BY id',(last_profile,)).fetchall(); c.close()
                for pr in rows:
                    last_profile=pr['id']
                    try: await ws.send_json(json.loads(pr['payload']))
                    except: pass
                continue
            typ=x.get('type');cid=x.get('channel_id')
            if typ=='subscribe':
                item['channel_id']=cid;c=db();head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0];c.close();await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
            elif typ=='resume':
                c=db();rows=c.execute('SELECT event_id,type,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,int(x.get('since_event_id',0)))).fetchall();head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0];c.close()
                for r in rows: await ws.send_json({'type':r['type'],'event_id':r['event_id'],'channel_id':cid,**json.loads(r['payload'])})
                await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
    except WebSocketDisconnect: pass
    finally:
        connections.remove(item) if item in connections else None;c=db();c.execute('DELETE FROM presence WHERE connection_id=?',(ident,));c.commit();c.close()
@app.get('/')
async def index(): return HTMLResponse('<html><body><h1>Huddle</h1><div id="app"></div></body></html>')
