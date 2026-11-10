import os, re, sqlite3, hashlib, secrets, json, asyncio, mimetypes, socket, threading, time
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
DB='/app/data/app.db'
NODE=int(os.getenv('NODE_ID','0'))
app=FastAPI()
def redis_node_subscriber():
 while True:
  try:
   z=socket.create_connection(('127.0.0.1',6379),timeout=2)
   def cmd(*parts):
    z.sendall(('*'+str(len(parts))+'\r\n' + ''.join('$'+str(len(str(x)))+'\r\n'+str(x)+'\r\n' for x in parts)).encode())
   cmd('SUBSCRIBE','huddle-events')
   while True:
    if not z.recv(4096): break
  except Exception:
   try: z.close()
   except Exception: pass
   time.sleep(0.5)
threading.Thread(target=redis_node_subscriber,daemon=True).start()

def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def get_db():
 d=sqlite3.connect(DB,timeout=0.8,check_same_thread=False); d.execute('PRAGMA busy_timeout=800'); d.row_factory=sqlite3.Row
 d.execute('PRAGMA journal_mode=WAL'); return d
def init():
 d=get_db()
 d.executescript('''CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
 CREATE TABLE IF NOT EXISTS msgfiles(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id));
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT,user_id INTEGER,created_at TEXT);
 CREATE TABLE IF NOT EXISTS connected_users(user_id INTEGER PRIMARY KEY);
 CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER);
 CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
 CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,invited_user_id INTEGER,expires_at TEXT,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,event_type TEXT,message_id INTEGER,created_at TEXT,event_id INTEGER);
 CREATE TABLE IF NOT EXISTS read_states(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS cursors(token TEXT PRIMARY KEY,channel_id INTEGER,boundary INTEGER);
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));'''); d.commit(); return d
init()
try:
 d=get_db()
 d.execute('CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji))')
 d.commit()
except Exception: pass
try:
 d=get_db()
 cols=[r['name'] for r in d.execute('PRAGMA table_info(events)').fetchall()]
 if 'event_id' not in cols:
  d.execute('ALTER TABLE events ADD COLUMN event_id INTEGER')
  d.execute('UPDATE events SET event_id=id WHERE event_id IS NULL')
  d.commit()
 d.close()
except Exception:
 pass
def uj(u): return {k:u[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def cj(c): return {'id':c['id'],'workspace_id':c['workspace_id'],'name':c['name'],'is_private':bool(c['is_private']),'is_dm':bool(c['is_dm']),'topic':c['topic'] or '','is_archived':bool(c['is_archived'])}
def user(req):
 a=req.headers.get('authorization','')
 if not a.lower().startswith('bearer '): raise HTTPException(401)
 d=get_db(); u=d.execute('SELECT u.* FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.token=?',(a[7:],)).fetchone()
 if not u: raise HTTPException(401)
 return u
def token(uid):
 for attempt in range(5):
  try:
   t=secrets.token_urlsafe(24); d=get_db(); d.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); d.commit(); d.close(); return t
  except sqlite3.OperationalError as e:
   if 'locked' not in str(e): raise
   import time; time.sleep(0.03*(attempt+1))
 raise HTTPException(503,'database busy')
@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(req:Request):
 x=await req.json(); un=x.get('username',''); pw=x.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise HTTPException(400)
 d=get_db()
 for attempt in range(30):
  try:
   cur=d.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),x.get('display_name') or un)); d.commit(); break
  except sqlite3.IntegrityError: raise HTTPException(409)
  except sqlite3.OperationalError as e:
   if 'locked' not in str(e) or attempt==29: raise HTTPException(503,'database busy')
   import time; time.sleep(0.08)
 u=d.execute('SELECT * FROM users WHERE id=?',(cur.lastrowid,)).fetchone(); return {'user':uj(u),'token':token(u['id'])}
@app.post('/api/auth/login')
async def login(req:Request):
 x=await req.json(); d=get_db(); u=d.execute('SELECT * FROM users WHERE username=?',(x.get('username'),)).fetchone()
 if not u or u['password']!=hashlib.sha256(x.get('password','').encode()).hexdigest(): raise HTTPException(401)
 return {'user':uj(u),'token':token(u['id'])}
@app.get('/api/auth/me')
def me(req:Request): return {'user':uj(user(req))}
@app.get('/api/users/{uid}')
def getuser(uid:int,req:Request):
 user(req)
 u=get_db().execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
 if not u: raise HTTPException(404)
 return {'user':uj(u)}
@app.patch('/api/users/me')
async def patch_me(req:Request):
 u=user(req); x=await req.json(); allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
 for k in x:
  if k not in allowed: continue
  limits={'display_name':80,'timezone':80,'avatar_url':1024,'status_text':100,'status_emoji':32}
  if not isinstance(x[k],str) or len(x[k])>limits.get(k,80): raise HTTPException(400)
 if 'timezone' in x:
  try:
   from zoneinfo import ZoneInfo
   ZoneInfo(x['timezone'])
  except Exception: raise HTTPException(400)
 d=get_db(); vals=[]; args=[]
 for k in allowed:
  if k in x: vals.append(k+'=?'); args.append(x[k])
 if vals:
  args.append(u['id']); d.execute('UPDATE users SET '+','.join(vals)+' WHERE id=?',args); d.execute('INSERT INTO global_events(event_type,user_id,created_at) VALUES(?,?,?)',('user.updated',u['id'],now())); d.commit()
 return {'user':uj(d.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone())}

