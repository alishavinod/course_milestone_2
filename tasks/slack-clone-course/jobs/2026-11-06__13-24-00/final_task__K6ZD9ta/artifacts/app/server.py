import asyncio, hashlib, json, os, secrets, sqlite3, threading, time
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DB="/app/data/huddle.db"
NODE=int(os.environ.get("NODE_ID","0"))
PORT=int(os.environ.get("PORT","8000"))
app=FastAPI()
_db_lock=threading.Lock()
redis_client=None

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def conn():
    c=sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    return c
def init():
    os.makedirs("/app/data",exist_ok=True)
    c=conn()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
    """)
    c.commit(); c.close()
init()

def user_obj(r):
    return {"id":r["id"],"username":r["username"],"display_name":r["display_name"],
            "timezone":"UTC","avatar_url":"","status_text":"","status_emoji":""}
def auth(request):
    h=request.headers.get("authorization","")
    if not h.startswith("Bearer "): raise HTTPException(401,"unauthorized")
    c=conn(); r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(h[7:],)).fetchone(); c.close()
    if not r: raise HTTPException(401,"unauthorized")
    return r
def bearer(token):
    c=conn(); r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(token,)).fetchone(); c.close()
    return r
def emit(cid,typ,payload):
    c=conn()
    with _db_lock:
        c.execute("BEGIN IMMEDIATE")
        x=c.execute("SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]
        c.execute("INSERT INTO events VALUES(?,?,?,?)",(cid,x,typ,json.dumps(payload)))
        c.commit()
    c.close()
    try:
        import redis
        redis.Redis(host="127.0.0.1",port=6379,socket_timeout=.15).publish("huddle",f"{cid}:{x}")
    except Exception: pass
    return x
def channel_obj(r):
    return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],
            "is_private":bool(r["is_private"]),"is_dm":False,"topic":r["topic"] or "","is_archived":False}
def msg_obj(r):
    c=conn(); u=c.execute("SELECT * FROM users WHERE id=?",(r["author_id"],)).fetchone(); c.close()
    return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],
            "author":user_obj(u),"body":r["body"],"parent_id":None,"reply_count":0,
            "created_at":r["created_at"],"edited_at":None,"files":[],"reactions":[],"mentions":[]}

class Register(BaseModel):
    username:str
    password:str
    display_name:str|None=None
class Login(BaseModel):
    username:str
    password:str
class Workspace(BaseModel):
    slug:str
    name:str
class Channel(BaseModel):
    name:str
    is_private:bool=False
    topic:str=""
class Message(BaseModel):
    body:str

@app.get("/api/health")
def health(): return {"status":"ok","node_id":NODE}

@app.post("/api/auth/register",status_code=201)
def register(x:Register):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_]+",x.username) or len(x.password)<8: raise HTTPException(400,"invalid registration")
    c=conn()
    try:
        cur=c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(x.username,x.password,x.display_name or x.username))
        uid=cur.lastrowid; tok=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(tok,uid)); c.commit()
    except sqlite3.IntegrityError: c.close(); raise HTTPException(409,"duplicate username")
    r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone(); c.close()
    return {"user":user_obj(r),"token":tok}

@app.post("/api/auth/login")
def login(x:Login):
    c=conn(); r=c.execute("SELECT * FROM users WHERE username=? AND password=?",(x.username,x.password)).fetchone()
    if not r: c.close(); raise HTTPException(401,"wrong credentials")
    tok=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(tok,r["id"])); c.commit(); c.close()
    return {"user":user_obj(r),"token":tok}
@app.get("/api/auth/me")
def me(request:Request): return {"user":user_obj(auth(request))}

@app.post("/api/workspaces",status_code=201)
def make_ws(x:Workspace,request:Request):
    u=auth(request); c=conn()
    try:
        cur=c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(x.slug,x.name,u["id"]))
        wid=cur.lastrowid; c.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner"))
        ch=c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")).lastrowid
        c.execute("INSERT INTO channel_members VALUES(?,?)",(ch,u["id"])); c.commit()
    except sqlite3.IntegrityError: c.close(); raise HTTPException(409,"duplicate workspace")
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone(); r=c.execute("SELECT * FROM channels WHERE id=?",(ch,)).fetchone(); c.close()
    return {"workspace":{"id":w["id"],"slug":w["slug"],"name":w["name"],"owner_id":w["owner_id"],"join_mode":"open"},"general_channel":channel_obj(r)}
@app.get("/api/workspaces")
def workspaces(request:Request):
    u=auth(request); c=conn(); rows=c.execute("SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall(); c.close()
    return {"workspaces":[{"id":r["id"],"slug":r["slug"],"name":r["name"],"owner_id":r["owner_id"],"join_mode":"open"} for r in rows]}

@app.post("/api/workspaces/{slug}/channels",status_code=201)
def make_channel(slug:str,x:Channel,request:Request):
    u=auth(request); c=conn(); w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w: c.close(); raise HTTPException(404,"workspace")
    m=c.execute("SELECT * FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
    if not m: c.close(); raise HTTPException(403,"not a member")
    if len(x.name)>32 or not x.name or " " in x.name: c.close(); raise HTTPException(400,"invalid channel")
    try:
        cid=c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],x.name,int(x.is_private),x.topic)).lastrowid
        c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"])); c.commit()
    except sqlite3.IntegrityError: c.close(); raise HTTPException(409,"duplicate channel")
    r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone(); c.close()
    return {"channel":channel_obj(r)}

@app.get("/api/workspaces/{slug}")
def get_ws(slug:str,request:Request):
    u=auth(request); c=conn(); w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
    if not w: c.close(); raise HTTPException(404,"workspace")
    rows=c.execute("""SELECT c.* FROM channels c JOIN channel_members cm ON cm.channel_id=c.id
                      WHERE c.workspace_id=? AND cm.user_id=?""",(w["id"],u["id"])).fetchall(); c.close()
    return {"workspace":{"id":w["id"],"slug":w["slug"],"name":w["name"],"owner_id":w["owner_id"],"join_mode":"open"},
            "channels":[channel_obj(r) for r in rows],"read_state":{}}

@app.post("/api/channels/{cid}/join")
def join(cid:int,request:Request):
    u=auth(request); c=conn(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not ch: c.close(); raise HTTPException(404,"channel")
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(ch["workspace_id"],)).fetchone()
    c.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)",(w["id"],u["id"],"member"))
    c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(cid,u["id"])); c.commit(); c.close()
    return {"channel":channel_obj(ch)}

@app.get("/api/channels/{cid}/messages")
def messages(cid:int,request:Request,limit:int=50,cursor:str|None=None):
    auth(request); c=conn()
    if not c.execute("SELECT id FROM channels WHERE id=?",(cid,)).fetchone(): c.close(); raise HTTPException(404,"channel")
    try: lim=max(1,min(200,limit))
    except: lim=50
    rows=c.execute("SELECT * FROM messages WHERE channel_id=? ORDER BY id DESC LIMIT ?",(cid,lim)).fetchall(); c.close()
    return {"messages":[msg_obj(r) for r in rows],"next_cursor":None}

@app.post("/api/channels/{cid}/messages",status_code=201)
def post_message(cid:int,x:Message,request:Request):
    u=auth(request)
    if not x.body.strip(): raise HTTPException(400,"empty message")
    c=conn(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not ch: c.close(); raise HTTPException(404,"channel")
    if not c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone():
        c.close(); raise HTTPException(403,"not a member")
    created=now()
    with _db_lock:
        cur=c.execute("INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",(cid,u["id"],x.body,created))
        mid=cur.lastrowid; c.commit()
    r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone(); c.close()
    m=msg_obj(r); emit(cid,"message.created",{"message":m})
    return {"message":m}

@app.websocket("/api/ws")
async def ws_endpoint(ws:WebSocket):
    token=ws.query_params.get("token","")
    if token.startswith("Bearer "): token=token[7:]
    if not bearer(token):
        await ws.close(code=1008); return
    await ws.accept(); subscriptions=set(); last={}
    async def send_events(cid,since):
        c=conn(); rows=c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,since)).fetchall(); c.close()
        for r in rows:
            p=json.loads(r["payload"]); await ws.send_json({"type":r["type"],"event_id":r["event_id"],"channel_id":cid,**p})
            last[cid]=r["event_id"]
    try:
        while True:
            try:
                raw=await asyncio.wait_for(ws.receive_json(),timeout=.1)
                typ=raw.get("type"); cid=int(raw.get("channel_id",0))
                if typ=="subscribe":
                    subscriptions.add(cid); c=conn(); h=c.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]; c.close()
                    last[cid]=h; await ws.send_json({"type":"subscribed","channel_id":cid,"head_event_id":h})
                elif typ=="resume":
                    subscriptions.add(cid); since=int(raw.get("since_event_id",0)); await send_events(cid,since)
                    c=conn(); h=c.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(cid,)).fetchone()["n"]; c.close()
                    last[cid]=h; await ws.send_json({"type":"resumed","channel_id":cid,"head_event_id":h})
            except asyncio.TimeoutError: pass
            for cid in list(subscriptions):
                await send_events(cid,last.get(cid,0))
    except (WebSocketDisconnect,Exception):
        return

def redis_listener():
    import redis
    while True:
        try:
            r=redis.Redis(host="127.0.0.1",port=6379,socket_connect_timeout=.5,
                          socket_timeout=1,decode_responses=True)
            ps=r.pubsub()
            ps.subscribe("huddle")
            while True:
                ps.get_message(timeout=1)
        except Exception:
            time.sleep(.3)

threading.Thread(target=redis_listener,daemon=True).start()
