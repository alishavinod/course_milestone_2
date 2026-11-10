import os, re, json, sqlite3, secrets, hashlib, asyncio, threading
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles

DB='/app/data/huddle.db'; NODE=int(os.getenv('NODE_ID','0'))
lock=threading.RLock(); app=FastAPI()
app.mount('/static', StaticFiles(directory='/app/static'), name='static')
def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=30000'); return c
def init():
 with lock:
  c=db()
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
  CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
  CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
  CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
  CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
  CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
  CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
  CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
  CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB,attached_message_id INTEGER);
  CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
  CREATE TABLE IF NOT EXISTS group_users(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
  CREATE TABLE IF NOT EXISTS presence(connection_id TEXT PRIMARY KEY,user_id INTEGER,seen REAL);
  CREATE TABLE IF NOT EXISTS readstate(user_id INTEGER,channel_id INTEGER,mention_count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
  CREATE TABLE IF NOT EXISTS broadcasts(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT,created_at REAL);'''); c.commit()
init()
def redis_subscription():
 import socket,time
 while True:
  try:
   z=socket.create_connection(('127.0.0.1',6379),timeout=2)
   z.sendall(b'*2\\r\\n$9\\r\\nSUBSCRIBE\\r\\n$13\\r\\nhuddle-events\\r\\n')
   z.settimeout(2)
   ack=z.recv(4096)
   if b'subscribe' not in ack.lower(): raise OSError('redis subscribe rejected')
   z.settimeout(None)
   while True:
    if not z.recv(4096): break
  except Exception:
   time.sleep(0.5)
  finally:
   try: z.close()
   except Exception: pass
threading.Thread(target=redis_subscription,daemon=True).start()
def q(sql,args=(),one=False):
 with lock:
  c=db(); x=c.execute(sql,args); rows=x.fetchall() if not one else x.fetchone(); c.commit(); return rows
def userobj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(req):
 h=req.headers.get('authorization',''); tok=h[7:] if h.lower().startswith('bearer ') else ''
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,),True)
 if not r: raise HTTPException(401,'unauthorized')
 return r
def bad(msg): raise HTTPException(400,msg)
def channelobj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'],'is_archived':bool(r['is_archived'])}
def reactions(mid):
 rows=q('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY user_id',(mid,))
 out={}
 for x in rows: out.setdefault(x['emoji'],[]).append(x['user_id'])
 return [{'emoji':e,'count':len(v),'user_ids':v} for e,v in out.items()]
def fileobj(r):
 return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def check_attachable(ids, uid):
 if not isinstance(ids,list): bad('file_ids')
 out=[]
 for fid in ids:
  if not isinstance(fid,int): bad('file_ids')
  f=q('SELECT * FROM files WHERE id=?',(fid,),True)
  if not f or f['uploader_id']!=uid or f['attached_message_id'] is not None: bad('file attachment')
  out.append(f)
 return out
def mention_ids(body,cid,author):
 ids=set()
 members={x['id'] for x in q('SELECT user_id id FROM cmembers WHERE channel_id=?',(cid,))}
 for name in re.findall(r'(?<![\\w@])@([A-Za-z0-9_]+)',body):
  if name=='channel': ids.update(members)
  elif name=='here': ids.update(x['user_id'] for x in q('SELECT user_id FROM presence WHERE seen>?',(__import__('time').time()-3,)) if x['user_id'] in members)
  else:
   u=q('SELECT id FROM users WHERE username=?',(name,),True)
   if u and u['id'] in members: ids.add(u['id'])
 c=q('SELECT workspace_id FROM channels WHERE id=?',(cid,),True)
 for h in re.findall(r'(?<![\\w@])@([A-Za-z0-9_-]+)',body):
  g=q('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(c['workspace_id'],h),True)
  if g:
   ids.update(x['user_id'] for x in q('SELECT user_id FROM group_users WHERE group_id=?',(g['id'],)) if x['user_id'] in members)
 ids.discard(author)
 return sorted(ids)
def msgobj(r):
 a=q('SELECT * FROM users WHERE id=?',(r['author_id'],),True)
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':q('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],),True)['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[fileobj(x) for x in q('SELECT * FROM files WHERE attached_message_id=?',(r['id'],))],'reactions':reactions(r['id']),'mentions':mention_ids(r['body'],r['channel_id'],r['author_id'])}
async def emit(cid,typ,payload):
 import time
 for attempt in range(30):
  try:
   with lock:
    c=db()
    c.execute('BEGIN IMMEDIATE')
    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
    c.execute('INSERT INTO events(channel_id,event_id,type,payload,created_at) VALUES(?,?,?,?,?)',(cid,n,typ,json.dumps(payload),now()))
    c.commit(); c.close()
   ev={'type':typ,'event_id':n,'channel_id':cid,**payload}
   for ws in list(conns):
    if ws.subs and cid in ws.subs:
     try: ws.inbox.append(ev)
     except Exception: pass
   return
  except sqlite3.OperationalError:
   time.sleep(0.02)
 raise HTTPException(503,'event store busy')

conns=set()
@app.exception_handler(HTTPException)
async def eh(req,e): return JSONResponse({'error':e.detail},status_code=e.status_code)
@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(req:Request):
 b=await req.json()
 if not re.fullmatch(r'[A-Za-z0-9_]+',b.get('username','')) or len(b.get('password',''))<8: bad('invalid registration')
 try:
  with lock:
   q('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(b['username'],hashlib.sha256(b['password'].encode()).hexdigest(),b.get('display_name') or b['username']))
   u=q('SELECT * FROM users WHERE username=?',(b['username'],),True); t=secrets.token_urlsafe(24); q('INSERT INTO tokens VALUES(?,?)',(t,u['id'],))
  return {'user':userobj(u),'token':t}
 except sqlite3.IntegrityError: raise HTTPException(409,'duplicate username')
@app.post('/api/auth/login')
async def login(req:Request):
 b=await req.json(); u=q('SELECT * FROM users WHERE username=?',(b.get('username'),),True)
 if not u or hashlib.sha256(b.get('password','').encode()).hexdigest()!=u['password']: raise HTTPException(401,'invalid credentials')
 t=secrets.token_urlsafe(24); q('INSERT INTO tokens VALUES(?,?)',(t,u['id'],)); return {'user':userobj(u),'token':t}
@app.get('/api/auth/me')
def me(req:Request): return {'user':userobj(auth(req))}
@app.get('/api/users/{uid}')
def getuser(uid:int,req:Request):
 auth(req); r=q('SELECT * FROM users WHERE id=?',(uid,),True)
 if not r: raise HTTPException(404,'not found')
 return {'user':userobj(r)}
@app.patch('/api/users/me')
async def patchme(req:Request):
 u=auth(req); b=await req.json(); allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
 if b.get('timezone') and len(b['timezone'])>64: bad('timezone')
 vals={k:v for k,v in b.items() if k in allowed}
 limits={'display_name':80,'status_text':100,'status_emoji':32,'avatar_url':1024}
 for k,v in vals.items():
  if not isinstance(v,str) or len(v)>limits.get(k,10**9): bad('field length')
 if 'timezone' in vals:
  try:
   from zoneinfo import ZoneInfo
   ZoneInfo(vals['timezone'])
  except Exception: bad('timezone')
 if vals:
  q('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(u['id'],))
  fresh=userobj(q('SELECT * FROM users WHERE id=?',(u['id'],),True))
  frame={'type':'user.updated','user':fresh}
  q('INSERT INTO broadcasts(payload,created_at) VALUES(?,?)',(json.dumps(frame),__import__('time').time()))
  for ws in list(conns):
   try: await ws.send_json(frame)
   except Exception: pass
 return {'user':userobj(q('SELECT * FROM users WHERE id=?',(u['id'],),True))}
@app.post('/api/workspaces',status_code=201)
async def createws(req:Request):
 u=auth(req); b=await req.json()
 if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')): bad('slug')
 try:
  q('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b.get('name',''),u['id']))
  w=q('SELECT * FROM workspaces WHERE slug=?',(b['slug'],),True); q('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'owner')); q('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w['id'],'general')); ch=q('SELECT * FROM channels WHERE workspace_id=?',(w['id'],),True); q('INSERT INTO cmembers VALUES(?,?)',(ch['id'],u['id']))
  return {'workspace':dict(w),'general_channel':channelobj(ch)}
 except sqlite3.IntegrityError: raise HTTPException(409,'duplicate')
@app.get('/api/workspaces')
def workspaces(req:Request):
 u=auth(req); return {'workspaces':[dict(x) for x in q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],))]}
@app.get('/api/workspaces/{slug}')
def getws(slug:str,req:Request):
 u=auth(req); w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
 if not w: raise HTTPException(404,'not found')
 cs=q('SELECT c.* FROM channels c JOIN cmembers m ON m.channel_id=c.id WHERE c.workspace_id=? AND m.user_id=?',(w['id'],u['id']))
 return {'workspace':dict(w),'channels':[channelobj(x) for x in cs],'read_state':{}}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def createch(slug:str,req:Request):
 u=auth(req); b=await req.json(); w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
 if not w: raise HTTPException(404,'not found')
 if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')) or len(b.get('topic',''))>250: bad('invalid channel')
 if q('SELECT id FROM channels WHERE workspace_id=? AND name=?',(w['id'],b['name']),True): raise HTTPException(409,'duplicate')
 q('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic','')))
 c=q('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],b['name']),True); q('INSERT INTO cmembers VALUES(?,?)',(c['id'],u['id'])); return {'channel':channelobj(c)}
@app.patch('/api/channels/{cid}')
async def patch_channel(cid:int,req:Request):
 u=auth(req); c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
 if not c: raise HTTPException(404,'not found')
 if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True): raise HTTPException(403,'forbidden')
 b=await req.json()
 if 'topic' in b and (not isinstance(b['topic'],str) or len(b['topic'])>250): bad('topic')
 if 'topic' in b: q('UPDATE channels SET topic=? WHERE id=?',(b['topic'],cid))
 return {'channel':channelobj(q('SELECT * FROM channels WHERE id=?',(cid,),True))}
@app.post('/api/channels/{cid}/join')
def join(cid:int,req:Request):
 u=auth(req); c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
 if not c: raise HTTPException(404,'not found')
 q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c['workspace_id'],u['id'],'member')); q('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id'])); return {'channel':channelobj(c)}
@app.get('/api/channels/{cid}/members')
def channel_members(cid:int,req:Request):
 auth(req); c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
 if not c: raise HTTPException(404,'not found')
 rows=q('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(cid,))
 return {'members':[userobj(x) for x in rows]}
@app.post('/api/channels/{cid}/messages',status_code=201)
async def postmsg(cid:int,req:Request):
 u=auth(req); b=await req.json(); c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
 if not c: raise HTTPException(404,'not found')
 body=b.get('body','')
 if body.startswith('/') and not body.startswith('//'):
  parts=body.split(' ',1); cmd=parts[0]; arg=parts[1].strip() if len(parts)>1 else ''
  if cmd=='/me':
   if not arg: bad('text')
  elif cmd=='/shrug':
   body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
  elif cmd=='/topic':
   if not arg: bad('topic')
   q('UPDATE channels SET topic=? WHERE id=?',(arg,cid)); c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   return JSONResponse({'message':None,'channel':channelobj(c)},status_code=201)
  elif cmd=='/invite':
   if not arg.startswith('@'): bad('user')
   un=arg[1:].split()[0]; target=q('SELECT * FROM users WHERE username=?',(un,),True)
   if not target: raise HTTPException(404,'user')
   q('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,target['id'],))
   return JSONResponse({'message':None,'invited_user':userobj(target)},status_code=201)
  elif cmd in ('/archive','/unarchive'):
   w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
   if w['owner_id']!=u['id']: raise HTTPException(403,'forbidden')
   archived=1 if cmd=='/archive' else 0
   q('UPDATE channels SET is_archived=? WHERE id=?',(archived,cid)); c=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   return JSONResponse({'message':None,'channel':channelobj(c)},status_code=201)
  else: bad('unknown command')
 if not body.strip(): bad('empty body')
 if c['is_archived']: raise HTTPException(423,'archived')
 b['body']=body
 ment=mention_ids(body,cid,u['id'])
 for mid in ment: q('INSERT OR IGNORE INTO readstate(user_id,channel_id,mention_count) VALUES(?,?,0)',(mid,cid)); q('UPDATE readstate SET mention_count=mention_count+1 WHERE user_id=? AND channel_id=?',(mid,cid))
 attach=check_attachable(b.get('file_ids',[]),u['id'])
 if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True): raise HTTPException(403,'not member')
 q('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],b['body'],b.get('parent_id'),now())); m=q('SELECT * FROM messages WHERE channel_id=? AND author_id=? ORDER BY id DESC LIMIT 1',(cid,u['id']),True); q('UPDATE files SET attached_message_id=? WHERE id IN (%s)' % (','.join('?'*len(attach)) if attach else 'NULL'),tuple([m['id']]+[x['id'] for x in attach]) if attach else ()) if attach else None; typ='message.reply' if b.get('parent_id') else 'message.created'; obj=msgobj(m); await emit(cid,typ,{'message':obj}); return {'message':obj}
@app.get('/api/channels/{cid}/messages')
def listmsg(cid:int,req:Request,limit:int=50):
 u=auth(req)
 if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True): raise HTTPException(403,'not member')
 rows=q('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT ?',(cid,min(limit,200),)); return {'messages':[msgobj(x) for x in rows],'next_cursor':None}
@app.patch('/api/messages/{mid}')
async def edit_message_route(mid:int,req:Request):
 u=auth(req); m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not m: raise HTTPException(404,'not found')
 if m['author_id']!=u['id']: raise HTTPException(403,'forbidden')
 if m['deleted']: raise HTTPException(409,'deleted')
 b=await req.json()
 if not isinstance(b.get('body'),str) or not b['body'].strip(): bad('empty body')
 stamp=now(); q('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],stamp,mid))
 m=q('SELECT * FROM messages WHERE id=?',(mid,),True); obj=msgobj(m)
 await emit(m['channel_id'],'message.updated',{'message':obj})
 return {'message':obj}
@app.delete('/api/messages/{mid}')
async def delete_message_route(mid:int,req:Request):
 u=auth(req); m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not m: raise HTTPException(404,'not found')
 if m['deleted']: return {'deleted':True}
 c=q('SELECT * FROM channels WHERE id=?',(m['channel_id'],),True)
 w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True) if c and c['workspace_id'] else None
 if m['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']): raise HTTPException(403,'forbidden')
 q('UPDATE messages SET deleted=1 WHERE id=?',(mid,))
 await emit(m['channel_id'],'message.deleted',{'message_id':mid})
 return {'deleted':True}
@app.get('/api/messages/{mid}/replies')
def replies(mid:int,req:Request):
 auth(req); rows=q('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,)); return {'replies':[msgobj(x) for x in rows],'next_cursor':None}
@app.post('/api/messages/{mid}/reactions')
async def addreact(mid:int,req:Request):
 u=auth(req); b=await req.json(); m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not m: raise HTTPException(404,'not found')
 if not b.get('emoji'): bad('emoji')
 q('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],b['emoji'])); await emit(m['channel_id'],'reaction.added',{'message_id':mid,'reactions':reactions(mid)}); return {'reactions':reactions(mid)}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
async def delreact(mid:int,emoji:str,req:Request):
 u=auth(req); m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not m: raise HTTPException(404,'not found')
 q('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)); await emit(m['channel_id'],'reaction.removed',{'message_id':mid,'reactions':reactions(mid)}); return {'reactions':reactions(mid)}
def group_json(g):
 return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in q('SELECT user_id FROM group_users WHERE group_id=?',(g['id'],))]}
def workspace_for(slug):
 w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
 if not w: raise HTTPException(404,'workspace')
 return w
def ensure_member(w,uid):
 if not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid),True): raise HTTPException(403,'forbidden')
@app.post('/api/workspaces/{slug}/groups',status_code=201)
async def create_group(slug:str,req:Request):
 u=auth(req); w=workspace_for(slug); ensure_member(w,u['id']); b=await req.json()
 if not b.get('handle'): bad('handle')
 if q('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(w['id'],b['handle']),True): raise HTTPException(409,'duplicate')
 q('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],b['handle'],b.get('name',''),u['id']))
 g=q('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],b['handle']),True)
 for uid in b.get('user_ids',[]): q('INSERT OR IGNORE INTO group_users VALUES(?,?)',(g['id'],uid))
 return {'group':group_json(g)}
@app.get('/api/workspaces/{slug}/groups')
def list_groups(slug:str,req:Request):
 u=auth(req); w=workspace_for(slug); ensure_member(w,u['id'])
 return {'groups':[group_json(g) for g in q('SELECT * FROM groups WHERE workspace_id=?',(w['id'],))]}
@app.patch('/api/workspaces/{slug}/groups/{handle}')
async def update_group(slug:str,handle:str,req:Request):
 u=auth(req); w=workspace_for(slug); ensure_member(w,u['id']); g=q('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle),True)
 if not g: raise HTTPException(404,'group')
 if g['creator_id']!=u['id'] and w['owner_id']!=u['id']: raise HTTPException(403,'forbidden')
 b=await req.json(); q('UPDATE groups SET name=? WHERE id=?',(b.get('name',g['name']),g['id']))
 if 'user_ids' in b:
  q('DELETE FROM group_users WHERE group_id=?',(g['id'],))
  for uid in b['user_ids']: q('INSERT OR IGNORE INTO group_users VALUES(?,?)',(g['id'],uid))
 return {'group':group_json(q('SELECT * FROM groups WHERE id=?',(g['id'],),True))}
@app.delete('/api/workspaces/{slug}/groups/{handle}')
def delete_group(slug:str,handle:str,req:Request):
 u=auth(req); w=workspace_for(slug); ensure_member(w,u['id']); g=q('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle),True)
 if g and g['creator_id']!=u['id'] and w['owner_id']!=u['id']: raise HTTPException(403,'forbidden')
 if g: q('DELETE FROM groups WHERE id=?',(g['id'],))
 return {'deleted':True}
@app.get('/api/channels/{cid}/read')
def read_state(cid:int,req:Request):
 u=auth(req); r=q('SELECT * FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],cid),True)
 return {'read_state':{'channel_id':cid,'last_read_event_id':0,'unread_count':0,'mention_count':r['mention_count'] if r else 0}}
@app.post('/api/files',status_code=201)
async def upload_file(req:Request):
 u=auth(req)
 try: form=await req.form(); up=form.get('file')
 except Exception: bad('file')
 if up is None or not hasattr(up,'read'): bad('file')
 data=await up.read()
 if len(data)>10485760: raise HTTPException(413,'file too large')
 name=getattr(up,'filename','upload') or 'upload'; ctype=getattr(up,'content_type',None) or 'application/octet-stream'
 q('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],name,ctype,len(data),now(),data))
 f=q('SELECT * FROM files WHERE uploader_id=? ORDER BY id DESC LIMIT 1',(u['id'],),True)
 return {'file':fileobj(f)}
@app.get('/api/files/{fid}')
def file_details(fid:int,req:Request):
 auth(req); f=q('SELECT * FROM files WHERE id=?',(fid,),True)
 if not f: raise HTTPException(404,'not found')
 return {'file':fileobj(f)}
@app.get('/api/files/{fid}/download')
def file_download(fid:int,req:Request):
 auth(req); f=q('SELECT * FROM files WHERE id=?',(fid,),True)
 if not f: raise HTTPException(404,'not found')
 return Response(bytes(f['data']),media_type=f['content_type'],headers={'Content-Disposition':f'attachment; filename="{f["filename"]}"'})
@app.post('/api/dms')
async def dm(req:Request):
 u=auth(req); b=await req.json(); other=b.get('user_id')
 if other is None: bad('user_id')
 if not q('SELECT id FROM users WHERE id=?',(other,),True): raise HTTPException(404,'user')
 a,bb=sorted((u['id'],int(other))); r=q('SELECT channel_id FROM dms WHERE a=? AND b=?',(a,bb),True)
 if r: return {'channel_id':r['channel_id']}
 q('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(None,f'dm-{a}-{bb}')); cid=q('SELECT id FROM channels WHERE workspace_id IS NULL AND name=? ORDER BY id DESC LIMIT 1',(f'dm-{a}-{bb}',),True)['id']; q('INSERT INTO dms VALUES(?,?,?)',(a,bb,cid)); q('INSERT INTO cmembers VALUES(?,?),(?,?)',(cid,a,cid,bb)); return {'channel_id':cid}
@app.get('/')
def root(): return FileResponse('/app/static/index.html')
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
 await ws.accept(); tok=ws.query_params.get('token',''); r=q('SELECT user_id FROM tokens WHERE token=?',(tok,),True)
 if not r: await ws.close(); return
 ws.subs=set(); ws.seen={}; ws.inbox=[]; conns.add(ws); conn=secrets.token_hex(12); q('INSERT INTO presence VALUES(?,?,?)',(conn,r['user_id'],__import__('time').time()))
 async def poll_broadcasts():
  last=0
  cursors={}
  while True:
   try:
    await asyncio.sleep(0.02)
    rows=q('SELECT id,payload FROM broadcasts WHERE id>? ORDER BY id',(last,))
    for row in rows:
     last=row['id']
     await ws.send_json(json.loads(row['payload']))
    for cid in list(ws.subs):
     since=cursors.get(cid,ws.seen.get(cid,0))
     rows=q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since))
     for e in rows:
      frame={'type':e['type'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])}
      await ws.send_json(frame)
      cursors[cid]=e['event_id']
      ws.seen[cid]=e['event_id']
   except asyncio.CancelledError:
    return
   except Exception as ex:
    try: open('/tmp/ws-errors.log','a').write(repr(ex)+'\\n')
    except Exception: pass
    await asyncio.sleep(0.05)

 poll=asyncio.create_task(poll_broadcasts())
 try:
  while True:
   x=json.loads(await ws.receive_text()); cid=int(x.get('channel_id',0)); ws.subs.add(cid); head=q('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,),True)['n']; ws.seen[cid]=head
   if x.get('type')=='resume':
    since=int(x.get('since_event_id',0)); 
    for e in q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)): await ws.send_json({'type':e['type'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])})
    await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
   else: await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
 except (WebSocketDisconnect,Exception):
  conns.discard(ws); q('DELETE FROM presence WHERE connection_id=?',(conn,)); poll.cancel()