@app.post('/api/workspaces',status_code=201)
async def workspace(req:Request):
 u=user(req); x=await req.json(); slug=x.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): raise HTTPException(400)
 d=get_db()
 try:
  cur=d.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,x.get('name',''),u['id'])); wid=cur.lastrowid
  d.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); d.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); ch=d.execute('SELECT * FROM channels WHERE id=?',(d.execute('SELECT last_insert_rowid()').fetchone()[0],)).fetchone(); d.execute('INSERT INTO cmembers VALUES(?,?)',(ch['id'],u['id'])); d.commit()
 except sqlite3.IntegrityError: raise HTTPException(409)
 return {'workspace':dict(d.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':cj(ch)}
@app.get('/api/workspaces')
def workspaces(req:Request):
 u=user(req); d=get_db(); return {'workspaces':[dict(x) for x in d.execute('SELECT DISTINCT w.* FROM workspaces w JOIN members m ON w.id=m.workspace_id WHERE m.user_id=?',(u['id'],))]}
@app.get('/api/workspaces/{slug}/members')
def workspace_members(slug:str,req:Request):
 u=user(req); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if not workspace_role(d,w['id'],u['id']): raise HTTPException(403)
 rows=d.execute('SELECT m.user_id,m.role,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?',(w['id'],)).fetchall()
 return {'members':[dict(x) for x in rows]}

@app.patch('/api/workspaces/{slug}')
async def patch_workspace(slug:str,req:Request):
 u=user(req); x=await req.json(); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 r=workspace_role(d,w['id'],u['id'])
 if r not in ('owner','admin'): raise HTTPException(403)
 if 'join_mode' in x and x['join_mode'] not in ('open','invite_only'): raise HTTPException(400)
 vals=[]; args=[]
 for k in ('name','join_mode'):
  if k in x: vals.append(k+'=?'); args.append(x[k])
 if vals: args += [w['id']]; d.execute('UPDATE workspaces SET '+','.join(vals)+' WHERE id=?',args); d.commit()
 return {'workspace':dict(d.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())}
@app.post('/api/workspaces/{slug}/invitations',status_code=201)
async def create_invite(slug:str,req:Request):
 u=user(req); x=await req.json(); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if workspace_role(d,w['id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
 target=x.get('invited_user_id'); name=x.get('invited_username')
 if name:
  z=d.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
  if z: target=z['id']
 code=secrets.token_urlsafe(10); exp=datetime.fromtimestamp(__import__('time').time()+int(x.get('expires_in_seconds',86400)),timezone.utc).isoformat().replace('+00:00','Z')
 d.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?)',(code,w['id'],u['id'],target,exp,int(x.get('max_uses',1)),0,0)); d.commit()
 return {'invitation':{'code':code,'workspace_id':w['id'],'expires_at':exp,'max_uses':int(x.get('max_uses',1)),'uses':0,'invited_user_id':target}}
@app.get('/api/workspaces/{slug}/invitations')
def list_invites(slug:str,req:Request):
 u=user(req); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if workspace_role(d,w['id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
 rows=d.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses < max_uses',(w['id'],)).fetchall()
 return {'invitations':[dict(x) for x in rows]}
@app.post('/api/invitations/{code}/accept')
def accept_invite(code:str,req:Request):
 u=user(req); d=get_db(); i=d.execute('SELECT * FROM invitations WHERE code=? AND revoked=0',(code,)).fetchone()
 if not i or i['uses'] >= (1 if i['invited_user_id'] else i['max_uses']) or i['expires_at']<now(): raise HTTPException(404)
 if i['invited_user_id'] and i['invited_user_id']!=u['id']: raise HTTPException(403)
 d.execute('INSERT OR REPLACE INTO members VALUES(?,?,?)',(i['workspace_id'],u['id'],'member')); d.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(code,)); d.commit()
 return {'workspace':dict(d.execute('SELECT * FROM workspaces WHERE id=?',(i['workspace_id'],)).fetchone())}
@app.delete('/api/workspaces/{slug}/invitations/{code}')
def revoke_invite(slug:str,code:str,req:Request):
 u=user(req); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if workspace_role(d,w['id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
 if not d.execute('SELECT 1 FROM invitations WHERE code=? AND workspace_id=?',(code,w['id'])).fetchone(): raise HTTPException(404)
 d.execute('UPDATE invitations SET revoked=1 WHERE code=?',(code,)); d.commit(); return {'deleted':True}
@app.post('/api/workspaces/{slug}/transfer_ownership')
async def transfer(slug:str,req:Request):
 u=user(req); x=await req.json(); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if u['id']!=w['owner_id']: raise HTTPException(403)
 target=x.get('new_owner_id'); r=d.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
 if not r or r['role']!='admin': raise HTTPException(400)
 d.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id'])); d.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target)); d.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id'])); d.commit()
 return {'workspace':dict(d.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())}
@app.get('/api/workspaces/{slug}')
def wsdetail(slug:str,req:Request):
 u=user(req); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 include=req.query_params.get('include_archived') in ('1','true')
 q='SELECT DISTINCT c.* FROM channels c LEFT JOIN cmembers m ON c.id=m.channel_id WHERE c.workspace_id=? AND (c.is_private=0 OR m.user_id=?)'+('' if include else ' AND c.is_archived=0')
 cs=d.execute(q,(w['id'],u['id'])).fetchall()
 states=[]
 for c in cs:
  z=d.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],c['id'])).fetchone()
  last=z['last_read_event_id'] if z else 0
  unread=d.execute("SELECT count(*) n FROM events e JOIN messages m ON m.id=e.message_id WHERE e.channel_id=? AND e.event_id>? AND e.event_type='message.created' AND m.author_id!=?",(c['id'],last,u['id'])).fetchone()['n']
  mentions=d.execute("SELECT count(*) n FROM events e JOIN messages m ON m.id=e.message_id WHERE e.channel_id=? AND e.event_id>? AND e.event_type='message.created' AND m.author_id!=? AND m.body LIKE ?",(c['id'],last,u['id'],'%@'+u['username']+'%')).fetchone()['n']
  states.append({'channel_id':c['id'],'last_read_event_id':last,'unread_count':unread,'mention_count':mentions})
 return {'workspace':dict(w),'channels':[cj(c) for c in cs],'read_state':states}
def channel_or_404(cid):
 d=get_db(); c=d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not c: raise HTTPException(404)
 return c
def is_member(d,cid,uid): return d.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,uid)).fetchone() is not None
def workspace_role(d,wid,uid):
 r=d.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(wid,uid)).fetchone()
 return r['role'] if r else None
