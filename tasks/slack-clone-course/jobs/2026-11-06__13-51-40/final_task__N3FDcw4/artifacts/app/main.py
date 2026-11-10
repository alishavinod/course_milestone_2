import os, sqlite3, json, secrets, hashlib, re, asyncio, threading, io, time
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import JSONResponse, Response, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from zoneinfo import ZoneInfo
DB='/app/data/huddle.sqlite'
NODE=int(os.getenv('NODE_ID','0')); PORT=int(os.getenv('PORT','8000'))
lock=threading.RLock()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row; return c
def init():
 with lock:
  c=db(); c.executescript('''
 PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS wm(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cm(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB,attached INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS mf(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS gm(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
 CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,mention_count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
 CREATE TABLE IF NOT EXISTS ws_presence(connection_id TEXT PRIMARY KEY,user_id INTEGER,seen REAL);
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT);
 '''); c.commit(); c.close()
init()
app=FastAPI(); app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])
sockets=[]; socklock=threading.RLock()
def userjson(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(req):
 h=req.headers.get('authorization','')
 if not h.startswith('Bearer '): raise HTTPException(401)
 with lock:
  c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close()
 if not r: raise HTTPException(401)
 return r
def tokenuser(t):
 with lock:
  c=db(); r=c.execute('SELECT u.* FROM tokens x JOIN users u ON u.id=x.user_id WHERE x.token=?',(t,)).fetchone(); c.close(); return r
def bad(msg='bad request',code=400): raise HTTPException(code,msg)
def chan(cid):
 with lock:
  c=db(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
 if not r: raise HTTPException(404)
 return r
def member(uid,cid):
 with lock:
  c=db(); r=c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone(); c.close()
 return bool(r)
def emit(cid,typ,payload):
 with lock:
  c=db(); n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
  c.execute('INSERT INTO events VALUES(?,?,?,?)',(cid,n,typ,json.dumps(payload))); c.commit(); c.close()
 return n
def reactions(mid):
 with lock:
  c=db(); rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY user_id',(mid,)).fetchall(); c.close()
 out={}
 for r in rows: out.setdefault(r['emoji'],[]).append(r['user_id'])
 return [{'emoji':e,'count':len(ids),'user_ids':ids} for e,ids in out.items()]
def filejson(r): return {k:r[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}
def msgjson(r):
 with lock:
  c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
  fs=[c.execute('SELECT f.* FROM files f JOIN mf ON f.id=mf.file_id WHERE mf.message_id=?',(r['id'],)).fetchone()]
  c.close()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userjson(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':countreplies(r['id']),'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[filejson(x) for x in fs if x],'reactions':reactions(r['id']),'mentions':mention_ids(r['body'],r['channel_id'],r['author_id'])}
def countreplies(mid):
 with lock:
  c=db(); n=c.execute('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(mid,)).fetchone()[0]; c.close(); return n
@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(req:Request):
 b=await req.json()
 if not re.fullmatch(r'[A-Za-z0-9_]+',str(b.get('username',''))) or len(str(b.get('password',''))) < 8: bad()
 with lock:
  c=db()
  if c.execute('SELECT 1 FROM users WHERE username=?',(b['username'],)).fetchone(): c.close(); raise HTTPException(409)
  c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(b['username'],hashlib.sha256(b['password'].encode()).hexdigest(),b.get('display_name') or b['username']))
  uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24)
  c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close()
 return {'user':userjson(u),'token':t}
@app.post('/api/auth/login')
async def login(req:Request):
 b=await req.json()
 with lock:
  c=db(); u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(str(b.get('password','')).encode()).hexdigest())).fetchone()
  if not u: c.close(); raise HTTPException(401)
  t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id'])); c.commit(); c.close()
 return {'user':userjson(u),'token':t}
@app.get('/api/auth/me')
async def me(req:Request): return {'user':userjson(auth(req))}
@app.get('/api/users/{uid}')
async def getuser(uid:int,req:Request):
 auth(req)
 with lock:
  c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close()
 if not u: raise HTTPException(404)
 return {'user':userjson(u)}
