import os, json, time, uuid, sqlite3, asyncio, threading, hashlib, secrets
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from pydantic import BaseModel
import redis

NODE=int(os.environ.get("NODE_ID","0"))
DB="/app/data/huddle.db"
app=FastAPI()
lock=threading.RLock()
subs={}
wake=asyncio.Event()
loop=None

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def db():
    c=sqlite3.connect(DB,timeout=10,check_same_thread=False)
    c.row_factory=sqlite3.Row
    return c
def init():
    with lock:
        c=db()
        c.executescript("""PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
        CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
        CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
        CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_dm INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
        """); c.commit(); c.close()
init()

def user_obj(r):
    return {"id":r["id"],"username":r["username"],"display_name":r["display_name"],"timezone":"UTC","avatar_url":"","status_text":"","status_emoji":""}
def auth(req):
    h=req.headers.get("authorization","")
    if not h.startswith("Bearer "): raise HTTPException(401)
    with lock:
        r=db().execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(h[7:],)).fetchone()
    if not r: raise HTTPException(401)
    return r
def auth_token(t):
    with lock:
        return db().execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(t,)).fetchone()
def ch_obj(r):
    return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"] or "","is_archived":False}
def msg_obj(r):
    with lock:
        u=db().execute("SELECT * FROM users WHERE id=?",(r["author_id"],)).fetchone()
        replies=db().execute("SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0",(r["id"],)).fetchone()["n"]
    return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":user_obj(u),"body":r["body"],"parent_id":r["parent_id"],"reply_count":replies,"created_at":r["created_at"],"edited_at":r["edited_at"],"files":[],"reactions":[],"mentions":[]}
def publish(cid,eid):
    try:
        r=redis.Redis(host="127.0.0.1",port=6379,socket_connect_timeout=.15,socket_timeout=.15)
        r.publish("huddle",f"{cid}:{eid}")
    except Exception: pass
def emit(cid,typ,payload):
    with lock:
        c=db(); q=c.execute("SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?",(cid,)).fetchone()[0]
        c.execute("INSERT INTO events VALUES(?,?,?,?,?)",(cid,q,typ,json.dumps(payload),now())); c.commit(); c.close()
    publish(cid,q); wake.set(); return q
async def dispatcher():
    sent={}
    while True:
        await asyncio.sleep(.08)
        with lock:
            c=db()
            for cid, clients in list(subs.items()):
                for ws in list(clients):
                    key=id(ws); last=sent.get(key,0)
                    rows=c.execute("SELECT event_id,type,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,last)).fetchall()
                    for r in rows:
                        try:
                            await ws.send_text(json.dumps({"type":r["type"],"event_id":r["event_id"],"channel_id":cid,**json.loads(r["payload"])})); sent[key]=r["event_id"]
                        except Exception: clients.discard(ws)
            c.close()
async def redis_listener():
    while True:
        try:
            r=redis.Redis(host="127.0.0.1",port=6379,socket_connect_timeout=1)
            p=r.pubsub(); p.subscribe("huddle")
            for _ in p.listen():
                wake.set()
        except Exception: await asyncio.sleep(1)
@app.on_event("startup")
async def startup():
    global loop
    loop=asyncio.get_running_loop()
    asyncio.create_task(dispatcher()); asyncio.create_task(redis_listener())
@app.get("/api/health")
async def health(): return {"status":"ok","node_id":NODE}
@app.post("/api/auth/register")
async def register(x:dict):
    un=x.get("username",""); pw=x.get("password","")
    if not __import__("re").fullmatch(r"[A-Za-z0-9_]+",un) or len(pw)<8: raise HTTPException(400)
    with lock:
        c=db()
        if c.execute("SELECT 1 FROM users WHERE username=?",(un,)).fetchone(): raise HTTPException(409)
        c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(un,hashlib.sha256(pw.encode()).hexdigest(),x.get("display_name") or un)); uid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        tok=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(tok,uid)); c.commit(); u=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone(); c.close()
    return JSONResponse({"user":user_obj(u),"token":tok},201)
@app.post("/api/auth/login")
async def login(x:dict):
    with lock:
        c=db(); u=c.execute("SELECT * FROM users WHERE username=? AND password=?",(x.get("username"),hashlib.sha256(x.get("password","").encode()).hexdigest())).fetchone()
        if not u: raise HTTPException(401)
        t=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(t,u["id"])); c.commit(); c.close()
    return {"user":user_obj(u),"token":t}
@app.get("/api/auth/me")
async def me(request:Request): return {"user":user_obj(auth(request))}
@app.post("/api/workspaces")
async def workspace(x:dict,request:Request):
    u=auth(request); slug=x.get("slug","")
    if not __import__("re").fullmatch(r"[a-z0-9-]{2,32}",slug): raise HTTPException(400)
    with lock:
        c=db()
        if c.execute("SELECT 1 FROM workspaces WHERE slug=?",(slug,)).fetchone(): raise HTTPException(409)
        c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(slug,x.get("name",""),u["id"])); wid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner")); c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")); cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.execute("INSERT INTO cmembers VALUES(?,?)",(cid,u["id"])); c.commit()
        w=c.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close()
    return JSONResponse({"workspace":{"id":w["id"],"slug":w["slug"],"name":w["name"],"owner_id":w["owner_id"],"join_mode":"open"},"general_channel":ch_obj(ch)},201)