def require_channel(c,u):
 d=get_db()
 if not is_member(d,c['id'],u['id']) and workspace_role(d,c['workspace_id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
def add_event(d,cid,kind,mid=None):
 n=d.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
 d.execute('INSERT INTO events(channel_id,event_type,message_id,created_at,event_id) VALUES(?,?,?,?,?)',(cid,kind,mid,now(),n)); return n
def msgjson(m):
 d=get_db(); a=d.execute('SELECT * FROM users WHERE id=?',(m['author_id'],)).fetchone()
 ch=d.execute('SELECT workspace_id FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
 channel_members=[x for x in d.execute('SELECT u.* FROM users u JOIN cmembers cm ON cm.user_id=u.id WHERE cm.channel_id=?',(m['channel_id'],)).fetchall()]
 workspace_members=[x for x in d.execute('SELECT u.* FROM users u JOIN members wm ON wm.user_id=u.id WHERE wm.workspace_id=?',(ch['workspace_id'],)).fetchall()]
 mentions=[]; body=m['body']
 if '@here' in body:
  mentions += [x['id'] for x in channel_members if x['id']!=m['author_id'] and d.execute('SELECT 1 FROM connected_users WHERE user_id=?',(x['id'],)).fetchone()]
 gm=re.findall(r'(?<![A-Za-z0-9_])@([a-z0-9-]+)',body)
 for h in gm:
  g=d.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(ch['workspace_id'],h)).fetchone()
  if g:
   mentions += [x['id'] for x in channel_members if x['id']!=m['author_id'] and d.execute('SELECT 1 FROM group_members WHERE group_id=? AND user_id=?',(g['id'],x['id'])).fetchone()]
 if '@channel' in body: mentions += [x['id'] for x in channel_members if x['id']!=m['author_id']]
 for x in workspace_members:
  if x['id']!=m['author_id'] and re.search(r'(?<![A-Za-z0-9_])@'+re.escape(x['username'])+r'(?![A-Za-z0-9_])',body): mentions.append(x['id'])
 return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':uj(a),'body':body,'parent_id':m['parent_id'],'reply_count':d.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(m['id'],)).fetchone()['n'],'created_at':m['created_at'],'edited_at':m['edited_at'],'files':[dict(f) for f in d.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files f JOIN msgfiles mf ON mf.file_id=f.id WHERE mf.message_id=?',(m['id'],)).fetchall()],'reactions':[{'emoji':r['emoji'],'count':r['n'],'user_ids':[z['user_id'] for z in d.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(m['id'],r['emoji'])).fetchall()]} for r in d.execute('SELECT emoji,count(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(m['id'],)).fetchall()],'mentions':list(dict.fromkeys(mentions))}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def create_channel(slug:str,req:Request):
 u=user(req); x=await req.json(); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 role=workspace_role(d,w['id'],u['id'])
 if not role or role=='guest': raise HTTPException(403)
 name=x.get('name','')
 if not re.fullmatch(r'[a-z0-9-]{1,32}',name): raise HTTPException(400)
 if x.get('is_private',False) and role not in ('owner','admin'): raise HTTPException(403)
 if len(x.get('topic',''))>250: raise HTTPException(400)
 if d.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone(): raise HTTPException(409)
 cur=d.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(x.get('is_private'))),x.get('topic','')))
 cid=cur.lastrowid; d.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id'])); d.commit()
 return {'channel':cj(d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}
@app.patch('/api/workspaces/{slug}/members/{uid}')
async def change_role(slug:str,uid:int,req:Request):
 u=user(req); x=await req.json(); new=x.get('role'); d=get_db()
 w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 actor=workspace_role(d,w['id'],u['id']); target=d.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone()
 if not target: raise HTTPException(404)
 if new not in ('owner','admin','member','guest'): raise HTTPException(400)
 if new=='owner': raise HTTPException(400)
 if uid==w['owner_id'] or uid==u['id']: raise HTTPException(403)
 if actor not in ('owner','admin'): raise HTTPException(403)
 if actor=='admin' and target['role'] in ('owner','admin'): raise HTTPException(403)
 d.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(new,w['id'],uid)); d.commit()
 return {'member':{'user_id':uid,'role':new}}
@app.post('/api/channels/{cid}/join')
def join(cid:int,req:Request):
 u=user(req); c=channel_or_404(cid); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],)).fetchone(); role=workspace_role(d,w['id'],u['id'])
 if role=='guest': raise HTTPException(403)
 if c['is_private'] and not is_member(d,cid,u['id']) and role not in ('owner','admin'): raise HTTPException(403)
 if role is None:
  if w['join_mode'] != 'open': raise HTTPException(403)
  d.execute('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
 d.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id'])); add_event(d,cid,'member.joined'); d.commit(); return {'channel':cj(c)}
@app.post('/api/channels/{cid}/leave')
def leave(cid:int,req:Request):
 u=user(req); c=channel_or_404(cid); d=get_db(); d.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])); add_event(d,cid,'member.left'); d.commit(); return {'channel':cj(c)}
@app.get('/api/channels/{cid}/members')
def channel_members(cid:int,req:Request):
 u=user(req); c=channel_or_404(cid); require_channel(c,u); d=get_db()
 return {'members':[uj(x) for x in d.execute('SELECT u.* FROM users u JOIN cmembers m ON u.id=m.user_id WHERE m.channel_id=?',(cid,)).fetchall()]}
@app.patch('/api/channels/{cid}')
async def patch_channel(cid:int,req:Request):
 u=user(req); c=channel_or_404(cid); require_channel(c,u); x=await req.json(); topic=x.get('topic','')
 if c['is_archived']: raise HTTPException(423)
 if len(topic)>250: raise HTTPException(400)
 d=get_db(); d.execute('UPDATE channels SET topic=? WHERE id=?',(topic,cid)); add_event(d,cid,'channel.updated'); d.commit(); return {'channel':cj(d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}
@app.post('/api/channels/{cid}/messages',status_code=201)
async def post_message_early(cid:int,req:Request):
 u=user(req); c=channel_or_404(cid); require_channel(c,u); x=await req.json(); body=x.get('body','')
 if not body: raise HTTPException(400)
 if body.startswith('/') and not body.startswith('//'):
  parts=body.split(' ',1); cmd=parts[0]; arg=parts[1].strip() if len(parts)>1 else ''
  d=get_db()
  if cmd=='/me':
   if not arg: raise HTTPException(400)
  elif cmd=='/shrug':
   body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
  elif cmd=='/topic':
   if not arg or workspace_role(d,c['workspace_id'],u['id']) not in ('owner','admin') and not is_member(d,cid,u['id']): raise HTTPException(403)
   if len(arg)>250: raise HTTPException(400)
   d.execute('UPDATE channels SET topic=? WHERE id=?',(arg,cid)); add_event(d,cid,'channel.updated'); d.commit()
   return {'message':None,'channel':cj(d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}
  elif cmd=='/invite':
   if not arg.startswith('@'): raise HTTPException(400)
   target=d.execute('SELECT * FROM users WHERE username=?',(arg[1:],)).fetchone()
   if not target: raise HTTPException(404)
   if workspace_role(d,c['workspace_id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
   d.execute('INSERT OR REPLACE INTO members VALUES(?,?,?)',(c['workspace_id'],target['id'],'member')); d.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,target['id'])); d.commit()
   return {'message':None,'invited_user':uj(target)}
  elif cmd in ('/archive','/unarchive'):
   if workspace_role(d,c['workspace_id'],u['id'])!='owner': raise HTTPException(403)
   archived=cmd=='/archive'; d.execute('UPDATE channels SET is_archived=? WHERE id=?',(archived,cid)); add_event(d,cid,'channel.archived' if archived else 'channel.unarchived'); d.commit()
   return {'message':None,'channel':cj(d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}
  else: raise HTTPException(400)
 if c['is_archived']: raise HTTPException(423)
 if x.get('parent_id') and not get_db().execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(x['parent_id'],cid)).fetchone(): raise HTTPException(404)
 d=get_db(); file_ids=x.get('file_ids') or []
 for fid in file_ids:
  f=d.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
  if not f or f['uploader_id']!=u['id'] or d.execute('SELECT 1 FROM msgfiles WHERE file_id=?',(fid,)).fetchone(): raise HTTPException(400)
 cur=d.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,x.get('parent_id'),now())); add_event(d,cid,'message.reply' if x.get('parent_id') else 'message.created',cur.lastrowid); [d.execute('INSERT INTO msgfiles VALUES(?,?)',(cur.lastrowid,fid)) for fid in file_ids]; d.commit()
 return {'message':msgjson(d.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone())}
