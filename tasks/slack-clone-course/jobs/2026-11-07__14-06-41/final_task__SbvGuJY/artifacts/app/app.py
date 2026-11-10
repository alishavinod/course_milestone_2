import os, re, sqlite3, hashlib, secrets, json
from datetime import datetime, timezone
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import JSONResponse, HTMLResponse, Response
from pydantic import BaseModel

DB='/app/data/app.db'
app=FastAPI()
_db_ready=False
_db_lock=__import__('threading').Lock()
def get_db():
    global _db_ready
    os.makedirs(os.path.dirname(DB),exist_ok=True)
    c=sqlite3.connect(DB,timeout=30,check_same_thread=False,isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=30000')
    if not _db_ready:
        with _db_lock:
            if not _db_ready:
                c.execute('PRAGMA journal_mode=WAL')
                c.executescript("""
                CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
                CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
                CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
                CREATE TABLE IF NOT EXISTS workspace_members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
                CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
                CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id)); CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0); CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT); CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id)); CREATE TABLE IF NOT EXISTS read_states(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id)); CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle)); CREATE TABLE IF NOT EXISTS presence(connection_id TEXT PRIMARY KEY,user_id INTEGER,seen REAL); CREATE TABLE IF NOT EXISTS profile_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,payload TEXT); CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id)); CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji)); CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB); CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE); CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires_at REAL,max_uses INTEGER,uses INTEGER DEFAULT 0,invited_user_id INTEGER,invited_username TEXT,revoked INTEGER DEFAULT 0)
                """)
                c.commit()
                _db_ready=True
    return c
def hash_password(password): return hashlib.sha256(('huddle:'+password).encode()).hexdigest()
def user_to_json(u):
    return {k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def create_user(username,password,display_name=None):
    if not re.fullmatch(r'[A-Za-z0-9_]+',username or '') or len(password or '')<8: raise ValueError()
    c=get_db()
    try:
        cur=c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',
          (username,hash_password(password),display_name or username,'UTC','','',''))
        c.commit(); u=c.execute('SELECT * FROM users WHERE id=?',(cur.lastrowid,)).fetchone(); return u
    except sqlite3.IntegrityError: raise KeyError()
def issue_token(user_id):
    t=secrets.token_urlsafe(32); c=get_db(); c.execute('INSERT INTO tokens VALUES(?,?)',(t,user_id)); c.commit(); return t
def find_user(request):
    h=request.headers.get('authorization','')
    if not h.lower().startswith('bearer '): return None
    c=get_db(); return c.execute('SELECT u.* FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.token=?',(h.split(None,1)[1],)).fetchone()
def require_user(request):
    u=find_user(request)
    if not u: raise PermissionError()
    return u
class Auth(BaseModel):
    username:str
    password:str
    display_name:str|None=None

def get_workspace(slug):
    w=get_db().execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    return w
def get_role(workspace_id,user_id):
    r=get_db().execute('SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?',(workspace_id,user_id)).fetchone()
    return r['role'] if r else None
def add_member(workspace_id,user_id,role):
    c=get_db(); c.execute('INSERT OR IGNORE INTO workspace_members(workspace_id,user_id,role) VALUES(?,?,?)',(workspace_id,user_id,role)); c.commit()
def workspace_to_json(w):
    return {k:w[k] for k in ('id','slug','name','owner_id','join_mode')}
def channel_to_json(ch):
    return {'id':ch['id'],'workspace_id':ch['workspace_id'],'name':ch['name'],'is_private':bool(ch['is_private']),'is_dm':bool(ch['is_dm']),'topic':ch['topic'] or '','is_archived':bool(ch['is_archived'])}
def create_channel(workspace_id,name,is_private,creator_id):
    c=get_db(); cur=c.execute('INSERT INTO channels(workspace_id,name,is_private) VALUES(?,?,?)',(workspace_id,name,int(is_private))); c.execute('INSERT INTO channel_members VALUES(?,?)',(cur.lastrowid,creator_id)); c.commit(); return c.execute('SELECT * FROM channels WHERE id=?',(cur.lastrowid,)).fetchone()
def join_channel(channel,user):
    c=get_db(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(channel['workspace_id'],)).fetchone()
    if channel['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(channel['id'],user['id'])).fetchone(): raise PermissionError()
    if not can_join_channel(get_role(w['id'],user['id'])): raise PermissionError()
    if not get_role(w['id'],user['id']) and w['join_mode'] != 'open': raise PermissionError()
    if not can_join_channel(get_role(w['id'],user['id'])): raise PermissionError()
    if w['join_mode']=='open': add_member(w['id'],user['id'],'member')
    before=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(channel['id'],user['id'])).fetchone()
    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(channel['id'],user['id'])); c.commit()
    if not before: emit_event(channel['id'],'member.joined',{'user_id':user['id']})
@app.post('/api/workspaces')
async def create_workspace(request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    except:return JSONResponse({'error':'invalid input'},400)
    slug=d.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not d.get('name'): return JSONResponse({'error':'invalid input'},400)
    c=get_db()
    try:
        cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d['name'],u['id'])); wid=cur.lastrowid
        c.execute('INSERT INTO workspace_members VALUES(?,?,?)',(wid,u['id'],'owner')); c.commit()
        ch=create_channel(wid,'general',False,u['id'])
        w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()
        return JSONResponse({'workspace':workspace_to_json(w),'general_channel':channel_to_json(ch)},201)
    except sqlite3.IntegrityError:return JSONResponse({'error':'duplicate'},409)
