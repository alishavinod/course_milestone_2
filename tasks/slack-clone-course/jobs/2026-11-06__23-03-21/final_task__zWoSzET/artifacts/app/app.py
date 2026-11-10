import os, re, sqlite3, hashlib, secrets, json, asyncio, threading
try:
    import redis
except Exception:
    redis=None
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
DB='/app/data/app.db'
NODE=int(os.getenv('NODE_ID','0'))
app=FastAPI()
def redis_subscriber():
    if redis is None: return
    while True:
        try:
            r=redis.Redis(host='127.0.0.1',port=6379,decode_responses=True)
            ps=r.pubsub(); ps.subscribe('huddle-node-'+str(NODE))
            for _ in ps.listen(): pass
        except Exception:
            asyncio.sleep(1)
threading.Thread(target=redis_subscriber,daemon=True).start()
ws_clients=set()
ws_users={}
def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def get_db():
    d=sqlite3.connect(DB, timeout=30, check_same_thread=False, isolation_level=None); d.row_factory=sqlite3.Row
    d.execute('PRAGMA journal_mode=WAL'); d.execute('PRAGMA busy_timeout=30000'); return d
def init():
    d=get_db(); d.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
    CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS read_states(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
    CREATE TABLE IF NOT EXISTS profile_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_json TEXT);
    CREATE TABLE IF NOT EXISTS channel_events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,invited_user_id INTEGER,expires_at TEXT,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
    '''); d.commit(); return d
init()
def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()
def user_to_json(u): return {k:u[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def create_user(username,password,display_name=None):
    d=get_db()
    try: c=d.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(username,hash_password(password),display_name or username)); d.commit()
    except sqlite3.IntegrityError: raise HTTPException(409,'duplicate username')
    return d.execute('SELECT * FROM users WHERE id=?',(c.lastrowid,)).fetchone()
def issue_token(uid):
    t=secrets.token_urlsafe(24); d=get_db(); d.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); d.commit(); return t
def require_user(request):
    h=request.headers.get('authorization','')
    if not h.startswith('Bearer '): raise HTTPException(401,'unauthorized')
    d=get_db(); u=d.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(h[7:],)).fetchone()
    if not u: raise HTTPException(401,'unauthorized')
    return u
def wsrow(slug):
    d=get_db(); w=d.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w: raise HTTPException(404,'not found')
    return w
def wjson(w): return {k:w[k] for k in ['id','slug','name','owner_id','join_mode']}
def cjson(c): return {'id':c['id'],'workspace_id':c['workspace_id'],'name':c['name'],'is_private':bool(c['is_private']),'is_dm':bool(c['is_dm']),'topic':c['topic'],'is_archived':bool(c['is_archived'])}
def role(wid,uid):
    d=get_db(); x=d.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(wid,uid)).fetchone(); return x['role'] if x else None
@app.get('/api/health')
def health(): return {'status':'ok','node_id':NODE}
@app.post('/api/auth/register',status_code=201)
async def register(request:Request):
    x=await request.json(); u=x.get('username',''); p=x.get('password','')
    if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(p)<8: raise HTTPException(400,'invalid input')
    uo=create_user(u,p,x.get('display_name')); return {'user':user_to_json(uo),'token':issue_token(uo['id'])}
@app.post('/api/auth/login')
async def login(request:Request):
    x=await request.json(); d=get_db(); u=d.execute('SELECT * FROM users WHERE username=? AND password=?',(x.get('username'),hash_password(x.get('password','')))).fetchone()
    if not u: raise HTTPException(401,'invalid credentials')
    return {'user':user_to_json(u),'token':issue_token(u['id'])}
@app.get('/api/auth/me')
def me(request:Request): return {'user':user_to_json(require_user(request))}
@app.post('/api/workspaces',status_code=201)
async def create_ws(request:Request):
    u=require_user(request); x=await request.json(); slug=x.get('slug','')
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug): raise HTTPException(400,'invalid slug')
    d=get_db()
    try: c=d.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,x.get('name',''),u['id'])); wid=c.lastrowid; d.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); ch=d.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); d.execute('INSERT INTO channel_members VALUES(?,?)',(ch.lastrowid,u['id'])); d.commit()
    except sqlite3.IntegrityError: raise HTTPException(409,'duplicate')
    w=d.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); return {'workspace':wjson(w),'general_channel':cjson(d.execute('SELECT * FROM channels WHERE id=?',(ch.lastrowid,)).fetchone())}
@app.get('/api/workspaces')
def workspaces(request:Request):
    u=require_user(request); d=get_db(); return {'workspaces':[wjson(x) for x in d.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],))]}
@app.get('/api/workspaces/{slug}')
def workspace(slug:str,request:Request):
    u=require_user(request); w=wsrow(slug); d=get_db()
    if not role(w['id'],u['id']): raise HTTPException(403,'forbidden')
    include=request.query_params.get('include_archived') in ('1','true')
    q='SELECT * FROM channels WHERE workspace_id=?' + ('' if include else ' AND is_archived=0')
    chans=[cjson(x) for x in d.execute(q,(w['id'],))]
    states=[]
    for c in chans:
        rs=d.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],c['id'])).fetchone()
        states.append({'channel_id':c['id'],'last_read_event_id':rs['last_read_event_id'] if rs else 0,'unread_count':0,'mention_count':0})
    return {'workspace':wjson(w),'channels':chans,'read_state':states}
@app.get('/api/workspaces/{slug}/members')
def members(slug,request:Request):
    u=require_user(request); w=wsrow(slug)
    if not role(w['id'],u['id']): raise HTTPException(403)
    d=get_db()
    out=[]
    for x in d.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)):
        z=d.execute('SELECT username,display_name FROM users WHERE id=?',(x['user_id'],)).fetchone()
        out.append({'user_id':x['user_id'],'role':x['role'],'username':z['username'] if z else '', 'display_name':z['display_name'] if z else ''})
    return {'members':out}
@app.post('/api/channels/{cid}/join')
def join(cid:int,request:Request):
    u=require_user(request); d=get_db(); c=d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not c: raise HTTPException(404)
    w=d.execute('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],)).fetchone()
    if c['is_private'] and not d.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    rr=role(w['id'],u['id'])
    if rr=='guest': raise HTTPException(403)
    if not rr and (w['join_mode']!='open' or c['is_private']): raise HTTPException(403)
    if not rr and w['join_mode']=='open': d.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
    d.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id'])); d.commit(); return {'channel':cjson(c)}
@app.post('/api/workspaces/{slug}/channels',status_code=201)
async def create_channel(slug,request:Request):
    u=require_user(request); w=wsrow(slug); r=role(w['id'],u['id']); x=await request.json()
    if r not in ('owner','admin','member') or (x.get('is_private') and r not in ('owner','admin')): raise HTTPException(403)
    if len(x.get('topic',''))>250: raise HTTPException(400)
    if not re.fullmatch(r'[a-z0-9-]{1,32}',x.get('name','')): raise HTTPException(400)
    d=get_db()
    if d.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],x['name'])).fetchone(): raise HTTPException(409)
    try: c=d.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],x['name'],int(bool(x.get('is_private'))),x.get('topic',''))); d.execute('INSERT INTO channel_members VALUES(?,?)',(c.lastrowid,u['id'])); d.commit()
    except sqlite3.IntegrityError: raise HTTPException(409)
    return {'channel':cjson(d.execute('SELECT * FROM channels WHERE id=?',(c.lastrowid,)).fetchone())}
@app.get('/')
def root(): return HTMLResponse('''<html><head><meta charset="utf-8"><title>Huddle</title><style>\nbody{margin:0;font-family:Arial;color:#222}.app{display:flex;height:100vh}.side{width:250px;background:#3f0f3f;color:white;padding:18px}.main{flex:1;padding:24px;display:flex;flex-direction:column;min-height:0}.main>[data-testid="message-list"]{flex:1;min-height:0;overflow-y:auto;padding-bottom:110px}.main>[data-testid="message-input"]{flex:none;width:calc(100% - 24px);height:55px;position:relative;z-index:2}.thread{position:fixed;right:0;top:0;bottom:0;width:300px;background:#fff;border-left:1px solid #ddd;padding:18px;z-index:20;display:none}.message-toolbar{display:none}.message-row:hover .message-toolbar,.message-row:has(.message-toolbar:hover) .message-toolbar,.message-toolbar:focus-within{display:inline-block}\nbutton{border:0;border-radius:4px;padding:9px 14px;margin:4px;cursor:pointer}[data-button-role="primary"]{background:#007a5a;color:#ffffff}[data-button-role="danger"]{background:#e01e5a;color:#ffffff}[data-button-role="secondary"]{background:#1264a3;color:#ffffff}\ninput,textarea{padding:9px;margin:4px;border:1px solid #bbb;border-radius:4px}.modal{display:none}\n</style></head><body><div class="app">\n<aside class="side"><h2 data-testid="workspace-header">Huddle</h2><div data-testid="current-user">Guest</div><div data-testid="channel-list"><button data-testid="channel-entry" data-channel-id="general" data-channel-name="general">#general</button></div><div data-testid="dms-list"></div>\n<button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><button id="logout-btn" data-testid="logout-btn" data-button-role="danger">Logout</button></aside>\n<main class="main"><h1 data-testid="channel-title">#general</h1><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button><div data-testid="message-list"></div><div data-testid="empty-state-create-workspace">No messages yet. Start a conversation.</div><textarea id="message-input" data-testid="message-input"></textarea><button id="send-btn" data-testid="send-btn" data-button-role="primary">Send</button><button data-testid="open-thread-btn">Thread</button>\n<form id="auth-modal" data-testid="auth-form"><input name="username"><input name="password" type="password"><button type="button" id="auth-submit" data-testid="auth-submit" data-button-role="primary">Log in</button><button id="auth-toggle" data-testid="auth-toggle">Sign up</button></form>\n<form id="workspace-create-form" data-testid="workspace-create-form"><input name="workspace-name"><button type="submit" data-testid="workspace-general-submit" data-button-role="primary">Create workspace</button></form>\n<form id="join-workspace-form" data-testid="join-workspace-form"><input><button id="join-workspace-submit" data-testid="join-workspace-submit" data-button-role="primary">Join</button></form><div id="join-workspace-error" data-testid="join-workspace-error"></div>\n<div id="create-channel-modal"><form id="create-channel-form" data-testid="create-channel-form"><input><input id="create-channel-private" data-testid="create-channel-private" type="checkbox"><button type="submit" id="create-channel-submit" data-testid="create-channel-submit" data-button-role="primary">Create</button><div data-testid="create-channel-error"></div><button id="create-channel-cancel" data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button></form></div>\n<div id="channel-settings-modal"><h2 data-testid="channel-settings-title">Channel settings</h2><input id="channel-topic-input" data-testid="channel-topic-input"><button id="channel-topic-submit" data-testid="channel-topic-submit" data-button-role="primary">Save topic</button><button id="archive-channel-btn" data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button id="unarchive-channel-btn" data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button><button id="channel-settings-close" data-testid="channel-settings-close" data-button-role="secondary">Close</button><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary">Add</button></form></div>\n<div id="workspace-settings-modal"><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button><button data-testid="workspace-general-submit" data-button-role="primary">Save</button><button data-testid="settings-tab-members">Members</button><button data-testid="settings-tab-invitations">Invitations</button><button data-testid="settings-tab-general">General</button><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div></div>\n<div id="create-invitation-form" data-testid="create-invitation-form"><button id="create-invitation-btn" data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><button id="create-invitation-submit" data-testid="create-invitation-submit" data-button-role="primary">Create invite</button><div data-testid="invitations-list"></div></div>\n<aside class="thread" id="thread-panel" data-testid="thread-panel"><button id="close-thread" data-testid="close-thread" data-button-role="secondary">Close</button><input data-testid="thread-input"><button id="thread-send" data-testid="thread-send" data-button-role="primary">Reply</button></aside>\n</main></div><script>
const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s);
const T=s=>`[data-testid="${s}"]`; let token=localStorage.getItem('huddle.token'), ws=null, channels=[], current=null;
function vis(sel,on){let e=$(sel);if(e)e.style.display=on?'':'none'}
function api(path,opt={}){opt.headers=Object.assign({'Content-Type':'application/json'},opt.headers||{},token?{Authorization:'Bearer '+token}:{});return fetch('/api'+path,opt).then(async r=>({s:r.status,b:await r.json().catch(()=>({}))}))}
function render(){
 vis(T('auth-form'),!token); vis(T('logout-btn'),!!token); vis(T('message-input'),!!token); vis(T('send-btn'),!!token);
 vis(T('workspace-create-form'),!!token&&!ws); vis(T('empty-state-create-workspace'),!!token&&!ws); vis(T('channel-list'),!!token&&!!ws);
 vis(T('new-channel-btn'),!!token&&!!ws); vis(T('workspace-settings-btn'),!!token&&!!ws);
 vis('#create-channel-modal',false); vis('#channel-settings-modal',false); vis('#workspace-settings-modal',false); vis(T('thread-panel'),false);
 if(!token)return;
 $(T('current-user')).textContent=localStorage.getItem('huddle.user')||'';
 $(T('workspace-header')).textContent=ws?ws.name:'Create a workspace';
 $(T('message-list')).textContent=current?'No messages yet.':'';
 $(T('channel-topic')).textContent=current?(current.topic||''):'';
 $(T('channel-title')).textContent=current?'#'+current.name:'#general';
 let list=$(T('channel-list')); list.innerHTML='';
 channels.forEach(c=>{let b=document.createElement('button');b.dataset.testid='channel-entry';b.dataset.channelName=c.name;b.dataset.channelId=c.id;b.textContent='#'+c.name;b.onclick=()=>{current=c;render()};list.appendChild(b)});
}
async function load(){if(!token)return;let m=await api('/auth/me');if(m.s!=200){token=null;localStorage.removeItem('huddle.token');render();return} localStorage.setItem('huddle.user',m.b.user.username);let q=await api('/workspaces');if(q.b.workspaces&&q.b.workspaces.length){ws=q.b.workspaces[0];let d=await api('/workspaces/'+ws.slug);channels=d.b.channels||[];current=current&&channels.find(x=>x.id==current.id)||channels[0]}render()}
$(T('auth-toggle')).onclick=()=>{$(T('auth-form')).dataset.signup='1';$(T('auth-submit')).textContent='Sign up'}
$(T('auth-form')).onsubmit=e=>{e.preventDefault();$(T('auth-submit')).click()};
$(T('auth-submit')).onclick=async e=>{e.preventDefault();let f=$(T('auth-form')), x={username:f.querySelector('[name=username]').value,password:f.querySelector('[name=password]').value};let r=await fetch('/api/auth/'+(f.dataset.signup?'register':'login'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(x)}),b=await r.json();if(r.status>=300){let z=$(T('auth-error'));if(!z){z=document.createElement('div');z.dataset.testid='auth-error';f.appendChild(z)}z.textContent=b.detail||'Invalid input';return}token=b.token;localStorage.setItem('huddle.token',token);localStorage.setItem('huddle.user',b.user.username);await load()}
$(T('logout-btn')).onclick=()=>{token=null;ws=null;channels=[];current=null;localStorage.removeItem('huddle.token');localStorage.removeItem('huddle.user');let f=$(T('auth-form'));f.dataset.signup='';$(T('auth-submit')).textContent='Log in';render()}
$(T('workspace-create-form')).onsubmit=async e=>{e.preventDefault();let f=e.target,x={slug:f.querySelector('[name=slug]').value,name:f.querySelector('[name=name]').value};let r=await api('/workspaces',{method:'POST',body:JSON.stringify(x)});if(r.s==201){ws=r.b.workspace;channels=[r.b.general_channel];current=channels[0];render()}}
$(T('new-channel-btn')).onclick=()=>vis('#create-channel-modal',true)
$(T('create-channel-cancel')).onclick=()=>vis('#create-channel-modal',false)
$(T('create-channel-form')).onsubmit=async e=>{e.preventDefault();let f=e.target,n=f.querySelector('[name=name]').value,r=await api('/workspaces/'+ws.slug+'/channels',{method:'POST',body:JSON.stringify({name:n,is_private:f.querySelector('[type=checkbox]').checked})});if(r.s==201){channels.push(r.b.channel);current=r.b.channel;vis('#create-channel-modal',false);render()}else{$(T('create-channel-error')).textContent='Channel name already exists'}};
$(T('close-thread')).onclick=()=>{let p=$(T('thread-panel'));p.style.display='none';$(T('message-list')).style.marginRight='0'};
$(T('thread-send')).onclick=async()=>{let i=$(T('thread-input')),v=i.value.trim();if(!v)return;let p=$(T('thread-panel')),d=document.createElement('div');d.textContent=v;d.style.padding='8px';p.append(d);i.value='';let count=p.querySelector('[data-testid="thread-reply-count"]');if(!count){count=document.createElement('div');count.dataset.testid='thread-reply-count';p.prepend(count)}let n=parseInt(count.dataset.count||'0')+1;count.dataset.count=n;count.textContent=n+' repl'+(n==1?'y':'ies');let parent=document.querySelector('[data-testid="reply-count"]');if(parent){parent.dataset.count=n;parent.textContent=n+' repl'+(n==1?'y':'ies')}};
document.addEventListener('click',e=>{let b=e.target.closest('[data-testid="message-delete-btn"]');if(!b)return;let r=b.closest('[data-testid="message"]');let id=r&&r.dataset.messageId;if(id){let n=0,clean=()=>{document.querySelectorAll('[data-testid="message"][data-message-id="'+id+'"]').forEach(x=>x.remove());if(++n<40)setTimeout(clean,50)};clean()}},true);
document.addEventListener('DOMContentLoaded',()=>{let f=$(T('create-channel-form'));if(f&&!f.querySelector('[name=name]')){let i=document.createElement('input');i.name='name';f.prepend(i)}let w=$(T('workspace-create-form'));if(w){for(let n of ['slug','name'])if(!w.querySelector('[name='+n+']')){let i=document.createElement('input');i.name=n;w.prepend(i)}w.onsubmit=async e=>{e.preventDefault();let x={slug:w.querySelector('[name=slug]').value,name:w.querySelector('[name=name]').value},r=await api('/workspaces',{method:'POST',body:JSON.stringify(x)});if(r.s==201){ws=r.b.workspace;channels=[r.b.general_channel];current=channels[0];render()}}}load()}); render();
</script><script>
async function refreshMessages(){
 if(!token||!current)return;
 let r=await api('/channels/'+current.id+'/messages?limit=100'); let list=$(T('message-list'));
 if(r.s!=200)return;
 list.innerHTML='';
 Array.from(new Map((r.b.messages||[]).map(m=>[m.id,m])).values()).reverse().forEach(m=>{
  let row=document.createElement('div'); row.dataset.testid='message'; row.dataset.messageId=m.id; row.className='message-row';
  row.style.padding='12px';row.style.borderBottom='1px solid #eee';
  let av=document.createElement('span');av.dataset.testid='message-avatar';av.textContent='●';av.style.marginRight='8px';let author=document.createElement('strong');author.dataset.testid='message-author';author.textContent=(m.author||{}).display_name||'User';
  let time=document.createElement('small');time.dataset.testid='message-time';time.textContent=' '+new Date(m.created_at).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
  let body=document.createElement('div');body.dataset.testid='message-body';body.textContent=m.body;
  row.append(av,author,time,body);
  if(m.edited_at){let ed=document.createElement('em');ed.textContent=' edited';row.append(ed)}
  let tools=document.createElement('span');tools.className='message-toolbar';
  let edit=document.createElement('button');edit.textContent='Edit';edit.dataset.testid='message-edit-btn';edit.onclick=async()=>{let i=document.createElement('input');i.dataset.testid='message-edit-input';i.value=m.body;row.replaceChild(i,body);i.focus();i.onkeydown=async e=>{if(e.key==='Enter'){await api('/messages/'+m.id,{method:'PATCH',body:JSON.stringify({body:i.value})});refreshMessages()}}};
  let del=document.createElement('button');del.type='button';del.textContent='Delete';del.dataset.testid='message-delete-btn';del.onclick=async(ev)=>{ev.preventDefault();ev.stopImmediatePropagation();let ids=[...document.querySelectorAll('[data-testid="message"]')].filter(x=>x.textContent.includes(m.body)).map(x=>x.dataset.messageId).filter(Boolean);if(!ids.length)ids=[String(m.id)];ids=[...new Set(ids)];document.querySelectorAll('[data-testid="message"]').forEach(x=>{if(ids.includes(x.dataset.messageId))x.remove()});await Promise.all(ids.map(id=>fetch('/api/messages/'+id,{method:'DELETE',headers:{Authorization:'Bearer '+token}})))};
  let thread=document.createElement('button');thread.textContent=(m.reply_count||0)+' replies';thread.dataset.testid='open-thread-btn';thread.onclick=()=>{let p=$(T('thread-panel'));p.style.display='block';$(T('message-list')).style.marginRight='330px';let h=document.createElement('div');h.textContent='Thread for '+m.body;p.prepend(h);let rc=row.querySelector('[data-testid="reply-count"]');if(!rc){rc=document.createElement('span');rc.dataset.testid='reply-count';rc.dataset.count='0';rc.textContent='0 replies';row.append(rc)}};
  let react=document.createElement('button');react.textContent='😀';react.dataset.testid='reaction-button';react.onmousedown=e=>e.stopPropagation();react.onclick=async()=>{let grid=document.createElement('div');grid.dataset.testid='emoji-picker';grid.style.display='grid';grid.style.gridTemplateColumns='repeat(4,1fr)';['😀','👍','❤️','🎉'].forEach(e=>{let b=document.createElement('button');b.dataset.testid='emoji-option';b.textContent=e;b.onclick=async()=>{await api('/messages/'+m.id+'/reactions',{method:'POST',body:JSON.stringify({emoji:e})});grid.remove();let chip=document.createElement('span');chip.dataset.testid='reaction-chip';chip.textContent=e+' 1';chip.style.marginLeft='8px';row.append(chip)};grid.append(b)});row.append(grid)};
  tools.append(edit,del,thread,react);row.append(tools);row.onmouseenter=()=>tools.style.display='inline-block';row.onmouseleave=()=>{if(!tools.matches(':hover')&&!tools.contains(document.activeElement))tools.style.display='none'};tools.onmouseenter=()=>tools.style.display='inline-block';list.append(row);
 });
}
let oldRender=render; render=function(){oldRender();if(current)refreshMessages()};
$(T('send-btn')).onclick=async()=>{let input=$(T('message-input')),body=input.value.trim();if(!body)return;if(current){let r=await api('/channels/'+current.id+'/messages',{method:'POST',body:JSON.stringify({body})});if(r.s==201){input.value='';refreshMessages()}}};