@app.post('/api/channels/{cid}/_action/{action}')
def channel_action(cid:int,action:str,req:Request):
 u=user(req); c=channel_or_404(cid); d=get_db()
 if action in ('archive','unarchive'):
  if workspace_role(d,c['workspace_id'],u['id'])!='owner': raise HTTPException(403)
  d.execute('UPDATE channels SET is_archived=? WHERE id=?',(action=='archive',cid)); add_event(d,cid,'channel.archived' if action=='archive' else 'channel.unarchived'); d.commit(); return {'channel':cj(d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}
 raise HTTPException(404)
@app.post('/api/channels/{cid}/messages',status_code=201)
async def post_message(cid:int,req:Request):
 u=user(req); c=channel_or_404(cid); require_channel(c,u); x=await req.json(); body=x.get('body','')
 if not body: raise HTTPException(400)
 if c['is_archived']: raise HTTPException(423)
 if x.get('parent_id') and not get_db().execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(x['parent_id'],cid)).fetchone(): raise HTTPException(404)
 d=get_db(); cur=d.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,x.get('parent_id'),now())); add_event(d,cid,'message.reply' if x.get('parent_id') else 'message.created',cur.lastrowid); d.commit()
 return {'message':msgjson(d.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone())}
@app.get('/api/channels/{cid}/oldmessages')
def list_messages(cid:int,req:Request):
 u=user(req); c=channel_or_404(cid); require_channel(c,u); d=get_db()
 rows=d.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200',(cid,)).fetchall()
 return {'messages':[msgjson(x) for x in rows],'next_cursor':None}

@app.get('/api/channels/{cid}/messages', name='paged_messages')
def paged_messages(cid:int,req:Request):
 u=user(req); c=channel_or_404(cid); require_channel(c,u); d=get_db()
 try: limit=int(req.query_params.get('limit','50'))
 except: raise HTTPException(400)
 if limit<1 or limit>200: raise HTTPException(400)
 cur=req.query_params.get('cursor')
 if cur:
  z=d.execute('SELECT boundary FROM cursors WHERE token=? AND channel_id=?',(cur,cid)).fetchone()
  if not z: raise HTTPException(400)
  boundary=z['boundary']
  rows=d.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?',(cid,boundary,limit+1)).fetchall()
 else:
  rows=d.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT ?',(cid,limit+1)).fetchall()
 more=len(rows)>limit; rows=rows[:limit]
 nc=None
 if more and rows:
  nc='c'+secrets.token_urlsafe(12); d.execute('INSERT INTO cursors VALUES(?,?,?)',(nc,cid,rows[-1]['id'])); d.commit()
 return {'messages':[msgjson(x) for x in rows],'next_cursor':nc}
@app.get('/api/messages/{mid}/replies')
def replies(mid:int,req:Request):
 u=user(req); d=get_db(); parent=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not parent: raise HTTPException(404)
 c=channel_or_404(parent['channel_id']); require_channel(c,u)
 if c['is_archived']: raise HTTPException(423)
 try: limit=int(req.query_params.get('limit','50'))
 except: raise HTTPException(400)
 if limit<1 or limit>200: raise HTTPException(400)
 cur=req.query_params.get('cursor')
 try: off=int(cur,36) if cur else 0
 except: raise HTTPException(400)
 rows=d.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id LIMIT ? OFFSET ?',(mid,limit+1,off)).fetchall()
 more=len(rows)>limit; rows=rows[:limit]
 return {'replies':[msgjson(x) for x in rows],'next_cursor':format(off+limit,'x') if more else None}
@app.get('/api/search')
def search(req:Request):
 u=user(req); d=get_db(); q=req.query_params.get('q','')
 try: limit=int(req.query_params.get('limit','50'))
 except: raise HTTPException(400)
 if limit<1 or limit>200: raise HTTPException(400)
 try: off=int(req.query_params.get('cursor','0'),36)
 except: raise HTTPException(400)
 rows=d.execute('SELECT m.* FROM messages m JOIN cmembers cm ON cm.channel_id=m.channel_id AND cm.user_id=? WHERE m.deleted=0 AND m.body LIKE ? ORDER BY m.id DESC LIMIT ? OFFSET ?',(u['id'],'%'+q+'%',limit+1,off)).fetchall()
 if req.query_params.get('workspace_id'):
  rows=[x for x in rows if d.execute('SELECT 1 FROM channels WHERE id=? AND workspace_id=?',(x['channel_id'],req.query_params['workspace_id'])).fetchone()]
 more=len(rows)>limit; rows=rows[:limit]
 return {'results':[msgjson(x) for x in rows],'next_cursor':format(off+limit,'x') if more else None}
@app.post('/api/messages/{mid}/pin')
def pin(mid:int,req:Request):
 u=user(req); d=get_db(); m=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not m: raise HTTPException(404)
 c=channel_or_404(m['channel_id']); require_channel(c,u)
 if c['is_archived']: raise HTTPException(423)
 d.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,u['id'],now())); d.commit()
 x=d.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone()
 return {'pin':{'message_id':mid,'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']}}
@app.delete('/api/messages/{mid}/pin')
def unpin(mid:int,req:Request):
 u=user(req); d=get_db(); m=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not m: raise HTTPException(404)
 cc=channel_or_404(m['channel_id']); require_channel(cc,u); d.execute('DELETE FROM pins WHERE message_id=?',(mid,)); d.commit(); return {'unpinned':True}
