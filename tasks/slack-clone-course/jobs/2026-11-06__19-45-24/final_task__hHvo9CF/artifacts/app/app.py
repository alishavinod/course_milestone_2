import os, re, json, sqlite3, hashlib, secrets, datetime, asyncio, threading, mimetypes, time
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

DB='/app/data/huddle.db'; Path('/app/data').mkdir(exist_ok=True); Path('/app/files').mkdir(exist_ok=True)
NODE=int(os.getenv('NODE_ID','0'))
db=sqlite3.connect(DB,check_same_thread=False); db.row_factory=sqlite3.Row
lock=threading.RLock()
def q(sql,args=(),one=False):
 with lock:
  c=db.execute(sql,args); r=c.fetchone() if one else c.fetchall(); db.commit(); return r
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def init():
 q('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '')''')
 q('''CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER)''')
 q('''CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open')''')
 q('''CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id))''')
 q('''CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0)''')
 q('''CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id))''')
 q('''CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0)''')
 q('''CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji))''')
 q('''CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id))''')
 q('''CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b))''')
 q('''CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,path TEXT,created_at TEXT)''')
 q('''CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,UNIQUE(message_id,file_id))''')
 q('''CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER)''')
 q('''CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id))''')
 q('''CREATE TABLE IF NOT EXISTS mention_counts(user_id INTEGER,channel_id INTEGER,count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id))''')
 q('''CREATE TABLE IF NOT EXISTS presence(user_id INTEGER,channel_id INTEGER,updated_at REAL,PRIMARY KEY(user_id,channel_id))''')
 q('''CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,payload TEXT,created_at REAL)''')
init()
try: q('ALTER TABLE messages ADD COLUMN mention_json TEXT')
except: pass
def obj_user(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(req):
 t=req.headers.get('authorization','')
 if not t.lower().startswith('bearer '): raise HTTPException(401)
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t.split(None,1)[1],),True)
 if not r: raise HTTPException(401)
 return r
def token(u): 
 t=secrets.token_urlsafe(24); q('INSERT INTO tokens VALUES(?,?)',(t,u)); return t
def wsrow(slug):
 r=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
 if not r: raise HTTPException(404)
 return r
def chan(cid):
 r=q('SELECT * FROM channels WHERE id=?',(cid,),True)
 if not r: raise HTTPException(404)
 return r
def ensure_member(uid,cid):
 c=chan(cid)
 if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,uid),True): raise HTTPException(403)
 return c
def channel_obj(r):
 d=dict(r); d['is_private']=bool(d['is_private']); d['is_dm']=bool(d['is_dm']); d['is_archived']=bool(d['is_archived']); return d
def userobj(uid):
 return obj_user(q('SELECT * FROM users WHERE id=?',(uid,),True))
def reactions(mid):
 rows=q('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY emoji,user_id',(mid,))
 d={}
 for r in rows: d.setdefault(r['emoji'],[]).append(r['user_id'])
 return [{'emoji':e,'count':len(v),'user_ids':v} for e,v in d.items()]
def msgobj(r):
 d=dict(r); d['author']=userobj(d['author_id']); d['parent_id']=d['parent_id']; d['reply_count']=q('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(d['id'],),True)['n']; d['created_at']=d['created_at']; d['edited_at']=d['edited_at']; d['files']=[dict(x) for x in q('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(d['id'],))]
 d['reactions']=reactions(d['id']); d['mentions']=json.loads(d.get('mention_json') or '[]') if 'mention_json' in d else []
 return d
clients=[]; clients_lock=threading.Lock()
def event(cid,typ,payload):
 n=q('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,),True)['n']
 q('INSERT INTO events VALUES(?,?,?,?,?)',(cid,n,typ,json.dumps(payload),now()))
 return n

def redis_worker():
 while True:
  try:
   import redis
   r=redis.Redis(host='127.0.0.1',port=6379,decode_responses=True)
   ps=r.pubsub(); ps.subscribe('huddle-events')
   for _ in ps.listen(): pass
  except Exception:
   time.sleep(0.5)
threading.Thread(target=redis_worker,daemon=True).start()
app=FastAPI(); app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])
@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(req:Request):
 b=await req.json(); un=b.get('username',''); pw=b.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise HTTPException(400)
 try: q('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,b.get('display_name') or un))
 except sqlite3.IntegrityError: raise HTTPException(409)
 u=q('SELECT * FROM users WHERE username=?',(un,),True); return {'user':obj_user(u),'token':token(u['id'])}
@app.post('/api/auth/login')
async def login(req:Request):
 b=await req.json(); u=q('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password')),True)
 if not u: raise HTTPException(401)
 return {'user':obj_user(u),'token':token(u['id'])}