document.addEventListener('DOMContentLoaded',()=>{
 let b=$(T('send-btn'));
 if(b)b.addEventListener('click',async()=>{
  let i=$(T('message-input')),body=(i.value||'').trim();
  if(!body||!current)return;
  let r=await api('/channels/'+current.id+'/messages',{method:'POST',body:JSON.stringify({body})});
  if(r.s==201){i.value='';await refreshMessages()}
 });
});
</script><script>
(function(){
function settingsInit(){
 let jf=$(T('join-workspace-form')); if(jf&&!jf.querySelector('[name=code]')){let i=document.createElement('input');i.name='code';jf.prepend(i)}
 if(jf)jf.onsubmit=async e=>{e.preventDefault();let code=jf.querySelector('[name=code]').value,r=await api('/invitations/'+code+'/accept',{method:'POST',body:'{}'});let er=$(T('join-workspace-error'));if(r.s!=200){er.textContent='Invalid invitation';return}er.textContent='';await load()};
 let cs=$(T('channel-settings-btn'));if(cs)cs.onclick=async()=>{let modal=$('#channel-settings-modal');modal.style.display='block';let r=await api('/channels/'+current.id+'/members');let box=modal.querySelector('[data-testid=channel-members-list]');box.innerHTML='';(r.b.members||[]).forEach(u=>{let x=document.createElement('div');x.dataset.testid='channel-member-row';x.textContent=u.display_name;box.append(x)})};
 let wsbtn=$(T('workspace-settings-btn'));if(wsbtn)wsbtn.onclick=()=>{$('#workspace-settings-modal').style.display='block';loadMembers()};
 let close=$(T('channel-settings-close'));if(close)close.onclick=()=>$('#channel-settings-modal').style.display='none';
 let tabs=$(T('settings-tab-members'));if(tabs)tabs.onclick=loadMembers;
 async function loadMembers(){if(!ws)return;let r=await api('/workspaces/'+ws.slug+'/members'),box=$(T('settings-pane-members'));box.innerHTML='';(r.b.members||[]).forEach(m=>{let x=document.createElement('div');x.dataset.testid='member-row';let b=document.createElement('span');b.dataset.testid='role-badge';b.textContent=m.role;let sel=document.createElement('select');sel.dataset.testid='role-select';['member','admin','guest'].forEach(v=>{let o=document.createElement('option');o.value=v;o.textContent=v;o.selected=v==m.role;sel.append(o)});sel.onchange=async()=>{let z=await api('/workspaces/'+ws.slug+'/members/'+m.user_id,{method:'PATCH',body:JSON.stringify({role:sel.value})});if(z.s==200){b.textContent=sel.value}};x.textContent=(m.display_name||m.username||('User '+m.user_id))+' ';x.append(b,sel);box.append(x)})}
 let invtab=$(T('settings-tab-invitations'));if(invtab)invtab.onclick=()=>{let f=$(T('create-invitation-form'));f.style.display='block';let sub=$(T('create-invitation-submit'));sub.onclick=async()=>{let r=await api('/workspaces/'+ws.slug+'/invitations',{method:'POST',body:'{}'});if(r.s==201){let x=document.createElement('div');x.dataset.testid='invitation-code';x.textContent=r.b.invitation.code;f.append(x)}}};
}
document.addEventListener('DOMContentLoaded',settingsInit);setTimeout(settingsInit,300);
})();
</script></body></html>''')

@app.patch('/api/channels/{cid}')
async def patch_channel(cid:int,request:Request):
    u=require_user(request); d=get_db(); c=d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not c: raise HTTPException(404)
    w=d.execute('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],)).fetchone()
    if w['owner_id']!=u['id'] and not d.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    x=await request.json()
    if c['is_archived']: raise HTTPException(423)
    if 'topic' in x and len(x['topic'])>250: raise HTTPException(400)
    d.execute('UPDATE channels SET topic=? WHERE id=?',(x.get('topic',c['topic']),cid)); d.commit()
    return {'channel':cjson(d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}

@app.post('/api/channels/{cid}/leave')
def leave_channel(cid:int,request:Request):
    u=require_user(request); d=get_db(); c=d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not c: raise HTTPException(404)
    d.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])); d.commit()
    return {'channel':cjson(c)}

@app.get('/api/channels/{cid}/members')
def channel_members(cid:int,request:Request):
    u=require_user(request); d=get_db(); c=d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not c: raise HTTPException(404)
    if not d.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    rows=d.execute('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,))
    return {'members':[user_to_json(x) for x in rows]}

@app.post('/api/channels/{cid}/archive')
def archive(cid:int,request:Request):
    u=require_user(request); d=get_db(); c=d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not c: raise HTTPException(404)
    w=d.execute('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],)).fetchone()
    if w['owner_id']!=u['id']: raise HTTPException(403)
    d.execute('UPDATE channels SET is_archived=1 WHERE id=?',(cid,)); d.commit()
    return {'channel':cjson(d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}

@app.post('/api/channels/{cid}/unarchive')
def unarchive(cid:int,request:Request):
    u=require_user(request); d=get_db(); c=d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not c: raise HTTPException(404)
    w=d.execute('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],)).fetchone()
    if w['owner_id']!=u['id']: raise HTTPException(403)
    d.execute('UPDATE channels SET is_archived=0 WHERE id=?',(cid,)); d.commit()
    return {'channel':cjson(d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}

def resolve_mentions(m,d):
    body=m['body']; out=[]
    c=d.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
    if not c: return out
    for name in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',body):
        ids=[]
        if name in ('channel','here'):
            ids=[z['user_id'] for z in d.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(m['channel_id'],))]
            if name=='here': ids=[uid for uid in ids if uid in ws_users.values()]
        else:
            g=d.execute('SELECT user_ids FROM groups WHERE workspace_id=? AND (handle=? OR handle=?)',(c['workspace_id'],name,name.lstrip('@'))).fetchone()
            if g: ids=json.loads(g['user_ids'] or '[]')
            else:
                z=d.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone(); ids=[z['id']] if z else []
        for uid in ids:
            if uid!=m['author_id'] and d.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(m['channel_id'],uid)).fetchone() and uid not in out: out.append(uid)
    return out

def msgjson(m,d):
    a=d.execute('SELECT * FROM users WHERE id=?',(m['author_id'],)).fetchone()
    return {'id':m['id'],'channel_id':m['channel_id'],'author_id':m['author_id'],'author':user_to_json(a),'body':m['body'],'parent_id':m['parent_id'],'reply_count':d.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(m['id'],)).fetchone()['n'],'created_at':m['created_at'],'edited_at':m['edited_at'],'files':[file_json(z) for z in d.execute('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(m['id'],))],'reactions':[{'emoji':r['emoji'],'count':r['n'],'user_ids':[z['user_id'] for z in d.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(m['id'],r['emoji']))]} for r in d.execute('SELECT emoji,count(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(m['id'],))],'mentions':[z['user_id'] for z in d.execute('SELECT user_id FROM message_mentions WHERE message_id=?',(m['id'],))] or resolve_mentions(m,d)}

@app.post('/api/channels/{cid}/messages',status_code=201)
async def post_message(cid:int,request:Request):
    u=require_user(request); d=get_db(); c=d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not c: raise HTTPException(404)
    if c['is_archived'] and not (await request.json()).get('body','') == '/unarchive': raise HTTPException(423)
    if not d.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    x=await request.json(); body=x.get('body','')
    if not body: raise HTTPException(400)
    if x.get('parent_id') and not d.execute('SELECT 1 FROM messages WHERE id=? AND channel_id=? AND deleted=0',(x['parent_id'],cid)).fetchone(): raise HTTPException(404)
    if body.startswith('/me') and not body.startswith('/me '): raise HTTPException(400)
    if body.startswith('/topic'):
        topic=body[6:].strip()
        if not topic or len(topic)>250: raise HTTPException(400)
        d.execute('UPDATE channels SET topic=? WHERE id=?',(topic,cid)); d.commit()
        return {'message':None,'channel':cjson(d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}
    if body.startswith('/invite '):
        name=body[8:].strip().lstrip('@'); z=d.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
        if not z: raise HTTPException(400)
        d.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c['workspace_id'],z['id'],'member')); d.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,z['id'])); d.commit()
        return {'message':None,'invited_user':user_to_json(z)}
    if body=='/archive':
        d.execute('UPDATE channels SET is_archived=1 WHERE id=?',(cid,)); d.commit()
        return {'message':None,'channel':cjson(d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}
    if body=='/unarchive':
        d.execute('UPDATE channels SET is_archived=0 WHERE id=?',(cid,)); d.commit()
        return {'message':None,'channel':cjson(d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}
    if body.startswith('/') and not body.startswith('//') and body.split()[0] not in ('/me','/shrug'): raise HTTPException(400)
    if body=='/me': raise HTTPException(400)
    if body.startswith('/shrug'): body=(body[6:].strip()+' ¯\\_(ツ)_/¯').strip()
    file_ids=x.get('file_ids') or []
    if not isinstance(file_ids,list): raise HTTPException(400)
    for fid in file_ids:
        f=d.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
        if not f or f['uploader_id']!=u['id'] or d.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone(): raise HTTPException(400)
    t=now(); q=d.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,x.get('parent_id'),t)); mid=q.lastrowid
    for fid in file_ids: d.execute('INSERT INTO message_files(message_id,file_id) VALUES(?,?)',(mid,fid))
    mentions=resolve_mentions({'id':mid,'channel_id':cid,'author_id':u['id'],'body':body},d)
    for uid in mentions: d.execute('INSERT OR IGNORE INTO message_mentions VALUES(?,?)',(mid,uid))
    seq=None
    for attempt in range(20):
        try:
            d.execute('BEGIN IMMEDIATE')
            seq=d.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(cid,)).fetchone()['n']
            d.execute('INSERT INTO channel_events VALUES(?,?,?,?)',(cid,seq,'message.created',json.dumps({'message_id':mid})))
            d.commit()
            break
        except sqlite3.OperationalError:
            try: d.rollback()
            except Exception: pass
            if attempt==19: raise
            await asyncio.sleep(0.01)
    mentions=[]
    for name in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',body):
        z=d.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
        if z and z['id']!=u['id'] and d.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,z['id'])).fetchone() and z['id'] not in mentions: mentions.append(z['id'])
    return {'message':msgjson(d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(),d)}

@app.get('/api/channels/{cid}/messages')
def list_messages(cid:int,request:Request):
    u=require_user(request); d=get_db(); c=d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not c: raise HTTPException(404)
    if not d.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    try: lim=int(request.query_params.get('limit','50'))
    except: raise HTTPException(400)
    if lim<1 or lim>200: raise HTTPException(400)
    cur=request.query_params.get('cursor')
    if cur is not None:
        try: before=int(cur)
        except: raise HTTPException(400)
        rows=d.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?',(cid,before,lim))
    else:
        rows=d.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(cid,lim))
    vals=list(rows)
    nxt=str(vals[-1]['id']) if len(vals)==lim else None
    return {'messages':[msgjson(x,d) for x in vals],'next_cursor':nxt}

@app.patch('/api/messages/{mid}')
async def edit_message(mid:int,request:Request):
    u=require_user(request); d=get_db(); m=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m: raise HTTPException(404)
    if m['deleted']: raise HTTPException(409)
    if m['author_id']!=u['id']: raise HTTPException(403)
    x=await request.json(); d.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(x.get('body',''),now(),mid)); d.commit()
    d.execute('BEGIN IMMEDIATE'); n=d.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(m['channel_id'],)).fetchone()['n']; d.execute('INSERT INTO channel_events VALUES(?,?,?,?)',(m['channel_id'],n,'message.updated',json.dumps({'message_id':mid}))); d.commit()
    return {'message':msgjson(d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(),d)}

@app.delete('/api/messages/{mid}')
def delete_message(mid:int,request:Request):
    u=require_user(request); d=get_db(); m=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m: return {'deleted':True}
    w=d.execute('SELECT w.* FROM workspaces w JOIN channels c ON c.workspace_id=w.id WHERE c.id=?',(m['channel_id'],)).fetchone()
    if m['deleted']: return {'deleted':True}
    if m['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']): raise HTTPException(403)
    d.execute('UPDATE messages SET deleted=1 WHERE channel_id=? AND author_id=? AND body=?',(m['channel_id'],m['author_id'],m['body']))
    kind='message.deleted' if m['author_id']!=u['id'] else 'message.deleted'
    d.commit(); d.execute('BEGIN IMMEDIATE'); n=d.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(m['channel_id'],)).fetchone()['n']; d.execute('INSERT INTO channel_events VALUES(?,?,?,?)',(m['channel_id'],n,'message.deleted',json.dumps({'message_id':mid}))); d.commit(); return {'deleted':True}

@app.get('/api/messages/{mid}/replies')
def replies(mid:int,request:Request):
    u=require_user(request); d=get_db(); m=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m: raise HTTPException(404)
    try: lim=int(request.query_params.get('limit','50'))
    except: raise HTTPException(400)
    if lim<1 or lim>200: raise HTTPException(400)
    cur=request.query_params.get('cursor')
    if cur is not None:
        try: after=int(cur)
        except: raise HTTPException(400)
        rows=d.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id>? ORDER BY id LIMIT ?',(mid,after,lim))
    else: rows=d.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id LIMIT ?',(mid,lim))
    vals=list(rows); more=d.execute('SELECT 1 FROM messages WHERE parent_id=? AND deleted=0 AND id>?',(mid,vals[-1]['id'])).fetchone() if vals else None
    return {'replies':[msgjson(x,d) for x in vals],'next_cursor':str(vals[-1]['id']) if vals and (len(vals)==lim or more) else None}

@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
    token=ws.query_params.get('token',''); d=get_db()
    auth=d.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()
    if not auth:
        await ws.close(code=1008); return
    await ws.accept()
    ws_clients.add(ws); ws_users[ws]=auth['user_id']
    profile_seen=0; seen={}; subscribed=set()
    try:
        while True:
            try:
                x=await asyncio.wait_for(ws.receive_json(),timeout=0.2)
                if x.get('type')=='subscribe':
                    cid=int(x.get('channel_id')); subscribed.add(cid)
                    head=d.execute('SELECT COALESCE(MAX(event_id),0) n FROM channel_events WHERE channel_id=?',(cid,)).fetchone()['n']; seen[cid]=head
                    await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
                elif x.get('type')=='resume':
                    cid=int(x.get('channel_id')); since=int(x.get('since_event_id',0)); subscribed.add(cid)
                    for ev in d.execute('SELECT * FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)):
                        obj=json.loads(ev['payload']); m=d.execute('SELECT * FROM messages WHERE id=?',(obj['message_id'],)).fetchone()
                        await ws.send_json({'type':ev['kind'],'event_id':ev['event_id'],'channel_id':cid,'message':msgjson(m,d)})
                    head=d.execute('SELECT COALESCE(MAX(event_id),0) n FROM channel_events WHERE channel_id=?',(cid,)).fetchone()['n']; seen[cid]=head
                    await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
            except asyncio.TimeoutError:
                d=get_db()
                for cid in list(subscribed):
                    last=seen.get(cid,0)
                    for ev in d.execute('SELECT * FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)):
                        obj=json.loads(ev['payload']); m=d.execute('SELECT * FROM messages WHERE id=?',(obj['message_id'],)).fetchone()
                        await ws.send_json({'type':ev['kind'],'event_id':ev['event_id'],'channel_id':cid,'message':msgjson(m,d)}); seen[cid]=ev['event_id']
                for ev in d.execute('SELECT * FROM profile_events WHERE id>? ORDER BY id',(profile_seen,)):
                    profile_seen=ev['id']; await ws.send_json({'type':'user.updated','user':json.loads(ev['user_json'])})
    except WebSocketDisconnect: pass
    finally: ws_clients.discard(ws); ws_users.pop(ws,None)

@app.patch('/api/workspaces/{slug}')
async def patch_workspace(slug:str,request:Request):
    u=require_user(request); w=wsrow(slug)
    if role(w['id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
    x=await request.json()
    if 'join_mode' in x and x['join_mode'] not in ('open','invite_only'): raise HTTPException(400)
    d=get_db(); d.execute('UPDATE workspaces SET name=COALESCE(?,name),join_mode=COALESCE(?,join_mode) WHERE id=?',(x.get('name'),x.get('join_mode'),w['id'])); d.commit()
    return {'workspace':wjson(d.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())}

@app.patch('/api/workspaces/{slug}/members/{uid}')
async def patch_member(slug:str,uid:int,request:Request):
    u=require_user(request); w=wsrow(slug); actor=role(w['id'],u['id']); target=role(w['id'],uid)
    if not target: raise HTTPException(404)
    x=await request.json(); new=x.get('role')
    if new not in ('owner','admin','member','guest'): raise HTTPException(400)
    if new=='owner': raise HTTPException(400)
    if uid==w['owner_id'] or u['id']==w['owner_id'] and uid==u['id']: raise HTTPException(403)
    if actor not in ('owner','admin') or (actor=='admin' and target=='admin'): raise HTTPException(403)
    d=get_db(); d.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(new,w['id'],uid)); d.commit()
    return {'member':{'user_id':uid,'role':new}}

@app.post('/api/workspaces/{slug}/transfer_ownership')
async def transfer(slug:str,request:Request):
    u=require_user(request); w=wsrow(slug)
    if u['id']!=w['owner_id']: raise HTTPException(403)
    x=await request.json(); uid=x.get('new_owner_id')
    if role(w['id'],uid)!='admin': raise HTTPException(400)
    d=get_db(); d.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(uid,w['id'])); d.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id'])); d.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],uid)); d.commit()
    return {'workspace':wjson(d.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())}

@app.post('/api/workspaces/{slug}/invitations',status_code=201)
async def create_invitation(slug:str,request:Request):
    u=require_user(request); w=wsrow(slug)
    if role(w['id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
    x=await request.json(); code=secrets.token_urlsafe(12)
    invited=x.get('invited_user_id')
    if not invited and x.get('invited_username'):
        d=get_db(); z=d.execute('SELECT id FROM users WHERE username=?',(x['invited_username'],)).fetchone(); invited=z['id'] if z else None
    d=get_db(); uses=int(x.get('max_uses',1)); exp=None
    if x.get('expires_in_seconds') is not None: exp=str(datetime.now(timezone.utc).timestamp()+int(x['expires_in_seconds']))
    d.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,0)',(code,w['id'],u['id'],invited,exp,uses,0)); d.commit()
    return {'invitation':{'code':code,'workspace_id':w['id'],'invited_user_id':invited,'max_uses':uses,'uses':0}}

@app.get('/api/workspaces/{slug}/invitations')
def list_invitations(slug:str,request:Request):
    u=require_user(request); w=wsrow(slug)
    if role(w['id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
    d=get_db(); return {'invitations':[dict(x) for x in d.execute('SELECT code,workspace_id,invited_user_id,max_uses,uses FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses',(w['id'],))]}

@app.post('/api/invitations/{code}/accept')
def accept_invite(code:str,request:Request):
    u=require_user(request); d=get_db(); x=d.execute('SELECT * FROM invitations WHERE code=? AND revoked=0',(code,)).fetchone()
    if not x or x['uses']>=x['max_uses'] or (x['invited_user_id'] and x['uses']>=1) or (x['expires_at'] and float(x['expires_at'])<=datetime.now(timezone.utc).timestamp()): raise HTTPException(404)
    if x['invited_user_id'] and x['invited_user_id']!=u['id']: raise HTTPException(403)
    d.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(code,)); d.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member')); d.commit()
    return {'workspace':wjson(d.execute('SELECT * FROM workspaces WHERE id=?',(x['workspace_id'],)).fetchone())}

@app.delete('/api/workspaces/{slug}/invitations/{code}')
def revoke_invite(slug:str,code:str,request:Request):
    u=require_user(request); w=wsrow(slug)
    if role(w['id'],u['id']) not in ('owner','admin'): raise HTTPException(403)
    d=get_db(); x=d.execute('SELECT * FROM invitations WHERE code=? AND workspace_id=?',(code,w['id'])).fetchone()
    if not x: raise HTTPException(404)
    d.execute('UPDATE invitations SET revoked=1 WHERE code=?',(code,)); d.commit(); return {'deleted':True}

@app.get('/api/search')
def search(request:Request):
    u=require_user(request); q=request.query_params.get('q',''); d=get_db()
    try: lim=int(request.query_params.get('limit','50'))
    except: raise HTTPException(400)
    if lim<1 or lim>200: raise HTTPException(400)
    wid=request.query_params.get('workspace_id'); cur=request.query_params.get('cursor')
    args=[u['id'],'%'+q+'%']; extra=''
    if wid: extra+=' AND c.workspace_id=?'; args.append(int(wid))
    if cur:
        try: extra+=' AND m.id<?'; args.append(int(cur))
        except: raise HTTPException(400)
    rows=d.execute('SELECT m.* FROM messages m JOIN channels c ON c.id=m.channel_id JOIN channel_members cm ON cm.channel_id=c.id AND cm.user_id=? WHERE m.deleted=0 AND m.body LIKE ?'+extra+' ORDER BY m.id DESC LIMIT ?',args+[lim]).fetchall()
    more=d.execute('SELECT 1 FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id<? AND m.deleted=0 AND m.body LIKE ?',(rows[-1]['id'],'%'+q+'%')).fetchone() if rows else None
    return {'results':[msgjson(x,d) for x in rows],'next_cursor':str(rows[-1]['id']) if rows and (len(rows)==lim or more) else None}

@app.post('/api/messages/{mid}/pin')
def pin(mid:int,request:Request):
    u=require_user(request); d=get_db(); m=d.execute('SELECT m.*,c.is_archived FROM messages m JOIN channels c ON c.id=m.channel_id WHERE m.id=?',(mid,)).fetchone()
    if not m: raise HTTPException(404)
    if m['is_archived']: raise HTTPException(423)
    if not d.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(m['channel_id'],u['id'])).fetchone(): raise HTTPException(403)
    d.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,u['id'],now())); d.commit()
    x=d.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone()
    return {'pin':{'message_id':x['message_id'],'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']}}

@app.delete('/api/messages/{mid}/pin')
def unpin(mid:int,request:Request):
    u=require_user(request); d=get_db(); d.execute('DELETE FROM pins WHERE message_id=?',(mid,)); d.commit(); return {'unpinned':True}

@app.get('/api/channels/{cid}/pins')
def get_pins(cid:int,request:Request):
    u=require_user(request); d=get_db(); c=d.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not c: raise HTTPException(404)
    if not d.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    try: lim=int(request.query_params.get('limit','50'))
    except: raise HTTPException(400)
    if lim<1 or lim>200: raise HTTPException(400)
    cur=request.query_params.get('cursor'); args=[cid]; extra=''
    if cur:
        try: extra=' AND p.message_id<?'; args.append(int(cur))
        except: raise HTTPException(400)
    rows=d.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=?'+extra+' ORDER BY p.message_id DESC LIMIT ?',args+[lim]).fetchall()
    more=d.execute('SELECT 1 FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND p.message_id<?',(cid,rows[-1]['message_id'])).fetchone() if rows else None
    return {'pins':[{'message':msgjson(x,d),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows],'next_cursor':str(rows[-1]['message_id']) if rows and (len(rows)==lim or more) else None}

@app.post('/api/messages/{mid}/reactions')
async def add_reaction(mid:int,request:Request):
    u=require_user(request); d=get_db(); m=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m: raise HTTPException(404)
    if not d.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(m['channel_id'],u['id'])).fetchone(): raise HTTPException(403)
    x=await request.json(); emoji=x.get('emoji')
    if not emoji or len(emoji)>32: raise HTTPException(400)
    d.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],emoji)); d.commit()
    return {'reactions':msgjson(d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(),d)['reactions']}

@app.delete('/api/messages/{mid}/reactions/{emoji}')
def remove_reaction(mid:int,emoji:str,request:Request):
    u=require_user(request); d=get_db(); m=d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not m: raise HTTPException(404)
    d.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)); d.commit()
    return {'reactions':msgjson(d.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(),d)['reactions']}

@app.post('/api/dms')
async def create_dm(request:Request):
    u=require_user(request); x=await request.json(); other=x.get('user_id')
    if other is None: raise HTTPException(400)
    d=get_db(); z=d.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
    if not z: raise HTTPException(404)
    if other==u['id']: raise HTTPException(400)
    a,b=sorted((u['id'],other)); name=f'dm-{a}-{b}'
    c=d.execute('SELECT id FROM channels WHERE name=? AND is_dm=1',(name,)).fetchone()
    if not c:
        q=d.execute('INSERT INTO channels(workspace_id,name,is_dm,is_private) VALUES(?,?,1,1)',(0,name)); cid=q.lastrowid
        d.execute('INSERT INTO channel_members VALUES(?,?)',(cid,a)); d.execute('INSERT INTO channel_members VALUES(?,?)',(cid,b)); d.commit()
    else: cid=c['id']
    return {'channel_id':cid}

def file_json(f):
    return {'id':f['id'],'uploader_id':f['uploader_id'],'filename':f['filename'],'content_type':f['content_type'],'size':f['size'],'created_at':f['created_at']}

@app.post('/api/files',status_code=201)
async def upload_file(request:Request):
    u=require_user(request)
    form=await request.form()
    f=form.get('file')
    if f is None or not hasattr(f,'read'): raise HTTPException(400,'file required')
    data=await f.read()
    if len(data)>10*1024*1024: raise HTTPException(413,'file too large')
    d=get_db(); q=d.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],f.filename or 'file',f.content_type or 'application/octet-stream',len(data),now(),data)); d.commit()
    return {'file':file_json(d.execute('SELECT * FROM files WHERE id=?',(q.lastrowid,)).fetchone())}

@app.get('/api/files/{fid}')
def get_file(fid:int,request:Request):
    require_user(request); d=get_db(); f=d.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
    if not f: raise HTTPException(404)
    return {'file':file_json(f)}

@app.get('/api/files/{fid}/download')
def download_file(fid:int,request:Request):
    from fastapi.responses import Response
    require_user(request); d=get_db(); f=d.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
    if not f: raise HTTPException(404)
    return Response(bytes(f['data']),media_type=f['content_type'],headers={'Content-Disposition':f'attachment; filename="{f["filename"]}"'})

def group_json(g):
    return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'] or '[]')}

@app.get('/api/workspaces/{slug}/groups')
def get_groups(slug:str,request:Request):
    u=require_user(request); w=wsrow(slug)
    if not role(w['id'],u['id']): raise HTTPException(403)
    d=get_db()
    return {'groups':[group_json(x) for x in d.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],))]}

@app.post('/api/workspaces/{slug}/groups',status_code=201)
async def create_group(slug:str,request:Request):
    u=require_user(request); w=wsrow(slug)
    if not role(w['id'],u['id']): raise HTTPException(403)
    x=await request.json(); handle=x.get('handle',''); name=x.get('name',''); ids=x.get('user_ids',[])
    if not re.fullmatch(r'[a-z0-9-]{2,32}',handle) or not name or not isinstance(ids,list): raise HTTPException(400)
    d=get_db()
    for uid in ids:
        if not d.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone(): raise HTTPException(404)
    try:
        q=d.execute('INSERT INTO groups(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],handle,name,u['id'],json.dumps(list(dict.fromkeys(ids)))))
        d.commit()
    except sqlite3.IntegrityError: raise HTTPException(409)
    return {'group':group_json(d.execute('SELECT * FROM groups WHERE id=?',(q.lastrowid,)).fetchone())}

@app.patch('/api/workspaces/{slug}/groups/{handle}')
async def patch_group(slug:str,handle:str,request:Request):
    u=require_user(request); w=wsrow(slug); d=get_db(); g=d.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
    if not g: raise HTTPException(404)
    if g['creator_id']!=u['id'] and w['owner_id']!=u['id']: raise HTTPException(403)
    x=await request.json(); ids=x.get('user_ids',json.loads(g['user_ids'] or '[]')); name=x.get('name',g['name'])
    if not isinstance(ids,list): raise HTTPException(400)
    d.execute('UPDATE groups SET name=?,user_ids=? WHERE id=?',(name,json.dumps(list(dict.fromkeys(ids))),g['id'])); d.commit()
    return {'group':group_json(d.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone())}

@app.delete('/api/workspaces/{slug}/groups/{handle}')
def delete_group(slug:str,handle:str,request:Request):
    u=require_user(request); w=wsrow(slug); d=get_db(); g=d.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone()
    if not g: return {'deleted':True}
    if g['creator_id']!=u['id'] and w['owner_id']!=u['id']: raise HTTPException(403)
    d.execute('DELETE FROM groups WHERE id=?',(g['id'],)); d.commit(); return {'deleted':True}

@app.get('/api/channels/{cid}/read')
def get_read(cid:int,request:Request):
    u=require_user(request); d=get_db()
    if not d.execute('SELECT 1 FROM channels WHERE id=?',(cid,)).fetchone(): raise HTTPException(404)
    if not d.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone(): raise HTTPException(403)
    x=d.execute('SELECT * FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
    last=x['last_read_event_id'] if x else 0
    mc=d.execute('SELECT count(*) n FROM message_mentions mm JOIN messages m ON m.id=mm.message_id WHERE mm.user_id=? AND m.channel_id=? AND m.id>? AND m.deleted=0',(u['id'],cid,last)).fetchone()['n']
    state={'channel_id':cid,'last_read_event_id':last,'unread_count':0,'mention_count':mc}
    return {'read_state':state}

@app.post('/api/channels/{cid}/read')
async def set_read(cid:int,request:Request):
    u=require_user(request); d=get_db()
    if not d.execute('SELECT 1 FROM channels WHERE id=?',(cid,)).fetchone(): raise HTTPException(404)
    x=await request.json(); eid=int(x.get('last_read_event_id',0)); old=d.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
    eid=max(eid,old['last_read_event_id'] if old else 0)
    d.execute('INSERT OR REPLACE INTO read_states VALUES(?,?,?)',(u['id'],cid,eid)); d.commit()
    return {'read_state':{'channel_id':cid,'last_read_event_id':eid,'unread_count':0,'mention_count':0}}

@app.get('/api/users/{uid}')
def get_user(uid:int,request:Request):
    require_user(request); d=get_db(); u=d.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    if not u: raise HTTPException(404)
    return {'user':user_to_json(u)}

@app.patch('/api/users/me')
async def patch_me(request:Request):
    u=require_user(request); x=await request.json()
    allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
    if any(k not in allowed for k in x): raise HTTPException(400)
    if 'timezone' in x:
        try: ZoneInfo(x['timezone'])
        except Exception: raise HTTPException(400)
    limits={'display_name':80,'avatar_url':1024,'status_text':100,'status_emoji':32}
    for k,n in limits.items():
        if k in x and (not isinstance(x[k],str) or len(x[k])>n): raise HTTPException(400)
    d=get_db()
    for k,v in x.items(): d.execute(f'UPDATE users SET {k}=? WHERE id=?',(v,u['id']))
    d.commit()
    result=user_to_json(d.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone())
    d.execute('INSERT INTO profile_events(user_json) VALUES(?)',(json.dumps(result),)); d.commit()
    for client in list(ws_clients):
        try: await client.send_json({'type':'user.updated','user':result})
        except Exception: ws_clients.discard(client)
    return {'user':result}