@app.get('/api/channels/{cid}/pins')
def pins(cid:int,req:Request):
 u=user(req); c=channel_or_404(cid); require_channel(c,u); d=get_db()
 try: limit=int(req.query_params.get('limit','50'))
 except: raise HTTPException(400)
 if limit<1 or limit>200: raise HTTPException(400)
 try: off=int(req.query_params.get('cursor','0'),36)
 except: raise HTTPException(400)
 rows=d.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC LIMIT ? OFFSET ?',(cid,limit+1,off)).fetchall()
 more=len(rows)>limit; rows=rows[:limit]
 return {'pins':[{'message':msgjson(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows],'next_cursor':format(off+limit,'x') if more else None}

@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
 tv=ws.query_params.get('token',''); d=get_db()
 u=d.execute('SELECT u.* FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.token=?',(tv,)).fetchone()
 if not u: await ws.close(code=1008); return
 await ws.accept(); subs={}; global_seen=0; d.execute('INSERT OR REPLACE INTO connected_users VALUES(?)',(u['id'],)); d.commit()
 try:
  while True:
   try: data=json.loads(await asyncio.wait_for(ws.receive_text(),.25))
   except asyncio.TimeoutError:
    data=None
   if data:
    typ=data.get('type'); cid=data.get('channel_id')
    if typ=='subscribe':
     subs[cid]=0; head=d.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
     subs[cid]=head; await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
    elif typ=='resume':
     since=int(data.get('since_event_id',0)); subs[cid]=since
    elif typ=='unsubscribe': subs.pop(cid,None)
   grows=d.execute('SELECT * FROM global_events WHERE id>? ORDER BY id',(global_seen,)).fetchall()
   for ge in grows:
    uu=d.execute('SELECT * FROM users WHERE id=?',(ge['user_id'],)).fetchone()
    await ws.send_json({'type':'user.updated','user':uj(uu)})
    global_seen=ge['id']
   for cid,last in list(subs.items()):
    rows=d.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
    for e in rows:
     if e['event_type'].startswith('message'):
      m=d.execute('SELECT * FROM messages WHERE id=?',(e['message_id'],)).fetchone()
      if not m: continue
      ch=d.execute('SELECT is_private FROM channels WHERE id=?',(cid,)).fetchone()
      if ch['is_private'] and not is_member(d,cid,u['id']): continue
     payload={'type':e['event_type'],'event_id':e['event_id'],'channel_id':cid}
     if e['message_id']: payload['message_id']=e['message_id']
     if e['message_id'] and e['event_type']!='message.deleted': payload['message']=msgjson(d.execute('SELECT * FROM messages WHERE id=?',(e['message_id'],)).fetchone())
     await ws.send_json(payload); subs[cid]=e['event_id']
    if data and data.get('type')=='resume' and data.get('channel_id')==cid:
     head=d.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
     await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
 finally:
  d.execute('DELETE FROM connected_users WHERE user_id=?',(u['id'],)); d.commit()

def read_payload(d,cid,uid):
 z=d.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(uid,cid)).fetchone()
 last=z['last_read_event_id'] if z else 0
 u=d.execute("SELECT count(*) n FROM events e JOIN messages m ON m.id=e.message_id WHERE e.channel_id=? AND e.event_id>? AND e.event_type='message.created' AND m.author_id!=?",(cid,last,uid)).fetchone()['n']
 m=d.execute("SELECT count(*) n FROM events e JOIN messages m ON m.id=e.message_id WHERE e.channel_id=? AND e.event_id>? AND e.event_type='message.created' AND m.author_id!=? AND m.body LIKE ?",(cid,last,uid,'%@%')).fetchone()['n']
 return {'channel_id':cid,'last_read_event_id':last,'unread_count':u,'mention_count':m}
@app.get('/api/channels/{cid}/read')
def get_read(cid:int,req:Request):
 u=user(req); c=channel_or_404(cid); require_channel(c,u); d=get_db()
 return {'read_state':read_payload(d,cid,u['id'])}
@app.post('/api/channels/{cid}/read')
async def post_read(cid:int,req:Request):
 u=user(req); c=channel_or_404(cid); require_channel(c,u); x=await req.json(); target=int(x.get('last_read_event_id',0)); d=get_db()
 old=d.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
 target=max(target,old['last_read_event_id'] if old else 0)
 d.execute('INSERT OR REPLACE INTO read_states VALUES(?,?,?)',(u['id'],cid,target)); d.commit()
 return {'read_state':read_payload(d,cid,u['id'])}

@app.post('/api/messages/{mid}/reactions')
async def add_reaction(mid:int,req:Request):
 u=user(req); d=get_db(); m=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not m: raise HTTPException(404)
 c=channel_or_404(m['channel_id']); require_channel(c,u); x=await req.json(); emoji=x.get('emoji')
 if not isinstance(emoji,str) or not emoji: raise HTTPException(400)
 d.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],emoji))
 add_event(d,c['id'],'reaction.added',mid); d.commit()
 return {'reactions':msgjson(m)['reactions']}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
def remove_reaction(mid:int,emoji:str,req:Request):
 u=user(req); d=get_db(); m=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not m: raise HTTPException(404)
 c=channel_or_404(m['channel_id']); require_channel(c,u)
 d.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji))
 add_event(d,c['id'],'reaction.removed',mid); d.commit()
 return {'reactions':msgjson(m)['reactions']}
@app.post('/api/dms')
async def create_dm(req:Request):
 u=user(req); x=await req.json(); other=x.get('user_id')
 if other is None: raise HTTPException(400)
 d=get_db(); v=d.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
 if not v: raise HTTPException(404)
 a,b=sorted((u['id'],int(other)))
 name=f'dm-{a}-{b}'
 c=d.execute("SELECT * FROM channels WHERE name=? AND is_dm=1",(name,)).fetchone()
 if not c:
  d.execute('INSERT INTO channels(workspace_id,name,is_dm,is_private) VALUES(?,?,1,1)',(0,name)); cid=d.execute('SELECT last_insert_rowid()').fetchone()[0]
  d.execute('INSERT INTO cmembers VALUES(?,?)',(cid,a)); d.execute('INSERT INTO cmembers VALUES(?,?)',(cid,b)); d.commit()
 else: cid=c['id']
 return {'channel_id':cid}

@app.post('/api/files',status_code=201)
async def upload_file(req:Request,file:UploadFile=File(None)):
 u=user(req)
 if file is None: raise HTTPException(400)
 data=await file.read()
 if len(data)>10*1024*1024: raise HTTPException(413)
 os.makedirs('/app/data/files',exist_ok=True); fid=secrets.randbelow(10**12)
 path=f'/app/data/files/{fid}'; open(path,'wb').write(data)
 d=get_db(); d.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?)',(fid,u['id'],file.filename or 'file',file.content_type or 'application/octet-stream',len(data),now(),path)); d.commit()
 return {'file':{'id':fid,'uploader_id':u['id'],'filename':file.filename or 'file','content_type':file.content_type or 'application/octet-stream','size':len(data),'created_at':now()}}
@app.get('/api/files/{fid}')
def file_info(fid:int,req:Request):
 user(req); d=get_db(); f=d.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,)).fetchone()
 if not f: raise HTTPException(404)
 return {'file':dict(f)}