@app.get('/api/auth/me')
async def me(req:Request): return {'user':obj_user(auth(req))}
@app.get('/api/users/{uid}')
async def getuser(uid:int,req:Request):
 auth(req); u=q('SELECT * FROM users WHERE id=?',(uid,),True)
 if not u: raise HTTPException(404)
 return {'user':obj_user(u)}
@app.patch('/api/users/me')
async def patchme(req:Request):
 u=auth(req); b=await req.json(); allowed=['display_name','timezone','avatar_url','status_text','status_emoji']; vals=[b[k] for k in allowed if k in b]
 if 'timezone' in b:
  try:
   from zoneinfo import ZoneInfo
   ZoneInfo(b['timezone'])
  except: raise HTTPException(400)
 limits={'display_name':80,'status_text':100,'status_emoji':32,'avatar_url':1024}
 if any(len(str(b[k]))>limits[k] for k in limits if k in b): raise HTTPException(400)
 if vals:
  q('UPDATE users SET '+','.join(k+'=?' for k in allowed if k in b)+' WHERE id=?',vals+[u['id']])
  uu=obj_user(q('SELECT * FROM users WHERE id=?',(u['id'],),True)); q('INSERT INTO global_events(type,payload,created_at) VALUES(?,?,?)',('user.updated',json.dumps({'user':uu}),__import__('time').time()))
 return {'user':obj_user(q('SELECT * FROM users WHERE id=?',(u['id'],),True))}
@app.post('/api/workspaces',status_code=201)
async def createws(req:Request):
 u=auth(req); b=await req.json(); slug=b.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): raise HTTPException(400)
 try:q('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b.get('name',''),u['id']))
 except sqlite3.IntegrityError:raise HTTPException(409)
 w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); q('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'owner')); q('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w['id'],'general')); c=q('SELECT * FROM channels WHERE workspace_id=?',(w['id'],),True); q('INSERT INTO cmembers VALUES(?,?)',(c['id'],u['id']))
 return {'workspace':dict(w),'general_channel':dict(c)}
@app.get('/api/workspaces')
async def workspaces(req:Request):
 u=auth(req); return {'workspaces':[dict(x) for x in q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],))]}
@app.get('/api/workspaces/{slug}')
async def getws(slug:str,req:Request):
 u=auth(req); w=wsrow(slug); cs=q('SELECT c.* FROM channels c JOIN cmembers m ON m.channel_id=c.id WHERE c.workspace_id=? AND m.user_id=? AND c.is_archived=0',(w['id'],u['id']))
 return {'workspace':dict(w),'channels':[dict(x) for x in cs],'read_state':{}}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def createchan(slug:str,req:Request):
 u=auth(req); w=wsrow(slug); b=await req.json(); name=b.get('name','')
 if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(b.get('topic',''))>250: raise HTTPException(400)
 if q('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name),True): raise HTTPException(409)
 if b.get('is_private') and q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True)['role'] not in ('owner','admin'): raise HTTPException(403)
 q('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic','')))
 c=q('SELECT * FROM channels WHERE workspace_id=? AND name=?',(w['id'],name),True); q('INSERT INTO cmembers VALUES(?,?)',(c['id'],u['id']))
 return {'channel':channel_obj(c)}
@app.get('/api/channels/{cid}/members')
async def channel_members(cid:int,req:Request):
 u=auth(req); ensure_member(u['id'],cid)
 return {'members':[obj_user(x) for x in q('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(cid,))]}
@app.post('/api/channels/{cid}/join')
async def join(cid:int,req:Request):
 u=auth(req); c=chan(cid); w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
 if c['is_private']: raise HTTPException(403)
 q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member')); q('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id']))
 return {'channel':channel_obj(c)}
@app.post('/api/channels/{cid}/leave')
async def leave(cid:int,req:Request):
 u=auth(req); c=chan(cid); q('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])); return {'channel':channel_obj(c)}
@app.patch('/api/channels/{cid}')
async def patchchan(cid:int,req:Request):
 u=auth(req); c=chan(cid); ensure_member(u['id'],cid); b=await req.json()
 if len(b.get('topic',''))>250: raise HTTPException(400)
 q('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',c['topic']),cid)); return {'channel':channel_obj(chan(cid))}
@app.get('/api/workspaces/{slug}/groups')
async def listgroups(slug:str,req:Request):
 u=auth(req); w=wsrow(slug)
 if not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True): raise HTTPException(403)
 out=[]
 for g in q('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)):
  d={'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in q('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],))]}; out.append(d)
 return {'groups':out}