@app.patch('/api/users/me')
async def profile(req:Request):
 u=auth(req); b=await req.json(); allowed={'display_name':80,'status_text':100,'status_emoji':32,'avatar_url':1024}
 vals={}
 for k,n in allowed.items():
  if k in b:
   if not isinstance(b[k],str) or len(b[k])>n: bad()
   vals[k]=b[k]
 if 'timezone' in b:
  try: ZoneInfo(b['timezone'])
  except: bad()
  vals['timezone']=b['timezone']
 with lock:
  c=db()
  for k,v in vals.items(): c.execute(f'UPDATE users SET {k}=? WHERE id=?',(v,u['id']))
  c.commit(); x=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); c.close()
 with lock:
  c=db(); c.execute('INSERT INTO global_events(payload) VALUES(?)',(json.dumps({'type':'user.updated','user':userjson(x)}),)); c.commit(); c.close()
 await broadcast({'type':'user.updated','user':userjson(x)})
 return {'user':userjson(x)}
@app.post('/api/workspaces',status_code=201)
async def createws(req:Request):
 u=auth(req); b=await req.json()
 if not re.fullmatch(r'[a-z0-9-]{2,32}',str(b.get('slug',''))) or not b.get('name'): bad()
 with lock:
  c=db()
  if c.execute('SELECT 1 FROM workspaces WHERE slug=?',(b['slug'],)).fetchone(): c.close(); raise HTTPException(409)
  c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b['name'],u['id']))
  wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO wm VALUES(?,?,?)',(wid,u['id'],'owner'))
  c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id']))
  c.commit(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
 return {'workspace':dict(w),'general_channel':dict(ch)}
@app.get('/api/workspaces')
async def listws(req:Request):
 u=auth(req)
 with lock:
  c=db(); rs=c.execute('SELECT w.* FROM workspaces w JOIN wm ON wm.workspace_id=w.id WHERE wm.user_id=?',(u['id'],)).fetchall(); c.close()
 return {'workspaces':[dict(x) for x in rs]}
def chjson(r): return {**dict(r),'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'is_archived':bool(r['is_archived'])}
@app.get('/api/workspaces/{slug}')
async def getws(slug:str,req:Request):
 u=auth(req)
 with lock:
  c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
  if not w: c.close(); raise HTTPException(404)
  rs=c.execute('SELECT c.* FROM channels c JOIN cm ON cm.channel_id=c.id WHERE c.workspace_id=? AND cm.user_id=?',(w['id'],u['id'])).fetchall(); c.close()
 return {'workspace':dict(w),'channels':[chjson(x) for x in rs],'read_state':[]}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def createch(slug:str,req:Request):
 u=auth(req); b=await req.json()
 with lock:
  c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
  if not w: c.close(); raise HTTPException(404)
  role=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
  if not role: c.close(); raise HTTPException(403)
  if not re.fullmatch(r'[a-z0-9-]{1,32}',str(b.get('name',''))) or len(str(b.get('topic','')))>250: c.close(); bad()
  if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],b['name'])).fetchone(): c.close(); raise HTTPException(409)
  if b.get('is_private') and role['role'] not in ('owner','admin'): c.close(); raise HTTPException(403)
  c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic','')))
  cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cm VALUES(?,?)',(cid,u['id'])); c.commit(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
 return {'channel':chjson(x)}
@app.post('/api/channels/{cid}/join')
async def join(cid:int,req:Request):
 u=auth(req); r=chan(cid)
 with lock:
  c=db(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(r['workspace_id'],)).fetchone()
  if r['is_private']: 
   if not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(): c.close(); raise HTTPException(403)
  if not c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(): c.execute('INSERT INTO wm VALUES(?,?,?)',(w['id'],u['id'],'member'))
  c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(cid,u['id'])); c.commit(); c.close()
 return {'channel':chjson(chan(cid))}
@app.get('/api/channels/{cid}/members')
async def members(cid:int,req:Request):
 u=auth(req); chan(cid)
 with lock:
  c=db(); rs=c.execute('SELECT u.* FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)).fetchall(); c.close()
 return {'members':[userjson(x) for x in rs]}
def mention_ids(body,cid,author):
 ids=set()
 with lock:
  c=db()
  users=c.execute('SELECT u.id,u.username FROM users u JOIN cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)).fetchall()
  for r in users:
   if r['id']!=author and re.search(r'(?<![\w@])@'+re.escape(r['username'])+r'\b',body): ids.add(r['id'])
  ch=c.execute('SELECT workspace_id FROM channels WHERE id=?',(cid,)).fetchone()
  if ch:
   gs=c.execute('SELECT g.*,gm.user_id FROM groups_ g JOIN gm ON gm.group_id=g.id WHERE g.workspace_id=?',(ch['workspace_id'],)).fetchall()
   handles=re.findall(r'(?<![\w@])@([A-Za-z0-9_-]+)\b',body)
   for g in gs:
    if g['handle'] in handles and g['user_id']!=author and c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,g['user_id'])).fetchone(): ids.add(g['user_id'])
   if 'here' in handles:
    live=c.execute('SELECT DISTINCT user_id FROM ws_presence WHERE seen>?',(time.time()-3,)).fetchall()
    for x in live:
     if x['user_id']!=author and c.execute('SELECT 1 FROM cm WHERE channel_id=? AND user_id=?',(cid,x['user_id'])).fetchone(): ids.add(x['user_id'])
  c.close()
 return list(ids)
