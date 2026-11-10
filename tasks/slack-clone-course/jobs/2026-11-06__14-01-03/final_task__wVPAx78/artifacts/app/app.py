import os, re, json, time, uuid, sqlite3, hashlib, secrets, threading
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
import redis

DB="/app/data/chat.db"; os.makedirs("/app/data",exist_ok=True)
NODE=int(os.getenv("NODE_ID","0"))
app=FastAPI()
lock=threading.Lock()
def conn():
    c=sqlite3.connect(DB,timeout=2,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA busy_timeout=2000")
    return c
with conn() as c:
    c.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_dm INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));""")
def now(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
def userrow(r): return {"id":r["id"],"username":r["username"],"display_name":r["display_name"]}
def auth(req):
    h=req.headers.get("authorization","")
    if not h.startswith("Bearer "): raise HTTPException(401)
    with conn() as c:
        r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(h[7:],)).fetchone()
    if not r: raise HTTPException(401)
    return r
def channel_obj(r): return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"] or "","is_archived":False}
def redis_listener():
    import socket
    while True:
        sock=None
        try:
            sock=socket.create_connection(("127.0.0.1",6379),timeout=2)
            sock.sendall(b"*2\x0d\x0a$9\x0d\x0aSUBSCRIBE\x0d\x0a$6\x0d\x0aevents\x0d\x0a")
            sock.settimeout(5)
            while True:
                data=sock.recv(4096)
                if not data: raise OSError("redis closed")
        except Exception:
            try:
                if sock: sock.close()
            except Exception: pass
            time.sleep(.25)


@app.on_event("startup")
async def startup_listener():
    threading.Thread(target=redis_listener,daemon=True).start()

def publish(cid,eid):
    try:
        redis.Redis(host="127.0.0.1",port=6379,socket_timeout=.15).publish("events",f"{cid}:{eid}")
    except Exception: pass
def emit(c,cid,typ,payload):
    row=c.execute("SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?",(cid,)).fetchone()
    eid=row["n"]; c.execute("INSERT INTO events VALUES(?,?,?,?)",(cid,eid,typ,json.dumps(payload)))
    c.commit(); publish(cid,eid); return eid
def member(c,cid,uid):
    return c.execute("SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?",(cid,uid)).fetchone()
@app.get("/api/health")
def health(): return {"status":"ok","node_id":NODE}
@app.post("/api/auth/register",status_code=201)
async def register(req:Request):
    b=await req.json(); u=b.get("username",""); p=b.get("password","")
    if not re.fullmatch(r"[A-Za-z0-9_]+",u) or len(p)<8: raise HTTPException(400)
    with conn() as c:
        try:
            c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(u,hashlib.sha256(p.encode()).hexdigest(),b.get("display_name") or u)); uid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
            t=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(t,uid)); c.commit()
            return {"user":userrow(c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()),"token":t}
        except sqlite3.IntegrityError: raise HTTPException(409)
@app.post("/api/auth/login")
async def login(req:Request):
    b=await req.json()
    with conn() as c:
        r=c.execute("SELECT * FROM users WHERE username=? AND password=?",(b.get("username"),hashlib.sha256(b.get("password","").encode()).hexdigest())).fetchone()
        if not r: raise HTTPException(401)
        t=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(t,r["id"])); c.commit(); return {"user":userrow(r),"token":t}
@app.get("/api/auth/me")
async def me(req:Request): return {"user":userrow(auth(req))}
@app.post("/api/workspaces",status_code=201)
async def workspace(req:Request):
    u=auth(req); b=await req.json(); slug=b.get("slug","")
    if not re.fullmatch(r"[a-z0-9-]{2,32}",slug) or not b.get("name"): raise HTTPException(400)
    with conn() as c:
        try:
            c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(slug,b["name"],u["id"])); wid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner")); c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")); ch=c.execute("SELECT * FROM channels WHERE id=last_insert_rowid()").fetchone(); c.execute("INSERT INTO cmembers VALUES(?,?)",(ch["id"],u["id"])); c.commit()
            return {"workspace":{"id":wid,"slug":slug,"name":b["name"],"owner_id":u["id"],"join_mode":"open"},"general_channel":channel_obj(ch)}
        except sqlite3.IntegrityError: raise HTTPException(409)
@app.get("/api/workspaces")
async def workspaces(req:Request):
    u=auth(req)
    with conn() as c: rs=c.execute("SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall()
    return {"workspaces":[{"id":r["id"],"slug":r["slug"],"name":r["name"],"owner_id":r["owner_id"],"join_mode":"open"} for r in rs]}
@app.post("/api/workspaces/{slug}/channels",status_code=201)
async def create_channel(slug:str,req:Request):
    u=auth(req); b=await req.json(); name=b.get("name","")
    if not re.fullmatch(r"[a-z0-9-]{1,32}",name): raise HTTPException(400)
    with conn() as c:
        w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
        if not w: raise HTTPException(404)
        m=c.execute("SELECT * FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
        if not m: raise HTTPException(403)
        if c.execute("SELECT 1 FROM channels WHERE workspace_id=? AND name=?",(w["id"],name)).fetchone(): raise HTTPException(409)
        c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],name,int(bool(b.get("is_private"))),b.get("topic",""))); r=c.execute("SELECT * FROM channels WHERE id=last_insert_rowid()").fetchone(); c.execute("INSERT INTO cmembers VALUES(?,?)",(r["id"],u["id"])); c.commit(); return {"channel":channel_obj(r)}
@app.post("/api/channels/{cid}/join")
async def join(cid:int,req:Request):
    u=auth(req)
    with conn() as c:
        r=c.execute("SELECT ch.*,w.owner_id FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?",(cid,)).fetchone()
        if not r: raise HTTPException(404)
        if not c.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(r["workspace_id"],u["id"])).fetchone(): c.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)",(r["workspace_id"],u["id"],"member"))
        c.execute("INSERT OR IGNORE INTO cmembers VALUES(?,?)",(cid,u["id"])); c.commit(); return {"channel":channel_obj(r)}
@app.get("/api/channels/{cid}/messages")
async def messages(cid:int,req:Request):
    u=auth(req)
    with conn() as c:
        if not member(c,cid,u["id"]): raise HTTPException(403)
        rs=c.execute("SELECT m.*,u.username,u.display_name FROM messages m JOIN users u ON u.id=m.author_id WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200",(cid,)).fetchall()
    return {"messages":[msg(r) for r in rs],"next_cursor":None}
def msg(r):
    return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":{"id":r["author_id"],"username":r["username"],"display_name":r["display_name"]},"body":r["body"],"parent_id":r["parent_id"],"reply_count":0,"created_at":r["created_at"],"edited_at":r["edited_at"],"files":[],"reactions":[],"mentions":[]}
@app.post("/api/channels/{cid}/messages",status_code=201)
async def post(cid:int,req:Request):
    u=auth(req); b=await req.json(); body=b.get("body","")
    if not body.strip(): raise HTTPException(400)
    with conn() as c:
        if not member(c,cid,u["id"]): raise HTTPException(403)
        t=now(); c.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)",(cid,u["id"],body,b.get("parent_id"),t)); r=c.execute("SELECT m.*,u.username,u.display_name FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=last_insert_rowid()").fetchone(); ob=msg(r); emit(c,cid,"message.created",{"message":ob}); return {"message":ob}
@app.websocket("/api/ws")
async def ws(sock:WebSocket):
    token=sock.query_params.get("token",""); await sock.accept()
    with conn() as c: ur=c.execute("SELECT user_id FROM tokens WHERE token=?",(token.replace("Bearer ",""),)).fetchone()
    if not ur: await sock.close(code=1008); return
    subs={}; sent={}
    try:
      while True:
        try:
          data=await __import__("asyncio").wait_for(sock.receive_json(),.05)
          typ=data.get("type"); cid=int(data.get("channel_id"))
          if typ=="subscribe":
            with conn() as c: h=c.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]
            sent[cid]=h
            await sock.send_json({"type":"subscribed","channel_id":cid,"head_event_id":h})
          elif typ=="resume":
            subs[cid]=int(data.get("since_event_id",0)); sent[cid]=subs[cid]
            with conn() as c: h=c.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]
            for e in c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,sent[cid])):
                await sock.send_json({"type":e["type"],"event_id":e["event_id"],"channel_id":cid,**json.loads(e["payload"])}); sent[cid]=e["event_id"]
            await sock.send_json({"type":"resumed","channel_id":cid,"head_event_id":h})
        except Exception: pass
        with conn() as c:
          for cid,last in list(sent.items()):
            for e in c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,last)):
              await sock.send_json({"type":e["type"],"event_id":e["event_id"],"channel_id":cid,**json.loads(e["payload"])}); sent[cid]=e["event_id"]
        await __import__("asyncio").sleep(.01)
    except (WebSocketDisconnect,Exception): pass
@app.get("/")
async def root(): return HTMLResponse("<html><body><h1>Huddle</h1></body></html>")