@app.get("/api/workspaces")
async def workspaces(request:Request):
    u=auth(request)
    with lock:
        c=db(); rows=c.execute("SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall(); c.close()
    return {"workspaces":[{"id":r["id"],"slug":r["slug"],"name":r["name"],"owner_id":r["owner_id"],"join_mode":"open"} for r in rows]}
@app.get("/api/workspaces/{slug}")
async def workspace_detail(slug:str,request:Request):
    u=auth(request)
    with lock:
        c=db(); w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
        if not w: raise HTTPException(404)
        member=c.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
        if not member: raise HTTPException(403)
        cs=c.execute("SELECT * FROM channels WHERE workspace_id=?",(w["id"],)).fetchall(); c.close()
    return {"workspace":{"id":w["id"],"slug":w["slug"],"name":w["name"],"owner_id":w["owner_id"],"join_mode":"open"},"channels":[ch_obj(x) for x in cs],"read_state":[]}
@app.post("/api/workspaces/{slug}/channels")
async def create_channel(slug:str,x:dict,request:Request):
    u=auth(request); name=x.get("name","")
    if not __import__("re").fullmatch(r"[a-z0-9-]{1,32}",name): raise HTTPException(400)
    with lock:
        c=db(); w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
        if not w: raise HTTPException(404)
        role=c.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
        if not role: raise HTTPException(403)
        if c.execute("SELECT 1 FROM channels WHERE workspace_id=? AND name=?",(w["id"],name)).fetchone(): raise HTTPException(409)
        c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],name,int(bool(x.get("is_private"))),x.get("topic",""))); cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO cmembers VALUES(?,?)",(cid,u["id"])); c.commit(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close()
    return JSONResponse({"channel":ch_obj(ch)},201)
@app.post("/api/channels/{cid}/join")
async def join(cid:int,request:Request):
    u=auth(request)
    with lock:
        c=db(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
        if not ch: raise HTTPException(404)
        if not c.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(ch["workspace_id"],u["id"])).fetchone():
            c.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)",(ch["workspace_id"],u["id"],"member"))
        c.execute("INSERT OR IGNORE INTO cmembers VALUES(?,?)",(cid,u["id"])); c.commit(); c.close()
    return {"channel":ch_obj(ch)}
@app.get("/api/channels/{cid}/messages")
async def messages(cid:int,request:Request):
    u=auth(request)
    with lock:
        c=db(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
        if not ch: raise HTTPException(404)
        if not c.execute("SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone(): raise HTTPException(403)
        rows=c.execute("SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200",(cid,)).fetchall(); c.close()
    return {"messages":[msg_obj(r) for r in rows],"next_cursor":None}
@app.post("/api/channels/{cid}/messages")
async def post_message(cid:int,x:dict,request:Request):
    u=auth(request); body=x.get("body","")
    if not isinstance(body,str) or not body.strip(): raise HTTPException(400)
    with lock:
        c=db(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
        if not ch: raise HTTPException(404)
        if not c.execute("SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone(): raise HTTPException(403)
        t=now(); c.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)",(cid,u["id"],body,x.get("parent_id"),t)); mid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.commit(); r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone(); c.close()
    m=msg_obj(r); emit(cid,"message.reply" if x.get("parent_id") else "message.created",{"message":m})
    return JSONResponse({"message":m},201)
@app.patch("/api/messages/{mid}")
async def edit_message(mid:int,x:dict,request:Request):
    u=auth(request); body=x.get("body","")
    if not body.strip(): raise HTTPException(400)
    with lock:
        c=db(); r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
        if not r: raise HTTPException(404)
        if r["author_id"]!=u["id"]: raise HTTPException(403)
        t=now(); c.execute("UPDATE messages SET body=?,edited_at=? WHERE id=?",(body,t,mid)); c.commit(); r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone(); c.close()
    m=msg_obj(r); emit(r["channel_id"],"message.updated",{"message":m}); return {"message":m}
@app.delete("/api/messages/{mid}")
async def delete_message(mid:int,request:Request):
    u=auth(request)
    with lock:
        c=db(); r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
        if not r: return {"deleted":True}
        if r["author_id"]!=u["id"]: raise HTTPException(403)
        c.execute("UPDATE messages SET deleted=1 WHERE id=?",(mid,)); c.commit(); c.close()
    emit(r["channel_id"],"message.deleted",{"message_id":mid}); return {"deleted":True}
@app.websocket("/api/ws")
async def ws_endpoint(ws:WebSocket):
    token=ws.query_params.get("token","")
    u=auth_token(token)
    if not u:
        await ws.close(code=4401); return
    await ws.accept()
    joined=set()
    try:
        while True:
            data=await ws.receive_json()
            typ=data.get("type"); cid=int(data.get("channel_id",0))
            if typ=="subscribe":
                with lock:
                    c=db(); head=c.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]; c.close()
                subs.setdefault(cid,set()).add(ws); joined.add(cid)
                await ws.send_json({"type":"subscribed","channel_id":cid,"head_event_id":head})
            elif typ=="resume":
                since=int(data.get("since_event_id",0))
                with lock:
                    c=db(); rows=c.execute("SELECT event_id,type,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,since)).fetchall()
                    head=c.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]; c.close()
                subs.setdefault(cid,set()).add(ws); joined.add(cid)
                for r in rows:
                    await ws.send_json({"type":r["type"],"event_id":r["event_id"],"channel_id":cid,**json.loads(r["payload"])})
                await ws.send_json({"type":"resumed","channel_id":cid,"head_event_id":head})
    except (WebSocketDisconnect,Exception):
        for cid in joined: subs.get(cid,set()).discard(ws)

@app.get("/")
async def index():
    return FileResponse("/app/static/index.html")