def ensure_access(u,cid):
 r=chan(cid)
 if not member(u['id'],cid): raise HTTPException(403)
 return r
def post_message(cid,u,body,parent=None,file_ids=None):
 r=ensure_access(u,cid)
 if r['is_archived']: raise HTTPException(423)
 if not isinstance(body,str) or not body.strip(): bad()
 with lock:
  c=db(); t=now(); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,parent,t)); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
  if file_ids:
   for fid in file_ids:
    f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
    if not f or f['uploader_id']!=u['id'] or f['attached']: c.close(); bad()
    c.execute('INSERT INTO mf VALUES(?,?)',(mid,fid)); c.execute('UPDATE files SET attached=1 WHERE id=?',(fid,))
  c.commit(); m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); c.close()
 mids=mention_ids(body,cid,u['id'])
 with lock:
  c=db()
  for x in mids:
   c.execute('INSERT OR IGNORE INTO reads(user_id,channel_id) VALUES(?,?)',(x,cid))
   c.execute('UPDATE reads SET mention_count=mention_count+1 WHERE user_id=? AND channel_id=?',(x,cid))
  c.commit(); c.close()
 payload=msgjson(m); typ='message.reply' if parent else 'message.created'; eid=emit(cid,typ,payload); broadcast_event(cid,eid,typ,payload)
 return payload
@app.post('/api/channels/{cid}/messages',status_code=201)
async def create_message(cid:int,req:Request):
 u=auth(req); b=await req.json(); body=b.get('body')
 if not isinstance(body,str): bad()
 if body.startswith('/') and not body.startswith('//'):
  parts=body.split(' ',1); cmd=parts[0]; arg=parts[1] if len(parts)>1 else ''
  if cmd=='/me':
   if not arg: bad()
  elif cmd=='/shrug': body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
  elif cmd=='/topic':
   ensure_access(u,cid)
   if not arg: bad()
   with lock:
    c=db(); c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,cid)); c.commit(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
   return JSONResponse({'message':None,'channel':chjson(x)},201)
  elif cmd=='/archive' or cmd=='/unarchive':
   r=ensure_access(u,cid)
   with lock:
    c=db(); c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if cmd=='/archive' else 0,cid)); c.commit(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
   return JSONResponse({'message':None,'channel':chjson(x)},201)
  elif cmd=='/invite':
   m=re.search(r'@([A-Za-z0-9_]+)',arg)
   if not m: bad()
   with lock:
    c=db(); target=c.execute('SELECT * FROM users WHERE username=?',(m.group(1),)).fetchone()
    if not target: c.close(); raise HTTPException(404)
    r=chan(cid); c.execute('INSERT OR IGNORE INTO wm VALUES(?,?,?)',(r['workspace_id'],target['id'],'member')); c.execute('INSERT OR IGNORE INTO cm VALUES(?,?)',(cid,target['id'])); c.commit(); c.close()
   return JSONResponse({'message':None,'invited_user':userjson(target)},201)
  else: bad()
 return {'message':post_message(cid,u,body,b.get('parent_id'),b.get('file_ids'))}
@app.get('/api/channels/{cid}/messages')
async def list_messages(cid:int,req:Request):
 u=auth(req); ensure_access(u,cid); limit=int(req.query_params.get('limit','50'))
 if limit<1 or limit>200: bad()
 with lock:
  c=db(); rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT ?',(cid,limit)).fetchall(); c.close()
 return {'messages':[msgjson(x) for x in rs],'next_cursor':None}
@app.get('/api/messages/{mid}/replies')
async def replies(mid:int,req:Request):
 u=auth(req)
 with lock:
  c=db(); p=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
  if not p: c.close(); raise HTTPException(404)
  ensure_access(u,p['channel_id']); rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,)).fetchall(); c.close()
 return {'replies':[msgjson(x) for x in rs],'next_cursor':None}