@app.get('/api/workspaces')
def list_workspaces(request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    rows=get_db().execute('SELECT w.* FROM workspaces w JOIN workspace_members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
    return {'workspaces':[workspace_to_json(x) for x in rows]}
@app.get('/api/workspaces/{slug}')
def workspace_detail(slug:str,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    w=get_workspace(slug)
    if not w:return JSONResponse({'error':'not found'},404)
    c=get_db(); include=request.query_params.get('include_archived') in ('1','true')
    q='SELECT * FROM channels WHERE workspace_id=?' + ('' if include else ' AND is_archived=0')
    chans=c.execute(q,(w['id'],)).fetchall()
    chans=[x for x in chans if not x['is_private'] or c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(x['id'],u['id'])).fetchone() or get_role(w['id'],u['id']) in ('owner','admin')]
    states=[get_read_state(u['id'],x['id']) for x in chans if c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(x['id'],u['id'])).fetchone()]
    return {'workspace':workspace_to_json(w),'channels':[channel_to_json(x) for x in chans],'read_state':states}
@app.get('/api/workspaces/{slug}/members')
def workspace_members(slug:str,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    w=get_workspace(slug)
    if not w:return JSONResponse({'error':'not found'},404)
    if not get_role(w['id'],u['id']):return JSONResponse({'error':'forbidden'},403)
    rows=get_db().execute('SELECT user_id,role FROM workspace_members WHERE workspace_id=?',(w['id'],)).fetchall()
    return {'members':[dict(x) for x in rows]}
def can_create_channel(role,is_private):
    return role in ('owner','admin') if is_private else role in ('owner','admin','member')
def can_join_channel(role):
    return role != 'guest'
def set_role(workspace,actor_id,target_id,new_role):
    if new_role not in ('owner','admin','member','guest'): raise ValueError()
    c=get_db(); ar=get_role(workspace['id'],actor_id); tr=get_role(workspace['id'],target_id)
    if not tr: raise LookupError()
    if target_id==workspace['owner_id'] or actor_id==target_id or new_role=='owner': raise PermissionError()
    if ar not in ('owner','admin') or (ar=='admin' and tr in ('owner','admin')): raise PermissionError()
    c.execute('UPDATE workspace_members SET role=? WHERE workspace_id=? AND user_id=?',(new_role,workspace['id'],target_id)); c.commit()
    return c.execute('SELECT user_id,role FROM workspace_members WHERE workspace_id=? AND user_id=?',(workspace['id'],target_id)).fetchone()
@app.patch('/api/workspaces/{slug}/members/{uid}')
async def patch_member(slug:str,uid:int,request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    except:return JSONResponse({'error':'invalid input'},400)
    w=get_workspace(slug)
    if not w:return JSONResponse({'error':'not found'},404)
    role=d.get('role')
    if role == 'owner' or role not in ('owner','admin','member','guest'):return JSONResponse({'error':'invalid role'},400)
    try:r=set_role(w,u['id'],uid,role)
    except LookupError:return JSONResponse({'error':'not found'},404)
    except PermissionError:return JSONResponse({'error':'forbidden'},403)
    return {'member':dict(r)}
@app.post('/api/workspaces/{slug}/channels')
async def create_workspace_channel(slug:str,request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    except:return JSONResponse({'error':'invalid input'},400)
    w=get_workspace(slug)
    if not w:return JSONResponse({'error':'not found'},404)
    role=get_role(w['id'],u['id'])
    if not role or not can_create_channel(role,bool(d.get('is_private',False))):return JSONResponse({'error':'forbidden'},403)
    name=d.get('name','')
    if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or not d.get('name'):return JSONResponse({'error':'invalid input'},400)
    private=bool(d.get('is_private',False))
    topic=d.get('topic','')
    if not isinstance(topic,str) or len(topic)>250:return JSONResponse({'error':'invalid'},400)
    if private and role not in ('owner','admin'):return JSONResponse({'error':'forbidden'},403)
    try:
        ch=create_channel(w['id'],name,private,u['id'])
        c=get_db(); c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id'])); c.commit()
        return JSONResponse({'channel':channel_to_json(get_channel(ch['id']))},201)
    except sqlite3.IntegrityError:return JSONResponse({'error':'duplicate'},409)

@app.post('/api/channels/{cid}/join')
def join(cid:int,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    ch=get_db().execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return JSONResponse({'error':'not found'},404)
    try:join_channel(ch,u)
    except PermissionError:return JSONResponse({'error':'forbidden'},403)
    return {'channel':channel_to_json(ch)}


def update_workspace(workspace,fields):
    c=get_db(); name=fields.get('name',workspace['name']); mode=fields.get('join_mode',workspace['join_mode'])
    if mode not in ('open','invite_only'): raise ValueError()
    c.execute('UPDATE workspaces SET name=?,join_mode=? WHERE id=?',(name,mode,workspace['id'])); c.commit()
    return c.execute('SELECT * FROM workspaces WHERE id=?',(workspace['id'],)).fetchone()
def create_invitation(workspace,creator_id,expires_in_seconds=86400,max_uses=1,invited_user_id=None,invited_username=None):
    import time
    code=secrets.token_urlsafe(18); c=get_db()
    c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?,?)',(code,workspace['id'],creator_id,time.time()+int(expires_in_seconds or 86400),int(max_uses or 1),0,invited_user_id,invited_username,0)); c.commit()
    return c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone()
def invitation_json(x):
    return {k:x[k] for k in ('code','workspace_id','creator_id','expires_at','max_uses','uses','invited_user_id','invited_username')}
def accept_invitation(code,user):
    import time
    c=get_db(); x=c.execute('SELECT * FROM invitations WHERE code=? AND revoked=0 AND expires_at>? AND uses<max_uses',(code,time.time())).fetchone()
    if not x: raise LookupError()
    if x['invited_user_id'] and x['invited_user_id']!=user['id']: raise PermissionError()
    if x['invited_username'] and x['invited_username']!=user['username']: raise PermissionError()
    c.execute('UPDATE invitations SET uses=uses+1,revoked=CASE WHEN invited_user_id IS NOT NULL OR invited_username IS NOT NULL OR uses+1>=max_uses THEN 1 ELSE revoked END WHERE code=? AND uses<max_uses',(code,))
    if c.total_changes!=1: raise LookupError()
    c.commit()
    add_member(x['workspace_id'],user['id'],'member')
    return x
def list_invitations(workspace):
    import time
    return get_db().execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND expires_at>? AND uses<max_uses',(workspace['id'],time.time())).fetchall()
def revoke_invitation(workspace,code):
    c=get_db(); x=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND code=?',(workspace['id'],code)).fetchone()
    if not x: raise LookupError()
    c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(code,)); c.commit()
def transfer_ownership(workspace,actor_id,new_owner_id):
    if get_role(workspace['id'],actor_id)!='owner': raise PermissionError()
    if get_role(workspace['id'],new_owner_id)!='admin': raise ValueError()
    c=get_db(); c.execute('UPDATE workspace_members SET role="admin" WHERE workspace_id=? AND user_id=?',(workspace['id'],actor_id)); c.execute('UPDATE workspace_members SET role="owner" WHERE workspace_id=? AND user_id=?',(workspace['id'],new_owner_id)); c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(new_owner_id,workspace['id'])); c.commit()
    return c.execute('SELECT * FROM workspaces WHERE id=?',(workspace['id'],)).fetchone()
@app.patch('/api/workspaces/{slug}')
async def patch_workspace(slug:str,request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    w=get_workspace(slug)
    if not w:return JSONResponse({'error':'not found'},404)
    if get_role(w['id'],u['id']) not in ('owner','admin'):return JSONResponse({'error':'forbidden'},403)
    if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return JSONResponse({'error':'invalid'},400)
    try:return {'workspace':workspace_to_json(update_workspace(w,d))}
    except:return JSONResponse({'error':'invalid'},400)
@app.post('/api/workspaces/{slug}/invitations')
async def make_invite(slug:str,request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    w=get_workspace(slug)
    if not w:return JSONResponse({'error':'not found'},404)
    if get_role(w['id'],u['id']) not in ('owner','admin'):return JSONResponse({'error':'forbidden'},403)
    target=d.get('invited_user_id'); username=d.get('invited_username')
    if username:
        z=get_db().execute('SELECT id FROM users WHERE username=?',(username,)).fetchone(); target=z['id'] if z else None
    x=create_invitation(w,u['id'],d.get('expires_in_seconds',86400),d.get('max_uses',1),target,username)
    return JSONResponse({'invitation':invitation_json(x)},201)
@app.get('/api/workspaces/{slug}/invitations')
def get_invites(slug:str,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    w=get_workspace(slug)
    if not w:return JSONResponse({'error':'not found'},404)
    if get_role(w['id'],u['id']) not in ('owner','admin'):return JSONResponse({'error':'forbidden'},403)
    return {'invitations':[invitation_json(x) for x in list_invitations(w)]}
@app.delete('/api/workspaces/{slug}/invitations/{code}')
def del_invite(slug:str,code:str,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    w=get_workspace(slug)
    if not w:return JSONResponse({'error':'not found'},404)
    if get_role(w['id'],u['id']) not in ('owner','admin'):return JSONResponse({'error':'forbidden'},403)
    try:revoke_invitation(w,code)
    except LookupError:return JSONResponse({'error':'not found'},404)
    return {'deleted':True}
@app.post('/api/invitations/{code}/accept')
def accept_invite(code:str,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    try:x=accept_invitation(code,u)
    except PermissionError:return JSONResponse({'error':'forbidden'},403)
    except LookupError:return JSONResponse({'error':'not found'},404)
    return {'workspace_id':x['workspace_id']}
@app.post('/api/workspaces/{slug}/transfer_ownership')
async def transfer(slug:str,request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    w=get_workspace(slug)
    if not w:return JSONResponse({'error':'not found'},404)
    try:return {'workspace':workspace_to_json(transfer_ownership(w,u['id'],d.get('new_owner_id')))}
    except ValueError:return JSONResponse({'error':'invalid'},400)
    except PermissionError:return JSONResponse({'error':'forbidden'},403)


def get_channel(channel_id):
    return get_db().execute('SELECT * FROM channels WHERE id=?',(channel_id,)).fetchone()
def channel_access(ch,user):
    c=get_db()
    return c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],user['id'])).fetchone()
@app.patch('/api/channels/{cid}')
async def patch_channel(cid:int,request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    ch=get_channel(cid)
    if not ch:return JSONResponse({'error':'not found'},404)
    if ch['is_archived']:return JSONResponse({'error':'archived'},423)
    w=get_db().execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if not channel_access(ch,u) and w['owner_id']!=u['id']:return JSONResponse({'error':'forbidden'},403)
    topic=d.get('topic','')
    if len(topic)>250:return JSONResponse({'error':'invalid'},400)
    c=get_db(); c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,cid)); c.commit()
    emit_event(cid,'channel.updated',{'channel':channel_to_json(get_channel(cid))})
    return {'channel':channel_to_json(get_channel(cid))}
@app.post('/api/channels/{cid}/archive')
def archive(cid:int,request:Request):
    return archive_toggle(cid,request,True)
@app.post('/api/channels/{cid}/unarchive')
def unarchive(cid:int,request:Request):
    return archive_toggle(cid,request,False)
def archive_toggle(cid,request,archived):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    ch=get_channel(cid)
    if not ch:return JSONResponse({'error':'not found'},404)
    w=get_db().execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if w['owner_id']!=u['id']:return JSONResponse({'error':'forbidden'},403)
    c=get_db(); c.execute('UPDATE channels SET is_archived=? WHERE id=?',(int(archived),cid)); c.commit()
    emit_event(cid,'channel.archived' if archived else 'channel.unarchived',{'channel':channel_to_json(get_channel(cid))})
    return {'channel':channel_to_json(get_channel(cid))}
@app.post('/api/channels/{cid}/leave')
def leave(cid:int,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    ch=get_channel(cid)
    if not ch:return JSONResponse({'error':'not found'},404)
    c=get_db(); existed=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(); c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])); c.commit()
    if existed: emit_event(cid,'member.left',{'user_id':u['id']})
    return {'channel':channel_to_json(ch)}
@app.get('/api/channels/{cid}/members')
def members(cid:int,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    ch=get_channel(cid)
    if not ch:return JSONResponse({'error':'not found'},404)
    rows=get_db().execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall()
    return {'members':[user_to_json(x) for x in rows]}

def resolve_mentions(body,channel,author_id):
    c=get_db(); w=c.execute('SELECT workspace_id FROM channels WHERE id=?',(channel['id'],)).fetchone()
    ids=[]
    for name in re.findall(r'(?<![\w@])@([A-Za-z0-9_-]+)',body):
        if name=='here':
            rows=c.execute('SELECT DISTINCT cm.user_id FROM channel_members cm JOIN presence p ON p.user_id=cm.user_id WHERE cm.channel_id=? AND cm.user_id!=? AND p.seen>?',(channel['id'],author_id,__import__('time').time()-3)).fetchall() if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='presence'").fetchone() else []
            ids.extend(x['user_id'] for x in rows); continue
        g=c.execute('SELECT g.id FROM groups g WHERE g.workspace_id=? AND g.handle=?',(w['workspace_id'],name)).fetchone()
        if g:
            rows=c.execute('SELECT gm.user_id FROM group_members gm JOIN channel_members cm ON cm.user_id=gm.user_id WHERE gm.group_id=? AND cm.channel_id=? AND gm.user_id!=?',(g['id'],channel['id'],author_id)).fetchall()
            ids.extend(x['user_id'] for x in rows); continue
        if name=='channel':
            rows=c.execute('SELECT user_id FROM channel_members WHERE channel_id=? AND user_id!=?',(channel['id'],author_id)).fetchall()
            ids.extend(x['user_id'] for x in rows); continue
        x=c.execute('SELECT u.id FROM users u JOIN workspace_members m ON m.user_id=u.id WHERE m.workspace_id=? AND u.username=? AND u.id!=?',(w['workspace_id'],name,author_id)).fetchone()
        if x: ids.append(x['id'])
    return list(dict.fromkeys(ids))
def emit_event(channel_id,kind,payload):
    import json
    c=get_db()
    c.execute('BEGIN IMMEDIATE')
    row=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(channel_id,)).fetchone()
    eid=row['n']; c.execute('INSERT INTO events VALUES(?,?,?,?)',(channel_id,eid,kind,json.dumps(payload))); c.commit(); return eid
def channel_head(channel_id):
    return get_db().execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(channel_id,)).fetchone()['n']

@app.post('/api/channels/{cid}/messages')
async def post_message(cid:int,request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    ch=get_channel(cid)
    if not ch:return JSONResponse({'error':'not found'},404)
    if not channel_access(ch,u):return JSONResponse({'error':'forbidden'},403)
    body=d.get('body','')
    if isinstance(body,str) and body.startswith('/') and not body.startswith('//'):
        parts=body.split(' ',1); cmd=parts[0]; arg=parts[1] if len(parts)>1 else ''
        w=get_db().execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
        if cmd=='/me':
            if not arg.strip(): return JSONResponse({'error':'invalid command'},400)
        elif cmd=='/shrug':
            body=(arg.strip()+' ' if arg.strip() else '')+'¯\\_(ツ)_/¯'
        elif cmd=='/topic':
            if len(arg)>250:return JSONResponse({'error':'invalid'},400)
            c=get_db(); c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch['id'])); c.commit()
            emit_event(ch['id'],'channel.updated',{'channel':channel_to_json(get_channel(ch['id']))})
            return JSONResponse({'message':None,'channel':channel_to_json(get_channel(ch['id']))},201)
        elif cmd=='/invite':
            if not arg.startswith('@'):return JSONResponse({'error':'invalid command'},400)
            z=get_db().execute('SELECT * FROM users WHERE username=?',(arg[1:].rstrip('.,'),)).fetchone()
            if not z:return JSONResponse({'error':'invalid command'},400)
            get_db().execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],z['id'])); get_db().commit()
            return JSONResponse({'message':None,'invited_user':user_to_json(z)},201)
        elif cmd in ('/archive','/unarchive'):
            if w['owner_id']!=u['id']:return JSONResponse({'error':'forbidden'},403)
            archived=cmd=='/archive'; c=get_db(); c.execute('UPDATE channels SET is_archived=? WHERE id=?',(int(archived),ch['id'])); c.commit()
            emit_event(ch['id'],'channel.archived' if archived else 'channel.unarchived',{'channel':channel_to_json(get_channel(ch['id']))})
            return JSONResponse({'message':None,'channel':channel_to_json(get_channel(ch['id']))},201)
        else:return JSONResponse({'error':'unknown command'},400)
    if ch['is_archived']:return JSONResponse({'error':'archived'},423)
    if not isinstance(body,str) or not body.strip():return JSONResponse({'error':'empty'},400)
    if d.get('parent_id') is not None and not get_db().execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(d['parent_id'],cid)).fetchone(): return JSONResponse({'error':'parent not found'},404)
    now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'); c=get_db()
    try:file_ids=check_attachable(d.get('file_ids',[]),u)
    except ValueError:return JSONResponse({'error':'invalid files'},400)
    mentions=resolve_mentions(body,ch,u['id'])
    cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,d.get('parent_id'),now)); c.commit()
    m=c.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone()
    for fid in file_ids: c.execute('INSERT INTO message_files(message_id,file_id) VALUES(?,?)',(m['id'],fid))
    c.commit()
    obj=message_to_json(m); obj['mentions']=mentions
    emit_event(cid,'message.reply' if m['parent_id'] else 'message.created',{'message':obj})
    return JSONResponse({'message':obj},201)


