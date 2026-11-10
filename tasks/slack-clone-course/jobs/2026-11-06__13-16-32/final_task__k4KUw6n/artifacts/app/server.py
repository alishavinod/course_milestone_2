import os, re, json, sqlite3, secrets, hashlib, mimetypes, asyncio
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

DB="/app/data/huddle.sqlite"; BLOB="/app/data/files"
Path(BLOB).mkdir(exist_ok=True)
app=FastAPI()
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])
con=sqlite3.connect(DB,check_same_thread=False,timeout=30); con.row_factory=sqlite3.Row
if os.getenv("NODE_ID","0") == "0":
    try: con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError: pass
def init():
    con.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '',mention_count INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER,mentions TEXT DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT,attached INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER);
    CREATE TABLE IF NOT EXISTS group_users(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
    CREATE TABLE IF NOT EXISTS reads(channel_id INTEGER,user_id INTEGER,last_read INTEGER DEFAULT 0,mention_count INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER,channel_id INTEGER,node_id INTEGER,updated_at REAL,PRIMARY KEY(user_id,node_id));
    CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT);
    CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER UNIQUE);
    """); con.commit()
init()
connections={}
def now(): return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def err(code,msg): raise HTTPException(code,msg)
def user_obj(r):
    return {k:r[k] for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji")}
def current(req):
    h=req.headers.get("authorization","")
    if not h.startswith("Bearer "): err(401,"authentication required")
    r=con.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(h[7:],)).fetchone()
    if not r: err(401,"invalid token")
    return r
async def body(req):
    try: return await req.json()
    except: err(400,"invalid JSON")
def require_fields(d,*keys):
    if any(k not in d for k in keys): err(400,"missing field")
def channel_row(cid):
    r=con.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not r: err(404,"channel not found")
    return r
def is_member(cid,uid):
    return con.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,uid)).fetchone() is not None
def ws_for(cid): return con.execute("SELECT * FROM workspaces WHERE id=(SELECT workspace_id FROM channels WHERE id=?)",(cid,)).fetchone()
def ensure_access(cid,u):
    c=channel_row(cid); w=ws_for(cid)
    if not is_member(cid,u["id"]): err(403,"not a channel member")
    return c,w
def add_member(cid,uid):
    con.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(cid,uid)); con.commit()
@app.get("/api/health")
def health(): return {"status":"ok","node_id":int(os.getenv("NODE_ID","0"))}
@app.post("/api/auth/register",status_code=201)
async def register(req:Request):
    d=await body(req); require_fields(d,"username","password")
    if not re.fullmatch(r"[A-Za-z0-9_]+",str(d["username"])) or len(d["password"])<8: err(400,"invalid registration")
    try:
        cur=con.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(d["username"],hashlib.sha256(d["password"].encode()).hexdigest(),d.get("display_name") or d["username"])); con.commit()
    except sqlite3.IntegrityError: err(409,"username exists")
    uid=cur.lastrowid; tok=secrets.token_urlsafe(24); con.execute("INSERT INTO tokens VALUES(?,?)",(tok,uid)); con.commit()
    return {"user":user_obj(con.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()),"token":tok}
@app.post("/api/auth/login")
async def login(req:Request):
    d=await body(req); r=con.execute("SELECT * FROM users WHERE username=? AND password=?",(d.get("username"),hashlib.sha256(str(d.get("password","")).encode()).hexdigest())).fetchone()
    if not r: err(401,"wrong credentials")
    tok=secrets.token_urlsafe(24); con.execute("INSERT INTO tokens VALUES(?,?)",(tok,r["id"])); con.commit(); return {"user":user_obj(r),"token":tok}
@app.get("/api/auth/me")
async def me(req:Request): return {"user":user_obj(current(req))}
@app.get("/api/users/{uid}")
async def get_user(uid:int,req:Request):
    current(req); r=con.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
    if not r: err(404,"user not found")
    return {"user":user_obj(r)}
@app.patch("/api/users/me")
async def patch_me(req:Request):
    u=current(req); d=await body(req)
    allowed={"display_name":80,"status_text":100,"status_emoji":32,"avatar_url":1024}
    if "timezone" in d:
        try: from zoneinfo import ZoneInfo; ZoneInfo(d["timezone"])
        except: err(400,"invalid timezone")
    vals=[]; args=[]
    for k,n in allowed.items():
        if k in d:
            if not isinstance(d[k],str) or len(d[k])>n: err(400,"field too long")
            vals.append(k+"=?"); args.append(d[k])
    if "timezone" in d: vals.append("timezone=?"); args.append(d["timezone"])
    if vals: con.execute("UPDATE users SET "+",".join(vals)+" WHERE id=?",(*args,u["id"])); con.commit()
    r=con.execute("SELECT * FROM users WHERE id=?",(u["id"],)).fetchone()
    con.execute("INSERT INTO global_events(kind,payload) VALUES(?,?)",("user.updated",json.dumps({"type":"user.updated","user":user_obj(r)})))
    con.commit()
    return {"user":user_obj(r)}
@app.post("/api/workspaces",status_code=201)
async def create_ws(req:Request):
    u=current(req); d=await body(req); require_fields(d,"slug","name")
    if not re.fullmatch(r"[a-z0-9-]{2,32}",d["slug"]): err(400,"invalid slug")
    try:
        x=con.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(d["slug"],d["name"],u["id"])); wid=x.lastrowid
        con.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner"))
        c=con.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")); cid=c.lastrowid
        con.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"])); con.execute("INSERT INTO seq VALUES(?,0)",(cid,)); con.commit()
    except sqlite3.IntegrityError: err(409,"workspace exists")
    return {"workspace":ws_json(wid),"general_channel":channel_json(cid)}
def ws_json(wid):
    r=con.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone()
    return {k:r[k] for k in ("id","slug","name","owner_id","join_mode")}
def channel_json(cid):
    r=channel_row(cid)
    return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"],"is_archived":bool(r["is_archived"])}
@app.get("/api/workspaces")
async def list_ws(req:Request):
    u=current(req); rows=con.execute("SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall()
    return {"workspaces":[ws_json(r["id"]) for r in rows]}
@app.get("/api/workspaces/{slug}")
async def get_ws(slug:str,req:Request):
    u=current(req); w=con.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w: err(404,"workspace not found")
    if not con.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone(): err(403,"not member")
    cs=con.execute("SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)",(w["id"],req.query_params.get("include_archived") in ("1","true"))).fetchall()
    return {"workspace":ws_json(w["id"]),"channels":[channel_json(c["id"]) for c in cs],"read_state":[]}
@app.get("/api/workspaces/{slug}/members")
async def members(slug:str,req:Request):
    u=current(req); w=con.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w: err(404,"workspace not found")
    if not con.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone(): err(403,"not member")
    return {"members":[{"user_id":r["user_id"],"role":r["role"]} for r in con.execute("SELECT user_id,role FROM members WHERE workspace_id=?",(w["id"],))]}
@app.post("/api/workspaces/{slug}/channels",status_code=201)
async def create_channel(slug:str,req:Request):
    u=current(req); d=await body(req); require_fields(d,"name")
    w=con.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w: err(404,"workspace not found")
    mr=con.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
    if not mr: err(403,"not member")
    if not re.fullmatch(r"[a-z0-9-]{1,32}",d["name"]): err(400,"invalid name")
    if d.get("is_private") and mr["role"] not in ("owner","admin"): err(403,"forbidden")
    if len(d.get("topic",""))>250: err(400,"topic too long")
    try:
        x=con.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],d["name"],int(bool(d.get("is_private"))),d.get("topic",""))); cid=x.lastrowid
        con.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"],)); con.execute("INSERT INTO seq VALUES(?,0)",(cid,)); con.commit()
    except sqlite3.IntegrityError: err(409,"channel exists")
    return {"channel":channel_json(cid)}
@app.post("/api/channels/{cid}/join")
async def join(cid:int,req:Request):
    u=current(req); c=channel_row(cid); w=ws_for(cid)
    if c["is_private"]: err(403,"private channel")
    con.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)",(w["id"],u["id"],"member"))
    add_member(cid,u["id"]); return {"channel":channel_json(cid)}
@app.get("/api/channels/{cid}/members")
async def channel_members(cid:int,req:Request):
    u=current(req); ensure_access(cid,u)
    rows=con.execute("SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?",(cid,)).fetchall()
    return {"members":[user_obj(r) for r in rows]}
@app.post("/api/channels/{cid}/leave")
async def leave(cid:int,req:Request):
    u=current(req); channel_row(cid); con.execute("DELETE FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])); con.commit(); return {"channel":channel_json(cid)}
def next_event(cid):
    con.execute("UPDATE seq SET n=n+1 WHERE channel_id=?",(cid,)); con.commit()
    return con.execute("SELECT n FROM seq WHERE channel_id=?",(cid,)).fetchone()["n"]
def msg_json(r):
    au=con.execute("SELECT * FROM users WHERE id=?",(r["author_id"],)).fetchone()
    rs=[]
    for x in con.execute("SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji",(r["id"],)):
        ids=[z["user_id"] for z in con.execute("SELECT user_id FROM reactions WHERE message_id=? AND emoji=?",(r["id"],x["emoji"]))]
        rs.append({"emoji":x["emoji"],"count":x["n"],"user_ids":ids})
    fs=[file_json(x) for x in con.execute("SELECT * FROM files WHERE attached=?",(r["id"],))]
    return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":user_obj(au),"body":r["body"],"parent_id":r["parent_id"],"reply_count":con.execute("SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0",(r["id"],)).fetchone()["n"],"created_at":r["created_at"],"edited_at":r["edited_at"],"files":fs,"reactions":rs,"mentions":json.loads(r["mentions"] or "[]")}
def accessible(cid,uid):
    if not is_member(cid,uid): err(403,"not a member")
@app.post("/api/channels/{cid}/messages",status_code=201)
async def post_message(cid:int,req:Request):
    u=current(req); d=await body(req); c,w=ensure_access(cid,u)
    if c["is_archived"] and d.get("body") != "/unarchive": err(423,"archived")
    text=d.get("body")
    if not isinstance(text,str) or not text.strip(): err(400,"empty message")
    if text.startswith("/") and not text.startswith("//"):
        parts=text.split(" ",1); cmd=parts[0]; rest=parts[1] if len(parts)>1 else ""
        if cmd=="/me" and not rest: err(400,"missing text")
        if cmd=="/shrug": text=rest+" ¯\\_(ツ)_/¯" if rest else "¯\\_(ツ)_/¯"
        elif cmd=="/me": text=text
        elif cmd=="/topic":
            if not rest or len(rest)>250: err(400,"bad topic")
            con.execute("UPDATE channels SET topic=? WHERE id=?",(rest,cid)); con.commit(); return {"message":None,"channel":channel_json(cid)}
        elif cmd=="/invite":
            m=re.match(r"@([A-Za-z0-9_]+)",rest); target=con.execute("SELECT * FROM users WHERE username=?",(m.group(1),)).fetchone() if m else None
            if not target: err(400,"unknown user")
            con.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(cid,target["id"],)); con.commit(); return {"message":None,"invited_user":user_obj(target)}
        elif cmd in ("/archive","/unarchive"):
            con.execute("UPDATE channels SET is_archived=? WHERE id=?",(int(cmd=="/archive"),cid)); con.commit(); return {"message":None,"channel":channel_json(cid)}
        else: err(400,"unknown command")
    files=d.get("file_ids",[])
    if files:
        for fid in files:
            f=con.execute("SELECT * FROM files WHERE id=?",(fid,)).fetchone()
            if not f or f["uploader_id"]!=u["id"] or f["attached"]: err(400,"file not attachable")
    eid=next_event(cid); mention_ids=resolve_mentions(cid,u,text); cur=con.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id,mentions) VALUES(?,?,?,?,?,?,?)",(cid,u["id"],text,d.get("parent_id"),now(),eid,json.dumps(mention_ids))); mid=cur.lastrowid
    for fid in files: con.execute("UPDATE files SET attached=? WHERE id=?",(mid,fid))
    con.commit(); r=con.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    await broadcast(cid, {"type":"message.created","event_id":eid,"channel_id":cid,"message":msg_json(r)})
    return {"message":msg_json(r)}
@app.get("/api/channels/{cid}/messages")
async def list_messages(cid:int,req:Request):
    u=current(req); accessible(cid,u["id"]); rows=con.execute("SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT 200",(cid,)).fetchall()
    return {"messages":[msg_json(r) for r in rows],"next_cursor":None}
@app.patch("/api/channels/{cid}")
async def patch_channel(cid:int,req:Request):
    u=current(req); c,w=ensure_access(cid,u); d=await body(req)
    if "topic" in d and (not isinstance(d["topic"],str) or len(d["topic"])>250): err(400,"bad topic")
    if "topic" in d: con.execute("UPDATE channels SET topic=? WHERE id=?",(d["topic"],cid)); con.commit()
    return {"channel":channel_json(cid)}
@app.patch("/api/messages/{mid}")
async def edit_message(mid:int,req:Request):
    u=current(req); d=await body(req); r=con.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    if not r: err(404,"message not found")
    if r["deleted"]: err(409,"deleted")
    if r["author_id"]!=u["id"]: err(403,"forbidden")
    if not isinstance(d.get("body"),str) or not d["body"].strip(): err(400,"empty")
    con.execute("UPDATE messages SET body=?,edited_at=? WHERE id=?",(d["body"],now(),mid)); con.commit()
    return {"message":msg_json(con.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone())}
@app.delete("/api/messages/{mid}")
async def delete_message(mid:int,req:Request):
    u=current(req); r=con.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    if not r: return {"deleted":True}
    w=ws_for(r["channel_id"])
    role=con.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
    if r["author_id"]!=u["id"] and (not role or role["role"]!="owner"): err(403,"forbidden")
    con.execute("UPDATE messages SET deleted=1 WHERE id=?",(mid,)); con.commit(); return {"deleted":True}
@app.get("/api/messages/{mid}/replies")
async def replies(mid:int,req:Request):
    u=current(req); p=con.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    if not p: err(404,"message not found")
    accessible(p["channel_id"],u["id"])
    rows=con.execute("SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id",(mid,)).fetchall()
    return {"replies":[msg_json(x) for x in rows],"next_cursor":None}
@app.post("/api/messages/{mid}/reactions")
async def react(mid:int,req:Request):
    u=current(req); r=con.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    if not r: err(404,"message not found")
    accessible(r["channel_id"],u["id"]); d=await body(req)
    if not d.get("emoji"): err(400,"emoji required")
    before=con.execute("SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(mid,u["id"],d["emoji"])).fetchone()
    con.execute("INSERT OR IGNORE INTO reactions VALUES(?,?,?)",(mid,u["id"],d["emoji"]))
    eid=next_event(r["channel_id"]) if not before else con.execute("SELECT event_id FROM messages WHERE id=?",(mid,)).fetchone()["event_id"]
    con.commit()
    reactions=msg_json(con.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone())["reactions"]
    if not before: await broadcast(r["channel_id"], {"type":"reaction.added","event_id":eid,"channel_id":r["channel_id"],"message_id":mid,"reactions":reactions})
    return {"reactions":reactions}
@app.delete("/api/messages/{mid}/reactions/{emoji}")
async def unreact(mid:int,emoji:str,req:Request):
    u=current(req); r=con.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
    if not r: err(404,"message not found")
    accessible(r["channel_id"],u["id"])
    existed=con.execute("SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(mid,u["id"],emoji)).fetchone()
    con.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(mid,u["id"],emoji))
    eid=next_event(r["channel_id"]) if existed else None
    con.commit()
    reactions=msg_json(con.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone())["reactions"]
    if existed: await broadcast(r["channel_id"], {"type":"reaction.removed","event_id":eid,"channel_id":r["channel_id"],"message_id":mid,"reactions":reactions})
    return {"reactions":reactions}
@app.post("/api/dms")
async def dm(req:Request):
    u=current(req); d=await body(req); require_fields(d,"user_id")
    if not con.execute("SELECT 1 FROM users WHERE id=?",(d["user_id"],)).fetchone(): err(404,"user not found")
    a,b=sorted((u["id"],int(d["user_id"])))
    x=con.execute("SELECT channel_id FROM dms WHERE a=? AND b=?",(a,b)).fetchone()
    if x: return {"channel_id":x["channel_id"]}
    cur=con.execute("INSERT INTO channels(workspace_id,name,is_dm) VALUES(NULL,?,1)",(f"dm-{a}-{b}",)); cid=cur.lastrowid
    con.execute("INSERT INTO dms VALUES(?,?,?)",(a,b,cid)); con.execute("INSERT INTO channel_members VALUES(?,?),(?,?)",(cid,a,cid,b)); con.execute("INSERT INTO seq VALUES(?,0)",(cid,)); con.commit()
    return {"channel_id":cid}
def file_json(r):
    return {k:r[k] for k in ("id","uploader_id","filename","content_type","size","created_at")}
@app.post("/api/files",status_code=201)
async def upload(req:Request,file:UploadFile|None=File(None)):
    u=current(req)
    if file is None: err(400,"file required")
    data=await file.read()
    if len(data)>10485760: err(413,"file too large")
    cur=con.execute("INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)",(u["id"],file.filename or "file",file.content_type or "application/octet-stream",len(data),now(),"")); fid=cur.lastrowid
    path=f"{BLOB}/{fid}"; open(path,"wb").write(data); con.execute("UPDATE files SET path=? WHERE id=?",(path,fid)); con.commit()
    return {"file":file_json(con.execute("SELECT * FROM files WHERE id=?",(fid,)).fetchone())}
@app.get("/api/files/{fid}")
async def file_detail(fid:int,req:Request):
    current(req); r=con.execute("SELECT * FROM files WHERE id=?",(fid,)).fetchone()
    if not r: err(404,"file not found")
    return {"file":file_json(r)}
@app.get("/api/files/{fid}/download")
async def file_download(fid:int,req:Request):
    current(req); r=con.execute("SELECT * FROM files WHERE id=?",(fid,)).fetchone()
    if not r: err(404,"file not found")
    return FileResponse(r["path"],media_type=r["content_type"],headers={"Content-Disposition":f'attachment; filename="{r["filename"]}"'})
@app.post("/api/workspaces/{slug}/groups",status_code=201)
async def group_create(slug:str,req:Request):
    u=current(req); d=await body(req); require_fields(d,"handle","name","user_ids")
    w=con.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w: err(404,"workspace")
    mr=con.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
    if not mr: err(403,"forbidden")
    if con.execute("SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?",(w["id"],d["handle"])).fetchone(): err(409,"duplicate")
    cur=con.execute("INSERT INTO groups_(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)",(w["id"],d["handle"],d["name"],u["id"])); gid=cur.lastrowid
    for uid in d["user_ids"]: con.execute("INSERT OR IGNORE INTO group_users VALUES(?,?)",(gid,uid))
    con.commit(); return {"group":group_json(gid)}
def group_json(gid):
    r=con.execute("SELECT * FROM groups_ WHERE id=?",(gid,)).fetchone()
    return {"id":r["id"],"handle":r["handle"],"name":r["name"],"member_user_ids":[x["user_id"] for x in con.execute("SELECT user_id FROM group_users WHERE group_id=?",(gid,))]}
def get_group(w,h):
    r=con.execute("SELECT * FROM groups_ WHERE workspace_id=? AND handle=?",(w["id"],h)).fetchone()
    if not r: err(404,"group")
    return r
@app.get("/api/workspaces/{slug}/groups")
async def group_list(slug:str,req:Request):
    u=current(req); w=con.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w: err(404,"workspace")
    if not con.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone(): err(403,"forbidden")
    return {"groups":[group_json(x["id"]) for x in con.execute("SELECT id FROM groups_ WHERE workspace_id=?",(w["id"],))]}
@app.patch("/api/workspaces/{slug}/groups/{handle}")
async def group_patch(slug:str,handle:str,req:Request):
    u=current(req); d=await body(req); w=con.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w: err(404,"workspace")
    g=get_group(w,handle); role=con.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
    if g["creator_id"]!=u["id"] and (not role or role["role"]!="owner"): err(403,"forbidden")
    con.execute("UPDATE groups_ SET name=? WHERE id=?",(d.get("name",g["name"]),g["id"]))
    if "user_ids" in d:
        con.execute("DELETE FROM group_users WHERE group_id=?",(g["id"],))
        for x in d["user_ids"]: con.execute("INSERT OR IGNORE INTO group_users VALUES(?,?)",(g["id"],x))
    con.commit(); return {"group":group_json(g["id"])}
@app.delete("/api/workspaces/{slug}/groups/{handle}")
async def group_delete(slug:str,handle:str,req:Request):
    u=current(req); w=con.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w: err(404,"workspace")
    g=con.execute("SELECT * FROM groups_ WHERE workspace_id=? AND handle=?",(w["id"],handle)).fetchone()
    if not g: return {"deleted":True}
    role=con.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
    if g["creator_id"]!=u["id"] and (not role or role["role"]!="owner"): err(403,"forbidden")
    con.execute("DELETE FROM groups_ WHERE id=?",(g["id"],)); con.commit(); return {"deleted":True}
async def broadcast(cid, event):
    dead=[]
    for key, info in list(connections.items()):
        if info.get("channel")==cid:
            try: await info["ws"].send_json(event)
            except Exception: dead.append(key)
    for key in dead: connections.pop(key,None)

def resolve_mentions(cid, author, text):
    c=channel_row(cid); w=ws_for(cid); found=set()
    if not w: return []
    members=[x["user_id"] for x in con.execute("SELECT user_id FROM channel_members WHERE channel_id=?",(cid,))]
    for name in re.findall(r"(?<![\w@])@([A-Za-z0-9_-]+)",text):
        if name in ("channel","here"):
            if name=="channel": found.update(members)
            else: found.update([x["user_id"] for x in con.execute("SELECT DISTINCT user_id FROM ws_presence WHERE updated_at> ?",(__import__("time").time()-60,)) if x["user_id"] in members])
            continue
        g=con.execute("SELECT id FROM groups_ WHERE workspace_id=? AND handle=?",(w["id"],name)).fetchone()
        if g:
            found.update(x["user_id"] for x in con.execute("SELECT user_id FROM group_users WHERE group_id=?",(g["id"],)) if x["user_id"] in members)
        else:
            x=con.execute("SELECT id FROM users WHERE username=?",(name,)).fetchone()
            if x and x["id"] in members: found.add(x["id"])
    found.discard(author["id"])
    for uid in found:
        con.execute("INSERT OR IGNORE INTO reads(channel_id,user_id,last_read,mention_count) VALUES(?,?,0,0)",(cid,uid))
        con.execute("UPDATE reads SET mention_count=mention_count+1 WHERE channel_id=? AND user_id=?",(cid,uid))
    con.commit()
    return list(found)
@app.get("/api/channels/{cid}/read")
async def read_get(cid:int,req:Request):
    u=current(req); accessible(cid,u["id"]); r=con.execute("SELECT * FROM reads WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone()
    last=r["last_read"] if r else 0; mentions=r["mention_count"] if r else 0
    head=con.execute("SELECT n FROM seq WHERE channel_id=?",(cid,)).fetchone()
    unread=con.execute("SELECT COUNT(*) n FROM messages WHERE channel_id=? AND event_id>? AND deleted=0",(cid,last)).fetchone()["n"]
    return {"read_state":{"channel_id":cid,"last_read_event_id":last,"unread_count":unread,"mention_count":mentions}}
@app.post("/api/channels/{cid}/read")
async def read_post(cid:int,req:Request):
    u=current(req); accessible(cid,u["id"]); d=await body(req); x=int(d.get("last_read_event_id",0))
    r=con.execute("SELECT last_read FROM reads WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone()
    if r: x=max(x,r["last_read"]); con.execute("UPDATE reads SET last_read=? WHERE channel_id=? AND user_id=?",(x,cid,u["id"]))
    else: con.execute("INSERT INTO reads(channel_id,user_id,last_read) VALUES(?,?,?)",(cid,u["id"],x))
    con.commit(); return await read_get(cid,req)
@app.get("/")
async def home():
    return HTMLResponse("""<!doctype html><html><head><title>Huddle</title></head><body><main id="app"><h1>Huddle</h1><p>Team chat</p><form data-testid="auth-form"><input name="username"><input name="password" type="password"><button data-testid="auth-submit" data-button-role="primary">Login</button></form></main></body></html>""")
@app.websocket("/api/ws")
async def websocket(ws:WebSocket):
    token=ws.query_params.get("token",""); r=con.execute("SELECT user_id FROM tokens WHERE token=?",(token,)).fetchone()
    if not r: await ws.close(code=4401); return
    await ws.accept(); key=id(ws); connections[key]={"ws":ws,"user_id":r["user_id"],"channel":None,"global_event":con.execute("SELECT COALESCE(MAX(id),0) n FROM global_events").fetchone()["n"]}
    con.execute("INSERT OR REPLACE INTO ws_presence(user_id,channel_id,node_id,updated_at) VALUES(?,?,?,?)",(r["user_id"],0,int(os.getenv("NODE_ID","0")),__import__("time").time())); con.commit()
    try:
        while True:
            try:
                raw=await asyncio.wait_for(ws.receive_text(),timeout=0.2)
                d=json.loads(raw); typ=d.get("type"); cid=int(d.get("channel_id",0))
            except asyncio.TimeoutError:
                typ=None; cid=0
            ge=con.execute("SELECT id,payload FROM global_events WHERE id>? ORDER BY id LIMIT 20",(connections[key].get("global_event",0),)).fetchall()
            for ev in ge:
                await ws.send_json(json.loads(ev["payload"]))
                connections[key]["global_event"]=ev["id"]
            if typ in ("subscribe","resume"):
                if not con.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,r["user_id"])).fetchone():
                    continue
                connections[key]["channel"]=cid
                head=con.execute("SELECT n FROM seq WHERE channel_id=?",(cid,)).fetchone()["n"]
                if typ=="resume":
                    since=int(d.get("since_event_id",0))
                    rows=con.execute("SELECT * FROM messages WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,since)).fetchall()
                    for m in rows:
                        await ws.send_json({"type":"message.created","event_id":m["event_id"],"channel_id":cid,"message":msg_json(m)})
                    await ws.send_json({"type":"resumed","channel_id":cid,"head_event_id":head})
                else: await ws.send_json({"type":"subscribed","channel_id":cid,"head_event_id":head})
    except (WebSocketDisconnect,Exception):
        pass
    finally:
        connections.pop(key,None)
        con.execute("DELETE FROM ws_presence WHERE user_id=? AND node_id=?",(r["user_id"],int(os.getenv("NODE_ID","0"))))
        con.commit()
@app.on_event("startup")
async def startup():
    pass
if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host="127.0.0.1",port=int(os.getenv("PORT","8000")))