@app.post('/api/messages/{mid}/reactions')
async def addreact(mid:int,req:Request):
 u=auth(req); b=await req.json(); emoji=b.get('emoji')
 if not emoji: bad()
 with lock:
  c=db(); m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
  if not m: c.close(); raise HTTPException(404)
  ensure_access(u,m['channel_id']); c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],emoji)); c.commit(); c.close()
 rs=reactions(mid); eid=emit(m['channel_id'],'reaction.added',{'message_id':mid,'reactions':rs}); broadcast_event(m['channel_id'],eid,'reaction.added',{'message_id':mid,'reactions':rs})
 return {'reactions':rs}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
async def delreact(mid:int,emoji:str,req:Request):
 u=auth(req)
 with lock:
  c=db(); m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
  if not m: c.close(); raise HTTPException(404)
  ensure_access(u,m['channel_id']); c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)); c.commit(); c.close()
 rs=reactions(mid); eid=emit(m['channel_id'],'reaction.removed',{'message_id':mid,'reactions':rs}); broadcast_event(m['channel_id'],eid,'reaction.removed',{'message_id':mid,'reactions':rs})
 return {'reactions':rs}
@app.post('/api/dms')
async def open_dm(req:Request):
 u=auth(req); b=await req.json(); other=b.get('user_id')
 if other is None: bad()
 with lock:
  c=db(); v=c.execute('SELECT * FROM users WHERE id=?',(other,)).fetchone()
  if not v: c.close(); raise HTTPException(404)
  a,bid=sorted((u['id'],int(other))); x=c.execute('SELECT channel_id FROM dms WHERE a=? AND b=?',(a,bid)).fetchone()
  if x: cid=x['channel_id']
  else:
   c.execute('INSERT INTO channels(name,is_dm) VALUES(?,1)',(f'dm-{a}-{bid}',)); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   c.execute('INSERT INTO dms VALUES(?,?,?)',(a,bid,cid)); c.execute('INSERT INTO cm VALUES(?,?),(?,?)',(cid,a,cid,bid))
  c.commit(); c.close()
 return {'channel_id':cid}
@app.post('/api/files',status_code=201)
async def upload(req:Request,file:UploadFile=File(None)):
 u=auth(req)
 if file is None: bad()
 data=await file.read()
 if len(data)>10485760: raise HTTPException(413)
 with lock:
  c=db(); c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],file.filename,file.content_type or 'application/octet-stream',len(data),now(),data)); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit(); r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone(); c.close()
 return {'file':filejson(r)}
@app.get('/api/files/{fid}')
async def file_detail(fid:int,req:Request):
 auth(req)
 with lock:
  c=db(); r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone(); c.close()
 if not r: raise HTTPException(404)
 return {'file':filejson(r)}
@app.get('/api/files/{fid}/download')
async def file_download(fid:int,req:Request):
 auth(req)
 with lock:
  c=db(); r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone(); c.close()
 if not r: raise HTTPException(404)
 return Response(r['data'],media_type=r['content_type'],headers={'Content-Disposition':f'attachment; filename="{r["filename"]}"'})
def groupjson(r):
 with lock:
  c=db(); ids=[x[0] for x in c.execute('SELECT user_id FROM gm WHERE group_id=?',(r['id'],)).fetchall()]; c.close()
 return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':ids}
def wsrow(slug):
 with lock:
  c=db(); r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); c.close()
 if not r: raise HTTPException(404)
 return r
@app.post('/api/workspaces/{slug}/groups',status_code=201)
async def create_group(slug:str,req:Request):
 u=auth(req); w=wsrow(slug); b=await req.json(); h=b.get('handle')
 if not h: bad()
 with lock:
  c=db(); role=c.execute('SELECT role FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
  if not role: c.close(); raise HTTPException(403)
  if c.execute('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone(): c.close(); raise HTTPException(409)
  c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,b.get('name',h),u['id'])); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
  for x in b.get('user_ids',[]): c.execute('INSERT OR IGNORE INTO gm VALUES(?,?)',(gid,x))
  c.commit(); r=c.execute('SELECT * FROM groups_ WHERE id=?',(gid,)).fetchone(); c.close()
 return {'group':groupjson(r)}
@app.get('/api/workspaces/{slug}/groups')
async def list_groups(slug:str,req:Request):
 u=auth(req); w=wsrow(slug)
 with lock:
  c=db(); role=c.execute('SELECT 1 FROM wm WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
  if not role: c.close(); raise HTTPException(403)
  rs=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],)).fetchall(); c.close()
 return {'groups':[groupjson(x) for x in rs]}
