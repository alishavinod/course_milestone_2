import os, re, json, sqlite3, secrets, hashlib, mimetypes, asyncio, threading, time, socket, fcntl
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

DB='/app/data/huddle.db'; NODE=int(os.getenv('NODE_ID','0'))
lock=threading.Lock()
def db():
    c=sqlite3.connect(DB,check_same_thread=False,timeout=30, isolation_level=None); c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=30000'); c.execute('CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,payload TEXT)'); return c
def init():
    c=db()
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS dms(user1 INTEGER,user2 INTEGER,channel_id INTEGER,PRIMARY KEY(user1,user2));
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
    CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id));
    CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER);
    CREATE TABLE IF NOT EXISTS groupmembers(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id)); CREATE TABLE IF NOT EXISTS mention_counts(user_id INTEGER,channel_id INTEGER,count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));'''); c.commit()
init()
def redis_subscriber():
    while True:
        try:
            z=socket.create_connection(('127.0.0.1',6379),timeout=2); z.settimeout(2)
            z.sendall(b'*2\r\n$9\r\nSUBSCRIBE\r\n$6\r\nhuddle\r\n')
            ack=z.recv(4096)
            if b'subscribe' not in ack.lower(): raise RuntimeError('redis subscription rejected')
            while True:
                if not z.recv(4096):
                    raise ConnectionError('redis closed')
        except Exception:
            try: z.close()
            except Exception: pass
            time.sleep(0.5)
threading.Thread(target=redis_subscriber,daemon=True).start()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def row(c,q,p=()): return c.execute(q,p).fetchone()
def auth(req):
    t=req.headers.get('authorization','')
    if not t.startswith('Bearer '): raise HTTPException(401)
    r=row(db(),'SELECT user_id FROM tokens WHERE token=?',(t[7:],))
    if not r: raise HTTPException(401)
    return r['user_id']
def user(uid):
    r=row(db(),'SELECT * FROM users WHERE id=?',(uid,))
    if not r: return None
    return dict(r)
def uobj(uid): return user(uid)
def chan(cid):
    r=row(db(),'SELECT * FROM channels WHERE id=?',(cid,))
    if not r: return None
    d=dict(r); d['is_private']=bool(d['is_private']); d['is_dm']=bool(d['is_dm']); d['is_archived']=bool(d['is_archived']); return d
def member(cid,uid):
    return row(db(),'SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,uid)) is not None
def add_event(c,cid,typ,payload):
    with open('/tmp/huddle-events.lock','w') as lk:
        fcntl.flock(lk,fcntl.LOCK_EX)
        for _ in range(50):
            try:
                n=row(c,'SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,))['n']
                c.execute('INSERT INTO events VALUES(?,?,?,?)',(cid,n,typ,json.dumps(payload)))
                c.commit()
                return n
            except sqlite3.IntegrityError:
                c.rollback(); time.sleep(0.005)
            except sqlite3.OperationalError:
                c.rollback(); time.sleep(0.02)
        raise HTTPException(503,'event store busy')

def msgobj(c,r):
    if not r:return None
    d=dict(r); d['author']=uobj(d['author_id']); d['parent_id']=d['parent_id']
    d['reply_count']=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(d['id'],)).fetchone()['n']
    rs=[]
    for x in c.execute('SELECT emoji,count(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(d['id'],)):
        ids=[z['user_id'] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(d['id'],x['emoji']))]
        rs.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
    d['reactions']=rs; d['files']=[dict(x) for x in c.execute('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(d['id'],))]
    d['mentions']=[]
    return d
def resolve_mentions(c, body, cid, author):
    ids=set()
    ch=row(c,'SELECT workspace_id FROM channels WHERE id=?',(cid,))
    if not ch: return []
    for name in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body):
        if name in ('channel','here'):
            if name=='channel':
                ids.update(x['user_id'] for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(cid,)))
            else:
                for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(cid,)):
                    f=f'/tmp/huddle-ws-{x["user_id"]}'
                    if os.path.exists(f) and time.time()-os.path.getmtime(f)<4: ids.add(x['user_id'])
            continue
        g=row(c,'SELECT id FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],name))
        if g: ids.update(x['user_id'] for x in c.execute('SELECT user_id FROM groupmembers WHERE group_id=?',(g['id'],))); continue
        u=row(c,'SELECT id FROM users WHERE username=?',(name,))
        if u: ids.add(u['id'])
    ids.discard(author)
    valid={x['user_id'] for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(cid,))}
    ids &= valid
    return list(ids)
def emit(c,cid,typ,payload):
    eid=add_event(c,cid,typ,payload); return eid
app=FastAPI(); app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])
@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(req:Request):
    b=await req.json(); un=b.get('username',''); pw=b.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise HTTPException(400)
    c=db()
    try:c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un)); c.commit()
    except sqlite3.IntegrityError: raise HTTPException(409)
    uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit()
    return {'user':uobj(uid),'token':t}
@app.post('/api/auth/login')
async def login(req:Request):
    b=await req.json(); r=row(db(),'SELECT * FROM users WHERE username=?',(b.get('username'),))
    if not r or r['password']!=hashlib.sha256(b.get('password','').encode()).hexdigest(): raise HTTPException(401)
    t=secrets.token_urlsafe(24); c=db(); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); return {'user':uobj(r['id']),'token':t}
@app.get('/api/auth/me')
async def me(req:Request): return {'user':uobj(auth(req))}
@app.get('/api/users/{uid}')
def getuser(uid:int,req:Request):
    auth(req)
    u=user(uid)
    if not u: raise HTTPException(404)
    return {'user':u}
@app.patch('/api/users/me')
async def patchme(req:Request):
    uid=auth(req); b=await req.json(); allowed=['display_name','timezone','avatar_url','status_text','status_emoji']; c=db()
    limits={'display_name':80,'status_text':100,'status_emoji':32,'avatar_url':1024,'timezone':64}
    for k in allowed:
        if k in b:
            if len(str(b[k]))>limits.get(k,250): raise HTTPException(400)
            if k=='timezone':
                try: ZoneInfo(str(b[k]))
                except Exception: raise HTTPException(400)
            c.execute(f'UPDATE users SET {k}=? WHERE id=?',(b[k],uid))
    obj=uobj(uid); c.execute('INSERT INTO global_events(type,payload) VALUES(?,?)',('user.updated',json.dumps(obj))); c.commit(); return {'user':obj}
@app.post('/api/workspaces',status_code=201)
async def workspace(req:Request):
    uid=auth(req); b=await req.json(); slug=b.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): raise HTTPException(400)
    c=db()
    try:c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b.get('name',''),uid)); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO members VALUES(?,?,?)',(wid,uid,'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,uid)); c.commit()
    except sqlite3.IntegrityError: raise HTTPException(409)
    return {'workspace':dict(row(c,'SELECT * FROM workspaces WHERE id=?',(wid,))),'general_channel':chan(cid)}
@app.get('/api/workspaces')
def workspaces(req:Request):
    uid=auth(req); c=db(); return {'workspaces':[dict(x) for x in c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(uid,))]}
@app.get('/api/workspaces/{slug}')
def workspace_get(slug:str,req:Request):
    uid=auth(req); c=db(); w=row(c,'SELECT * FROM workspaces WHERE slug=?',(slug,))
    if not w: raise HTTPException(404)
    cs=[dict(x) for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],))]
    return {'workspace':dict(w),'channels':cs,'read_state':[]}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def create_channel(slug:str,req:Request):
    uid=auth(req); b=await req.json(); c=db(); w=row(c,'SELECT * FROM workspaces WHERE slug=?',(slug,))
    if not w: raise HTTPException(404)
    m=row(c,'SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid))
    if not m or (b.get('is_private') and m['role'] not in ('owner','admin')): raise HTTPException(403)
    if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')): raise HTTPException(400)
    if row(c,'SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],b['name'])): raise HTTPException(409)
    c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,uid)); c.commit(); return {'channel':chan(cid)}
@app.patch('/api/channels/{cid}')
async def patch_channel(cid:int,req:Request):
    uid=auth(req); b=await req.json(); c=db(); ch=row(c,'SELECT ch.*,w.owner_id FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(cid,))
    if not ch: raise HTTPException(404)
    if not member(cid,uid) and uid!=ch['owner_id']: raise HTTPException(403)
    if 'topic' in b and len(str(b['topic']))>250: raise HTTPException(400)
    c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',ch['topic']),cid)); c.commit(); return {'channel':chan(cid)}
@app.post('/api/channels/{cid}/join')
def join(cid:int,req:Request):
    uid=auth(req); c=db(); ch=chan(cid)
    if not ch: raise HTTPException(404)
    if not member(cid,uid): c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,uid)); c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],uid,'member')); c.commit()
    return {'channel':chan(cid)}
@app.get('/api/channels/{cid}/members')
def members(cid:int,req:Request):
    auth(req); c=db(); return {'members':[uobj(x['user_id']) for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(cid,))]}

def group_obj(c,g):
    d=dict(g); d['member_user_ids']=[x['user_id'] for x in c.execute('SELECT user_id FROM groupmembers WHERE group_id=?',(g['id'],))]; return d
@app.post('/api/workspaces/{slug}/groups',status_code=201)
async def create_group(slug:str,req:Request):
    uid=auth(req); b=await req.json(); c=db(); w=row(c,'SELECT * FROM workspaces WHERE slug=?',(slug,))
    if not w: raise HTTPException(404)
    if not row(c,'SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)): raise HTTPException(403)
    h=b.get('handle'); name=b.get('name')
    if not h or not name: raise HTTPException(400)
    if row(c,'SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h)): raise HTTPException(409)
    c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,uid)); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    for x in b.get('user_ids') or []:
        if row(c,'SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],x)): c.execute('INSERT OR IGNORE INTO groupmembers VALUES(?,?)',(gid,x))
    c.commit(); return {'group':group_obj(c,row(c,'SELECT * FROM groups_ WHERE id=?',(gid,)))}
@app.get('/api/workspaces/{slug}/groups')
def list_groups(slug:str,req:Request):
    uid=auth(req); c=db(); w=row(c,'SELECT * FROM workspaces WHERE slug=?',(slug,))
    if not w: raise HTTPException(404)
    if not row(c,'SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)): raise HTTPException(403)
    return {'groups':[group_obj(c,x) for x in c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],))]}
@app.patch('/api/workspaces/{slug}/groups/{handle}')
async def patch_group(slug:str,handle:str,req:Request):
    uid=auth(req); b=await req.json(); c=db(); g=row(c,'SELECT g.*,w.owner_id,w.id wid FROM groups_ g JOIN workspaces w ON w.id=g.workspace_id WHERE w.slug=? AND g.handle=?',(slug,handle))
    if not g: raise HTTPException(404)
    if uid not in (g['creator_id'],g['owner_id']): raise HTTPException(403)
    c.execute('UPDATE groups_ SET name=? WHERE id=?',(b.get('name',g['name']),g['id']))
    if 'user_ids' in b:
        c.execute('DELETE FROM groupmembers WHERE group_id=?',(g['id'],))
        for x in b['user_ids']: c.execute('INSERT OR IGNORE INTO groupmembers SELECT ?,user_id FROM members WHERE workspace_id=? AND user_id=?',(g['id'],g['wid'],x))
    c.commit(); return {'group':group_obj(c,row(c,'SELECT * FROM groups_ WHERE id=?',(g['id'],)))}
@app.delete('/api/workspaces/{slug}/groups/{handle}')
def delete_group(slug:str,handle:str,req:Request):
    uid=auth(req); c=db(); g=row(c,'SELECT g.*,w.owner_id FROM groups_ g JOIN workspaces w ON w.id=g.workspace_id WHERE w.slug=? AND g.handle=?',(slug,handle))
    if not g: return {'deleted':True}
    if uid not in (g['creator_id'],g['owner_id']): raise HTTPException(403)
    c.execute('DELETE FROM groupmembers WHERE group_id=?',(g['id'],)); c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],)); c.commit(); return {'deleted':True}
@app.get('/api/channels/{cid}/read')
def read_state(cid:int,req:Request):
    uid=auth(req); c=db()
    if not member(cid,uid): raise HTTPException(403)
    r=row(c,'SELECT count FROM mention_counts WHERE user_id=? AND channel_id=?',(uid,cid))
    head=row(c,'SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,))['n']
    return {'read_state':{'channel_id':cid,'last_read_event_id':head,'unread_count':0,'mention_count':r['count'] if r else 0}}
@app.post('/api/channels/{cid}/read')
async def mark_read(cid:int,req:Request):
    uid=auth(req); return await read_state(cid,req)
@app.get('/api/channels/{cid}/messages')
def list_messages(cid:int,req:Request):
    uid=auth(req); c=db()
    if not member(cid,uid): raise HTTPException(403)
    return {'messages':[msgobj(c,x) for x in c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200',(cid,))],'next_cursor':None}
@app.post('/api/channels/{cid}/messages',status_code=201)
async def post_message(cid:int,req:Request):
    uid=auth(req); b=await req.json(); c=db(); ch=chan(cid)
    if not ch: raise HTTPException(404)
    if not member(cid,uid): raise HTTPException(403)
    if ch['is_archived'] and b.get('body','').strip() != '/unarchive': raise HTTPException(423)
    body=b.get('body','')
    if not body: raise HTTPException(400)
    if body.startswith('/') and not body.startswith('//'):
        p=body.split(' ',1); cmd=p[0]; arg=p[1] if len(p)>1 else ''
        if cmd=='/me':
            if not arg: raise HTTPException(400)
        elif cmd=='/shrug': body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
        elif cmd=='/topic':
            if not arg: raise HTTPException(400)
            c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,cid)); c.commit(); return JSONResponse({'message':None,'channel':chan(cid)},status_code=201)
        elif cmd in ('/archive','/unarchive'):
            c.execute('UPDATE channels SET is_archived=? WHERE id=?',(cmd=='/archive',cid)); c.commit(); return JSONResponse({'message':None,'channel':chan(cid)},status_code=201)
        elif cmd=='/invite':
            m=re.search(r'@([A-Za-z0-9_]+)',arg); u=row(c,'SELECT * FROM users WHERE username=?',(m.group(1),)) if m else None
            if not u: raise HTTPException(400)
            c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id'])); c.commit(); return JSONResponse({'message':None,'invited_user':uobj(u['id'])},status_code=201)
        else: raise HTTPException(400)
    parent=b.get('parent_id')
    file_ids=b.get('file_ids') or []
    for fid in file_ids:
        f=row(c,'SELECT * FROM files WHERE id=?',(fid,))
        if not f or f['uploader_id']!=uid or row(c,'SELECT 1 FROM attachments WHERE file_id=?',(fid,)): raise HTTPException(400)
    c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,uid,body,parent,now()))
    mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    for fid in file_ids: c.execute('INSERT INTO attachments VALUES(?,?)',(mid,fid))
    mids=resolve_mentions(c,body,cid,uid); obj=msgobj(c,row(c,'SELECT * FROM messages WHERE id=?',(mid,))); obj['mentions']=mids
    for target in mids: c.execute('INSERT INTO mention_counts(user_id,channel_id,count) VALUES(?,?,1) ON CONFLICT(user_id,channel_id) DO UPDATE SET count=count+1',(target,cid))
    typ='message.reply' if parent else 'message.created'; emit(c,cid,typ,obj); c.commit()
    return {'message':obj}
@app.patch('/api/messages/{mid}')
async def edit(mid:int,req:Request):
    uid=auth(req); b=await req.json(); c=db(); m=row(c,'SELECT m.*,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(mid,))
    if not m: raise HTTPException(404)
    if m['deleted']: raise HTTPException(409)
    if m['author_id']!=uid: raise HTTPException(403)
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b.get('body',''),now(),mid)); obj=msgobj(c,row(c,'SELECT * FROM messages WHERE id=?',(mid,))); emit(c,m['channel_id'],'message.updated',obj); c.commit(); return {'message':obj}
@app.delete('/api/messages/{mid}')
def delete(mid:int,req:Request):
    uid=auth(req); c=db(); m=row(c,'SELECT m.*,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(mid,))
    if not m: raise HTTPException(404)
    if m['deleted']: return {'deleted':True}
    if uid not in (m['author_id'],m['owner_id']): raise HTTPException(403)
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); emit(c,m['channel_id'],'message.deleted',{'message_id':mid}); c.commit(); return {'deleted':True}
@app.post('/api/messages/{mid}/reactions')
async def reaction(mid:int,req:Request):
    uid=auth(req); b=await req.json(); emoji=b.get('emoji')
    if not emoji: raise HTTPException(400)
    c=db(); m=row(c,'SELECT * FROM messages WHERE id=? AND deleted=0',(mid,))
    if not m: raise HTTPException(404)
    c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,uid,emoji))
    if c.execute('SELECT changes()').fetchone()[0]: emit(c,m['channel_id'],'reaction.added',{'message_id':mid,'emoji':emoji,'user_id':uid})
    c.commit(); return {'reactions':msgobj(c,m)['reactions']}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
def unreact(mid:int,emoji:str,req:Request):
    uid=auth(req); c=db(); m=row(c,'SELECT * FROM messages WHERE id=?',(mid,))
    if not m: raise HTTPException(404)
    c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,uid,emoji))
    if c.execute('SELECT changes()').fetchone()[0]: emit(c,m['channel_id'],'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':uid})
    c.commit(); return {'reactions':msgobj(c,m)['reactions']}
@app.get('/api/messages/{mid}/replies')
def replies(mid:int,req:Request):
    uid=auth(req); c=db(); m=row(c,'SELECT channel_id FROM messages WHERE id=?',(mid,))
    if not m or not member(m['channel_id'],uid): raise HTTPException(404)
    return {'replies':[msgobj(c,x) for x in c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(mid,))],'next_cursor':None}
@app.post('/api/dms')
async def dm(req:Request):
    uid=auth(req); b=await req.json(); other=b.get('user_id')
    if not other: raise HTTPException(400)
    c=db()
    if not user(other): raise HTTPException(404)
    a,bx=sorted((uid,int(other))); r=row(c,'SELECT channel_id FROM dms WHERE user1=? AND user2=?',(a,bx))
    if r:return {'channel_id':r['channel_id']}
    c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(0,f'dm-{a}-{bx}')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO dms VALUES(?,?,?)',(a,bx,cid)); c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,a)); c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,bx)); c.commit(); return {'channel_id':cid}
@app.post('/api/files',status_code=201)
async def upload(req:Request,file:UploadFile=File(None)):
    uid=auth(req)
    if not file: raise HTTPException(400)
    data=await file.read()
    if len(data)>10*1024*1024: raise HTTPException(413)
    c=db(); c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(uid,file.filename,file.content_type or 'application/octet-stream',len(data),now(),data)); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit()
    f=row(c,'SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,)); return {'file':dict(f)}
@app.get('/api/files/{fid}')
def getfile(fid:int,req:Request):
    auth(req); c=db(); f=row(c,'SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,))
    if not f: raise HTTPException(404)
    return {'file':dict(f)}
@app.get('/api/files/{fid}/download')
def download(fid:int,req:Request):
    auth(req); c=db(); f=row(c,'SELECT * FROM files WHERE id=?',(fid,))
    if not f: raise HTTPException(404)
    return Response(f['data'],media_type=f['content_type'],headers={'Content-Disposition':f'attachment; filename="{f["filename"]}"'})
@app.websocket('/api/ws')
async def ws(sock:WebSocket):
    await sock.accept()
    token=sock.query_params.get('token',''); c0=db()
    r=row(c0,'SELECT user_id FROM tokens WHERE token=?',(token,)); c0.close()
    if not r: await sock.close(); return
    uid=r['user_id']; subs={}; ge=0; pf=f'/tmp/huddle-ws-{uid}'
    open(pf,'w').write(str(time.time()))
    receiver=asyncio.create_task(sock.receive_json())
    try:
        while True:
            try: os.utime(pf,None)
            except OSError: pass
            done,_=await asyncio.wait({receiver},timeout=0.2)
            if receiver in done:
                try: x=receiver.result()
                except (WebSocketDisconnect,RuntimeError,ConnectionError): break
                receiver=asyncio.create_task(sock.receive_json())
                cid=x.get('channel_id')
                c=db()
                try:
                    if x.get('type')=='subscribe':
                        head=row(c,'SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,))['n']
                        subs[cid]=int(x['since_event_id']) if 'since_event_id' in x else head
                        await sock.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
                    elif x.get('type')=='resume':
                        since=int(x.get('since_event_id') or 0); subs[cid]=since
                        for e in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)):
                            payload=json.loads(e['payload'])
                            q={'type':e['type'],'event_id':e['event_id'],'channel_id':cid}
                            if e['type'] in ('message.created','message.reply','message.updated'): q['message']=payload
                            else: q.update(payload)
                            await sock.send_json(q); subs[cid]=e['event_id']
                        head=row(c,'SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,))['n']
                        await sock.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
                finally: c.close()
            c=db()
            try:
                for e in c.execute('SELECT * FROM global_events WHERE id>? ORDER BY id',(ge,)):
                    q=json.loads(e['payload']); await sock.send_json({'type':e['type'],'user':q}); ge=e['id']
                for cid,last in list(subs.items()):
                    for e in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)):
                        payload=json.loads(e['payload'])
                        q={'type':e['type'],'event_id':e['event_id'],'channel_id':cid}
                        if e['type'] in ('message.created','message.reply','message.updated'): q['message']=payload
                        else: q.update(payload)
                        await sock.send_json(q); subs[cid]=e['event_id']
            finally: c.close()
    except (WebSocketDisconnect,RuntimeError,ConnectionError):
        pass
    finally:
        receiver.cancel()
        try: os.unlink(pf)
        except OSError: pass

@app.get('/')
def root(): return FileResponse('/app/static/index.html')
@app.get('/{path:path}')
def static_path(path:str):
    p='/app/static/'+path
    if os.path.isfile(p): return FileResponse(p)
    return FileResponse('/app/static/index.html')
