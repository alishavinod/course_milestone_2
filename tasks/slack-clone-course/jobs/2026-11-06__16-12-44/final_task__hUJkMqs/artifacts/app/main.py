import os,sqlite3,secrets,hashlib,re,json,datetime,asyncio,threading
from typing import Optional
from fastapi import FastAPI,Request,HTTPException,UploadFile,File,WebSocket,WebSocketDisconnect
from fastapi.responses import HTMLResponse,Response,FileResponse
from fastapi.middleware.cors import CORSMiddleware
DB='/app/data/huddle.db'; NODE=int(os.getenv('NODE_ID','0')); PORT=int(os.getenv('PORT','8000'))
app=FastAPI(); app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])
@app.middleware('http')
async def serialize_db_requests(request, call_next):
 with DB_LOCK:
  return await call_next(request)
con=sqlite3.connect(DB,check_same_thread=False,timeout=10); con.row_factory=sqlite3.Row
con.execute('PRAGMA busy_timeout=10000'); con.execute('PRAGMA synchronous=NORMAL')
DB_LOCK=threading.RLock()
con.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER,user_ids TEXT);
CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires_at TEXT,max_uses INTEGER,uses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id));
CREATE TABLE IF NOT EXISTS presence(user_id INTEGER PRIMARY KEY,updated_at TEXT);
CREATE TABLE IF NOT EXISTS mention_counts(user_id INTEGER,channel_id INTEGER,count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT,created_at TEXT);
''');
try: con.execute('alter table messages add column mentions text default \'[]\''); con.commit()
except sqlite3.OperationalError: pass
connections={}
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def rowdict(r): return dict(r) if r else None
def auth(req):
 t=req.headers.get('authorization','')
 if not t.startswith('Bearer '): raise HTTPException(401)
 r=con.execute('select user_id from tokens where token=?',(t[7:],)).fetchone()
 if not r: raise HTTPException(401)
 return r['user_id']
def user(uid):
 return rowdict(con.execute('select id,username,display_name,timezone,avatar_url,status_text,status_emoji from users where id=?',(uid,)).fetchone())
def channel_obj(r): return {**dict(r),'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'is_archived':bool(r['is_archived'])}
def ws_obj(r): return dict(r)
def member(w,u): return con.execute('select role from members where workspace_id=? and user_id=?',(w,u)).fetchone()
def msg_obj(r):
 d=dict(r); d.pop('event_id',None); d.pop('mentions',None); d['author']=user(d['author_id']); d['parent_id']=d['parent_id']
 d['files']=[dict(x) for x in con.execute('select id,uploader_id,filename,content_type,size,created_at from files where id in (select file_id from message_files where message_id=?)',(d['id'],))] if con.execute("select name from sqlite_master where type='table' and name='message_files'").fetchone() else []; d['reactions']=[]
 for x in con.execute('select emoji,count(*) count from reactions where message_id=? group by emoji',(d['id'],)):
  ids=[z['user_id'] for z in con.execute('select user_id from reactions where message_id=? and emoji=?',(d['id'],x['emoji']))]
  d['reactions'].append({'emoji':x['emoji'],'count':x['count'],'user_ids':ids})
 d['reply_count']=con.execute('select count(*) n from messages where parent_id=? and deleted=0',(d['id'],)).fetchone()['n']
 d['mentions']=[]
 try:
  mr=con.execute('select mentions from messages where id=?',(d['id'],)).fetchone()
  if mr and mr['mentions']: d['mentions']=json.loads(mr['mentions'])
 except Exception: pass
 for name in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',d['body']):
  g=con.execute('select g.user_ids from groups_ g join channels c on c.workspace_id=g.workspace_id where c.id=? and g.handle=?',(d['channel_id'],name)).fetchone()
  if g:
   for gid in json.loads(g['user_ids']):
    gid=int(gid)
    if gid!=d['author_id'] and accessible(d['channel_id'],gid) and gid not in d['mentions']: d['mentions'].append(gid)
   continue
  u=con.execute('select id from users where username=?',(name,)).fetchone()
  if u and u['id']!=d['author_id'] and accessible(d['channel_id'],u['id']) and u['id'] not in d['mentions']: d['mentions'].append(u['id'])
 return d
@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(req:Request):
 b=await req.json(); un=b.get('username',''); pw=b.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise HTTPException(400)
 try:
  cur=con.execute('insert into users(username,password,display_name) values(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un)); con.commit()
 except sqlite3.IntegrityError: raise HTTPException(409)
 tok=secrets.token_urlsafe(24); con.execute('insert into tokens values(?,?)',(tok,cur.lastrowid)); con.commit()
 return {'user':user(cur.lastrowid),'token':tok}
@app.post('/api/auth/login')
async def login(req:Request):
 b=await req.json(); r=con.execute('select * from users where username=? and password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest())).fetchone()
 if not r: raise HTTPException(401)
 tok=secrets.token_urlsafe(24); con.execute('insert into tokens values(?,?)',(tok,r['id'],)); con.commit(); return {'user':user(r['id']),'token':tok}
@app.get('/api/auth/me')
def me(req:Request): return {'user':user(auth(req))}
@app.get('/api/users/{uid}')
def getuser(uid:int,req:Request):
 auth(req)
 r=user(uid)
 if not r: raise HTTPException(404)
 return {'user':r}
@app.patch('/api/users/me')
async def profile(req:Request):
 uid=auth(req); b=await req.json(); allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
 if 'timezone' in b:
  try:
   from zoneinfo import ZoneInfo
   ZoneInfo(b['timezone'])
  except Exception: raise HTTPException(400)
 limits={'display_name':80,'status_text':100,'status_emoji':32,'avatar_url':1024}
 if any(k in b and (not isinstance(b[k],str) or len(b[k])>limits[k]) for k in limits): raise HTTPException(400)
 vals={k:v for k,v in b.items() if k in allowed}
 if vals:
  con.execute('update users set '+','.join(k+'=?' for k in vals)+' where id=?',(*vals.values(),uid))
  con.commit()
  con.execute('insert into global_events(kind,payload,created_at) values(?,?,?)',('user.updated',json.dumps(user(uid)),now())); con.commit()
 return {'user':user(uid)}
@app.post('/api/workspaces',status_code=201)
async def createws(req:Request):
 uid=auth(req); b=await req.json(); slug=b.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not b.get('name'): raise HTTPException(400)
 try:
  c=con.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(slug,b['name'],uid)); wid=c.lastrowid
  con.execute('insert into members values(?,?,?)',(wid,uid,'owner')); g=con.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general')); cid=g.lastrowid
  con.execute('insert into channel_members values(?,?)',(cid,uid)); con.commit()
 except sqlite3.IntegrityError: raise HTTPException(409)
 return {'workspace':ws_obj(con.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':channel_obj(con.execute('select * from channels where id=?',(cid,)).fetchone())}
@app.get('/api/workspaces')
def workspaces(req:Request):
 uid=auth(req); return {'workspaces':[ws_obj(x) for x in con.execute('select distinct w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=? or w.owner_id=?',(uid,uid))]}
@app.get('/api/workspaces/{slug}')
def getws(slug:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 inc=req.query_params.get('include_archived') in ('1','true')
 q='select c.* from channels c join channel_members cm on cm.channel_id=c.id where c.workspace_id=? and cm.user_id=?'
 if not inc: q+=' and c.is_archived=0'
 chans=[channel_obj(x) for x in con.execute(q,(w['id'],uid))]
 return {'workspace':ws_obj(w),'channels':chans,'read_state':[{'channel_id':x['id'],'last_read_event_id':0,'unread_count':0,'mention_count':0} for x in chans]}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def createch(slug:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 role=member(w['id'],uid)
 if not role or role['role'] not in ('owner','admin','member'): raise HTTPException(403)
 b=await req.json(); name=b.get('name','')
 if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(b.get('topic',''))>250: raise HTTPException(400)
 if b.get('is_private') and role['role'] not in ('owner','admin'): raise HTTPException(403)
 try:
  c=con.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic',''))); cid=c.lastrowid; con.execute('insert into channel_members values(?,?)',(cid,uid)); con.commit()
 except sqlite3.IntegrityError: raise HTTPException(409)
 return {'channel':channel_obj(con.execute('select * from channels where id=?',(cid,)).fetchone())}
@app.post('/api/channels/{cid}/join')
def join(cid:int,req:Request):
 uid=auth(req); c=con.execute('select * from channels where id=?',(cid,)).fetchone()
 if not c: raise HTTPException(404)
 w=con.execute('select * from workspaces where id=?',(c['workspace_id'],)).fetchone()
 role=member(w['id'],uid)
 if c['is_private'] and not accessible(cid,uid): raise HTTPException(403)
 if role and role['role']=='guest': raise HTTPException(403)
 if w['join_mode']=='invite_only' and not role: raise HTTPException(403)
 con.execute('insert or ignore into members values(?,?,?)',(w['id'],uid,'member'))
 con.execute('insert or ignore into channel_members values(?,?)',(cid,uid)); con.commit()
 return {'channel':channel_obj(c)}
@app.post('/api/channels/{cid}/leave')
def leave(cid:int,req:Request):
 uid=auth(req); con.execute('delete from channel_members where channel_id=? and user_id=?',(cid,uid)); con.commit()
 c=con.execute('select * from channels where id=?',(cid,)).fetchone()
 if not c: raise HTTPException(404)
 return {'channel':channel_obj(c)}
@app.get('/api/channels/{cid}/members')
def channel_members(cid:int,req:Request):
 auth(req); c=con.execute('select id from channels where id=?',(cid,)).fetchone()
 if not c: raise HTTPException(404)
 return {'members':[user(x['user_id']) for x in con.execute('select user_id from channel_members where channel_id=?',(cid,))]}
@app.patch('/api/channels/{cid}')
async def patch_channel(cid:int,req:Request):
 uid=auth(req); c=con.execute('select * from channels where id=?',(cid,)).fetchone()
 if not c: raise HTTPException(404)
 rrole=member(c['workspace_id'],uid)
 owner=con.execute('select owner_id from workspaces where id=?',(c['workspace_id'],)).fetchone()
 if not rrole or (rrole['role'] not in ('owner','admin') and owner['owner_id']!=uid): raise HTTPException(403)
 if c['is_archived']: raise HTTPException(423)
 b=await req.json()
 if len(b.get('topic',''))>250: raise HTTPException(400)
 if 'topic' in b: con.execute('update channels set topic=? where id=?',(b['topic'],cid)); con.commit()
 return {'channel':channel_obj(con.execute('select * from channels where id=?',(cid,)).fetchone())}
@app.post('/api/channels/{cid}/archive')
def archive(cid:int,req:Request):
 uid=auth(req); c=con.execute('select * from channels where id=?',(cid,)).fetchone()
 w=con.execute('select * from workspaces where id=?',(c['workspace_id'],)).fetchone() if c else None
 if not c: raise HTTPException(404)
 if w['owner_id']!=uid: raise HTTPException(403)
 con.execute('update channels set is_archived=1 where id=?',(cid,)); con.commit()
 return {'channel':channel_obj(con.execute('select * from channels where id=?',(cid,)).fetchone())}
@app.post('/api/channels/{cid}/unarchive')
def unarchive(cid:int,req:Request):
 uid=auth(req); c=con.execute('select * from channels where id=?',(cid,)).fetchone()
 if not c: raise HTTPException(404)
 w=con.execute('select owner_id from workspaces where id=?',(c['workspace_id'],)).fetchone()
 if w['owner_id']!=uid: raise HTTPException(403)
 con.execute('update channels set is_archived=0 where id=?',(cid,)); con.commit()
 return {'channel':channel_obj(con.execute('select * from channels where id=?',(cid,)).fetchone())}
def accessible(cid,uid):
 return con.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,uid)).fetchone()
def emit(cid,kind,data):
 con.execute('BEGIN IMMEDIATE')
 n=con.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(cid,)).fetchone()['n']
 con.execute('insert into events values(?,?,?,?,?)',(cid,n,kind,json.dumps(data),now()))
 con.commit()
 return n
@app.post('/api/channels/{cid}/messages',status_code=201)
async def post_message(cid:int,req:Request):
 uid=auth(req); c=con.execute('select * from channels where id=?',(cid,)).fetchone()
 if not c: raise HTTPException(404)
 if not accessible(cid,uid): raise HTTPException(403)
 b=await req.json(); body=b.get('body','')
 if c['is_archived'] and body.strip() != '/unarchive': raise HTTPException(423)
 if not isinstance(body,str) or not body.strip(): raise HTTPException(400)
 parent=b.get('parent_id')
 if parent and not con.execute('select id from messages where id=? and channel_id=? and deleted=0',(parent,cid)).fetchone(): raise HTTPException(404)
 if body.startswith('/') and not body.startswith('//'):
  if body.startswith('/shrug'):
   text=body[6:].strip(); body=(text+' ' if text else '')+'¯\\_(ツ)_/¯'
  elif body.startswith('/me '): pass
  elif body=='/me' or body.startswith('/me\t'): raise HTTPException(400)
  elif body.startswith('/topic '):
   topic=body[7:].strip()
   if not topic or len(topic)>250: raise HTTPException(400)
   con.execute('update channels set topic=? where id=?',(topic,cid)); con.commit()
   return {'message':None,'channel':channel_obj(con.execute('select * from channels where id=?',(cid,)).fetchone())}
  elif body.startswith('/invite @'):
   u=con.execute('select * from users where username=?',(body[9:].strip(),)).fetchone()
   if not u: raise HTTPException(404)
   con.execute('insert or ignore into members values(?,?,?)',(c['workspace_id'],u['id'],'member'))
   con.execute('insert or ignore into channel_members values(?,?)',(cid,u['id'])); con.commit()
   return {'message':None,'invited_user':user(u['id'])}
  elif body in ('/archive','/unarchive'):
   w=con.execute('select owner_id from workspaces where id=?',(c['workspace_id'],)).fetchone()
   if w['owner_id']!=uid: raise HTTPException(403)
   val=1 if body=='/archive' else 0
   con.execute('update channels set is_archived=? where id=?',(val,cid)); con.commit()
   return {'message':None,'channel':channel_obj(con.execute('select * from channels where id=?',(cid,)).fetchone())}
  else: raise HTTPException(400)
 fids=b.get('file_ids',[])
 for fid in fids:
  f=con.execute('select * from files where id=?',(fid,)).fetchone()
  if not f or f['uploader_id']!=uid or con.execute('select 1 from message_files where file_id=?',(fid,)).fetchone(): raise HTTPException(400)
 mentions=[]
 for name in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body):
  if name in ('channel','here'):
   if name=='channel':
    ids=[x['user_id'] for x in con.execute('select user_id from channel_members where channel_id=?',(cid,))]
   else:
    ids=[x['user_id'] for x in con.execute('select cm.user_id from channel_members cm join presence p on p.user_id=cm.user_id where cm.channel_id=? and p.updated_at>?',(cid,(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=2)).isoformat()) )]
   mentions += [x for x in ids if x!=uid]
  else:
   g=con.execute('select user_ids from groups_ where workspace_id=? and handle=?',(c['workspace_id'],name)).fetchone()
   if g:
    mentions += [x for x in json.loads(g['user_ids']) if x!=uid and accessible(cid,x)]
   else:
    u=con.execute('select id from users where username=?',(name,)).fetchone()
    if u and u['id']!=uid and accessible(cid,u['id']): mentions.append(u['id'])
 mentions=list(dict.fromkeys(mentions))
 eid=emit(cid,'message.reply' if parent else 'message.created',{})
 cur=con.execute('insert into messages(channel_id,author_id,body,parent_id,created_at,event_id,mentions) values(?,?,?,?,?,?,?)',(cid,uid,body,parent,now(),eid,json.dumps(mentions)))
 for fid in fids: con.execute('insert into message_files values(?,?)',(cur.lastrowid,fid))
 con.execute('update events set payload=? where channel_id=? and event_id=?',(json.dumps({'message':msg_obj(con.execute('select * from messages where id=?',(cur.lastrowid,)).fetchone())}),cid,eid))
 for mid in mentions: con.execute('insert into mention_counts(user_id,channel_id,count) values(?,?,1) on conflict(user_id,channel_id) do update set count=count+1',(mid,cid))
 con.commit()
 return {'message':msg_obj(con.execute('select * from messages where id=?',(cur.lastrowid,)).fetchone())}
@app.get('/api/channels/{cid}/messages')
def list_messages(cid:int,req:Request,limit:str='50',cursor:Optional[str]=None):
 try: limit=int(limit)
 except: raise HTTPException(400)
 uid=auth(req)
 if not con.execute('select id from channels where id=?',(cid,)).fetchone(): raise HTTPException(404)
 if not accessible(cid,uid): raise HTTPException(403)
 if limit<1 or limit>200: raise HTTPException(400)
 args=[cid,limit]
 if cursor:
  try: before=int(cursor)
  except: raise HTTPException(400)
  rows=con.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null and id<? order by id desc limit ?', (cid,before,limit+1)).fetchall()
 else:
  rows=con.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc limit ?', (cid,limit+1)).fetchall()
 nxt=str(rows[limit-1]['id']) if len(rows)>limit else None
 return {'messages':[msg_obj(x) for x in rows[:limit]],'next_cursor':nxt}
@app.patch('/api/messages/{mid}')
async def edit_message(mid:int,req:Request):
 uid=auth(req); r=con.execute('select * from messages where id=?',(mid,)).fetchone()
 if not r: raise HTTPException(404)
 if r['deleted']: raise HTTPException(409)
 if r['author_id']!=uid: raise HTTPException(403)
 b=await req.json()
 if not b.get('body','').strip(): raise HTTPException(400)
 con.execute('update messages set body=?,edited_at=? where id=?',(b['body'],now(),mid)); con.commit()
 emit(r['channel_id'],'message.updated',{'message':msg_obj(con.execute('select * from messages where id=?',(mid,)).fetchone())})
 return {'message':msg_obj(con.execute('select * from messages where id=?',(mid,)).fetchone())}
@app.delete('/api/messages/{mid}')
def delete_message(mid:int,req:Request):
 uid=auth(req); r=con.execute('select * from messages where id=?',(mid,)).fetchone()
 if not r or r['deleted']: return {'deleted':True}
 w=con.execute('select owner_id from workspaces w join channels c on c.workspace_id=w.id where c.id=?',(r['channel_id'],)).fetchone()
 if r['author_id']!=uid and (not w or w['owner_id']!=uid): raise HTTPException(403)
 con.execute('update messages set deleted=1 where id=?',(mid,)); con.commit(); emit(r['channel_id'],'message.deleted',{'message_id':mid}); return {'deleted':True}
@app.get('/api/messages/{mid}/replies')
def replies(mid:int,req:Request,limit:str='50',cursor:Optional[str]=None):
 try: limit=int(limit)
 except: raise HTTPException(400)
 uid=auth(req); p=con.execute('select * from messages where id=?',(mid,)).fetchone()
 if not p: raise HTTPException(404)
 if not accessible(p['channel_id'],uid): raise HTTPException(403)
 if not cursor: before=10**18
 else:
  try: before=int(cursor)
  except: raise HTTPException(400)
 rows=con.execute('select * from messages where parent_id=? and deleted=0 and id<? order by id limit ?',(mid,before,limit+1)).fetchall()
 nxt=str(rows[limit-1]['id']) if len(rows)>limit else None
 return {'replies':[msg_obj(x) for x in rows[:limit]],'next_cursor':nxt}
@app.post('/api/messages/{mid}/reactions')
async def add_reaction(mid:int,req:Request):
 uid=auth(req); r=con.execute('select * from messages where id=? and deleted=0',(mid,)).fetchone()
 if not r: raise HTTPException(404)
 if not accessible(r['channel_id'],uid): raise HTTPException(403)
 emoji=(await req.json()).get('emoji','')
 if not emoji: raise HTTPException(400)
 con.execute('insert or ignore into reactions values(?,?,?)',(mid,uid,emoji)); con.commit()
 emit(r['channel_id'],'reaction.added',{'message_id':mid,'emoji':emoji,'user_id':uid})
 return {'reactions':msg_obj(con.execute('select * from messages where id=?',(mid,)).fetchone())['reactions']}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
def remove_reaction(mid:int,emoji:str,req:Request):
 uid=auth(req); r0=con.execute('select channel_id from messages where id=?',(mid,)).fetchone()
 con.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,uid,emoji)); con.commit()
 if r0: emit(r0['channel_id'],'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':uid})
 r=con.execute('select * from messages where id=?',(mid,)).fetchone()
 if not r: raise HTTPException(404)
 return {'reactions':msg_obj(r)['reactions']}
@app.post('/api/dms')
async def dm(req:Request):
 uid=auth(req); b=await req.json(); other=b.get('user_id')
 if not other or not user(other): raise HTTPException(400 if not other else 404)
 old=con.execute('select c.id from channels c join channel_members a on a.channel_id=c.id join channel_members b on b.channel_id=c.id where c.is_dm=1 and a.user_id=? and b.user_id=?',(uid,other)).fetchone()
 if old: return {'channel_id':old['id']}
 cur=con.execute('insert into channels(name,is_dm) values(?,1)',(f'dm-{uid}-{other}',)); cid=cur.lastrowid
 con.execute('insert into channel_members values(?,?)',(cid,uid)); con.execute('insert into channel_members values(?,?)',(cid,other)); con.commit()
 return {'channel_id':cid}
@app.get('/api/workspaces/{slug}/members')
def ws_members(slug:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if not member(w['id'],uid): raise HTTPException(403)
 return {'members':[dict(x) for x in con.execute('select user_id,role from members where workspace_id=?',(w['id'],))]}
@app.patch('/api/workspaces/{slug}')
async def patch_ws(slug:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if w['owner_id']!=uid and not (member(w['id'],uid) and member(w['id'],uid)['role']=='admin'): raise HTTPException(403)
 b=await req.json(); vals={k:b[k] for k in ('name','join_mode') if k in b}
 if vals.get('join_mode') not in (None,'open','invite_only'): raise HTTPException(400)
 if vals: con.execute('update workspaces set '+','.join(k+'=?' for k in vals)+' where id=?',(*vals.values(),w['id'])); con.commit()
 return {'workspace':ws_obj(con.execute('select * from workspaces where id=?',(w['id'],)).fetchone())}
@app.patch('/api/workspaces/{slug}/members/{other}')
async def patch_member(slug:str,other:int,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if w['owner_id']!=uid and not (member(w['id'],uid) and member(w['id'],uid)['role']=='admin'): raise HTTPException(403)
 if other==w['owner_id'] or other==uid: raise HTTPException(403)
 target_role=member(w['id'],other)
 actor_role=member(w['id'],uid)
 if target_role and target_role['role']=='admin' and actor_role and actor_role['role']=='admin' and w['owner_id']!=uid: raise HTTPException(403)
 if not user(other) or not target_role: raise HTTPException(404)
 role=(await req.json()).get('role')
 if role not in ('admin','member','guest'): raise HTTPException(400)
 con.execute('update members set role=? where workspace_id=? and user_id=?',(role,w['id'],other)); con.commit()
 return {'member':dict(con.execute('select user_id,role from members where workspace_id=? and user_id=?',(w['id'],other)).fetchone() or {'user_id':other,'role':role})}
@app.post('/api/messages/{mid}/pin')
def pin(mid:int,req:Request):
 uid=auth(req); r=con.execute('select * from messages where id=? and deleted=0',(mid,)).fetchone()
 if not r: raise HTTPException(404)
 if not accessible(r['channel_id'],uid): raise HTTPException(403)
 if con.execute('select is_archived from channels where id=?',(r['channel_id'],)).fetchone()['is_archived']: raise HTTPException(423)
 ts=now(); con.execute('insert or ignore into pins values(?,?,?)',(mid,uid,ts)); con.commit()
 p=con.execute('select * from pins where message_id=?',(mid,)).fetchone()
 return {'pin':{'message_id':p['message_id'],'pinned_by':p['pinned_by'],'pinned_at':p['pinned_at']}}
@app.delete('/api/messages/{mid}/pin')
def unpin(mid:int,req:Request):
 auth(req); con.execute('delete from pins where message_id=?',(mid,)); con.commit(); return {'unpinned':True}
@app.get('/api/channels/{cid}/pins')
def pins(cid:int,req:Request,limit:str='50',cursor:Optional[str]=None):
 try: limit=int(limit)
 except: raise HTTPException(400)
 uid=auth(req)
 if not accessible(cid,uid): raise HTTPException(403)
 out=[]; before=10**18
 if cursor:
  try: before=int(cursor)
  except: raise HTTPException(400)
 rows=con.execute('select p.*,m.* from pins p join messages m on m.id=p.message_id where m.channel_id=? and p.message_id<? order by p.pinned_at desc limit ?',(cid,before,limit+1)).fetchall()
 for p in rows[:limit]: out.append({'message':msg_obj(p),'pinned_by':p['pinned_by'],'pinned_at':p['pinned_at']})
 return {'pins':out,'next_cursor':str(rows[limit-1]['message_id']) if len(rows)>limit else None}
@app.post('/api/files',status_code=201)
async def upload(req:Request,file:UploadFile=File(None)):
 uid=auth(req)
 if file is None: raise HTTPException(400)
 data=await file.read()
 if len(data)>10*1024*1024: raise HTTPException(413)
 ts=now(); c=con.execute('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(uid,file.filename,file.content_type or 'application/octet-stream',len(data),ts,data)); con.commit()
 return {'file':{'id':c.lastrowid,'uploader_id':uid,'filename':file.filename,'content_type':file.content_type or 'application/octet-stream','size':len(data),'created_at':ts}}
@app.get('/api/files/{fid}')
def getfile(fid:int,req:Request):
 auth(req); f=con.execute('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(fid,)).fetchone()
 if not f: raise HTTPException(404)
 return {'file':dict(f)}
@app.get('/api/files/{fid}/download')
def download(fid:int,req:Request):
 auth(req); f=con.execute('select * from files where id=?',(fid,)).fetchone()
 if not f: raise HTTPException(404)
 return Response(f['data'],media_type=f['content_type'],headers={'Content-Disposition':f'attachment; filename="{f["filename"]}"'})
@app.get('/api/search')
def search(req:Request,q:str='',limit:str='50',cursor:Optional[str]=None):
 try: limit=int(limit)
 except: raise HTTPException(400)
 uid=auth(req); rows=[]; wid=req.query_params.get('workspace_id'); before=int(cursor) if cursor else 10**18
 sql='select m.* from messages m join channel_members cm on cm.channel_id=m.channel_id join channels c on c.id=m.channel_id where cm.user_id=? and m.body like ? and m.deleted=0 and m.id<?'
 args=[uid,'%'+q+'%',before]
 if wid: sql+=' and c.workspace_id=?'; args.append(wid)
 sql+=' order by m.id desc limit ?'; args.append(limit+1)
 for r in con.execute(sql,args):
  rows.append(msg_obj(r))
 return {'results':rows[:limit],'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None}
@app.get('/api/workspaces/{slug}/groups')
def groups(slug:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if not member(w['id'],uid): raise HTTPException(403)
 return {'groups':[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in con.execute('select * from groups_ where workspace_id=?',(w['id'],))]}
@app.post('/api/workspaces/{slug}/groups',status_code=201)
async def create_group(slug:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w or not member(w['id'],uid): raise HTTPException(404 if not w else 403)
 b=await req.json(); h=b.get('handle','')
 if not re.fullmatch(r'[A-Za-z0-9_-]+',h): raise HTTPException(400)
 try: c=con.execute('insert into groups_(workspace_id,handle,name,creator_id,user_ids) values(?,?,?,?,?)',(w['id'],h,b.get('name',h),uid,json.dumps(b.get('user_ids',[])))); con.commit()
 except sqlite3.IntegrityError: raise HTTPException(409)
 return {'group':{'id':c.lastrowid,'handle':h,'name':b.get('name',h),'member_user_ids':b.get('user_ids',[])}}
@app.patch('/api/workspaces/{slug}/groups/{handle}')
async def patch_group(slug:str,handle:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone(); g=con.execute('select * from groups_ where handle=?',(handle,)).fetchone()
 if not w or not g or g['workspace_id']!=w['id']: raise HTTPException(404)
 if uid!=g['creator_id'] and uid!=w['owner_id']: raise HTTPException(403)
 b=await req.json(); name=b.get('name',g['name']); ids=b.get('user_ids',json.loads(g['user_ids']))
 con.execute('update groups_ set name=?,user_ids=? where id=?',(name,json.dumps(ids),g['id'])); con.commit()
 return {'group':{'id':g['id'],'handle':handle,'name':name,'member_user_ids':ids}}
@app.delete('/api/workspaces/{slug}/groups/{handle}')
def delete_group(slug:str,handle:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone(); g=con.execute('select * from groups_ where handle=?',(handle,)).fetchone()
 if not w: raise HTTPException(404)
 if not g: return {'deleted':True}
 if uid!=g['creator_id'] and uid!=w['owner_id']: raise HTTPException(403)
 con.execute('delete from groups_ where id=?',(g['id'],)); con.commit(); return {'deleted':True}
@app.get('/api/channels/{cid}/read')
def readstate(cid:int,req:Request):
 uid=auth(req)
 if not accessible(cid,uid): raise HTTPException(403)
 return {'read_state':{'channel_id':cid,'last_read_event_id':0,'unread_count':0,'mention_count':con.execute('select coalesce(sum(count),0) n from mention_counts where user_id=? and channel_id=?',(uid,cid)).fetchone()['n']}}
@app.post('/api/channels/{cid}/read')
async def markread(cid:int,req:Request):
 auth(req); return {'read_state':{'channel_id':cid,'last_read_event_id':(await req.json()).get('last_read_event_id',0),'unread_count':0,'mention_count':0}}
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
 token=ws.query_params.get('token',''); r=con.execute('select user_id from tokens where token=?',(token,)).fetchone()
 if not r: await ws.close(code=1008); return
 await ws.accept(); wsuid=r['user_id']; con.execute('insert or replace into presence values(?,?)',(wsuid,now())); con.commit(); subscribed={}; sent={}; global_sent=0
 try:
  while True:
   try:
    raw=await asyncio.wait_for(ws.receive_text(),timeout=0.15)
    b=json.loads(raw)
    if b.get('type') in ('subscribe','resume'):
     cid=int(b.get('channel_id')); subscribed[cid]=True
     since=int(b.get('since_event_id',0)) if b.get('type')=='resume' else 0
     sent[cid]=since if b['type']=='resume' else con.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(cid,)).fetchone()['n']
     if b['type']=='resume':
      for e in con.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,since)):
       frame={'type':e['kind'],'event_id':e['event_id'],'channel_id':cid}; frame.update(json.loads(e['payload'])); await ws.send_json(frame); sent[cid]=e['event_id']
     head=con.execute('select coalesce(max(event_id),0) n from events where channel_id=?',(cid,)).fetchone()['n']
     await ws.send_json({'type':'subscribed' if b['type']=='subscribe' else 'resumed','channel_id':cid,'head_event_id':head})
   except asyncio.TimeoutError:
    pass
   for ge in con.execute('select * from global_events where id>? order by id',(global_sent,)):
    await ws.send_json({'type':ge['kind'],'user':json.loads(ge['payload'])}); global_sent=ge['id']
   for cid in list(subscribed):
    last=sent.get(cid,0)
    for e in con.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,last)):
     frame={'type':e['kind'],'event_id':e['event_id'],'channel_id':cid}; frame.update(json.loads(e['payload'])); await ws.send_json(frame); sent[cid]=e['event_id']
 except (WebSocketDisconnect,Exception):
  con.execute('delete from presence where user_id=?',(wsuid,)); con.commit()
  return

@app.post('/api/workspaces/{slug}/invitations',status_code=201)
async def create_invite(slug:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 r=member(w['id'],uid)
 if not r or r['role'] not in ('owner','admin'): raise HTTPException(403)
 b=await req.json(); target=b.get('invited_user_id')
 if not target and b.get('invited_username'):
  x=con.execute('select id from users where username=?',(b['invited_username'],)).fetchone()
  if x: target=x['id']
 uses=int(b.get('max_uses',1)); exp=b.get('expires_in_seconds')
 expires=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=int(exp))).isoformat() if exp else None
 code=secrets.token_urlsafe(18)
 con.execute('insert into invites values(?,?,?,?,?,0)',(code,w['id'],target,expires,uses)); con.commit()
 return {'invitation':{'code':code,'workspace_id':w['id'],'invited_user_id':target,'max_uses':uses,'uses':0,'expires_at':expires}}
@app.get('/api/workspaces/{slug}/invitations')
def list_invites(slug:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 r=member(w['id'],uid)
 if not r or r['role'] not in ('owner','admin'): raise HTTPException(403)
 out=[]
 for x in con.execute('select * from invites where workspace_id=? and uses<max_uses',(w['id'],)):
  if x['expires_at'] and x['expires_at']<now(): continue
  out.append(dict(x))
 return {'invitations':out}
@app.post('/api/invitations/{code}/accept')
def accept_invite(code:str,req:Request):
 uid=auth(req); x=con.execute('select * from invites where code=?',(code,)).fetchone()
 if not x or x['uses']>=x['max_uses'] or (x['invited_user_id'] and x['uses']>0) or (x['expires_at'] and x['expires_at']<now()): raise HTTPException(404)
 if x['invited_user_id'] and x['invited_user_id']!=uid: raise HTTPException(403)
 con.execute('insert or ignore into members values(?,?,?)',(x['workspace_id'],uid,'member'))
 con.execute('update invites set uses=uses+1 where code=?',(code,)); con.commit()
 return {'workspace':ws_obj(con.execute('select * from workspaces where id=?',(x['workspace_id'],)).fetchone()),'member':{'user_id':uid,'role':'member'}}
@app.delete('/api/workspaces/{slug}/invitations/{code}')
def revoke_invite(slug:str,code:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 r=member(w['id'],uid)
 if not r or r['role'] not in ('owner','admin'): raise HTTPException(403)
 x=con.execute('select * from invites where code=? and workspace_id=?',(code,w['id'])).fetchone()
 if not x: raise HTTPException(404)
 con.execute('delete from invites where code=?',(code,)); con.commit(); return {'deleted':True}
@app.post('/api/workspaces/{slug}/transfer_ownership')
async def transfer(slug:str,req:Request):
 uid=auth(req); w=con.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if w['owner_id']!=uid: raise HTTPException(403)
 target=(await req.json()).get('new_owner_id')
 if not member(w['id'],target): raise HTTPException(400)
 tr=member(w['id'],target)
 if tr['role']!='admin': raise HTTPException(400)
 con.execute('update workspaces set owner_id=? where id=?',(target,w['id']))
 con.execute('update members set role=? where workspace_id=? and user_id=?',('admin',w['id'],uid))
 con.execute('update members set role=? where workspace_id=? and user_id=?',('owner',w['id'],target)); con.commit()
 return {'workspace':ws_obj(con.execute('select * from workspaces where id=?',(w['id'],)).fetchone())}
@app.get('/',response_class=HTMLResponse)
def index(): return FileResponse('/app/static/index.html')
if __name__=='__main__':
 import uvicorn
 uvicorn.run(app,host='127.0.0.1',port=PORT)