@app.patch('/api/workspaces/{slug}/groups/{handle}')
async def update_group(slug:str,handle:str,req:Request):
 u=auth(req); w=wsrow(slug); b=await req.json()
 with lock:
  c=db(); g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
  if not g: c.close(); raise HTTPException(404)
  if g['creator_id']!=u['id'] and w['owner_id']!=u['id']: c.close(); raise HTTPException(403)
  c.execute('UPDATE groups_ SET name=? WHERE id=?',(b.get('name',g['name']),g['id']))
  if 'user_ids' in b:
   c.execute('DELETE FROM gm WHERE group_id=?',(g['id'],))
   for x in b['user_ids']: c.execute('INSERT OR IGNORE INTO gm VALUES(?,?)',(g['id'],x))
  c.commit(); g=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone(); c.close()
 return {'group':groupjson(g)}
@app.delete('/api/workspaces/{slug}/groups/{handle}')
async def delete_group(slug:str,handle:str,req:Request):
 u=auth(req); w=wsrow(slug)
 with lock:
  c=db(); g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
  if g and g['creator_id']!=u['id'] and w['owner_id']!=u['id']: c.close(); raise HTTPException(403)
  if g:
   c.execute('DELETE FROM gm WHERE group_id=?',(g['id'],)); c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],))
  c.commit(); c.close()
 return {'deleted':True}
@app.get('/api/channels/{cid}/read')
async def getread(cid:int,req:Request):
 u=auth(req); ensure_access(u,cid)
 with lock:
  c=db(); c.execute('INSERT OR IGNORE INTO reads(user_id,channel_id) VALUES(?,?)',(u['id'],cid)); r=c.execute('SELECT * FROM reads WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone(); c.close()
 return {'read_state':dict(r)}
async def broadcast(obj):
 with socklock:
  for ws in list(sockets):
   try: await ws.send_json(obj)
   except: pass
async def broadcast_event(cid,eid,typ,payload):
 await broadcast({'type':typ,'event_id':eid,'channel_id':cid,**payload})
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
 t=ws.query_params.get('token',''); u=tokenuser(t)
 if not u: await ws.close(code=4401); return
 await ws.accept()
 conn=secrets.token_hex(12)
 with lock:
  c=db(); c.execute('INSERT OR REPLACE INTO ws_presence VALUES(?,?,?)',(conn,u['id'],time.time())); c.commit(); c.close()
 with socklock: sockets.append(ws)
 subs=set(); sent={}
 with lock:
  c=db(); global_seen=c.execute('SELECT COALESCE(MAX(id),0) FROM global_events').fetchone()[0]; c.close()
 try:
  while True:
   try: obj=await asyncio.wait_for(ws.receive_json(),timeout=0.25)
   except asyncio.TimeoutError:
    obj=None
   if obj:
    typ=obj.get('type'); cid=obj.get('channel_id')
    if typ=='subscribe':
     cid=int(cid); subs.add(cid)
     with lock:
      c=db(); h=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]; c.close()
     sent[cid]=h
     await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':h})
    elif typ=='resume':
     cid=int(cid); since=int(obj.get('since_event_id',0)); subs.add(cid)
     with lock:
      c=db(); es=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall(); h=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]; c.close()
     for e in es: await ws.send_json({'type':e['type'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])})
     sent[cid]=h
     await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':h})
   with lock:
    c=db(); ge=c.execute('SELECT id,payload FROM global_events WHERE id>? ORDER BY id',(global_seen,)).fetchall(); c.close()
   for e in ge:
    await ws.send_json(json.loads(e['payload'])); global_seen=e['id']
   for cid in list(subs):
    after=sent.get(cid,0)
    with lock:
     c=db(); es=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,after)).fetchall(); c.close()
    for e in es:
     await ws.send_json({'type':e['type'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])})
     sent[cid]=e['event_id']
 except (WebSocketDisconnect,Exception): pass
 finally:
  with socklock:
   if ws in sockets: sockets.remove(ws)
  with lock:
   c=db(); c.execute('DELETE FROM ws_presence WHERE connection_id=?',(conn,)); c.commit(); c.close()
@app.get('/')
async def root(): return FileResponse('/app/static/index.html')
if __name__=='__main__':
 import uvicorn
 uvicorn.run(app,host='127.0.0.1',port=PORT)
