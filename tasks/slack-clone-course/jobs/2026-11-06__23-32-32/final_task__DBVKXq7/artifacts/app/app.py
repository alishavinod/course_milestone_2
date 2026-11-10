import os,sqlite3,secrets,hashlib,re,datetime,json,asyncio,socket,threading,time
from fastapi import FastAPI,Request,HTTPException,UploadFile,File,WebSocket,WebSocketDisconnect
from fastapi.responses import HTMLResponse,Response,FileResponse
from fastapi.middleware.cors import CORSMiddleware
DB='/app/data/huddle.db'; node=int(os.getenv('NODE_ID','0'))
app=FastAPI(); sockets={}; pending={}; global_pending=[]; global_queues=[]; online=set(); app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])

def redis_subscriber():
 while True:
  try:
   z=socket.create_connection(('127.0.0.1',6379),timeout=3); z.sendall(b'*2\\r\\n$9\\r\\nSUBSCRIBE\\r\\n$14\\r\\nhuddle-events\\r\\n'); z.settimeout(None)
   while z.recv(4096): pass
  except Exception:
   try: z.close()
   except: pass
   time.sleep(1)

def db():
 c=sqlite3.connect(DB,check_same_thread=False,timeout=30); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=30000')
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,data BLOB,created_at TEXT);
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,created_at TEXT); CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id)); CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle)); CREATE TABLE IF NOT EXISTS group_users(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id)); CREATE TABLE IF NOT EXISTS read_states(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,mention_count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id)); CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id)); CREATE TABLE IF NOT EXISTS online_users(user_id INTEGER PRIMARY KEY,seen_at REAL); CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT,created_at REAL); CREATE TABLE IF NOT EXISTS channel_events(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,event_id INTEGER,payload TEXT);'''); return c
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def user(r): return dict(r) if r else None
def auth(req):
 t=req.headers.get('authorization','').replace('Bearer ',''); r=db().execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,)).fetchone()
 if not r: raise HTTPException(401)
 return r
def obj(r):
 d=dict(r) if r else {}
 for k in ('is_private','is_dm','is_archived'):
  if k in d: d[k]=bool(d[k])
 return d
def msg(c,r):
 d=dict(r); d['author']=dict(c.execute('select * from users where id=?',(r['author_id'],)).fetchone()); d['files']=[dict(x) for x in c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id in (select file_id from message_files where message_id=?)',(r['id'],))] if c.execute("select name from sqlite_master where type='table' and name='message_files'").fetchone() else []; d['mentions']=[int(x['user_id']) for x in c.execute('select user_id from message_mentions where message_id=?',(r['id'],))] if c.execute("select name from sqlite_master where type='table' and name='message_mentions'").fetchone() else []; d['reply_count']=c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0]
 rr=c.execute('select emoji,count(*) n,group_concat(user_id) ids from reactions where message_id=? group by emoji',(r['id'],)).fetchall(); d['reactions']=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rr]; return d
@app.get('/api/health')
def health(): return {'status':'ok','node_id':node}
@app.post('/api/auth/register',status_code=201)
def register(x:dict):
 c=db(); u=x.get('username',''); p=x.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(p)<8: raise HTTPException(400)
 try: c.execute('insert into users(username,password,display_name) values(?,?,?)',(u,p,x.get('display_name') or u)); c.commit()
 except sqlite3.IntegrityError: raise HTTPException(409)
 uid=c.execute('select last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,uid)); c.commit()
 return {'user':user(c.execute('select * from users where id=?',(uid,)).fetchone()),'token':t}
@app.post('/api/auth/login')
def login(x:dict):
 c=db(); r=c.execute('select * from users where username=? and password=?',(x.get('username'),x.get('password'))).fetchone()
 if not r: raise HTTPException(401)
 t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,r['id'])); c.commit(); return {'user':user(r),'token':t}
@app.get('/api/auth/me')
def me(request:Request): return {'user':user(auth(request))}
@app.get('/api/users/{uid}')
def getuser(uid:int,request:Request):
 auth(request); r=db().execute('select * from users where id=?',(uid,)).fetchone()
 if not r: raise HTTPException(404)
 return {'user':user(r)}
@app.patch('/api/users/me')
def patchuser(request:Request,x:dict):
 r=auth(request); allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}; vals={k:v for k,v in x.items() if k in allowed}
 limits={'display_name':80,'status_text':100,'status_emoji':32,'avatar_url':1024}
 if any(len(str(v))>limits.get(k,250) for k,v in vals.items()): raise HTTPException(400)
 if 'timezone' in vals and vals['timezone'] not in ('UTC','America/New_York','America/Los_Angeles','Europe/London','Asia/Tokyo'): raise HTTPException(400)
 c=db()
 if vals: c.execute('update users set '+','.join(k+'=?' for k in vals)+' where id=?',tuple(vals.values())+(r['id'],)); c.commit()
 updated=user(c.execute('select * from users where id=?',(r['id'],)).fetchone()); event={'type':'user.updated','user':updated}; gc=db(); gc.execute('insert into global_events(payload,created_at) values(?,strftime(\'%s\',\'now\'))',(json.dumps(event),)); gc.commit(); gc.close(); [q.put_nowait(event) for q in list(global_queues)]
 return {'user':updated}
@app.post('/api/workspaces',status_code=201)
def workspace(request:Request,x:dict):
 r=auth(request); c=db(); slug=x.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): raise HTTPException(400)
 try: c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(slug,x.get('name',''),r['id'])); c.commit()
 except sqlite3.IntegrityError: raise HTTPException(409)
 wid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into members values(?,?,?)',(wid,r['id'],'owner')); c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general')); c.commit(); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into cmembers values(?,?)',(cid,r['id'])); c.commit()
 return {'workspace':dict(c.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':dict(c.execute('select * from channels where id=?',(cid,)).fetchone())}
@app.get('/api/workspaces')
def workspaces(request:Request):
 r=auth(request); c=db(); return {'workspaces':[dict(x) for x in c.execute('select distinct w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(r['id'],))]}
@app.post('/api/workspaces/{slug}/groups',status_code=201)
def group_create(slug:str,request:Request,x:dict):
 r=auth(request); c=db(); w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],r['id'])).fetchone(): raise HTTPException(403)
 if not x.get('handle') or not x.get('name') or not re.fullmatch(r'[a-z0-9-]+',x['handle']): raise HTTPException(400)
 if c.execute('select 1 from groups where workspace_id=? and handle=?',(w['id'],x['handle'])).fetchone(): raise HTTPException(409)
 c.execute('insert into groups(workspace_id,handle,name,creator_id) values(?,?,?,?)',(w['id'],x['handle'],x['name'],r['id'])); c.commit(); gid=c.execute('select last_insert_rowid()').fetchone()[0]
 for uid in x.get('user_ids',[]): c.execute('insert or ignore into group_users values(?,?)',(gid,int(uid)))
 c.commit(); return {'group':dict(c.execute('select * from groups where id=?',(gid,)).fetchone())|{'member_user_ids':[x['user_id'] for x in c.execute('select user_id from group_users where group_id=?',(gid,))]}}
@app.get('/api/workspaces/{slug}/groups')
def group_list(slug:str,request:Request):
 r=auth(request); c=db(); w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 out=[]
 for g in c.execute('select * from groups where workspace_id=?',(w['id'],)): out.append(dict(g)|{'member_user_ids':[x['user_id'] for x in c.execute('select user_id from group_users where group_id=?',(g['id'],))]})
 return {'groups':out}
@app.patch('/api/workspaces/{slug}/groups/{handle}')
def group_patch(slug:str,handle:str,request:Request,x:dict):
 r=auth(request); c=db(); g=c.execute('select g.* from groups g join workspaces w on w.id=g.workspace_id where w.slug=? and g.handle=?',(slug,handle)).fetchone()
 if not g: raise HTTPException(404)
 if r['id']!=g['creator_id'] and r['id']!=c.execute('select owner_id from workspaces where id=?',(g['workspace_id'],)).fetchone()[0]: raise HTTPException(403)
 c.execute('update groups set name=? where id=?',(x.get('name',g['name']),g['id'])); c.execute('delete from group_users where group_id=?',(g['id'],))
 for uid in x.get('user_ids',[]): c.execute('insert or ignore into group_users values(?,?)',(g['id'],int(uid)))
 c.commit(); return {'group':dict(c.execute('select * from groups where id=?',(g['id'],)).fetchone())|{'member_user_ids':[x['user_id'] for x in c.execute('select user_id from group_users where group_id=?',(g['id'],))]}}
@app.delete('/api/workspaces/{slug}/groups/{handle}')
def group_delete(slug:str,handle:str,request:Request):
 r=auth(request); c=db(); g=c.execute('select g.* from groups g join workspaces w on w.id=g.workspace_id where w.slug=? and g.handle=?',(slug,handle)).fetchone()
 if not g: return {'deleted':True}
 if r['id']!=g['creator_id'] and r['id']!=c.execute('select owner_id from workspaces where id=?',(g['workspace_id'],)).fetchone()[0]: raise HTTPException(403)
 c.execute('delete from groups where id=?',(g['id'],)); c.commit(); return {'deleted':True}
@app.get('/api/channels/{cid}/read')
def read_get(cid:int,request:Request):
 r=auth(request); c=db(); x=c.execute('select * from read_states where user_id=? and channel_id=?',(r['id'],cid)).fetchone()
 if not x: c.execute('insert or ignore into read_states(user_id,channel_id) values(?,?)',(r['id'],cid)); c.commit(); x=c.execute('select * from read_states where user_id=? and channel_id=?',(r['id'],cid)).fetchone()
 return {'read_state':dict(x)|{'unread_count':0}}
@app.post('/api/channels/{cid}/read')
def read_post(cid:int,request:Request,x:dict):
 r=auth(request); c=db(); c.execute('insert or ignore into read_states(user_id,channel_id,last_read_event_id) values(?,?,?)',(r['id'],cid,int(x.get('last_read_event_id',0)))); c.execute('update read_states set last_read_event_id=max(last_read_event_id,?) where user_id=? and channel_id=?',(int(x.get('last_read_event_id',0)),r['id'],cid)); c.commit(); return read_get(cid,request)
@app.get('/api/workspaces/{slug}')
def workspace_get(slug:str,request:Request):
 r=auth(request); c=db(); w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 return {'workspace':dict(w),'channels':[dict(x) for x in c.execute('select * from channels where workspace_id=? and is_archived=0',(w['id'],))],'read_state':{}}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
def channel(slug:str,request:Request,x:dict):
 r=auth(request); c=db(); w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if not re.fullmatch(r'[a-z0-9-]{1,32}',x.get('name','')): raise HTTPException(400)
 try: c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],x['name'],int(bool(x.get('is_private'))),x.get('topic',''))); c.commit()
 except sqlite3.IntegrityError: raise HTTPException(409)
 cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into cmembers values(?,?)',(cid,r['id'],)); c.commit(); return {'channel':dict(c.execute('select * from channels where id=?',(cid,)).fetchone())}
@app.patch('/api/channels/{cid}')
def patch_channel(cid:int,request:Request,x:dict):
 r=auth(request); c=db(); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
 if not ch: raise HTTPException(404)
 if 'topic' in x and len(str(x['topic']))>250: raise HTTPException(400)
 if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,r['id'])).fetchone():
  w=c.execute('select owner_id from workspaces where id=?',(ch['workspace_id'],)).fetchone()
  if not w or w['owner_id']!=r['id']: raise HTTPException(403)
 if 'topic' in x: c.execute('update channels set topic=? where id=?',(x['topic'],cid)); c.commit()
 return {'channel':obj(c.execute('select * from channels where id=?',(cid,)).fetchone())}
@app.post('/api/channels/{cid}/join')
def join(cid:int,request:Request):
 r=auth(request); c=db(); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
 if not ch: raise HTTPException(404)
 c.execute('insert or ignore into cmembers values(?,?)',(cid,r['id'],)); c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],r['id'],'member')); c.commit(); return {'channel':dict(ch)}
@app.get('/api/channels/{cid}/members')
def channel_members(cid:int,request:Request):
 auth(request); c=db(); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
 if not ch: raise HTTPException(404)
 return {'members':[user(x) for x in c.execute('select u.* from users u join cmembers m on m.user_id=u.id where m.channel_id=?',(cid,))]}
@app.get('/api/channels/{cid}/messages')
def messages(cid:int,request:Request):
 auth(request); c=db(); rows=c.execute('select * from messages where channel_id=? and deleted=0 order by id desc limit 200',(cid,)); return {'messages':[msg(c,x) for x in rows],'next_cursor':None}
@app.post('/api/channels/{cid}/messages',status_code=201)
def postmsg(cid:int,request:Request,x:dict):
 r=auth(request); body=x.get('body','')
 if not body: raise HTTPException(400)
 if body.startswith('/') and not body.startswith('//'):
  parts=body.split(' ',1); cmd=parts[0]; arg=parts[1] if len(parts)>1 else ''
  if cmd=='/me' and not arg.strip(): raise HTTPException(400)
  if cmd=='/shrug': body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
  elif cmd not in ('/me','/shrug','/topic','/invite','/archive','/unarchive'): raise HTTPException(400)
 c=db()
 ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
 if not ch: raise HTTPException(404)
 if ch['is_archived'] and not body.startswith('/unarchive'): raise HTTPException(423)
 if body.startswith('/topic '):
  topic=body[7:]
  if len(topic)>250: raise HTTPException(400)
  c.execute('update channels set topic=? where id=?',(topic,cid)); c.commit()
  return {'message':None,'channel':obj(c.execute('select * from channels where id=?',(cid,)).fetchone())}
 if body=='/archive':
  c.execute('update channels set is_archived=1 where id=?',(cid,)); c.commit()
  return {'message':None,'channel':obj(c.execute('select * from channels where id=?',(cid,)).fetchone())}
 if body=='/unarchive':
  c.execute('update channels set is_archived=0 where id=?',(cid,)); c.commit()
  return {'message':None,'channel':obj(c.execute('select * from channels where id=?',(cid,)).fetchone())}
 if body.startswith('/invite '):
  uname=body[8:].strip().lstrip('@'); u=c.execute('select * from users where username=?',(uname,)).fetchone()
  if not u: raise HTTPException(404)
  c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],u['id'],'member')); c.execute('insert or ignore into cmembers values(?,?)',(cid,u['id'])); c.commit()
  return {'message':None,'invited_user':dict(u)}
 file_ids=x.get('file_ids') or []
 if not isinstance(file_ids,list): raise HTTPException(400)
 for fid in file_ids:
  f=c.execute('select uploader_id from files where id=?',(fid,)).fetchone()
  if not f or f['uploader_id']!=r['id'] or c.execute('select 1 from message_files where file_id=?',(fid,)).fetchone(): raise HTTPException(400)
 c.execute('BEGIN IMMEDIATE'); ev=c.execute('select coalesce(max(event_id),0)+1 from channel_events where channel_id=?',(cid,)).fetchone()[0]; c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at,event_id) values(?,?,?,?,?,?)',(cid,r['id'],body,x.get('parent_id'),now(),ev)); mid=c.execute('select last_insert_rowid()').fetchone()[0]
 users=set()
 for uname in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_][A-Za-z0-9_-]*)',body):
  if uname=='channel':
   users.update(x['user_id'] for x in c.execute('select user_id from cmembers where channel_id=?',(cid,)) if x['user_id']!=r['id'])
  elif uname=='here':
   users.update(x['user_id'] for x in c.execute('select user_id from cmembers where channel_id=?',(cid,)) if x['user_id']!=r['id'] and x['user_id'] in {z['user_id'] for z in c.execute('select user_id from online_users')})
  else:
   u=c.execute('select id from users where username=?',(uname,)).fetchone()
   if u and c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,u['id'])).fetchone() and u['id']!=r['id']: users.add(u['id'])
   g=c.execute('select g.id from groups g join channels z on z.workspace_id=g.workspace_id where g.handle=? and z.id=?',(uname,cid)).fetchone()
   if g: users.update(x['user_id'] for x in c.execute('select user_id from group_users where group_id=?',(g['id'],)) if x['user_id']!=r['id'] and c.execute('select 1 from cmembers where channel_id=? and user_id=?',(cid,x['user_id'])).fetchone())
 for uid in users: c.execute('insert or ignore into message_mentions values(?,?)',(mid,uid)); c.execute('insert or ignore into read_states(user_id,channel_id,mention_count) values(?,?,?)',(uid,cid,0)); c.execute('update read_states set mention_count=mention_count+1 where user_id=? and channel_id=?',(uid,cid))
 c.commit(); [c.execute('insert into message_files(message_id,file_id) values(?,?)',(mid,fid)) for fid in file_ids]; c.commit(); mm=msg(c,c.execute('select * from messages where id=?',(mid,)).fetchone()); event={'type':'message.created','event_id':ev,'channel_id':cid,'message':mm}; c.execute('insert into channel_events(channel_id,event_id,payload) values(?,?,?)',(cid,ev,json.dumps(event))); c.commit(); return {'message':mm}
@app.patch('/api/messages/{mid}')
def edit(mid:int,request:Request,x:dict):
 r=auth(request); c=db(); m=c.execute('select * from messages where id=?',(mid,)).fetchone()
 if not m: raise HTTPException(404)
 if m['deleted']: raise HTTPException(409)
 if m['author_id']!=r['id']: raise HTTPException(403)
 c.execute('BEGIN IMMEDIATE'); ev=c.execute('select coalesce(max(event_id),0)+1 from channel_events where channel_id=?',(m['channel_id'],)).fetchone()[0]; c.execute('update messages set body=?,edited_at=?,event_id=? where id=?',(x.get('body',''),now(),ev,mid)); c.commit(); mm=msg(c,c.execute('select * from messages where id=?',(mid,)).fetchone()); event={'type':'message.updated','event_id':ev,'channel_id':m['channel_id'],'message':mm}; c.execute('insert into channel_events(channel_id,event_id,payload) values(?,?,?)',(m['channel_id'],ev,json.dumps(event))); c.commit(); pending.setdefault(m['channel_id'],[]).append(event); return {'message':mm}
@app.delete('/api/messages/{mid}')
def delete(mid:int,request:Request):
 r=auth(request); c=db(); m=c.execute('select m.*,w.owner_id from messages m join channels ch on ch.id=m.channel_id join workspaces w on w.id=ch.workspace_id where m.id=?',(mid,)).fetchone()
 if not m: raise HTTPException(404)
 if m['author_id']!=r['id'] and m['owner_id']!=r['id']: raise HTTPException(403)
 if m['deleted']: return {'deleted':True}
 c.execute('BEGIN IMMEDIATE'); ev=c.execute('select coalesce(max(event_id),0)+1 from channel_events where channel_id=?',(m['channel_id'],)).fetchone()[0]; c.execute('update messages set deleted=1,event_id=? where id=?',(ev,mid)); c.commit(); event={'type':'message.deleted','event_id':ev,'channel_id':m['channel_id'],'message_id':mid}; c.execute('insert into channel_events(channel_id,event_id,payload) values(?,?,?)',(m['channel_id'],ev,json.dumps(event))); c.commit(); pending.setdefault(m['channel_id'],[]).append(event); return {'deleted':True}
@app.post('/api/messages/{mid}/reactions')
def react(mid:int,request:Request,x:dict):
 r=auth(request); c=db(); m=c.execute('select * from messages where id=?',(mid,)).fetchone()
 if not m: raise HTTPException(404)
 emoji=x.get('emoji')
 if not isinstance(emoji,str) or not emoji or len(emoji)>32: raise HTTPException(400)
 c.execute('insert or ignore into reactions values(?,?,?)',(mid,r['id'],emoji)); c.commit(); cid=m['channel_id']; c.execute('BEGIN IMMEDIATE'); ev=c.execute('select coalesce(max(event_id),0)+1 from channel_events where channel_id=?',(cid,)).fetchone()[0]; c.execute('update messages set event_id=? where id=?',(ev,mid)); c.commit(); pending.setdefault(cid,[]).append({'type':'reaction.added','event_id':ev,'channel_id':cid,'message_id':mid}); return {'reactions':msg(c,m)['reactions']}
@app.post('/api/dms')
def dm(request:Request,x:dict):
 r=auth(request); c=db()
 if 'user_id' not in x: raise HTTPException(400)
 try: other=int(x['user_id'])
 except: raise HTTPException(400)
 if not c.execute('select id from users where id=?',(other,)).fetchone(): raise HTTPException(404)
 a,b=sorted((r['id'],other)); key=f'dm-{a}-{b}'; ch=c.execute('select id from channels where name=?',(key,)).fetchone()
 if not ch:
  c.execute('insert into channels(workspace_id,name,is_dm) values(?,?,1)',(0,key)); c.commit()
  return {'channel_id':c.execute('select last_insert_rowid()').fetchone()[0]}
 return {'channel_id':ch['id']}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
def unreact(mid:int,emoji:str,request:Request):
 r=auth(request); c=db(); m=c.execute('select * from messages where id=?',(mid,)).fetchone()
 if not m: raise HTTPException(404)
 c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,r['id'],emoji)); cid=m['channel_id']; c.execute('BEGIN IMMEDIATE'); ev=c.execute('select coalesce(max(event_id),0)+1 from channel_events where channel_id=?',(cid,)).fetchone()[0]; c.execute('update messages set event_id=? where id=?',(ev,mid)); c.commit(); pending.setdefault(cid,[]).append({'type':'reaction.removed','event_id':ev,'channel_id':cid,'message_id':mid}); return {'reactions':msg(c,m)['reactions']}

@app.post('/api/files',status_code=201)
async def upload_file(request:Request,file:UploadFile=File(None)):
 r=auth(request)
 if file is None: raise HTTPException(400)
 data=await file.read()
 if len(data)>10*1024*1024: raise HTTPException(413)
 c=db(); ct=file.content_type or 'application/octet-stream'; c.execute('insert into files(uploader_id,filename,content_type,size,data,created_at) values(?,?,?,?,?,?)',(r['id'],file.filename or 'file',ct,len(data),data,now())); c.commit(); fid=c.execute('select last_insert_rowid()').fetchone()[0]
 return {'file':dict(c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(fid,)).fetchone())}
@app.get('/api/files/{fid}')
def file_info(fid:int,request:Request):
 auth(request); c=db(); r=c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(fid,)).fetchone()
 if not r: raise HTTPException(404)
 return {'file':dict(r)}
@app.get('/api/files/{fid}/download')
def file_download(fid:int,request:Request):
 auth(request); c=db(); r=c.execute('select * from files where id=?',(fid,)).fetchone()
 if not r: raise HTTPException(404)
 return Response(r['data'],media_type=r['content_type'],headers={'Content-Disposition':f'attachment; filename="{r["filename"]}"'})
@app.get('/api/search')
def search(request:Request,q:str=''):
 auth(request); c=db(); return {'results':[msg(c,x) for x in c.execute('select * from messages where body like ? and deleted=0 order by id desc',(f'%{q}%',))],'next_cursor':None}
@app.get('/')
def home(): return FileResponse('/app/static/index.html')
@app.websocket('/api/ws')
async def ws(sock:WebSocket):
 t=sock.query_params.get('token',''); c=db(); r=c.execute('select user_id from tokens where token=?',(t,)).fetchone()
 if not r: await sock.close(code=1008); return
 await sock.accept(); online.add(r['user_id']); pc=db(); pc.execute('insert or replace into online_users(user_id,seen_at) values(?,strftime(\'%s\',\'now\'))',(r['user_id'],)); pc.commit(); pc.close(); q=asyncio.Queue(); global_queues.append(q); subscribed=[]; seen={}
 async def sender():
  while True:
   sent=False
   while global_pending: await sock.send_json(global_pending.pop(0)); sent=True
   for cid in list(subscribed):
    while pending.get(cid): await sock.send_json(pending[cid].pop(0)); sent=True
    ec=db(); rows=ec.execute('select payload,event_id from channel_events where channel_id=? and event_id>? order by event_id',(cid,seen.get(cid,0))).fetchall(); ec.close()
    for row in rows:
     ev=json.loads(row['payload']); await sock.send_json(ev); seen[cid]=row['event_id']; sent=True
   if not sent: await asyncio.sleep(.02)
 async def receiver():
  while True:
   x=await sock.receive_json()
   if x.get('type') in ('subscribe','resume'):
    cid=x.get('channel_id')
    if cid not in subscribed: subscribed.append(cid); sockets.setdefault(cid,[]).append(q)
    head=c.execute('select coalesce(max(event_id),0) from channel_events where channel_id=?',(cid,)).fetchone()[0]
    if x.get('type')=='resume':
     since=int(x.get('since_event_id',0)); rows=c.execute('select payload,event_id from channel_events where channel_id=? and event_id>? order by event_id',(cid,since)).fetchall()
     for row in rows: await sock.send_json(json.loads(row['payload']))
    seen[cid]=head
    await sock.send_json({'type':'subscribed' if x.get('type')=='subscribe' else 'resumed','channel_id':cid,'head_event_id':head})
 send=asyncio.create_task(sender()); recv=asyncio.create_task(receiver())
 try: await recv
 except (WebSocketDisconnect,Exception): pass
 finally:
  online.discard(r['user_id']);
  if q in global_queues: global_queues.remove(q)
  cc=db(); cc.execute('PRAGMA busy_timeout=1000')
  try: cc.execute('delete from online_users where user_id=?',(r['user_id'],)); cc.commit()
  except sqlite3.OperationalError: cc.rollback()
  finally: cc.close()
  send.cancel()
  for cid in subscribed:
   if q in sockets.get(cid,[]): sockets[cid].remove(q)

if __name__ == '__main__':
 threading.Thread(target=redis_subscriber,daemon=True).start()
 import uvicorn
 uvicorn.run(app,host='127.0.0.1',port=int(os.getenv('PORT','8000')),log_level='warning')