import base64
def encode_cursor(message_id): return base64.urlsafe_b64encode(str(message_id).encode()).decode().rstrip('=')
def decode_cursor(cursor):
    try:
        v=base64.urlsafe_b64decode(cursor+'='*((4-len(cursor)%4)%4)).decode()
        if not v.isdigit(): raise ValueError()
        return int(v)
    except: raise ValueError()
def read_limit(request):
    try:n=int(request.query_params.get('limit','50'))
    except: raise ValueError()
    if n<1 or n>200: raise ValueError()
    return n
def file_to_json(f):
    return {k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}
def file_list(mid):
    c=get_db(); return [file_to_json(x) for x in c.execute('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(mid,)).fetchall()]
def check_attachable(file_ids,user):
    c=get_db()
    try: ids=[int(x) for x in file_ids]
    except: raise ValueError()
    for fid in ids:
        f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
        used=c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone()
        if not f or f['uploader_id']!=user['id'] or used: raise ValueError()
    return ids
async def save_upload(upload,user):
    data=await upload.read()
    if len(data)>10485760:return None
    now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'); c=get_db()
    cur=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(user['id'],upload.filename or 'file',upload.content_type or 'application/octet-stream',len(data),now,data)); c.commit()
    return c.execute('SELECT * FROM files WHERE id=?',(cur.lastrowid,)).fetchone()
@app.post('/api/files')
async def upload_file(request:Request,file:UploadFile|None=File(None)):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    if file is None:return JSONResponse({'error':'invalid'},400)
    f=await save_upload(file,u)
    if f is None:return JSONResponse({'error':'too large'},413)
    return JSONResponse({'file':file_to_json(f)},201)
@app.get('/api/files/{fid}')
def file_detail(fid:int,request:Request):
    try:require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    f=get_db().execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
    return {'file':file_to_json(f)} if f else JSONResponse({'error':'not found'},404)
@app.get('/api/files/{fid}/download')
def file_download(fid:int,request:Request):
    try:require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    f=get_db().execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
    if not f:return JSONResponse({'error':'not found'},404)
    return Response(f['data'],media_type=f['content_type'],headers={'Content-Disposition':f'attachment; filename="{f["filename"]}"'})
def reaction_list(mid):
    rows=get_db().execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY emoji,user_id',(mid,)).fetchall()
    out={}
    for x in rows: out.setdefault(x['emoji'],[]).append(x['user_id'])
    return [{'emoji':e,'count':len(ids),'user_ids':ids} for e,ids in out.items()]
def add_reaction(message,user,emoji):
    c=get_db(); c.execute('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(message['id'],user['id'],emoji)); c.commit(); return reaction_list(message['id'])
def remove_reaction(message,user,emoji):
    c=get_db(); c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(message['id'],user['id'],emoji)); c.commit(); return reaction_list(message['id'])
def get_or_create_dm(user_a,user_b):
    c=get_db(); a,b=sorted((int(user_a),int(user_b)))
    x=c.execute('SELECT id FROM channels WHERE is_dm=1 AND name=?',(f'dm-{a}-{b}',)).fetchone()
    if x:return x['id']
    cur=c.execute('INSERT INTO channels(workspace_id,name,is_dm,is_private) VALUES(?,?,1,1)',(0,f'dm-{a}-{b}')); cid=cur.lastrowid
    c.execute('INSERT INTO channel_members VALUES(?,?),(?,?)',(cid,a,cid,b)); c.commit(); return cid
@app.post('/api/messages/{mid}/reactions')
async def react(mid:int,request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    except:return JSONResponse({'error':'invalid'},400)
    m=get_message(mid)
    if not m:return JSONResponse({'error':'not found'},404)
    emoji=d.get('emoji')
    if not emoji:return JSONResponse({'error':'invalid'},400)
    r=add_reaction(m,u,emoji); emit_event(m['channel_id'],'reaction.added',{'message':message_to_json(m),'reactions':r})
    return {'reactions':r}
@app.delete('/api/messages/{mid}/reactions/{emoji}')
def unreact(mid:int,emoji:str,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    m=get_message(mid)
    if not m:return JSONResponse({'error':'not found'},404)
    r=remove_reaction(m,u,emoji); emit_event(m['channel_id'],'reaction.removed',{'message':message_to_json(m),'reactions':r})
    return {'reactions':r}
@app.post('/api/dms')
async def open_dm(request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    except:return JSONResponse({'error':'invalid'},400)
    if 'user_id' not in d:return JSONResponse({'error':'invalid'},400)
    x=get_db().execute('SELECT id FROM users WHERE id=?',(d['user_id'],)).fetchone()
    if not x:return JSONResponse({'error':'not found'},404)
    if int(d['user_id'])==u['id']:return JSONResponse({'error':'invalid'},400)
    return {'channel_id':get_or_create_dm(u['id'],d['user_id'])}
def message_to_json(m):
    c=get_db(); u=c.execute('SELECT * FROM users WHERE id=?',(m['author_id'],)).fetchone()
    rc=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(m['id'],)).fetchone()['n']
    return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':user_to_json(u),'body':m['body'],'parent_id':m['parent_id'],'reply_count':rc,'created_at':m['created_at'],'edited_at':m['edited_at'], 'files':file_list(m['id']),'reactions':reaction_list(m['id']),'mentions':[]}
def visible_channel(ch,user):
    if not ch:return False
    c=get_db()
    if c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],user['id'])).fetchone():return True
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    return bool(w and w['owner_id']==user['id'])
@app.get('/api/channels/{cid}/messages')
def list_channel_messages(cid:int,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    ch=get_channel(cid)
    if not ch:return JSONResponse({'error':'not found'},404)
    if not visible_channel(ch,u):return JSONResponse({'error':'forbidden'},403)
    try:n=read_limit(request); cur=request.query_params.get('cursor'); after=decode_cursor(cur) if cur else None
    except:return JSONResponse({'error':'invalid paging'},400)
    q='SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0'
    args=[cid]
    if after is not None:q+=' AND id<?'; args.append(after)
    q+=' ORDER BY id DESC LIMIT ?'; args.append(n+1)
    rows=get_db().execute(q,args).fetchall(); more=len(rows)>n; rows=rows[:n]
    return {'messages':[message_to_json(x) for x in rows],'next_cursor':encode_cursor(rows[-1]['id']) if more and rows else None}
@app.get('/api/messages/{parent_id}/replies')
def replies(parent_id:int,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    parent=get_db().execute('SELECT * FROM messages WHERE id=?',(parent_id,)).fetchone()
    if not parent:return JSONResponse({'error':'not found'},404)
    ch=get_channel(parent['channel_id'])
    if not visible_channel(ch,u):return JSONResponse({'error':'forbidden'},403)
    try:n=read_limit(request); cur=request.query_params.get('cursor'); after=decode_cursor(cur) if cur else 0
    except:return JSONResponse({'error':'invalid paging'},400)
    rows=get_db().execute('SELECT * FROM messages WHERE parent_id=? AND id>? AND deleted=0 ORDER BY id ASC LIMIT ?', (parent_id,after,n+1)).fetchall()
    more=len(rows)>n; rows=rows[:n]
    return {'replies':[message_to_json(x) for x in rows],'next_cursor':encode_cursor(rows[-1]['id']) if more and rows else None}
@app.get('/api/search')
def search(request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    try:n=read_limit(request); cur=request.query_params.get('cursor'); after=decode_cursor(cur) if cur else None
    except:return JSONResponse({'error':'invalid paging'},400)
    text=request.query_params.get('q',''); wsid=request.query_params.get('workspace_id')
    sql='SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND m.body LIKE ?'; args=['%'+text+'%']
    if wsid:
        sql+=' AND ch.workspace_id=?'; args.append(int(wsid))
    sql+=' ORDER BY m.id DESC'
    if after is not None: sql='SELECT * FROM ('+sql+') WHERE id<?'; args.append(after)
    sql+=' LIMIT ?'; args.append(n+1)
    rows=get_db().execute(sql,args).fetchall(); more=len(rows)>n; rows=rows[:n]
    rows=[x for x in rows if visible_channel(get_channel(x['channel_id']),u)]
    return {'results':[message_to_json(x) for x in rows],'next_cursor':encode_cursor(rows[-1]['id']) if more and rows else None}


def pin_message(message,user):
    now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'); c=get_db()
    c.execute('INSERT OR IGNORE INTO pins(message_id,pinned_by,pinned_at) VALUES(?,?,?)',(message['id'],user['id'],now)); c.commit()
    return c.execute('SELECT * FROM pins WHERE message_id=?',(message['id'],)).fetchone()
@app.post('/api/messages/{mid}/pin')
def pin(mid:int,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    m=get_db().execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m:return JSONResponse({'error':'not found'},404)
    ch=get_channel(m['channel_id'])
    if ch['is_archived']:return JSONResponse({'error':'archived'},423)
    if not visible_channel(ch,u):return JSONResponse({'error':'forbidden'},403)
    x=pin_message(m,u)
    return {'pin':{'message_id':x['message_id'],'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']}}
@app.delete('/api/messages/{mid}/pin')
def unpin(mid:int,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    m=get_db().execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m:return JSONResponse({'error':'not found'},404)
    get_db().execute('DELETE FROM pins WHERE message_id=?',(mid,))
    return {'unpinned':True}
@app.get('/api/channels/{cid}/pins')
def pins(cid:int,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    try:n=read_limit(request); cur=request.query_params.get('cursor'); after=decode_cursor(cur) if cur else None
    except:return JSONResponse({'error':'invalid paging'},400)
    ch=get_channel(cid)
    if not ch:return JSONResponse({'error':'not found'},404)
    if not visible_channel(ch,u):return JSONResponse({'error':'forbidden'},403)
    q='SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?'; args=[cid]
    if after is not None:q+=' AND p.message_id<?'; args.append(after)
    q+=' ORDER BY p.message_id DESC LIMIT ?'; args.append(n+1)
    rows=get_db().execute(q,args).fetchall(); more=len(rows)>n; rows=rows[:n]
    return {'pins':[{'message':message_to_json(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows],'next_cursor':encode_cursor(rows[-1]['message_id']) if more and rows else None}


def get_message(message_id):
    return get_db().execute('SELECT * FROM messages WHERE id=? AND deleted=0',(message_id,)).fetchone()
def message_event_id(message_id,channel_id):
    import json
    rows=get_db().execute("SELECT event_id,payload FROM events WHERE channel_id=? AND type IN ('message.created','message.reply')",(channel_id,)).fetchall()
    for x in rows:
        try:
            if json.loads(x['payload']).get('message',{}).get('id')==message_id:return x['event_id']
        except: pass
    return 0
def get_read_state(user_id,channel_id):
    c=get_db(); r=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(user_id,channel_id)).fetchone()
    last=r['last_read_event_id'] if r else 0
    ch=get_channel(channel_id); rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND author_id!=? AND deleted=0',(channel_id,user_id)).fetchall()
    unread=mentions=0
    for m in rows:
        eid=message_event_id(m['id'],channel_id)
        if eid>last:
            unread+=1
            if user_id in resolve_mentions(m['body'],ch,m['author_id']): mentions+=1
    return {'channel_id':channel_id,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}
def mark_read(user_id,channel_id,event_id):
    c=get_db(); old=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(user_id,channel_id)).fetchone()
    value=max(old['last_read_event_id'] if old else 0,int(event_id))
    c.execute('INSERT INTO read_states(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id',(user_id,channel_id,value)); c.commit()
    return get_read_state(user_id,channel_id)
@app.get('/api/channels/{cid}/read')
def get_read(cid:int,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    ch=get_channel(cid)
    if not ch:return JSONResponse({'error':'not found'},404)
    if not visible_channel(ch,u):return JSONResponse({'error':'forbidden'},403)
    return {'read_state':get_read_state(u['id'],cid)}
@app.post('/api/channels/{cid}/read')
async def post_read(cid:int,request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    ch=get_channel(cid)
    if not ch:return JSONResponse({'error':'not found'},404)
    if not visible_channel(ch,u):return JSONResponse({'error':'forbidden'},403)
    try:r=mark_read(u['id'],cid,d.get('last_read_event_id',0))
    except:return JSONResponse({'error':'invalid'},400)
    return {'read_state':r}


def group_to_json(g):
    ids=[x['user_id'] for x in get_db().execute('SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id',(g['id'],)).fetchall()]
    return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}
def group_workspace(slug):
    return get_workspace(slug)
@app.post('/api/workspaces/{slug}/groups')
async def create_group(slug:str,request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    w=group_workspace(slug)
    if not w:return JSONResponse({'error':'not found'},404)
    if not get_role(w['id'],u['id']):return JSONResponse({'error':'forbidden'},403)
    if not d.get('handle') or not d.get('name'):return JSONResponse({'error':'invalid'},400)
    c=get_db()
    try:
        cur=c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],d['handle'],d['name'],u['id']))
        for uid in d.get('user_ids',[]): c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(cur.lastrowid,int(uid)))
        c.commit(); return JSONResponse({'group':group_to_json(c.execute('SELECT * FROM groups WHERE id=?',(cur.lastrowid,)).fetchone())},201)
    except sqlite3.IntegrityError:return JSONResponse({'error':'duplicate'},409)
@app.get('/api/workspaces/{slug}/groups')
def list_groups(slug:str,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    w=group_workspace(slug)
    if not w:return JSONResponse({'error':'not found'},404)
    if not get_role(w['id'],u['id']):return JSONResponse({'error':'forbidden'},403)
    return {'groups':[group_to_json(x) for x in get_db().execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall()]}
@app.patch('/api/workspaces/{slug}/groups/{handle}')
async def patch_group(slug:str,handle:str,request:Request):
    try:u=require_user(request); d=await request.json()
    except:return JSONResponse({'error':'unauthorized'},401)
    w=group_workspace(slug); c=get_db(); g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone() if w else None
    if not g:return JSONResponse({'error':'not found'},404)
    if u['id'] not in (g['creator_id'],w['owner_id']):return JSONResponse({'error':'forbidden'},403)
    c.execute('UPDATE groups SET name=? WHERE id=?',(d.get('name',g['name']),g['id'])); c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
    for uid in d.get('user_ids',[]):c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],int(uid)))
    c.commit(); return {'group':group_to_json(c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone())}
@app.delete('/api/workspaces/{slug}/groups/{handle}')
def delete_group(slug:str,handle:str,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    w=group_workspace(slug); c=get_db(); g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone() if w else None
    if not g:return {'deleted':True}
    if u['id'] not in (g['creator_id'],w['owner_id']):return JSONResponse({'error':'forbidden'},403)
    c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],)); c.execute('DELETE FROM groups WHERE id=?',(g['id'],)); c.commit(); return {'deleted':True}