@app.post('/api/workspaces/{slug}/groups',status_code=201)
async def creategroup(slug:str,req:Request):
 u=auth(req); w=wsrow(slug); b=await req.json(); h=b.get('handle')
 if not h or not b.get('name'): raise HTTPException(400)
 if not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True): raise HTTPException(403)
 if q('SELECT 1 FROM groups WHERE handle=?',(h,),True): raise HTTPException(409)
 q('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,b['name'],u['id']))
 g=q('SELECT * FROM groups WHERE handle=?',(h,),True)
 for uid in b.get('user_ids') or []:
  if q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid),True): q('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],uid))
 return {'group':{'id':g['id'],'handle':h,'name':g['name'],'member_user_ids':[x['user_id'] for x in q('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],))]}}
@app.patch('/api/workspaces/{slug}/groups/{handle}')
async def patchgroup(slug:str,handle:str,req:Request):
 u=auth(req); w=wsrow(slug); g=q('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle),True)
 if not g: raise HTTPException(404)
 if g['creator_id']!=u['id'] and w['owner_id']!=u['id']: raise HTTPException(403)
 b=await req.json(); q('UPDATE groups SET name=? WHERE id=?',(b.get('name',g['name']),g['id']))
 if 'user_ids' in b:
  q('DELETE FROM group_members WHERE group_id=?',(g['id'],))
  for uid in b['user_ids']: q('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],uid))
 g=q('SELECT * FROM groups WHERE id=?',(g['id'],),True); return {'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in q('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],))]}}
@app.delete('/api/workspaces/{slug}/groups/{handle}')
async def deletegroup(slug:str,handle:str,req:Request):
 u=auth(req); w=wsrow(slug); g=q('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle),True)
 if not g: return {'deleted':True}
 if g['creator_id']!=u['id'] and w['owner_id']!=u['id']: raise HTTPException(403)
 q('DELETE FROM group_members WHERE group_id=?',(g['id'],)); q('DELETE FROM groups WHERE id=?',(g['id'],)); return {'deleted':True}
@app.get('/api/channels/{cid}/read')
async def getread(cid:int,req:Request):
 u=auth(req); ensure_member(u['id'],cid); return {'read_state':{'channel_id':cid,'last_read_event_id':0,'unread_count':0,'mention_count':q('SELECT count FROM mention_counts WHERE user_id=? AND channel_id=?',(u['id'],cid),True)['count'] if q('SELECT count FROM mention_counts WHERE user_id=? AND channel_id=?',(u['id'],cid),True) else 0}}
@app.post('/api/channels/{cid}/read')
async def postread(cid:int,req:Request):
 u=auth(req); ensure_member(u['id'],cid); return await getread(cid,req)
@app.get('/api/channels/{cid}/messages')
async def listmsgs(cid:int,req:Request):
 u=auth(req); ensure_member(u['id'],cid)
 rows=q('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC',(cid,))
 return {'messages':[msgobj(x) for x in rows],'next_cursor':None}
@app.post('/api/channels/{cid}/messages',status_code=201)
async def postmsg(cid:int,req:Request):
 u=auth(req); c=ensure_member(u['id'],cid); b=await req.json(); body=b.get('body')
 if c['is_archived'] and not (isinstance(body,str) and body.startswith('/unarchive')): raise HTTPException(423)
 if isinstance(body,str) and body.startswith('/') and not body.startswith('//'):
  parts=body.split(' ',1); cmd=parts[0]; arg=parts[1] if len(parts)>1 else ''
  w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
  if cmd=='/topic':
   if not arg: raise HTTPException(400)
   q('UPDATE channels SET topic=? WHERE id=?',(arg,cid)); return {'message':None,'channel':channel_obj(chan(cid))}
  if cmd=='/invite':
   m=re.fullmatch(r'@([A-Za-z0-9_]+)',arg.strip()); target=q('SELECT * FROM users WHERE username=?',(m.group(1),),True) if m else None
   if not target: raise HTTPException(400)
   q('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,target['id'])); q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c['workspace_id'],target['id'],'member'))
   return {'message':None,'invited_user':obj_user(target)}
  if cmd in ('/archive','/unarchive'):
   if u['id']!=w['owner_id']: raise HTTPException(403)
   q('UPDATE channels SET is_archived=? WHERE id=?',(1 if cmd=='/archive' else 0,cid))
   return {'message':None,'channel':channel_obj(chan(cid))}
 if b.get('file_ids') is not None:
  if not isinstance(b.get('file_ids'),list): raise HTTPException(400)
  for fid in b['file_ids']:
   f=q('SELECT * FROM files WHERE id=?',(fid,),True)
   if not f or f['uploader_id']!=u['id'] or q('SELECT 1 FROM message_files WHERE file_id=?',(fid,),True): raise HTTPException(400)
 if not isinstance(body,str) or not body.strip(): raise HTTPException(400)
 if body.startswith('/') and not body.startswith('//'):
  if body.startswith('/shrug'):
   tail=body[6:].lstrip(); body=tail+(' ' if tail else '')+'¯\\_(ツ)_/¯'
  elif body.startswith('/me') and len(body.strip())==3: raise HTTPException(400)
  elif body.startswith('/me '): pass
  elif body.startswith('/'): raise HTTPException(400)
 mentions=set()
 for m0 in re.finditer(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body):
  h=m0.group(1)
  if h in ('channel','here'):
   ids=[x['user_id'] for x in q('SELECT user_id FROM cmembers WHERE channel_id=?',(cid,))]
   if h=='here':
    ids=[i for i in ids if q('SELECT 1 FROM presence WHERE user_id=? AND updated_at>?',(i,__import__('time').time()-5),True)]
  else:
   tu=q('SELECT id FROM users WHERE username=?',(h,),True); ids=[tu['id']] if tu and q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,tu['id']),True) else []
   ids=[x['user_id'] for x in q('SELECT gm.user_id FROM group_members gm JOIN groups g ON g.id=gm.group_id JOIN cmembers cm ON cm.user_id=gm.user_id WHERE g.workspace_id=? AND g.handle=? AND cm.channel_id=?',(c['workspace_id'],h,cid))]
  mentions.update(i for i in ids if i!=u['id'])
 q('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mention_json) VALUES(?,?,?,?,?,?)',(cid,u['id'],body,b.get('parent_id'),now(),json.dumps(list(mentions))))
 for mid in mentions: q('INSERT INTO mention_counts(user_id,channel_id,count) VALUES(?,?,1) ON CONFLICT(user_id,channel_id) DO UPDATE SET count=count+1',(mid,cid))

 m=q('SELECT * FROM messages WHERE channel_id=? AND author_id=? ORDER BY id DESC LIMIT 1',(cid,u['id']),True)
 for fid in b.get('file_ids') or []: q('INSERT INTO message_files VALUES(?,?)',(m['id'],fid))
 mo=msgobj(m)
 event(cid,'message.reply' if b.get('parent_id') else 'message.created',{'message':mo})
 return {'message':mo}
@app.get('/api/messages/{mid}/replies')
async def replies(mid:int,req:Request):
 u=auth(req); m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not m: raise HTTPException(404)
 ensure_member(u['id'],m['channel_id'])
 return {'replies':[msgobj(x) for x in q('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,))],'next_cursor':None}
@app.patch('/api/messages/{mid}')
async def edit(mid:int,req:Request):
 u=auth(req); m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not m: raise HTTPException(404)
 if m['author_id']!=u['id']: raise HTTPException(403)
 if m['deleted']: raise HTTPException(409)
 b=await req.json()
 if not isinstance(b.get('body'),str) or not b['body'].strip(): raise HTTPException(400)
 q('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],now(),mid)); mo=msgobj(q('SELECT * FROM messages WHERE id=?',(mid,),True))
 event(m['channel_id'],'message.updated',{'message':mo}); return {'message':mo}
@app.delete('/api/messages/{mid}')
async def delete(mid:int,req:Request):
 u=auth(req); m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not m: raise HTTPException(404)
 c=q('SELECT * FROM channels WHERE id=?',(m['channel_id'],),True); w=q('SELECT owner_id FROM workspaces WHERE id=?',(c['workspace_id'],),True) if c and c['workspace_id'] else None
 if m['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']): raise HTTPException(403)
 if not m['deleted']:
  q('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); event(m['channel_id'],'message.deleted',{'message_id':mid})
 return {'deleted':True}
@app.post('/api/messages/{mid}/reactions')
async def addreact(mid:int,req:Request):
 u=auth(req); m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not m: raise HTTPException(404)
 ensure_member(u['id'],m['channel_id']); b=await req.json()
 if not b.get('emoji'): raise HTTPException(400)
 q('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],b['emoji']))
 event(m['channel_id'],'reaction.added',{'message_id':mid,'reactions':reactions(mid)})
 return {'reactions':reactions(mid)}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
async def delreact(mid:int,emoji:str,req:Request):
 u=auth(req); m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
 if not m: raise HTTPException(404)
 ensure_member(u['id'],m['channel_id']); import urllib.parse
 emoji=urllib.parse.unquote(emoji); q('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji))
 event(m['channel_id'],'reaction.removed',{'message_id':mid,'reactions':reactions(mid)})
 return {'reactions':reactions(mid)}
@app.post('/api/dms')
async def dm(req:Request):
 u=auth(req); b=await req.json(); oid=b.get('user_id')
 if not oid: raise HTTPException(400)
 if not q('SELECT id FROM users WHERE id=?',(oid,),True): raise HTTPException(404)
 a,bb=sorted((u['id'],oid)); r=q('SELECT channel_id FROM dms WHERE a=? AND b=?',(a,bb),True)
 if r:return {'channel_id':r['channel_id']}
 q('INSERT INTO channels(workspace_id,name,is_dm) VALUES(NULL,?,1)',(f'dm-{a}-{bb}',)); c=q('SELECT id FROM channels WHERE id=last_insert_rowid()',(),True)
 q('INSERT INTO dms VALUES(?,?,?)',(a,bb,c['id'])); q('INSERT INTO cmembers VALUES(?,?),(?,?)',(c['id'],a,c['id'],bb))
 return {'channel_id':c['id']}
@app.post('/api/files',status_code=201)
async def upload(req:Request,file:UploadFile=File(None)):
 u=auth(req)
 if file is None: raise HTTPException(400)
 data=await file.read()
 if len(data)>10*1024*1024: raise HTTPException(413)
 fid=secrets.randbelow(900000000)+100000000; path=f'/app/files/{fid}'
 open(path,'wb').write(data); q('INSERT INTO files VALUES(?,?,?,?,?,?,?)',(fid,u['id'],file.filename,file.content_type or 'application/octet-stream',len(data),path,now()))
 return {'file':dict(q('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,),True))}
@app.get('/api/files/{fid}')
async def getfile(fid:int,req:Request):
 auth(req); r=q('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,),True)
 if not r: raise HTTPException(404)
 return {'file':dict(r)}
@app.get('/api/files/{fid}/download')
async def download(fid:int,req:Request):
 auth(req); r=q('SELECT * FROM files WHERE id=?',(fid,),True)
 if not r: raise HTTPException(404)
 return FileResponse(r['path'],media_type=r['content_type'],filename=r['filename'])
@app.websocket('/api/ws')
async def websocket(w:WebSocket):
 t=w.query_params.get('token'); r=q('SELECT user_id FROM tokens WHERE token=?',(t,),True)
 if not r: await w.close(code=4401); return
 await w.accept(); w._loop=asyncio.get_running_loop(); subs=set(); sent={}; global_seen=q('SELECT COALESCE(MAX(id),0) n FROM global_events',(),True)['n']
 with clients_lock: clients.append((w,subs,r['user_id']))
 q('INSERT OR REPLACE INTO presence VALUES(?,?,?)',(r['user_id'],0,__import__('time').time()))
 try:
  while True:
   try:
    x=json.loads(await asyncio.wait_for(w.receive_text(),0.25))
   except asyncio.TimeoutError:
    for cc in list(subs):
     last=sent.get(cc,0)
     for ge in q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cc,last)):
      await w.send_json({'type':ge['type'],'event_id':ge['event_id'],'channel_id':cc,**json.loads(ge['payload'])}); sent[cc]=ge['event_id']
    for ge in q('SELECT * FROM global_events WHERE id>? ORDER BY id',(global_seen,)):
     await w.send_json({'type':ge['type'],**json.loads(ge['payload'])})
    continue
   x=json.loads(x) if isinstance(x,str) else x; typ=x.get('type'); cid=x.get('channel_id')
   q('INSERT OR REPLACE INTO presence VALUES(?,?,?)',(r['user_id'],cid or 0,__import__('time').time()))
   if typ=='subscribe':
    subs.add(cid); h=q('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,),True)['n']; sent[cid]=h
    await w.send_json({'type':'subscribed','channel_id':cid,'head_event_id':h})
   elif typ=='resume':
    since=int(x.get('since_event_id',0)); rows=q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since))
    for z in rows:
     await w.send_json({'type':z['type'],'event_id':z['event_id'],'channel_id':cid,**json.loads(z['payload'])})
    h=q('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,),True)['n']
    await w.send_json({'type':'resumed','channel_id':cid,'head_event_id':h})
 except (WebSocketDisconnect,Exception): pass
 finally:
  with clients_lock:
   for z in list(clients):
    if z[0] is w: clients.remove(z)
  q('DELETE FROM presence WHERE user_id=?',(r['user_id'],))
@app.get('/')
async def index(): return HTMLResponse(open('/app/static/index.html').read())
