import os,sqlite3,secrets,re,datetime,json,asyncio
from fastapi import FastAPI,Request,HTTPException,WebSocket,WebSocketDisconnect,UploadFile,File
from fastapi.responses import FileResponse,HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
DB='/app/data/huddle.db'; NODE=int(os.getenv('NODE_ID','0'))
app=FastAPI(); app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])
def db():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row
 return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
 CREATE TABLE IF NOT EXISTS members(workspace_id,user_id,role,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cmembers(channel_id,user_id,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id,user_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id,user_id,emoji,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));'''); c.commit(); c.close()
init()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def user(r): return dict(r) if r else None
def auth(req):
 h=req.headers.get('authorization',''); t=h[7:] if h.lower().startswith('bearer ') else ''
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close()
 if not r: raise HTTPException(401,'unauthorized')
 return r
def juser(r):
 d=user(r); d.update(timezone='UTC',avatar_url='',status_text='',status_emoji=''); return d
def member(uid,wid): return db().execute('SELECT * FROM members WHERE user_id=? AND workspace_id=?',(uid,wid)).fetchone()
def channel(cid):
 return db().execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
def msgjson(r):
 c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(r['user_id'],)).fetchone()
 rx=[]
 for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  rx.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]})
 d=dict(r); d.update(author=juser(u),parent_id=d['parent_id'],reply_count=c.execute('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],files=[],reactions=rx,mentions=[])
 return d
async def broadcast(cid,typ,payload):
 c=db(); row=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone(); eid=row['n']
 c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(cid,eid,typ,json.dumps(payload),now())); c.commit(); c.close()
 return eid
@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(req:Request):
 x=await req.json(); un=x.get('username',''); pw=x.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise HTTPException(400,'invalid')
 c=db()
 try: c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,x.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 except sqlite3.IntegrityError: c.close(); raise HTTPException(409,'duplicate')
 t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close()
 return {'user':juser(r),'token':t}
@app.post('/api/auth/login')
async def login(req:Request):
 x=await req.json(); c=db(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(x.get('username'),x.get('password'))).fetchone()
 if not r: c.close(); raise HTTPException(401,'wrong')
 t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); c.close(); return {'user':juser(r),'token':t}
@app.get('/api/auth/me')
def me(request:Request): return {'user':juser(auth(request))}
@app.post('/api/workspaces',status_code=201)
async def workspace(req:Request):
 u=auth(req); x=await req.json(); slug=x.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): raise HTTPException(400,'slug')
 c=db()
 try: c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,x.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
 except sqlite3.IntegrityError: c.close(); raise HTTPException(409,'duplicate')
 c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); ch=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cmembers VALUES(?,?)',(ch,u['id'])); c.commit(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); q=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone(); c.close()
 return {'workspace':dict(w),'general_channel':dict(q)}
@app.get('/api/workspaces')
def workspaces(request:Request):
 u=auth(request); c=db(); a=[dict(x) for x in c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],))]; c.close(); return {'workspaces':a}
@app.get('/api/workspaces/{slug}')
def getworkspace(slug:str,request:Request):
 u=auth(request); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: raise HTTPException(404)
 cs=[dict(x) for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],))]; c.close(); return {'workspace':dict(w),'channels':cs,'read_state':{}}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def create_channel(slug:str,request:Request):
 u=auth(request); x=await request.json(); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w: c.close(); raise HTTPException(404)
 if not member(u['id'],w['id']): c.close(); raise HTTPException(403)
 name=x.get('name','')
 if not re.fullmatch(r'[a-z0-9-]{1,32}',name): c.close(); raise HTTPException(400)
 if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone(): c.close(); raise HTTPException(409)
 if x.get('is_private') and member(u['id'],w['id'])['role'] not in ('owner','admin'): c.close(); raise HTTPException(403)
 c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(x.get('is_private'))),x.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id'])); c.commit(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return {'channel':dict(r)}
@app.post('/api/channels/{cid}/join')
async def join(cid:int,request:Request):
 u=auth(request); c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch: c.close(); raise HTTPException(404)
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
 if ch['is_private']: c.close(); raise HTTPException(403)
 if not member(u['id'],w['id']): c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
 c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id'])); c.commit(); c.close(); return {'channel':dict(ch)}
@app.get('/api/channels/{cid}/members')
def channel_members(cid:int,request:Request):
 u=auth(request); c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch: c.close(); raise HTTPException(404)
 rows=[juser(x) for x in c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(cid,))]; c.close(); return {'members':rows}
@app.patch('/api/channels/{cid}')
async def patch_channel(cid:int,request:Request):
 u=auth(request); x=await request.json(); c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch: c.close(); raise HTTPException(404)
 if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): c.close(); raise HTTPException(403)
 c.execute('UPDATE channels SET topic=? WHERE id=?',(x.get('topic',''),cid)); c.commit(); r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone(); c.close(); return {'channel':dict(r)}
@app.post('/api/channels/{cid}/messages',status_code=201)
async def post_message(cid:int,request:Request):
 u=auth(request); x=await request.json(); body=x.get('body','')
 if not body or not body.strip(): raise HTTPException(400,'empty')
 c=db(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch: c.close(); raise HTTPException(404)
 if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): c.close(); raise HTTPException(403)
 t=now(); c.execute('INSERT INTO messages(channel_id,user_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,x.get('parent_id'),t)); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); c.commit(); c.close()
 d=msgjson(r); typ='message.reply' if x.get('parent_id') else 'message.created'; await broadcast(cid,typ,{'message':d}); return {'message':d}
@app.get('/api/channels/{cid}/messages')
def messages(cid:int,request:Request):
 u=auth(request); c=db()
 if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): c.close(); raise HTTPException(403)
 rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC',(cid,)).fetchall(); c.close(); return {'messages':[msgjson(x) for x in rows],'next_cursor':None}
@app.get('/api/messages/{mid}/replies')
def replies(mid:int,request:Request):
 auth(request); c=db(); rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,)).fetchall(); c.close(); return {'replies':[msgjson(x) for x in rows],'next_cursor':None}
@app.patch('/api/messages/{mid}')
async def edit(mid:int,request:Request):
 u=auth(request); x=await request.json(); c=db(); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r: c.close(); raise HTTPException(404)
 if r['deleted']: c.close(); raise HTTPException(409)
 if r['user_id']!=u['id']: c.close(); raise HTTPException(403)
 if not x.get('body','').strip(): c.close(); raise HTTPException(400)
 t=now(); c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(x['body'],t,mid)); c.commit(); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); c.close(); d=msgjson(r); await broadcast(r['channel_id'],'message.updated',{'message':d}); return {'message':d}
@app.delete('/api/messages/{mid}')
async def delete(mid:int,request:Request):
 u=auth(request); c=db(); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not r: c.close(); raise HTTPException(404)
 if r['deleted']: c.close(); return {'deleted':True}
 role=c.execute('SELECT m.role FROM members m JOIN channels h ON h.workspace_id=m.workspace_id WHERE m.user_id=? AND h.id=?',(u['id'],r['channel_id'])).fetchone()
 if r['user_id']!=u['id'] and (not role or role['role']!='owner'): c.close(); raise HTTPException(403)
 c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); c.commit(); c.close(); await broadcast(r['channel_id'],'message.deleted',{'message_id':mid}); return {'deleted':True}
@app.post('/api/messages/{mid}/reactions')
async def add_reaction(mid:int,request:Request):
 u=auth(request); x=await request.json(); c=db(); r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
 if not r: c.close(); raise HTTPException(404)
 c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],x.get('emoji','🙂'))); c.commit(); vals=[]
 for z in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)): vals.append({'emoji':z['emoji'],'count':z['n'],'user_ids':[int(a) for a in z['ids'].split(',')]})
 c.close(); await broadcast(r['channel_id'],'reaction.added',{'message_id':mid,'reactions':vals}); return {'reactions':vals}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
async def del_reaction(mid:int,emoji:str,request:Request):
 u=auth(request); c=db(); c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)); c.commit(); c.close(); return {'reactions':[]}
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
 await ws.accept(); token=ws.query_params.get('token',''); c=db(); r=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone(); c.close()
 if not r: await ws.close(code=1008); return
 subs={}
 try:
  while True:
   try: data=await asyncio.wait_for(ws.receive_json(),timeout=.25)
   except asyncio.TimeoutError: data=None
   if data:
    cid=int(data.get('channel_id',0))
    if data.get('type')=='subscribe':
     c=db(); h=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']; c.close(); subs[cid]=h; await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':h})
    elif data.get('type')=='resume':
     since=int(data.get('since_event_id',0)); subs[cid]=since; c=db(); ev=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall(); h=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']; c.close()
     for e in ev: await ws.send_json(dict({'type':e['type'],'event_id':e['event_id'],'channel_id':cid},**json.loads(e['payload'])))
     await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':h})
   for cid,last in list(subs.items()):
    c=db(); ev=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall(); c.close()
    for e in ev:
     await ws.send_json(dict({'type':e['type'],'event_id':e['event_id'],'channel_id':cid},**json.loads(e['payload']))); subs[cid]=e['event_id']
 except (WebSocketDisconnect,Exception): return
@app.get('/')
def home(): return FileResponse('/app/static/index.html')
@app.get('/static/{path:path}')
def static(path:str): return FileResponse('/app/static/'+path)