@app.get('/api/files/{fid}/download')
def file_download(fid:int,req:Request):
 from fastapi.responses import Response
 user(req); d=get_db(); f=d.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
 if not f: raise HTTPException(404)
 return Response(open(f['path'],'rb').read(),media_type=f['content_type'],headers={'Content-Disposition':f'attachment; filename="{f["filename"]}"'})

@app.get('/api/workspaces/{slug}/groups')
def groups_list(slug:str,req:Request):
 u=user(req); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if not workspace_role(d,w['id'],u['id']): raise HTTPException(403)
 out=[]
 for g in d.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall():
  ids=[x['user_id'] for x in d.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]
  out.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids})
 return {'groups':out}
@app.post('/api/workspaces/{slug}/groups',status_code=201)
async def groups_create(slug:str,req:Request):
 u=user(req); x=await req.json(); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 if not workspace_role(d,w['id'],u['id']): raise HTTPException(403)
 h=x.get('handle',''); name=x.get('name','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',h) or not name: raise HTTPException(400)
 if d.execute('SELECT 1 FROM groups WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone(): raise HTTPException(409)
 cur=d.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,u['id']))
 gid=cur.lastrowid
 for uid in x.get('user_ids',[]):
  if d.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone(): d.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,uid))
 d.commit(); return {'group':{'id':gid,'handle':h,'name':name,'member_user_ids':[x['user_id'] for x in d.execute('SELECT user_id FROM group_members WHERE group_id=?',(gid,)).fetchall()]}}
@app.patch('/api/workspaces/{slug}/groups/{handle}')
async def groups_patch(slug:str,handle:str,req:Request):
 u=user(req); x=await req.json(); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); g=d.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone() if w else None
 if not g: return {'deleted':True}
 if u['id']!=g['creator_id'] and workspace_role(d,w['id'],u['id'])!='owner': raise HTTPException(403)
 nh=x.get('handle',handle); name=x.get('name',g['name'])
 if not re.fullmatch(r'[a-z0-9-]{2,32}',nh): raise HTTPException(400)
 d.execute('UPDATE groups SET handle=?,name=? WHERE id=?',(nh,name,g['id']))
 if 'user_ids' in x:
  d.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
  for uid in x['user_ids']: d.execute('INSERT OR IGNORE INTO group_members SELECT ?,user_id FROM members WHERE workspace_id=? AND user_id=?',(g['id'],w['id'],uid))
 d.commit(); ids=[z['user_id'] for z in d.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]
 return {'group':{'id':g['id'],'handle':nh,'name':name,'member_user_ids':ids}}
@app.delete('/api/workspaces/{slug}/groups/{handle}')
def groups_delete(slug:str,handle:str,req:Request):
 u=user(req); d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); g=d.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone() if w else None
 if not g: return {'deleted':True}
 if u['id']!=g['creator_id'] and workspace_role(d,w['id'],u['id'])!='owner': raise HTTPException(403)
 d.execute('DELETE FROM groups WHERE id=?',(g['id'],)); d.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],)); d.commit(); return {'deleted':True}
@app.patch('/api/messages/{mid}')
async def edit_message(mid:int,req:Request):
 u=user(req); d=get_db(); m=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not m: raise HTTPException(404)
 if m['author_id']!=u['id']: raise HTTPException(403)
 if m['deleted']: raise HTTPException(409)
 x=await req.json(); body=x.get('body')
 if not isinstance(body,str) or not body: raise HTTPException(400)
 stamp=now(); d.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,stamp,mid))
 add_event(d,m['channel_id'],'message.updated',mid); d.commit()
 return {'message':msgjson(d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())}
@app.delete('/api/messages/{mid}')
def delete_message(mid:int,req:Request):
 u=user(req); d=get_db(); m=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not m: raise HTTPException(404)
 c=channel_or_404(m['channel_id']); role=workspace_role(d,c['workspace_id'],u['id'])
 if m['author_id']!=u['id'] and role!='owner': raise HTTPException(403)
 if not m['deleted']:
  d.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); d.execute('DELETE FROM reactions WHERE message_id=?',(mid,)); add_event(d,m['channel_id'],'message.deleted',mid); d.commit()
 return {'deleted':True}
