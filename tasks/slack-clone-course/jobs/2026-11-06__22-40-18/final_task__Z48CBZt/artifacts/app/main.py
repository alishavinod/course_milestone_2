import os, re, sqlite3, hashlib, secrets, datetime, json, asyncio, time, socket, threading
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
DB='/app/data/app.db'; NODE=int(os.getenv('NODE_ID','0'))
app=FastAPI()
OPEN_USERS=set()
OPEN_SOCKETS=set()
def redis_subscriber():
    while True:
        sock = None
        try:
            sock = socket.create_connection(('127.0.0.1', 6379), 2)
            cmd = b'*2\r\n$9\r\nSUBSCRIBE\r\n$13\r\nhuddle-events\r\n'
            sock.sendall(cmd)
            while sock.recv(4096):
                pass
        except Exception:
            time.sleep(1)
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

threading.Thread(target=redis_subscriber,daemon=True).start()

def get_db():
    c=sqlite3.connect(DB,timeout=30, isolation_level=None); c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=30000')
    try: c.execute('ALTER TABLE channel_events ADD COLUMN seq INTEGER')
    except sqlite3.OperationalError: pass
    try: c.execute('ALTER TABLE messages ADD COLUMN deleted INTEGER DEFAULT 0')
    except sqlite3.OperationalError: pass
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT);
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,invited_user_id INTEGER,expires_at REAL,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,member_user_ids TEXT,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS presence(connection_id TEXT PRIMARY KEY,user_id INTEGER,seen REAL);
    CREATE TABLE IF NOT EXISTS profile_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,payload TEXT,created_at REAL);
    CREATE TABLE IF NOT EXISTS channel_events(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,kind TEXT,payload TEXT,created_at REAL,seq INTEGER);
    CREATE TABLE IF NOT EXISTS read_state(channel_id INTEGER,user_id INTEGER,mention_count INTEGER DEFAULT 0,unread_count INTEGER DEFAULT 0,last_read_event_id INTEGER DEFAULT 0,UNIQUE(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB); CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
    '''); return c
def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()
def uj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def require_user(req):
    h=req.headers.get('authorization','')
    if not h.lower().startswith('bearer '): raise HTTPException(401)
    c=get_db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone()
    if not r: raise HTTPException(401)
    return r
def token(uid):
    t=secrets.token_urlsafe(24)
    import time
    for attempt in range(20):
        c=get_db()
        try:
            c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); return t
        except sqlite3.OperationalError as e:
            c.rollback()
            if 'locked' not in str(e).lower() or attempt == 19: raise
            time.sleep(0.05 * (attempt + 1))
        finally:
            c.close()
    return t
def wsj(r): return {k:r[k] for k in ('id','slug','name','owner_id','join_mode')}
def chj(r): return {k:(bool(r[k]) if k in ('is_private','is_dm','is_archived') else r[k]) for k in ('id','workspace_id','name','is_private','is_dm','topic','is_archived')}
def role(w,u):
    c=get_db(); r=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w,u)).fetchone(); return r['role'] if r else None
def emit_channel_event(c, channel_id, kind, payload):
    c.execute('BEGIN IMMEDIATE')
    try:
        seq=c.execute('SELECT COALESCE(max(seq),0)+1 q FROM channel_events WHERE channel_id=?',(channel_id,)).fetchone()['q']
        c.execute('INSERT INTO channel_events(channel_id,kind,payload,created_at,seq) VALUES(?,?,?,?,?)',(channel_id,kind,json.dumps(payload),time.time(),seq))
        c.commit()
        return seq
    except Exception:
        c.rollback()
        raise

def reaction_json(c,mid):
    rows=c.execute('SELECT emoji,count(*) count FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall()
    return [{'emoji':r['emoji'],'count':r['count'],'user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(mid,r['emoji'])).fetchall()]} for r in rows]

def resolve_mentions(c,cid,body,author_id):
    x=c.execute('SELECT workspace_id FROM channels WHERE id=?',(cid,)).fetchone()
    if not x: return []
    members={r['id']:r['username'] for r in c.execute('SELECT u.id,u.username FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)).fetchall()}
    found=[]
    for uid,name in members.items():
        if uid!=author_id and re.search(r'(?<![A-Za-z0-9_])@'+re.escape(name)+r'(?![A-Za-z0-9_])',body):
            found.append(uid)
    for g in c.execute('SELECT * FROM groups WHERE workspace_id=?',(x['workspace_id'],)).fetchall():
        if re.search(r'(?<![A-Za-z0-9_])@'+re.escape(g['handle'])+r'(?![A-Za-z0-9_])',body):
            found.extend(uid for uid in json.loads(g['member_user_ids'] or '[]') if uid in members and uid!=author_id)
    if re.search(r'(?<![A-Za-z0-9_])@channel(?![A-Za-z0-9_])',body):
        found.extend(uid for uid in members if uid!=author_id)
    if re.search(r'(?<![A-Za-z0-9_])@here(?![A-Za-z0-9_])',body):
        found.extend(uid for uid in members if uid in {r['user_id'] for r in c.execute('SELECT user_id FROM presence WHERE seen>?',(datetime.datetime.now().timestamp()-3,)).fetchall()} and uid!=author_id)
    return list(dict.fromkeys(found))

class Body(BaseModel): pass
@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(req:Request):
    d=await req.json()
    if not re.fullmatch(r'[A-Za-z0-9_]+',str(d.get('username',''))) or len(d.get('password',''))<8: raise HTTPException(400)
    c=get_db()
    try:
        cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(d['username'],hash_password(d['password']),d.get('display_name') or d['username'])); c.commit()
    except sqlite3.IntegrityError: raise HTTPException(409)
    u=c.execute('SELECT * FROM users WHERE id=?',(cur.lastrowid,)).fetchone(); return {'user':uj(u),'token':token(u['id'])}
@app.post('/api/auth/login')
async def login(req:Request):
    d=await req.json(); c=get_db(); u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hash_password(d.get('password','')))).fetchone()
    if not u: raise HTTPException(401)
    return {'user':uj(u),'token':token(u['id'])}
@app.get('/api/auth/me')
def me(req:Request): return {'user':uj(require_user(req))}
@app.post('/api/workspaces',status_code=201)
async def create_ws(req:Request):
    u=require_user(req); d=await req.json()
    if not re.fullmatch(r'[a-z0-9-]{2,32}',str(d.get('slug',''))): raise HTTPException(400)
    c=get_db()
    try: cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d.get('name',''),u['id']))
    except sqlite3.IntegrityError: raise HTTPException(409)
    wid=cur.lastrowid; c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); chcur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=chcur.lastrowid; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit()
    return {'workspace':wsj(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':chj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}
@app.get('/api/workspaces')
def list_ws(req:Request):
    u=require_user(req); c=get_db(); rs=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); return {'workspaces':[wsj(x) for x in rs]}
@app.get('/api/workspaces/{slug}')
def get_ws(slug,req:Request):
    u=require_user(req); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w or not role(w['id'],u['id']): raise HTTPException(404)
    cs=c.execute('SELECT * FROM channels WHERE workspace_id=?'+('' if req.query_params.get('include_archived') in ('1','true') else ' AND is_archived=0'),(w['id'],)).fetchall(); return {'workspace':wsj(w),'channels':[chj(x) for x in cs],'read_state':{}}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def create_ch(slug,req:Request):
    u=require_user(req); d=await req.json(); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404)
    r=role(w['id'],u['id'])
    if r not in ('owner','admin','member') or (d.get('is_private') and r not in ('owner','admin')): raise HTTPException(403)
    if not re.fullmatch(r'[a-z0-9-]{1,32}',str(d.get('name',''))) or len(d.get('topic',''))>250: raise HTTPException(400)
    try: cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')))
    except sqlite3.IntegrityError: raise HTTPException(409)
    c.execute('INSERT INTO channel_members VALUES(?,?)',(cur.lastrowid,u['id'])); c.commit(); return {'channel':chj(c.execute('SELECT * FROM channels WHERE id=?',(cur.lastrowid,)).fetchone())}
@app.post('/api/channels/{cid}/join')
def join(cid,req:Request):
    u=require_user(req); c=get_db(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not x: raise HTTPException(404)
    if x['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    wr=c.execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone(); rr=role(x['workspace_id'],u['id'])
    if rr=='guest': raise HTTPException(403)
    if not role(x['workspace_id'],u['id']):
        w=c.execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone()
        if w['join_mode']=='invite_only': raise HTTPException(403)
        c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit(); return {'channel':chj(x)}
@app.post('/api/channels/{cid}/messages',status_code=201)
async def post_msg(cid,req:Request):
    u=require_user(req); d=await req.json(); body=d.get('body','')
    c=get_db(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not x: raise HTTPException(404)
    if body.startswith('/') and not body.startswith('//'):
        parts=body.split(' ',1); cmd=parts[0]; arg=parts[1] if len(parts)>1 else ''
        if cmd=='/me':
            if not arg: raise HTTPException(400)
        elif cmd=='/shrug':
            body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
        elif cmd=='/topic':
            if not arg or role(x['workspace_id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
            c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,cid)); return {'message':None,'channel':chj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}
        elif cmd=='/invite':
            if not arg.startswith('@'): raise HTTPException(400)
            name=arg[1:].split()[0]; v=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
            if not v: raise HTTPException(404)
            c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,v['id'])); return {'message':None,'invited_user':uj(v)}
        elif cmd in ('/archive','/unarchive'):
            if role(x['workspace_id'],u['id'])!='owner': raise HTTPException(403)
            archived=1 if cmd=='/archive' else 0; c.execute('UPDATE channels SET is_archived=? WHERE id=?',(archived,cid)); return {'message':None,'channel':chj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}
        else: raise HTTPException(400)
    if not body: raise HTTPException(400)
    if x['is_archived']: raise HTTPException(423)
    for fid in d.get('file_ids',[]):
        f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
        if not f or f['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone(): raise HTTPException(400)
    if d.get('parent_id') is not None and not c.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=?',(d['parent_id'],cid)).fetchone(): raise HTTPException(404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    now=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z'); cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,d.get('parent_id'),now)); mid=cur.lastrowid
    for fid in d.get('file_ids',[]): c.execute('INSERT INTO message_files VALUES(?,?)',(mid,int(fid)))
    c.commit()
    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    mentions=resolve_mentions(c,cid,body,u['id']); z=dict(r); z.update(parent_id=r['parent_id'],reply_count=0,author=uj(c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()),edited_at=None,files=[dict(f) for f in c.execute('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],)).fetchall()],reactions=reaction_json(c,r['id']),mentions=mentions)
    for mentioned in mentions: c.execute('INSERT OR IGNORE INTO read_state(channel_id,user_id,mention_count) VALUES(?,?,0)',(cid,mentioned)); c.execute('UPDATE read_state SET mention_count=mention_count+1 WHERE channel_id=? AND user_id=?',(cid,mentioned))
    ev={'message':z}; emit_channel_event(c,cid,'message.created',{'message':z})
    return {'message':z}
@app.get('/')
def root(): return HTMLResponse(open('/app/static/index.html').read())

@app.websocket('/api/ws')
async def websocket(ws: WebSocket):
    raw=ws.query_params.get('token','')
    c=get_db()
    u=c.execute('SELECT user_id FROM tokens WHERE token=?',(raw,)).fetchone()
    if not u:
        await ws.close(code=1008)
        return
    await ws.accept()
    OPEN_SOCKETS.add(ws)
    conn_id=secrets.token_urlsafe(12); OPEN_USERS.add(u['user_id']); c.execute('INSERT OR REPLACE INTO presence VALUES(?,?,?)',(conn_id,u['user_id'],datetime.datetime.now().timestamp())); c.commit()
    subscribed=set(); start_event=int(c.execute('SELECT COALESCE(max(id),0) x FROM profile_events').fetchone()['x']); start_channel=int(c.execute('SELECT COALESCE(max(id),0) x FROM channel_events').fetchone()['x'])
    async def profile_watcher():
        nonlocal start_event, start_channel
        while True:
            await asyncio.sleep(0.2)
            db=get_db()
            for ev in db.execute('SELECT * FROM channel_events WHERE id>? ORDER BY id',(start_channel,)).fetchall():
                start_channel=ev['id']
                payload=json.loads(ev['payload'])
                if ev['channel_id'] in subscribed:
                    try: await ws.send_json(dict({'type':ev['kind'],'event_id':ev['seq'],'channel_id':ev['channel_id']},**payload))
                    except Exception: return
                if ev['kind']=='message.created':
                    m=payload.get('message',{}); ch=db.execute('SELECT ch.name,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(ev['channel_id'],)).fetchone()
                    if ch:
                        line='#'+ch['slug']+'/'+ch['name']; au=m.get('author',{}).get('username','user')
                        with IRC_LOCK:
                            for sock,nk,cid in list(IRC_CLIENTS):
                                if cid==ev['channel_id']:
                                    irc_send(sock,':'+au+'!'+au+'@localhost PRIVMSG '+line+' :'+m.get('body',''))
            for ev in db.execute('SELECT * FROM profile_events WHERE id>? ORDER BY id',(start_event,)).fetchall():
                start_event=ev['id']
                try: await ws.send_json({'type':'user.updated','user':json.loads(ev['payload'])})
                except Exception: return
    watcher=asyncio.create_task(profile_watcher())
    try:
        while True:
            msg=await ws.receive_text()
            try: data=json.loads(msg)
            except Exception: data={}
            if data.get('type') in ('subscribe','resume'):
                cid=data.get('channel_id'); subscribed.add(cid)
                c=get_db()
                headrow=c.execute('SELECT COALESCE(max(seq),0) head FROM channel_events WHERE channel_id=?',(cid,)).fetchone()
                head=headrow['head']
                if data.get('type')=='resume':
                    try: since=int(data.get('since_event_id',0))
                    except Exception: since=0
                    events=c.execute('SELECT * FROM channel_events WHERE channel_id=? AND seq>? ORDER BY seq',(cid,since)).fetchall()
                    for ev in events:
                        frame={'type':ev['kind'],'event_id':ev['seq'],'channel_id':cid}
                        frame.update(json.loads(ev['payload']))
                        await ws.send_json(frame)
                    await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
                else:
                    await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
    except WebSocketDisconnect:
        OPEN_USERS.discard(u['user_id']); OPEN_SOCKETS.discard(ws); watcher.cancel(); c=get_db(); c.execute('DELETE FROM presence WHERE connection_id=?',(conn_id,)); c.commit()
        return


@app.get('/api/workspaces/{slug}/members')
def workspace_members(slug,req:Request):
    u=require_user(req); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404)
    if not role(w['id'],u['id']): raise HTTPException(403)
    return {'members':[{'user_id':r['user_id'],'role':r['role']} for r in c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall()]}

@app.patch('/api/workspaces/{slug}')
async def patch_workspace(slug,req:Request):
    u=require_user(req); d=await req.json(); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404)
    r=role(w['id'],u['id'])
    if r not in ('owner','admin'): raise HTTPException(403)
    if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'): raise HTTPException(400)
    vals=[]; args=[]
    for k in ('name','join_mode'):
        if k in d: vals.append(k+'=?'); args.append(d[k])
    if vals: args.append(w['id']); c.execute('UPDATE workspaces SET '+','.join(vals)+' WHERE id=?',args)
    return {'workspace':wsj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())}

@app.patch('/api/workspaces/{slug}/members/{uid}')
async def patch_member(slug,uid,req:Request):
    u=require_user(req); d=await req.json(); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404)
    actor=role(w['id'],u['id']); target=role(w['id'],int(uid)); new=d.get('role')
    if new=='owner': raise HTTPException(400)
    if new not in ('admin','member','guest'): raise HTTPException(400)
    if not target: raise HTTPException(404)
    if new=='owner' or int(uid)==w['owner_id'] or actor not in ('owner','admin') or (actor=='admin' and target=='admin'): raise HTTPException(403)
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(new,w['id'],uid))
    return {'member':{'user_id':int(uid),'role':new}}

@app.get('/api/channels/{cid}/members')
def channel_members(cid,req:Request):
    u=require_user(req); c=get_db(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not x: raise HTTPException(404)
    if not role(x['workspace_id'],u['id']): raise HTTPException(403)
    rows=c.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall()
    return {'members':[uj(r) for r in rows]}

@app.patch('/api/channels/{cid}')
async def patch_channel(cid,req:Request):
    u=require_user(req); d=await req.json(); c=get_db(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not x: raise HTTPException(404)
    if len(d.get('topic',''))>250: raise HTTPException(400)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone() and role(x['workspace_id'],u['id'])!='owner': raise HTTPException(403)
    c.execute('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',''),cid)); return {'channel':chj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}

@app.get('/api/channels/{cid}/messages')
def list_messages(cid,req:Request):
    u=require_user(req); c=get_db(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not x: raise HTTPException(404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    try: lim=int(req.query_params.get('limit','50'))
    except: raise HTTPException(400)
    if lim<1 or lim>200: raise HTTPException(400)
    cur=req.query_params.get('cursor')
    if cur:
        try: anchor=int(cur)
        except: raise HTTPException(400)
        rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND COALESCE(deleted,0)=0 AND id<? ORDER BY id DESC LIMIT ?',(cid,anchor,lim)).fetchall()
    else:
        rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND COALESCE(deleted,0)=0 ORDER BY id DESC LIMIT ?',(cid,lim)).fetchall()
    out=[]
    for r in rows:
        z=dict(r); z.update(author=uj(c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()),reply_count=c.execute('SELECT count(*) n FROM messages WHERE parent_id=?',(r['id'],)).fetchone()['n'],files=[],reactions=reaction_json(c,r['id']),mentions=resolve_mentions(c,cid,r['body'],r['author_id']))
        out.append(z)
    more=len(rows)==lim and bool(rows)
    return {'messages':out,'next_cursor':str(rows[-1]['id']) if more else None}

@app.post('/api/channels/{cid}/leave')
def leave_channel(cid,req:Request):
    u=require_user(req); c=get_db(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not x: raise HTTPException(404)
    c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])); return {'channel':chj(x)}

@app.post('/api/workspaces/{slug}/invitations',status_code=201)
async def invite(slug,req:Request):
    u=require_user(req); d=await req.json(); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404)
    if role(w['id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
    target=d.get('invited_user_id')
    if not target and d.get('invited_username'):
        z=c.execute('SELECT id FROM users WHERE username=?',(d['invited_username'],)).fetchone()
        target=z['id'] if z else None
    code=secrets.token_urlsafe(12); exp=datetime.datetime.now().timestamp()+int(d.get('expires_in_seconds',604800))
    c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?)',(code,w['id'],u['id'],target,exp,int(d.get('max_uses',1)),0,0))
    return {'invitation':{'code':code,'workspace_id':w['id'],'max_uses':int(d.get('max_uses',1))}}
@app.get('/api/workspaces/{slug}/invitations')
def list_invite(slug,req:Request):
    u=require_user(req); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404)
    if role(w['id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
    now=datetime.datetime.now().timestamp(); rs=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND expires_at>?',(w['id'],now)).fetchall()
    return {'invitations':[dict(r) for r in rs]}
@app.post('/api/invitations/{code}/accept')
def accept_invite(code,req:Request):
    u=require_user(req); c=get_db(); r=c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone()
    if not r or r['revoked'] or r['uses']>=r['max_uses'] or r['expires_at']<datetime.datetime.now().timestamp(): raise HTTPException(404)
    if r['invited_user_id'] and r['uses']>0: raise HTTPException(404)
    if r['invited_user_id'] and r['invited_user_id']!=u['id']: raise HTTPException(403)
    c.execute('UPDATE invitations SET uses=uses+1 WHERE code=? AND uses<max_uses',(code,)); c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(r['workspace_id'],u['id'],'member')); c.commit()
    return {'accepted':True}
@app.delete('/api/workspaces/{slug}/invitations/{code}')
def revoke_invite(slug,code,req:Request):
    u=require_user(req); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404)
    if role(w['id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
    r=c.execute('SELECT 1 FROM invitations WHERE code=? AND workspace_id=?',(code,w['id'])).fetchone()
    if not r: raise HTTPException(404)
    c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(code,)); return {'deleted':True}

@app.post('/api/workspaces/{slug}/transfer_ownership')
async def transfer(slug,req:Request):
    u=require_user(req); d=await req.json(); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404)
    if u['id']!=w['owner_id']: raise HTTPException(403)
    target=int(d.get('new_owner_id',0)); r=role(w['id'],target)
    if r!='admin': raise HTTPException(400)
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target))
    c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id']))
    return {'workspace':wsj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())}

@app.post('/api/channels/{cid}/archive')
def archive(cid,req:Request):
    u=require_user(req); c=get_db(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not x: raise HTTPException(404)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone()
    if w['owner_id']!=u['id']: raise HTTPException(403)
    c.execute('UPDATE channels SET is_archived=1 WHERE id=?',(cid,)); return {'channel':chj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}

@app.post('/api/channels/{cid}/unarchive')
def unarchive(cid,req:Request):
    u=require_user(req); c=get_db(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not x: raise HTTPException(404)
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone()
    if w['owner_id']!=u['id']: raise HTTPException(403)
    c.execute('UPDATE channels SET is_archived=0 WHERE id=?',(cid,)); return {'channel':chj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}

@app.get('/api/messages/{parent_id}/replies')
def replies(parent_id,req:Request):
    u=require_user(req); c=get_db(); parent=c.execute('SELECT * FROM messages WHERE id=?',(parent_id,)).fetchone()
    if not parent: raise HTTPException(404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(parent['channel_id'],u['id'])).fetchone(): raise HTTPException(403)
    try: lim=int(req.query_params.get('limit','50'))
    except: raise HTTPException(400)
    if lim<1 or lim>200: raise HTTPException(400)
    rows=c.execute('SELECT * FROM messages WHERE parent_id=? ORDER BY id LIMIT ?',(parent_id,lim)).fetchall()
    out=[]
    for r in rows:
        z=dict(r); z.update(author=uj(c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()),parent_id=r['parent_id'],reply_count=0,edited_at=r['edited_at'],files=[],reactions=[],mentions=[]); out.append(z)
    return {'replies':out,'next_cursor':None}

@app.get('/api/search')
def search(req:Request):
    u=require_user(req); q=req.query_params.get('q',''); c=get_db()
    rows=c.execute("SELECT m.*,ch.workspace_id FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN channel_members cm ON cm.channel_id=ch.id AND cm.user_id=? WHERE m.body LIKE ? ORDER BY m.id DESC",(u['id'],'%'+q+'%')).fetchall()
    out=[]
    for r in rows:
        z=dict(r); z.update(author=uj(c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()),channel_id=r['channel_id'],parent_id=r['parent_id'],reply_count=0,edited_at=r['edited_at'],files=[],reactions=[],mentions=[]); out.append(z)
    return {'results':out,'next_cursor':None}

@app.post('/api/messages/{mid}/pin')
def pin(mid,req:Request):
    u=require_user(req); c=get_db(); m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m: raise HTTPException(404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(m['channel_id'],u['id'])).fetchone(): raise HTTPException(403)
    if c.execute('SELECT is_archived FROM channels WHERE id=?',(m['channel_id'],)).fetchone()['is_archived']: raise HTTPException(423)
    now=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
    c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,u['id'],now)); r=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone()
    return {'pin':dict(r)}

@app.delete('/api/messages/{mid}/pin')
def unpin(mid,req:Request):
    u=require_user(req); c=get_db(); m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m: raise HTTPException(404)
    c.execute('DELETE FROM pins WHERE message_id=?',(mid,)); return {'unpinned':True}

@app.get('/api/channels/{cid}/pins')
def list_pins(cid,req:Request):
    u=require_user(req); c=get_db(); x=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not x: raise HTTPException(404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    rows=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(cid,)).fetchall()
    return {'pins':[{'message':dict(r),'pinned_by':r['pinned_by'],'pinned_at':r['pinned_at']} for r in rows],'next_cursor':None}

@app.post('/api/messages/{mid}/reactions')
async def add_reaction(mid,req:Request):
    u=require_user(req); d=await req.json(); emoji=d.get('emoji')
    if not emoji: raise HTTPException(400)
    c=get_db(); m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m: raise HTTPException(404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(m['channel_id'],u['id'])).fetchone(): raise HTTPException(403)
    c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],emoji))
    rows=c.execute('SELECT emoji,count(*) count FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall()
    result={'reactions':[{'emoji':r['emoji'],'count':r['count'],'user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(mid,r['emoji'])).fetchall()]} for r in rows]}; emit_channel_event(c,m['channel_id'],'reaction.added',{'message_id':mid,'reactions':result['reactions']}); return result

@app.delete('/api/messages/{mid}/reactions/{emoji}')
def remove_reaction(mid,emoji,req:Request):
    u=require_user(req); c=get_db(); m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m: raise HTTPException(404)
    c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji))
    rows=c.execute('SELECT emoji,count(*) count FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)).fetchall()
    result={'reactions':[{'emoji':r['emoji'],'count':r['count'],'user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(mid,r['emoji'])).fetchall()]} for r in rows]}; emit_channel_event(c,m['channel_id'],'reaction.removed',{'message_id':mid,'reactions':result['reactions']}); return result

@app.post('/api/dms')
async def create_dm(req:Request):
    u=require_user(req); d=await req.json(); other=d.get('user_id')
    if other is None: raise HTTPException(400)
    c=get_db(); v=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
    if not v: raise HTTPException(404)
    name='dm-'+('-'.join(map(str,sorted((u['id'],int(other))))))
    row=c.execute('SELECT id FROM channels WHERE name=? AND is_dm=1',(name,)).fetchone()
    if row: return {'channel_id':row['id']}
    c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(0,name)); cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']
    c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,int(other))); c.commit()
    return {'channel_id':cid}

@app.post('/api/files',status_code=201)
async def upload_file(req:Request,file:UploadFile=File(None)):
    u=require_user(req)
    if file is None: raise HTTPException(400)
    data=await file.read()
    if len(data)>10485760: raise HTTPException(413)
    now=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z'); c=get_db()
    cur=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],file.filename or 'file',file.content_type or 'application/octet-stream',len(data),now,data))
    return {'file':{'id':cur.lastrowid,'uploader_id':u['id'],'filename':file.filename or 'file','content_type':file.content_type or 'application/octet-stream','size':len(data),'created_at':now}}

@app.get('/api/files/{fid}')
def file_detail(fid,req:Request):
    require_user(req); c=get_db(); f=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,)).fetchone()
    if not f: raise HTTPException(404)
    return {'file':dict(f)}

@app.get('/api/files/{fid}/download')
def file_download(fid,req:Request):
    require_user(req); c=get_db(); f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
    if not f: raise HTTPException(404)
    return Response(bytes(f['data']),media_type=f['content_type'],headers={'Content-Disposition':f'attachment; filename="{f["filename"]}"'})

def group_obj(r):
    return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['member_user_ids'] or '[]')}

@app.post('/api/workspaces/{slug}/groups',status_code=201)
async def create_group(slug,req:Request):
    u=require_user(req); d=await req.json(); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404)
    if not role(w['id'],u['id']): raise HTTPException(403)
    if not d.get('handle'): raise HTTPException(400)
    ids=d.get('user_ids',[])
    if any(not role(w['id'],int(x)) for x in ids): raise HTTPException(400)
    try: cur=c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id,member_user_ids) VALUES(?,?,?,?,?)',(w['id'],d['handle'],d.get('name',''),u['id'],json.dumps([int(x) for x in ids])))
    except sqlite3.IntegrityError: raise HTTPException(409)
    return {'group':group_obj(c.execute('SELECT * FROM groups WHERE id=?',(cur.lastrowid,)).fetchone())}

@app.get('/api/workspaces/{slug}/groups')
def list_groups(slug,req:Request):
    u=require_user(req); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404)
    if not role(w['id'],u['id']): raise HTTPException(403)
    return {'groups':[group_obj(x) for x in c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall()]}

@app.patch('/api/workspaces/{slug}/groups/{handle}')
async def update_group(slug,handle,req:Request):
    u=require_user(req); d=await req.json(); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'] if w else 0,handle)).fetchone()
    if not w: raise HTTPException(404)
    if not g: raise HTTPException(404)
    if g['creator_id']!=u['id'] and w['owner_id']!=u['id']: raise HTTPException(403)
    c.execute('UPDATE groups SET name=?,member_user_ids=? WHERE id=?',(d.get('name',g['name']),json.dumps([int(x) for x in d.get('user_ids',json.loads(g['member_user_ids']))]),g['id']))
    return {'group':group_obj(c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone())}

@app.delete('/api/workspaces/{slug}/groups/{handle}')
def delete_group(slug,handle,req:Request):
    u=require_user(req); c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404)
    g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
    if g and g['creator_id']!=u['id'] and w['owner_id']!=u['id']: raise HTTPException(403)
    if g: c.execute('DELETE FROM groups WHERE id=?',(g['id'],))
    return {'deleted':True}

@app.get('/api/channels/{cid}/read')
def read_state(cid,req:Request):
    u=require_user(req); c=get_db(); x=c.execute('SELECT 1 FROM channels WHERE id=?',(cid,)).fetchone()
    if not x: raise HTTPException(404)
    if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    return {'read_state':{'channel_id':int(cid),'last_read_event_id':0,'unread_count':0,'mention_count':(c.execute('SELECT mention_count FROM read_state WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone() or {'mention_count':0})['mention_count']}}

@app.get('/api/users/{uid}')
def get_user(uid,req:Request):
    require_user(req); c=get_db(); u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    if not u: raise HTTPException(404)
    return {'user':uj(u)}

@app.patch('/api/users/me')
async def patch_me(req:Request):
    u=require_user(req); d=await req.json(); c=get_db()
    from zoneinfo import ZoneInfo
    limits={'display_name':80,'status_text':100,'status_emoji':32,'avatar_url':1024}
    for k,v in d.items():
        if k in limits and len(str(v))>limits[k]: raise HTTPException(400)
    if 'timezone' in d:
        try: ZoneInfo(d['timezone'])
        except Exception: raise HTTPException(400)
    allowed=['display_name','timezone','avatar_url','status_text','status_emoji']; vals=[]; args=[]
    for k in allowed:
        if k in d: vals.append(k+'=?'); args.append(d[k])
    if vals: args.append(u['id']); c.execute('UPDATE users SET '+','.join(vals)+' WHERE id=?',args)
    updated=uj(c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone())
    c.execute('INSERT INTO profile_events(user_id,payload,created_at) VALUES(?,?,?)',(u['id'],json.dumps(updated),time.time())); c.commit()
    frame={'type':'user.updated','user':updated}
    for sock in list(OPEN_SOCKETS):
        try: await sock.send_json(frame)
        except Exception: OPEN_SOCKETS.discard(sock)
    return {'user':updated}

@app.patch('/api/messages/{mid}')
async def edit_message(mid,req:Request):
    u=require_user(req); d=await req.json(); body=d.get('body')
    if not body: raise HTTPException(400)
    c=get_db(); m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m: raise HTTPException(404)
    if m['deleted']: raise HTTPException(409)
    if m['author_id']!=u['id']: raise HTTPException(403)
    now=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,now,mid))
    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    seq=c.execute('SELECT COALESCE(max(seq),0)+1 q FROM channel_events WHERE channel_id=?',(r['channel_id'],)).fetchone()['q']; c.execute('INSERT INTO channel_events(channel_id,kind,payload,created_at,seq) VALUES(?,?,?,?,?)',(r['channel_id'],'message.updated',json.dumps({'message':dict(r)}),time.time(),seq))
    z=dict(r); z.update(author=uj(c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()),reply_count=0,parent_id=r['parent_id'],files=[],reactions=reaction_json(c,mid),mentions=resolve_mentions(c,r['channel_id'],body,r['author_id']))
    return {'message':z}

@app.delete('/api/messages/{mid}')
def delete_message(mid,req:Request):
    u=require_user(req); c=get_db(); m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m: raise HTTPException(404)
    w=c.execute('SELECT * FROM workspaces w JOIN channels ch ON ch.workspace_id=w.id WHERE ch.id=?',(m['channel_id'],)).fetchone()
    if m['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']): raise HTTPException(403)
    if not m['deleted']:
        c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); emit_channel_event(c,m['channel_id'],'message.deleted',{'message_id':mid})
    return {'deleted':True}

IRC_NICKS=set()
IRC_CLIENTS=[]
IRC_LOCK=threading.Lock()
def irc_send(w,line):
    try: w.sendall((line+'\r\n').encode())
    except Exception: pass
def irc_client(conn,addr):
    token=None; nick=None; registered=False; buf=b''; joined=set(); user_id=None

    try:
        while True:
            data=conn.recv(4096)
            if not data: break
            buf+=data
            while b'\n' in buf:
                raw,buf=buf.split(b'\n',1); line=raw.decode(errors='replace').strip('\r')
                if not line: continue
                parts=line.split(' ',2); cmd=parts[0].upper(); arg=parts[1] if len(parts)>1 else ''
                if cmd=='PASS':
                    token=parts[1] if len(parts)>1 else ''
                    c=get_db(); row=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone(); user_id=row['user_id'] if row else None
                    if not c.execute('SELECT 1 FROM tokens WHERE token=?',(token,)).fetchone():
                        irc_send(conn,':huddle 464 * :Password incorrect')
                        token=None
                elif cmd=='NICK':
                    proposed=parts[1] if len(parts)>1 else ''
                    with IRC_LOCK:
                        taken=proposed in IRC_NICKS
                        if not taken:
                            if nick: IRC_NICKS.discard(nick)
                            nick=proposed; IRC_NICKS.add(nick)
                    if taken:
                        irc_send(conn,':huddle 433 * '+proposed+' :Nickname is already in use')
                    elif token and not registered:
                        registered=True
                        for n,text in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1 1'),('005','CHANTYPES=# PREFIX=(ov)@+')]:
                            irc_send(conn,':huddle '+n+' '+(nick or '*')+' :'+text)
                        irc_send(conn,':huddle 422 '+(nick or '*')+' :MOTD File is missing')
                elif cmd=='USER':
                    if token and nick and not registered:
                        registered=True
                        for n,text in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1 1'),('005','CHANTYPES=# PREFIX=(ov)@+')]:
                            irc_send(conn,':huddle '+n+' '+nick+' :'+text)
                        irc_send(conn,':huddle 422 '+nick+' :MOTD File is missing')
                elif cmd=='JOIN':
                    cname=parts[1] if len(parts)>1 else ''
                    if not cname.startswith('#') or '/' not in cname:
                        irc_send(conn,':huddle 403 '+(nick or '*')+' '+cname+' :No such channel'); continue
                    slug,chname=cname[1:].split('/',1); c=get_db(); ch=c.execute('SELECT ch.* FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,chname)).fetchone()
                    if not ch:
                        irc_send(conn,':huddle 403 '+(nick or '*')+' '+cname+' :No such channel'); continue
                    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],user_id,'member')); c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],user_id)); c.commit(); joined.add(ch['id'])
                    with IRC_LOCK: IRC_CLIENTS.append((conn,nick,ch['id']))
                    irc_send(conn,':'+(nick or '*')+'!'+str(user_id)+'@localhost JOIN '+cname)
                    names=[r['username'] for r in c.execute('SELECT u.username FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],)).fetchall()]
                    irc_send(conn,':huddle 353 '+(nick or '*')+' = '+cname+' :'+(' '.join(names)))
                    irc_send(conn,':huddle 366 '+(nick or '*')+' '+cname+' :End of /NAMES list')
                elif cmd=='PRIVMSG':
                    cname=parts[1] if len(parts)>1 else ''; body=parts[2][1:] if len(parts)>2 and parts[2].startswith(':') else ''
                    if cname.startswith('#') and '/' in cname and user_id and body:
                        slug,chname=cname[1:].split('/',1); c=get_db(); ch=c.execute('SELECT ch.* FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,chname)).fetchone()
                        if ch:
                            now=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z'); cur=c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],user_id,body,now)); c.commit()
                            r=c.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone(); z=dict(r); z.update(author=uj(c.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()),parent_id=None,reply_count=0,edited_at=None,files=[],reactions=[],mentions=[])
                            emit_channel_event(c,ch['id'],'message.created',{'message':z})
                            with IRC_LOCK:
                                for sock,nk,cid in list(IRC_CLIENTS):
                                    if cid==ch['id'] and sock is not conn: irc_send(sock,':'+(nick or 'irc')+'!'+str(user_id)+'@localhost PRIVMSG '+cname+' :'+body)
                elif cmd=='PING':
                    irc_send(conn,':huddle PONG huddle :'+(parts[1] if len(parts)>1 else ''))
                elif cmd=='QUIT':
                    return
                elif cmd in ('PONG','NAMES','WHO','LIST','TOPIC','MODE'):
                    if cmd=='NAMES' and len(parts)>1: irc_send(conn,':huddle 366 '+(nick or '*')+' '+parts[1]+' :End of /NAMES list.')
                elif cmd not in ('PASS','USER'):
                    irc_send(conn,':huddle 421 '+(nick or '*')+' '+cmd+' :Unknown command')
    finally:
        with IRC_LOCK:
            if nick: IRC_NICKS.discard(nick)
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
def irc_event_bridge():
    last=0
    while True:
        try:
            c=get_db()
            rows=c.execute("SELECT * FROM channel_events WHERE id>? AND kind='message.created' ORDER BY id",(last,)).fetchall()
            for ev in rows:
                last=ev['id']
                payload=json.loads(ev['payload']); m=payload.get('message',{})
                ch=c.execute('SELECT ch.name,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(ev['channel_id'],)).fetchone()
                if ch:
                    target='#'+ch['slug']+'/'+ch['name']; author=(m.get('author') or {}).get('username','user')
                    with IRC_LOCK:
                        for sock,nk,cid in list(IRC_CLIENTS):
                            if cid==ev['channel_id']:
                                irc_send(sock,':'+author+'!'+author+'@localhost PRIVMSG '+target+' :'+m.get('body',''))
            time.sleep(0.1)
        except Exception:
            time.sleep(1)

def irc_server():
    threading.Thread(target=irc_event_bridge,daemon=True).start()
    srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    try:
        srv.bind(('0.0.0.0',6667)); srv.listen(100)
        while True:
            c,a=srv.accept(); threading.Thread(target=irc_client,args=(c,a),daemon=True).start()
    except Exception: pass
threading.Thread(target=irc_server,daemon=True).start()

if __name__ == '__main__':
    if os.getenv('GATEWAY_ONLY'):
        while True: time.sleep(60)
    else:
        import uvicorn
        uvicorn.run(app,host='127.0.0.1',port=int(os.getenv('PORT','8000')))