@app.patch('/api/users/me')
async def patch_profile(request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    allowed={'display_name':80,'status_text':100,'status_emoji':32,'avatar_url':1024}
    for k,n in allowed.items():
        if k in d and (not isinstance(d[k],str) or len(d[k])>n):return JSONResponse({'error':'invalid'},400)
    if 'timezone' in d:
        try: __import__('zoneinfo').ZoneInfo(d['timezone'])
        except:return JSONResponse({'error':'invalid'},400)
    c=get_db(); vals={k:d[k] for k in allowed if k in d}
    vals.update({k:d[k] for k in ('timezone',) if k in d})
    if vals:
        c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),u['id'])); c.commit()
    fresh=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
    import json
    c.execute('INSERT INTO profile_events(user_id,payload) VALUES(?,?)',(u['id'],json.dumps({'type':'user.updated','user':user_to_json(fresh)}))); c.commit()
    return {'user':user_to_json(fresh)}


def edit_message(message,user,body):
    if message['deleted']: raise LookupError()
    now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'); c=get_db()
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,now,message['id'])); c.commit()
    return c.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone()
def delete_message(message,user):
    if message['deleted']: return False
    c=get_db(); c.execute('UPDATE messages SET deleted=1 WHERE id=?',(message['id'],)); c.commit(); return True