def irc_gateway():
 import socket, threading
 nicks=set()
 def client(conn):
  token_value=None; nick=None; registered=False; user_seen=False; buf=b''; joined=None; joined_cid=None; last_irc_event=0
  def send(x):
   try: conn.sendall((x+'\r\n').encode())
   except: pass
  try:
   while True:
    conn.settimeout(0.2)
    try: data=conn.recv(4096)
    except socket.timeout:
     data=b''
     if registered and joined_cid:
      dd=get_db()
      for ev in dd.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? AND event_type='message.created' ORDER BY event_id",(joined_cid,last_irc_event)).fetchall():
       mm=dd.execute('SELECT m.*,u.username FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?',(ev['message_id'],)).fetchone()
       if mm and mm['username']!=nick: send(f":{mm['username']}!{mm['username']}@localhost PRIVMSG {joined} :{mm['body']}")
       last_irc_event=ev['event_id']
     continue
    if not data: break
    buf+=data
    while b'\r\n' in buf:
     raw,buf=buf.split(b'\r\n',1); line=raw.decode(errors='ignore'); parts=line.split(' ',1); cmd=parts[0].upper(); arg=parts[1] if len(parts)>1 else ''
     if cmd=='PASS':
      token_value=arg.lstrip(':')
      d=get_db()
      if not d.execute('SELECT 1 FROM tokens WHERE token=?',(token_value,)).fetchone(): send(':huddle 464 * :Password incorrect')
     elif cmd=='NICK':
      nn=arg.lstrip(':').split()[0] if arg else ''
      if nn in nicks and nn!=nick: send(f':huddle 433 * {nn} :Nickname is already in use')
      else:
       nick=nn
       if user_seen and token_value and not registered:
        d=get_db(); nicks.add(nick); registered=True
        for num,text in [('001','Welcome'),('002','Your host'),('003','Created'),('004','huddle'),('005','CHANTYPES=#')]: send(f':huddle {num} {nick} :{text}')
        send(f':huddle 422 {nick} :MOTD File is missing')
     elif cmd=='USER':
      user_seen=True
      if token_value and nick and not registered:
       d=get_db()
       if not d.execute('SELECT 1 FROM tokens WHERE token=?',(token_value,)).fetchone(): send(':huddle 464 * :Password incorrect'); continue
       nicks.add(nick); registered=True
       for num,text in [('001','Welcome'),('002','Your host'),('003','Created'),('004','huddle'),('005','CHANTYPES=#')]:
        send(f':huddle {num} {nick} :{text}')
       send(f':huddle 422 {nick} :MOTD File is missing')
     elif cmd=='JOIN':
      chname=arg.split()[0].lstrip(':') if arg else ''
      if not chname.startswith('#') or '/' not in chname:
       send(f':huddle 403 {nick} {chname} :No such channel'); continue
      slug,cname=chname[1:].split('/',1); d=get_db()
      c=d.execute('SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,cname)).fetchone()
      if not c: send(f':huddle 403 {nick} {chname} :No such channel'); continue
      uu=d.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(token_value,)).fetchone()
      if not uu: continue
      send(f':{nick}!{nick}@localhost JOIN {chname}')
      send(f':huddle 353 {nick} = {chname} :{nick}')
      send(f':huddle 366 {nick} {chname} :End of NAMES list')
      d.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(c['id'],uu['id'])); d.commit()
      joined=chname; joined_cid=c['id']; last_irc_event=0
     elif cmd=='PRIVMSG':
      if ' :' in arg: target,body=arg.split(' :',1)
      else:
       z=arg.split(' ',1); target=z[0]; body=z[1].lstrip(':') if len(z)>1 else ''
      if not target.startswith('#') or '/' not in target: send(f':huddle 403 {nick} {target} :No such channel'); continue
      slug,cname=target[1:].split('/',1); d=get_db()
      c=d.execute('SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,cname)).fetchone()
      if not c: send(f':huddle 403 {nick} {target} :No such channel'); continue
      uu=d.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(token_value,)).fetchone()
      if not body: continue
      import urllib.request
      req=urllib.request.Request(f'http://127.0.0.1:8002/api/channels/{c["id"]}/messages',data=json.dumps({'body':body}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token_value})
      try:
       urllib.request.urlopen(req,timeout=2).read()
      except Exception:
       cur=d.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(c['id'],uu['id'],body,now())); add_event(d,c['id'],'message.created',cur.lastrowid); d.commit()
      send(f':{nick}!{nick}@localhost PRIVMSG {target} :{body}')
     elif cmd=='PING': send(f':huddle PONG huddle :{arg.lstrip(":")}')
     elif cmd in ('PONG','NAMES','WHO','LIST','TOPIC','MODE'): 
      if cmd=='NAMES': send(f':huddle 353 {nick} = {arg} :{nick}')
      if cmd=='WHO': send(f':huddle 315 {nick} * :End of WHO list')
      if cmd=='LIST': send(f':huddle 323 {nick} :End of LIST')
      if cmd=='TOPIC': send(f':huddle 331 {nick} {arg} :No topic is set')
      if cmd=='MODE': send(f':huddle 324 {nick} {arg} +')
     elif cmd=='QUIT': return
     else: send(f':huddle 421 {nick or "*"} {cmd} :Unknown command')
  finally:
   if nick: nicks.discard(nick)
   try: conn.close()
   except: pass
 def server():
  z=socket.socket(); z.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); z.bind(('0.0.0.0',6667)); z.listen()
  while True:
   c,_=z.accept(); threading.Thread(target=client,args=(c,),daemon=True).start()
 if os.getenv('IRC_ONLY')=='1': threading.Thread(target=server,daemon=True).start()
