import os, sqlite3, secrets, hashlib, asyncio, json, re, threading, time
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
import redis

DB="/app/data/chat.db"
NODE=int(os.environ.get("NODE_ID","0"))
PORT=int(os.environ.get("PORT","8000"))
os.makedirs("/app/data",exist_ok=True)
db_lock=threading.RLock()
subs=[]
subs_lock=threading.RLock()
wake=threading.Event()

def conn():
    c=sqlite3.connect(DB,timeout=10,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=10000")
    return c
def init():
    with db_lock:
        c=conn()
        c.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
        CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
        CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
        CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));""")
        c.commit(); c.close()
init()
app=FastAPI()

def redis_subscription():
    while True:
        try:
            r=redis.Redis(host="127.0.0.1",port=6379,socket_connect_timeout=1,socket_timeout=2)
            ps=r.pubsub()
            ps.subscribe("chat-events")
            for item in ps.listen():
                if item.get("type") == "message": wake.set()
        except Exception:
            time.sleep(.5)

threading.Thread(target=redis_subscription,daemon=True).start()

def now(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
def auth(request):
    h=request.headers.get("authorization","")
    if not h.startswith("Bearer "): raise HTTPException(401)
    t=h[7:]
    with db_lock:
        r=conn().execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(t,)).fetchone()
    if not r: raise HTTPException(401)
    return r
def user_obj(r): return {"id":r["id"],"username":r["username"],"display_name":r["display_name"],"timezone":"UTC","avatar_url":"","status_text":"","status_emoji":""}
def channel_obj(r):
    return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":False,"topic":r["topic"] or "","is_archived":False}
def message_obj(c,r):
    a=c.execute("SELECT * FROM users WHERE id=?",(r["author_id"],)).fetchone()
    replies=c.execute("SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0",(r["id"],)).fetchone()["n"]
    return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":user_obj(a),"body":r["body"],"parent_id":r["parent_id"],"reply_count":replies,"created_at":r["created_at"],"edited_at":r["edited_at"],"files":[],"reactions":[],"mentions":[]}
def publish(cid,eid):
    wake.set()
    try:
        rc=redis.Redis(host="127.0.0.1",port=6379,socket_connect_timeout=.15,socket_timeout=.15)
        rc.publish("chat-events",f"{cid}:{eid}"); rc.close()
    except Exception: pass
def emit(cid,typ,payload):
    with db_lock:
        c=conn(); c.execute("BEGIN IMMEDIATE")
        x=c.execute("SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]
        c.execute("INSERT INTO events VALUES(?,?,?,?)",(cid,x,typ,json.dumps(payload,separators=(",",":")))); c.commit(); c.close()
    publish(cid,x); return x
def member(c,cid,uid):
    return c.execute("""SELECT 1 FROM channel_members cm JOIN channels ch ON ch.id=cm.channel_id
        WHERE cm.channel_id=? AND cm.user_id=?""",(cid,uid)).fetchone() is not None

class AuthBody(BaseModel): username:str; password:str; display_name:str|None=None
class WsBody(BaseModel): slug:str; name:str
class ChBody(BaseModel): name:str; is_private:bool=False; topic:str=""
class MsgBody(BaseModel): body:str; parent_id:int|None=None

@app.get("/api/health")
def health(): return {"status":"ok","node_id":NODE}
@app.post("/api/auth/register",status_code=201)
def register(b:AuthBody):
    if not re.fullmatch(r"[A-Za-z0-9_]+",b.username) or len(b.password)<8: raise HTTPException(400)
    with db_lock:
        c=conn()
        try:
            c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(b.username,hashlib.sha256(b.password.encode()).hexdigest(),b.display_name or b.username))
            uid=c.execute("SELECT last_insert_rowid() x").fetchone()["x"]; tok=secrets.token_urlsafe(24)
            c.execute("INSERT INTO tokens VALUES(?,?)",(tok,uid)); c.commit()
            u=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone(); return {"user":user_obj(u),"token":tok}
        except sqlite3.IntegrityError: raise HTTPException(409)
        finally: c.close()
@app.post("/api/auth/login")
def login(b:AuthBody):
    with db_lock:
        c=conn(); u=c.execute("SELECT * FROM users WHERE username=? AND password=?",(b.username,hashlib.sha256(b.password.encode()).hexdigest())).fetchone()
        if not u: raise HTTPException(401)
        t=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(t,u["id"])); c.commit(); c.close()
    return {"user":user_obj(u),"token":t}
@app.get("/api/auth/me")
def me(request:Request): return {"user":user_obj(auth(request))}
@app.post("/api/workspaces",status_code=201)
def create_ws(b:WsBody,request:Request):
    u=auth(request)
    if not re.fullmatch(r"[a-z0-9-]{2,32}",b.slug): raise HTTPException(400)
    with db_lock:
        c=conn()
        try:
            c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(b.slug,b.name,u["id"]))
            wid=c.execute("SELECT last_insert_rowid() x").fetchone()["x"]; c.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner"))
            c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")); cid=c.execute("SELECT last_insert_rowid() x").fetchone()["x"]; c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"])); c.commit()
            w=c.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close()
            return {"workspace":{"id":wid,"slug":b.slug,"name":b.name,"owner_id":u["id"],"join_mode":"open"},"general_channel":channel_obj(ch)}
        except sqlite3.IntegrityError: raise HTTPException(409)
@app.get("/api/workspaces")
def workspaces(request:Request):
    u=auth(request)
    with db_lock:
        c=conn(); rows=c.execute("SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall(); c.close()
    return {"workspaces":[{"id":r["id"],"slug":r["slug"],"name":r["name"],"owner_id":r["owner_id"],"join_mode":"open"} for r in rows]}
@app.get("/api/workspaces/{slug}")
def workspace(slug:str,request:Request):
    u=auth(request)
    with db_lock:
        c=conn(); w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
        if not w: raise HTTPException(404)
        if not c.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone(): raise HTTPException(403)
        cs=c.execute("SELECT * FROM channels WHERE workspace_id=?",(w["id"],)).fetchall(); c.close()
    return {"workspace":{"id":w["id"],"slug":w["slug"],"name":w["name"],"owner_id":w["owner_id"],"join_mode":"open"},"channels":[channel_obj(x) for x in cs],"read_state":{}}

@app.post("/api/workspaces/{slug}/channels",status_code=201)
def create_channel(slug:str,b:ChBody,request:Request):
    u=auth(request)
    if not re.fullmatch(r"[a-z0-9-]{1,32}",b.name) or len(b.topic)>250: raise HTTPException(400)
    with db_lock:
        c=conn(); w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
        if not w: raise HTTPException(404)
        role=c.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
        if not role: raise HTTPException(403)
        if b.is_private and role["role"] not in ("owner","admin"): raise HTTPException(403)
        try:
            c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],b.name,int(b.is_private),b.topic))
            cid=c.execute("SELECT last_insert_rowid() x").fetchone()["x"]; c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"])); c.commit()
            r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close(); return {"channel":channel_obj(r)}
        except sqlite3.IntegrityError: raise HTTPException(409)

@app.post("/api/channels/{cid}/join")
def join(cid:int,request:Request):
    u=auth(request)
    with db_lock:
        c=conn(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
        if not ch: raise HTTPException(404)
        c.execute("INSERT OR IGNORE INTO members(workspace_id,user_id,role) VALUES(?,?,?)",(ch["workspace_id"],u["id"],"member"))
        c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(cid,u["id"])); c.commit(); r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close()
    emit(cid,"member.joined",{"user_id":u["id"]})
    return {"channel":channel_obj(r)}

@app.get("/api/channels/{cid}/members")
def channel_members(cid:int,request:Request):
    u=auth(request)
    with db_lock:
        c=conn()
        if not member(c,cid,u["id"]): raise HTTPException(403)
        rows=c.execute("SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?",(cid,)).fetchall(); c.close()
    return {"members":[user_obj(x) for x in rows]}

@app.post("/api/channels/{cid}/messages",status_code=201)
def post_message(cid:int,b:MsgBody,request:Request):
    u=auth(request)
    if not b.body.strip(): raise HTTPException(400)
    with db_lock:
        c=conn(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
        if not ch: raise HTTPException(404)
        if not member(c,cid,u["id"]): raise HTTPException(403)
        t=now(); c.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)",(cid,u["id"],b.body,b.parent_id,t))
        mid=c.execute("SELECT last_insert_rowid() x").fetchone()["x"]; r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone(); obj=message_obj(c,r); c.commit(); c.close()
    typ="message.reply" if b.parent_id else "message.created"
    eid=emit(cid,typ,{"message":obj})
    return {"message":obj}

@app.get("/api/channels/{cid}/messages")
def messages(cid:int,request:Request,limit:int=50,cursor:str|None=None):
    u=auth(request)
    if limit<1 or limit>200 or cursor not in (None,""): raise HTTPException(400)
    with db_lock:
        c=conn()
        if not member(c,cid,u["id"]): raise HTTPException(403)
        rows=c.execute("SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT ?",(cid,limit)).fetchall()
        out=[message_obj(c,x) for x in rows]; c.close()
    return {"messages":out,"next_cursor":None}

@app.get("/api/messages/{mid}/replies")
def replies(mid:int,request:Request,limit:int=50,cursor:str|None=None):
    u=auth(request)
    with db_lock:
        c=conn(); p=c.execute("SELECT channel_id FROM messages WHERE id=?",(mid,)).fetchone()
        if not p or not member(c,p["channel_id"],u["id"]): raise HTTPException(404)
        rows=c.execute("SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id LIMIT ?",(mid,limit)).fetchall(); out=[message_obj(c,x) for x in rows]; c.close()
    return {"replies":out,"next_cursor":None}

@app.patch("/api/messages/{mid}")
def edit(mid:int,b:MsgBody,request:Request):
    u=auth(request)
    with db_lock:
        c=conn(); r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
        if not r: raise HTTPException(404)
        if r["author_id"]!=u["id"]: raise HTTPException(403)
        c.execute("UPDATE messages SET body=?,edited_at=? WHERE id=?",(b.body,now(),mid)); c.commit(); r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone(); obj=message_obj(c,r); c.close()
    emit(r["channel_id"],"message.updated",{"message":obj}); return {"message":obj}

@app.delete("/api/messages/{mid}")
def delete(mid:int,request:Request):
    u=auth(request)
    with db_lock:
        c=conn(); r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
        if not r: return {"deleted":True}
        if r["author_id"]!=u["id"]: raise HTTPException(403)
        c.execute("UPDATE messages SET deleted=1 WHERE id=?",(mid,)); c.commit(); c.close()
    emit(r["channel_id"],"message.deleted",{"message_id":mid}); return {"deleted":True}

async def sendframe(ws,obj):
    await ws.send_text(json.dumps(obj,separators=(",",":")))

def event_rows(cid,since):
    with db_lock:
        c=conn(); rows=c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,since)).fetchall(); c.close()
    return rows

@app.websocket("/api/ws")
async def ws_endpoint(ws:WebSocket):
    token=ws.query_params.get("token","")
    with db_lock:
        c=conn(); u=c.execute("SELECT user_id FROM tokens WHERE token=?",(token,)).fetchone(); c.close()
    if not u: await ws.close(code=1008); return
    await ws.accept(); subscribed=set(); sent={}
    try:
        while True:
            try:
                raw=await asyncio.wait_for(ws.receive_text(),timeout=.12); msg=json.loads(raw)
                cid=int(msg.get("channel_id",0)); typ=msg.get("type")
                if typ=="subscribe":
                    subscribed.add(cid)
                    with db_lock:
                        c=conn(); head=c.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]; c.close()
                    sent[cid]=head; await sendframe(ws,{"type":"subscribed","channel_id":cid,"head_event_id":head})
                elif typ=="resume":
                    since=int(msg.get("since_event_id",0)); subscribed.add(cid); sent[cid]=since
                    rows=event_rows(cid,since)
                    for r in rows:
                        x=json.loads(r["payload"]); await sendframe(ws,{"type":r["type"],"event_id":r["event_id"],"channel_id":cid,**x}); sent[cid]=r["event_id"]
                    await sendframe(ws,{"type":"resumed","channel_id":cid,"head_event_id":sent[cid]})
            except asyncio.TimeoutError: pass
            for cid in list(subscribed):
                rows=event_rows(cid,sent.get(cid,0))
                for r in rows:
                    x=json.loads(r["payload"]); await sendframe(ws,{"type":r["type"],"event_id":r["event_id"],"channel_id":cid,**x}); sent[cid]=r["event_id"]
    except (WebSocketDisconnect,Exception): return

@app.get("/")
def root():
    return HTMLResponse("<html><body><h1>Huddle</h1></body></html>")