@app.patch('/api/messages/{mid}')
async def edit_route(mid:int,request:Request):
    try:u=require_user(request); d=await request.json()
    except PermissionError:return JSONResponse({'error':'unauthorized'},401)
    m=get_db().execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m:return JSONResponse({'error':'not found'},404)
    if m['author_id']!=u['id']:return JSONResponse({'error':'forbidden'},403)
    body=d.get('body')
    if not isinstance(body,str) or not body.strip():return JSONResponse({'error':'invalid'},400)
    try:m=edit_message(m,u,body)
    except LookupError:return JSONResponse({'error':'deleted'},409)
    obj=message_to_json(m); emit_event(m['channel_id'],'message.updated',{'message':obj})
    return {'message':obj}
@app.delete('/api/messages/{mid}')
def delete_route(mid:int,request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    m=get_db().execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m:return JSONResponse({'error':'not found'},404)
    w=get_db().execute('SELECT owner_id FROM workspaces WHERE id=(SELECT workspace_id FROM channels WHERE id=?)',(m['channel_id'],)).fetchone()
    if m['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']):return JSONResponse({'error':'forbidden'},403)
    if not m['deleted']:
        delete_message(m,u); emit_event(m['channel_id'],'message.deleted',{'message_id':mid})
    return {'deleted':True}