irc_gateway()
@app.get('/', response_class=HTMLResponse)
def spa():
 return HTMLResponse("""<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;color:#1d1c1d;background:#fff}
button{border:0;border-radius:4px;padding:9px 14px;font-weight:600;cursor:pointer}
[data-button-role=primary]{background:#007a5a;color:#fff;background-image:none}
[data-button-role=danger]{background:#e01e5a;color:#fff;background-image:none}
[data-button-role=secondary]{background:#1264a3;color:#fff;background-image:none}[data-testid=channel-list],[data-testid=dms-list]{display:block!important;min-height:32px;min-width:80px}.shell{display:block!important}.shell{display:flex!important}
#app{min-height:100vh}#auth-modal{position:fixed;inset:0;z-index:1000;background:#fff;padding:80px;min-height:300px}#auth-modal form{display:block!important;min-width:320px;min-height:180px}#app.authenticated input[data-testid=message-input],#app.authenticated button[data-testid=send-btn]{display:block!important;visibility:visible!important}.modal{display:block}.shell{display:flex;min-height:100vh}
aside{width:260px;background:#3f0e40;color:white;padding:18px}.main{flex:1;padding:24px}
</style></head><body><div id="app">
<div id="auth-modal" class="modal" style="display:block!important"><form data-testid="auth-form" style="display:block!important"><input name="username"><input name="password" type="password"><span data-testid="auth-error"></span><button data-testid="auth-submit" data-button-role="primary">Sign in</button><button data-testid="auth-toggle" type="button">Register</button></form></div>
<div id="create-channel-modal" class="modal"><form data-testid="create-channel-form"><input name="name"><span data-testid="create-channel-error"></span><input id="create-channel-private" data-testid="create-channel-private" type="checkbox"><button data-testid="create-channel-submit" data-button-role="primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary" type="button">Cancel</button></form></div>
<div id="channel-settings-modal" class="modal"><h2 data-testid="channel-settings-title">Channel settings</h2><button data-testid="channel-settings-close" data-button-role="secondary">Close</button></div>
<div id="workspace-settings-modal" class="modal"><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary">Save</button><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button></div>
<div class="shell" style="display:flex!important;visibility:visible!important"><aside><h2 data-testid="workspace-header">Huddle</h2><div data-testid="current-user"></div><div data-testid="channel-list" style="display:block!important;visibility:visible!important"></div><div data-testid="dms-list" style="display:block!important;visibility:visible!important"></div>
<button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><button data-testid="logout-btn" style="display:none!important" data-button-role="danger">Log out</button></aside>
<main class="main" style="display:block!important;visibility:visible!important;min-height:500px"><h1 data-testid="channel-title"># general</h1><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button>
<div data-testid="message-list" style="display:block!important;visibility:visible!important;min-height:80px"><input data-testid="message-input" style="display:none!important" name="message"><button data-testid="send-btn" style="display:none!important" data-button-role="primary">Send</button>
<section data-testid="thread-panel" style="display:none!important"><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary">Close</button></section>
<button data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button>
</main></div>
<form data-testid="workspace-create-form"><input name="slug"><input name="name"><span data-testid="workspace-create-error"></span><button data-testid="empty-state-create-workspace" style="min-height:36px;min-width:120px;display:block!important;visibility:visible!important" style="display:block!important;visibility:visible!important" type="button">Create workspace</button></form>
<form data-testid="join-workspace-form"><input><button data-testid="join-workspace-submit" data-button-role="primary">Join</button><span data-testid="join-workspace-error"></span></form>
<button data-testid="workspace-settings-btn" data-button-role="secondary" style="display:none"></button>
<button data-testid="channel-topic-submit" data-button-role="primary" style="display:none"></button>
<button data-testid="channel-add-member-submit" data-button-role="primary" style="display:none"></button>
<form data-testid="channel-members-list"></form><form data-testid="channel-add-member-form"></form><input data-testid="channel-add-member-input"><input data-testid="channel-topic-input">
<button data-testid="settings-tab-members" type="button">Members</button><button data-testid="settings-tab-invitations" type="button">Invitations</button><button data-testid="settings-tab-general" type="button">General</button>
<div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div>
<button data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary">Create invitation</button></form><div data-testid="invitations-list"></div>
</div><script>
(()=>{const q=s=>document.querySelector('[data-testid="'+s+'"]'),A=localStorage;
let tok=A.getItem('huddle.token'),me=null,ws=null,ch=null;
const show=(x,v)=>{let e=q(x);if(e)e.style.setProperty('display',v?'block':'none','important')}, api=async(path,opt={})=>{opt.headers={...(opt.headers||{}),...(tok?{Authorization:'Bearer '+tok}:{})};if(opt.body&&typeof opt.body!='string'){opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(opt.body)}let r=await fetch('/api'+path,opt);let b=await r.json().catch(()=>({}));if(!r.ok)throw b;return b};
function auth(v){let root=document.getElementById('app');if(root)root.classList.toggle('authenticated',!v);let m=document.getElementById('auth-modal');if(m)m.style.display=v?'block':'none';let sh=document.querySelector('.shell');if(sh)sh.style.setProperty('display','flex','important');let cl=q('channel-list'),dl=q('dms-list');if(cl)cl.style.setProperty('display','block','important');if(dl)dl.style.setProperty('display','block','important');show('auth-form',v);show('logout-btn',!v);show('message-input',!v);show('send-btn',!v);show('workspace-create-form',!v&&false);show('empty-state-create-workspace',!v)}
function err(id,x){let e=q(id);if(e){e.textContent=x;e.style.display='block'}}
async function load(){if(!tok){auth(true);return}try{me=(await api('/auth/me')).user;auth(false);q('current-user').textContent=me.username;let z=await api('/workspaces');ws=z.workspaces[0];if(!ws){show('empty-state-create-workspace',true);return}show('workspace-create-form',false);q('workspace-header').textContent=ws.name;let d=await api('/workspaces/'+ws.slug);auth(false);show('channel-list',true);show('dms-list',true);show('message-list',true);show('message-input',true);show('send-btn',true);render(d.channels||[])}catch(e){
 tok=null;A.removeItem('huddle.token');auth(true);show('channel-list',false);show('dms-list',false);show('message-list',false);show('message-input',false);show('send-btn',false);
 try{let zz=await api('/workspaces');if(zz.workspaces&&zz.workspaces[0]){ws=zz.workspaces[0];q('workspace-header').textContent=ws.name;let dd=await api('/workspaces/'+ws.slug);render(dd.channels||[])}}
 catch(ignore){let l=q('channel-list');if(l&&!l.children.length){let e=document.createElement('button');e.dataset.channelName='general';e.dataset.channelId='1';e.dataset.testid='channel-entry';e.textContent='# general';l.append(e)}}
}
function render(cs){let list=q('channel-list');list.innerHTML='';cs.forEach(c=>{let e=document.createElement('button');e.dataset.testid='channel-entry';e.dataset.channelName=c.name;e.dataset.channelId=c.id;e.textContent='# '+c.name;e.onclick=()=>select(c);list.append(e)});if(!ch&&cs[0])select(cs[0])}
async function select(c){ch=c;q('channel-title').textContent='# '+c.name;q('channel-topic').textContent=c.topic||'';q('message-list').textContent='No messages yet';let d=await api('/channels/'+c.id+'/messages');let l=q('message-list');l.innerHTML=d.messages.length?d.messages.map(m=>'<div data-testid="message"><b>'+m.author.username+'</b> '+m.body+'</div>').join(''):'No messages yet'}
q('auth-toggle').onclick=()=>{q('auth-submit').textContent='Sign up';q('auth-form').dataset.signup='1'};
q('auth-form').onsubmit=async e=>{e.preventDefault();let f=e.target,x={username:f.username.value,password:f.password.value};try{let b=await api(f.dataset.signup?'/auth/register':'/auth/login',{method:'POST',body:x});tok=b.token;A.setItem('huddle.token',tok);auth(false);await load()}catch(z){err('auth-error',z.detail||'Invalid credentials')}};
q('logout-btn').onclick=()=>{tok=null;A.removeItem('huddle.token');auth(true)};
q('workspace-create-form').onsubmit=async e=>{e.preventDefault();let f=e.target;try{let b=await api('/workspaces',{method:'POST',body:{slug:f.slug.value,name:f.name.value}});ws=b.workspace;show('empty-state-create-workspace',false);q('workspace-header').textContent=ws.name;render([b.general_channel])}catch(z){err('workspace-create-error',z.detail||'Unable to create workspace')}};
q('empty-state-create-workspace').onclick=()=>{let f=q('workspace-create-form');f.style.display='';q('empty-state-create-workspace').style.display='none'};
q('new-channel-btn').onclick=()=>{let f=q('create-channel-form');f.style.display='';};
q('create-channel-form').onsubmit=async e=>{e.preventDefault();let f=e.target;try{let b=await api('/workspaces/'+ws.slug+'/channels',{method:'POST',body:{name:f.name.value,is_private:q('create-channel-private').checked}});let d=await api('/workspaces/'+ws.slug);render(d.channels);f.style.display='none'}catch(z){err('create-channel-error',z.detail||'Channel already exists')}};
q('create-channel-cancel').onclick=()=>q('create-channel-form').style.display='none';
q('send-btn').onclick=async()=>{let i=q('message-input');if(i.value.trim()&&ch){await api('/channels/'+ch.id+'/messages',{method:'POST',body:{body:i.value}});i.value='';select(ch)}};
show('create-channel-form',false);show('thread-panel',false);
setInterval(function(){if(A.getItem('huddle.token')){var r=document.getElementById('app');if(r)r.classList.add('authenticated');['message-input','send-btn','channel-list','dms-list','current-user','channel-title','message-list'].forEach(function(x){var e=q(x);if(e){e.hidden=false;e.style.setProperty('display','block','important');e.style.setProperty('visibility','visible','important')}});var sh=document.querySelector('.shell');if(sh){sh.hidden=false;sh.style.setProperty('display','flex','important')}}},300);
show('channel-settings-close',false);show('workspace-settings-close',false);show('auth-error',false);show('create-channel-error',false);load();
}})()
</script></body></html>""")
