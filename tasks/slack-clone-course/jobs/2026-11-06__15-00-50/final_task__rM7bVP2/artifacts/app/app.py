import os,sqlite3,json,secrets,re,hashlib,hmac,asyncio,datetime
from fastapi import FastAPI,Request,HTTPException,WebSocket,WebSocketDisconnect
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
app=FastAPI(); app.mount('/static',StaticFiles(directory='/app/static'),name='static'); app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])
def db():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 c=db(); c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created TEXT,edited TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));'''); c.commit(); c.close()
init()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def userobj(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'],'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}
def auth(req):
 x=req.headers.get('authorization','')
 if not x.lower().startswith('bearer '): raise HTTPException(401)
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(x[7:],)).fetchone(); c.close()
 if not r: raise HTTPException(401)
 return r
def tokuser(t):
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def workspace(c,slug):
 r=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not r: raise HTTPException(404)
 return r
def channel(c,cid):
 r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not r: raise HTTPException(404)
 return r
def member(c,ch,uid):
 return c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch,uid)).fetchone()
def emit(c,cid,typ,payload):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
 c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(cid,n,typ,json.dumps(payload))); return n
def msgjson(c,r):
 u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 reps=[]
 for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  reps.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]})
 cnt=c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':cnt,'created_at':r['created'],'edited_at':r['edited'],'files':[],'reactions':reps,'mentions':[]}
@app.get('/')
def root(): return FileResponse('/app/static/index.html')
@app.get('/api/health')
def health(): return {'status':'ok','node_id':int(os.getenv('NODE_ID','0'))}
@app.post('/api/auth/register',status_code=201)
def register(b:dict):
 un=b.get('username',''); pw=b.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise HTTPException(400)
 c=db()
 try:
  cur=c.execute('INSERT INTO users(username,password,display_name,created) VALUES(?,?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un,now())); uid=cur.lastrowid
  t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit()
 except sqlite3.IntegrityError: c.close(); raise HTTPException(409)
 r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return {'user':userobj(r),'token':t}
@app.post('/api/auth/login')
def login(b:dict):
 c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest())).fetchone()
 if not r: c.close(); raise HTTPException(401)
 t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); c.close(); return {'user':userobj(r),'token':t}
@app.get('/api/auth/me')
def me(req:Request): return {'user':userobj(auth(req))}
@app.get('/api/users/{uid}')
def getuser(uid:int,req:Request):
 auth(req); c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close()
 if not r: raise HTTPException(404)
 return {'user':userobj(r)}
@app.post('/api/workspaces',status_code=201)
def create_ws(b:dict,req:Request):
 u=auth(req); slug=b.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): raise HTTPException(400)
 c=db()
 try: wid=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b.get('name',''),u['id'])).lastrowid
 except sqlite3.IntegrityError: c.close(); raise HTTPException(409)
 c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); cid=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')).lastrowid; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id'])); c.commit()
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close()
 return {'workspace':{'id':wid,'slug':slug,'name':w['name'],'owner_id':u['id'],'join_mode':'open'},'general_channel':chobj(ch)}
def chobj(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':False,'topic':r['topic'],'is_archived':bool(r['is_archived'])}
@app.get('/api/workspaces')
def list_ws(req:Request):
 u=auth(req); c=db(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close()
 return {'workspaces':[{'id':r['id'],'slug':r['slug'],'name':r['name'],'owner_id':r['owner_id'],'join_mode':'open'} for r in rows]}
@app.get('/api/workspaces/{slug}')
def get_ws(slug:str,req:Request):
 u=auth(req); c=db(); w=workspace(c,slug); rows=c.execute('SELECT ch.* FROM channels ch WHERE ch.workspace_id=? AND (ch.is_private=0 OR EXISTS (SELECT 1 FROM cmembers m WHERE m.channel_id=ch.id AND m.user_id=?))',(w['id'],u['id'])).fetchall(); c.close()
 return {'workspace':{'id':w['id'],'slug':w['slug'],'name':w['name'],'owner_id':w['owner_id'],'join_mode':'open'},'channels':[chobj(x) for x in rows],'read_state':{}}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
def create_ch(slug:str,b:dict,req:Request):
 u=auth(req); c=db(); w=workspace(c,slug)
 if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(): raise HTTPException(403)
 name=b.get('name','')
 if not re.fullmatch(r'[a-z0-9-]{1,32}',name): raise HTTPException(400)
 try: cid=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic',''))).lastrowid
 except sqlite3.IntegrityError: c.close(); raise HTTPException(409)
 c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id'])); c.commit(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return {'channel':chobj(r)}
@app.post('/api/channels/{cid}/join')
def join(cid:int,req:Request):
 u=auth(req); c=db(); ch=channel(c,cid); c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id'])); c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member')); c.commit(); c.close(); return {'channel':chobj(ch)}
@app.patch('/api/channels/{cid}')
def patch_ch(cid:int,b:dict,req:Request):
 u=auth(req); c=db(); ch=channel(c,cid)
 if not member(c,cid,u['id']): raise HTTPException(403)
 c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',ch['topic']),cid)); c.commit(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return {'channel':chobj(r)}
@app.get('/api/channels/{cid}/messages')
def messages(cid:int,req:Request,limit:int=50,cursor:str=None):
 u=auth(req); c=db(); ch=channel(c,cid)
 if not member(c,cid,u['id']): raise HTTPException(403)
 rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT 200',(cid,)).fetchall(); out=[msgjson(c,r) for r in rows]; c.close(); return {'messages':out[:min(limit,200)],'next_cursor':None}
@app.post('/api/channels/{cid}/messages',status_code=201)
def post(cid:int,b:dict,req:Request):
 u=auth(req); body=b.get('body','')
 if not body.strip(): raise HTTPException(400)
 c=db(); ch=channel(c,cid)
 if not member(c,cid,u['id']): raise HTTPException(403)
 if ch['is_archived']: raise HTTPException(423)
 parent=b.get('parent_id'); t=now(); mid=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created) VALUES(?,?,?,?,?)',(cid,u['id'],body,parent,t)).lastrowid
 r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); obj=msgjson(c,r); typ='message.reply' if parent else 'message.created'; eid=emit(c,cid,typ,{'message':obj}); c.commit(); c.close(); return {'message':obj}
@app.patch('/api/messages/{mid}')
def edit(mid:int,b:dict,req:Request):
 u=auth(req); body=b.get('body','')
 if not body.strip(): raise HTTPException(400)
 c=db(); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r: c.close(); raise HTTPException(404)
 if r['deleted']: c.close(); raise HTTPException(409)
 if r['author_id']!=u['id']: c.close(); raise HTTPException(403)
 c.execute('UPDATE messages SET body=?,edited=? WHERE id=?',(body,now(),mid)); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); obj=msgjson(c,r); emit(c,r['channel_id'],'message.updated',{'message':obj}); c.commit(); c.close(); return {'message':obj}
@app.delete('/api/messages/{mid}')
def delete(mid:int,req:Request):
 u=auth(req); c=db(); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r: c.close(); raise HTTPException(404)
 w=c.execute('SELECT owner_id FROM workspaces w JOIN channels ch ON ch.workspace_id=w.id WHERE ch.id=?',(r['channel_id'],)).fetchone()
 if r['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']): c.close(); raise HTTPException(403)
 if not r['deleted']:
  c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); emit(c,r['channel_id'],'message.deleted',{'message_id':mid}); c.commit()
 c.close(); return {'deleted':True}
@app.get('/api/messages/{mid}/replies')
def replies(mid:int,req:Request,limit:int=50,cursor:str=None):
 u=auth(req); c=db(); p=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not p: raise HTTPException(404)
 if not member(c,p['channel_id'],u['id']): raise HTTPException(403)
 rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,)).fetchall(); out=[msgjson(c,x) for x in rs]; c.close(); return {'replies':out[:min(limit,200)],'next_cursor':None}
@app.post('/api/messages/{mid}/reactions')
def react(mid:int,b:dict,req:Request):
 u=auth(req); c=db(); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r: raise HTTPException(404)
 if not member(c,r['channel_id'],u['id']): raise HTTPException(403)
 c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],b.get('emoji','👍'))); c.commit(); rr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); out=msgjson(c,rr)['reactions']; emit(c,r['channel_id'],'reaction.added',{'message':msgjson(c,rr)}); c.commit(); c.close(); return {'reactions':out}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
def unreact(mid:int,emoji:str,req:Request):
 u=auth(req); c=db(); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r: raise HTTPException(404)
 c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)); c.commit(); out=msgjson(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())['reactions']; c.close(); return {'reactions':out}
@app.post('/api/dms')
def dm(b:dict,req:Request):
 u=auth(req); other=b.get('user_id'); c=db()
 rows=c.execute('SELECT id FROM channels WHERE is_private=1 AND name LIKE "dm-%"').fetchall()
 for x in rows:
  if set(r[0] for r in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(x['id'],)))=={u['id'],other}: c.close(); return {'channel_id':x['id']}
 ws=c.execute('SELECT id FROM workspaces WHERE owner_id=? ORDER BY id LIMIT 1',(u['id'],)).fetchone()
 if not ws: c.close(); raise HTTPException(404)
 cid=c.execute('INSERT INTO channels(workspace_id,name,is_private) VALUES(?,?,1)',(ws['id'],f'dm-{u["id"]}-{other}')).lastrowid
 c.execute('INSERT INTO cmembers VALUES(?,?),(?,?)',(cid,u['id'],cid,other)); c.commit(); c.close(); return {'channel_id':cid}
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
 token=ws.query_params.get('token','').removeprefix('Bearer ')
 u=tokuser(token)
 if not u: await ws.close(code=4401); return
 await ws.accept(); subs={}; last={}
 try:
  while True:
   try: data=await asyncio.wait_for(ws.receive_json(),.15)
   except asyncio.TimeoutError: data=None
   if data:
    cid=int(data.get('channel_id',0))
    if data.get('type')=='subscribe':
     subs[cid]=True; c=db(); head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]; c.close(); last[cid]=head; await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
    elif data.get('type')=='resume':
     since=int(data.get('since_event_id',0)); subs[cid]=True; last[cid]=since
   for cid in list(subs):
    c=db(); es=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last.get(cid,0))).fetchall(); head=last.get(cid,0)
    for e in es:
     p=json.loads(e['payload']); frame={'type':e['type'],'event_id':e['event_id'],'channel_id':cid,**p}; await ws.send_json(frame); head=e['event_id']
    last[cid]=head; c.close()
   await asyncio.sleep(.03)
 except (WebSocketDisconnect,Exception): return
@app.get('/api/channels/{cid}/members')
def chmembers(cid:int,req:Request):
 auth(req); c=db(); rows=c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall(); c.close(); return {'members':[userobj(x) for x in rows]}
@app.post('/api/channels/{cid}/leave')
def leave(cid:int,req:Request):
 u=auth(req); c=db(); c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])); c.commit(); r=channel(c,cid); c.close(); return {'channel':chobj(r)}