@app.get('/api/health')
def health():
    return {'status':'ok','node_id':int(os.getenv('NODE_ID','0'))}
@app.post('/api/auth/register')
def register(x:Auth):
    try: u=create_user(x.username,x.password,x.display_name)
    except KeyError: return JSONResponse({'error':'username already exists'},409)
    except: return JSONResponse({'error':'invalid input'},400)
    return JSONResponse({'user':user_to_json(u),'token':issue_token(u['id'])},201)
@app.post('/api/auth/login')
def login(x:Auth):
    c=get_db(); u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(x.username,hash_password(x.password))).fetchone()
    if not u:return JSONResponse({'error':'invalid credentials'},401)
    return {'user':user_to_json(u),'token':issue_token(u['id'])}
@app.get('/api/auth/me')
def me(request:Request):
    try:u=require_user(request)
    except:return JSONResponse({'error':'unauthorized'},401)
    return {'user':user_to_json(u)}
@app.get('/api/users/{uid}')
def user(uid:int,request:Request):
    try: require_user(request)
    except: return JSONResponse({'error':'unauthorized'},401)
    u=get_db().execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    return {'user':user_to_json(u)} if u else JSONResponse({'error':'not found'},404)
@app.websocket('/api/ws')
async def ws(sock:WebSocket):
    token=sock.query_params.get('token','')
    row=get_db().execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(token,)).fetchone()
    if not row:
        await sock.close(code=1008); return
    await sock.accept()
    import asyncio,json,time,uuid
    connection_id=uuid.uuid4().hex
    c=get_db(); c.execute('INSERT INTO presence(connection_id,user_id,seen) VALUES(?,?,?)',(connection_id,row['id'],time.time())); c.commit()
    subs={}
    try:
        while True:
            try:
                raw=await asyncio.wait_for(sock.receive_text(),0.1)
                msg=json.loads(raw); cid=int(msg.get('channel_id',0))
                ch=get_channel(cid)
                if not ch or not visible_channel(ch,row): continue
                if msg.get('type')=='subscribe':
                    subs[cid]=channel_head(cid)
                    await sock.send_json({'type':'subscribed','channel_id':cid,'head_event_id':subs[cid]})
                elif msg.get('type')=='resume':
                    since=int(msg.get('since_event_id',0))
                    events=get_db().execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
                    for e in events:
                        frame=json.loads(e['payload']); frame.update({'type':e['type'],'event_id':e['event_id'],'channel_id':cid}); await sock.send_json(frame)
                    subs[cid]=channel_head(cid)
                    await sock.send_json({'type':'resumed','channel_id':cid,'head_event_id':subs[cid]})
            except asyncio.TimeoutError:
                pass
            pe=get_db().execute('SELECT id,payload FROM profile_events WHERE id>? ORDER BY id',(locals().get('profile_last',0),)).fetchall()
            for e in pe:
                await sock.send_json(json.loads(e['payload'])); profile_last=e['id']
            for cid,last in list(subs.items()):
                rows=get_db().execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
                for e in rows:
                    frame=json.loads(e['payload']); frame.update({'type':e['type'],'event_id':e['event_id'],'channel_id':cid}); await sock.send_json(frame); subs[cid]=e['event_id']
    except (WebSocketDisconnect,Exception): pass
    finally:
        c=get_db(); c.execute('DELETE FROM presence WHERE connection_id=?',(connection_id,)); c.commit()
@app.get('/')
def index():
    return HTMLResponse(open('/app/static/index.html').read())
