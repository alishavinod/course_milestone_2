import os, re, json, sqlite3, secrets, hashlib, asyncio, datetime, mimetypes, threading, time
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
DB="/app/data/huddle.db"; os.makedirs("/app/data",exist_ok=True)
app=FastAPI(); app.mount("/static", StaticFiles(directory="/app/static"), name="static"); app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])
def db():
 c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; c.execute("PRAGMA busy_timeout=30000"); return c
def init():
 c=db(); c.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS dms(a INTEGER,b INTEGER,channel_id INTEGER,UNIQUE(a,b));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS groupsx(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER,user_ids TEXT);
CREATE TABLE IF NOT EXISTS readstate(user_id INTEGER,channel_id INTEGER,mention_count INTEGER DEFAULT 0,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS presence(connection_id TEXT PRIMARY KEY,user_id INTEGER,seen REAL);
CREATE TABLE IF NOT EXISTS user_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT)"""); c.commit(); c.close()
init()
def redis_listener():
 while True:
  try:
   import redis
   r=redis.Redis(host="127.0.0.1",port=6379,socket_connect_timeout=1,socket_timeout=2)
   ps=r.pubsub(); ps.subscribe("huddle-events")
   for _ in ps.listen(): pass
  except Exception:
   time.sleep(0.5)
threading.Thread(target=redis_listener,daemon=True).start()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z")
def err(code,msg): return JSONResponse({"error":msg},status_code=code)
def user_obj(r): return {k:r[k] for k in ("id","username","display_name","timezone","avatar_url","status_text","status_emoji")}
def auth(req):
 t=req.headers.get("authorization","")
 if not t.lower().startswith("bearer "): return None
 c=db(); r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(t[7:],)).fetchone(); c.close(); return r
def token_for(uid):
 t=secrets.token_urlsafe(24); c=db(); c.execute("INSERT INTO tokens VALUES(?,?)",(t,uid)); c.commit(); c.close(); return t
def channel_obj(r): return {"id":r["id"],"workspace_id":r["workspace_id"],"name":r["name"],"is_private":bool(r["is_private"]),"is_dm":bool(r["is_dm"]),"topic":r["topic"],"is_archived":bool(r["is_archived"])}
def file_obj(r):
 return {"id":r["id"],"uploader_id":r["uploader_id"],"filename":r["filename"],"content_type":r["content_type"],"size":r["size"],"created_at":r["created_at"]}
def files_for(mid):
 c=db(); rows=c.execute("SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?",(mid,)).fetchall(); c.close()
 return [file_obj(r) for r in rows]
def reactions(mid):
 c=db(); rows=c.execute("SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY emoji,user_id",(mid,)).fetchall(); c.close()
 out={}
 for r in rows: out.setdefault(r["emoji"],[]).append(r["user_id"])
 return [{"emoji":e,"count":len(ids),"user_ids":ids} for e,ids in out.items()]
def msg_obj(r):
 c=db(); u=c.execute("SELECT * FROM users WHERE id=?",(r["author_id"],)).fetchone(); c.close()
 return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":user_obj(u),"body":r["body"],"parent_id":r["parent_id"],"reply_count":count_replies(r["id"]),"created_at":r["created_at"],"edited_at":r["edited_at"],"files":files_for(r["id"]),"reactions":reactions(r["id"]),"mentions":[]}
def count_replies(mid):
 c=db(); n=c.execute("SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0",(mid,)).fetchone()["n"]; c.close(); return n
def member(uid,ch):
 c=db(); r=c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(ch,uid)).fetchone(); c.close(); return bool(r)
def emit(ch,typ,payload):
 for attempt in range(20):
  c=db()
  try:
   c.execute("BEGIN IMMEDIATE")
   n=c.execute("SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?",(ch,)).fetchone()["n"]
   c.execute("INSERT INTO events VALUES(?,?,?,?)",(ch,n,typ,json.dumps(payload)))
   c.commit(); c.close(); return n
  except sqlite3.IntegrityError:
   c.rollback(); c.close(); time.sleep(0.005)
  except sqlite3.OperationalError:
   c.rollback(); c.close(); time.sleep(0.01)
 raise RuntimeError("event allocation failed")
def add_reaction(message,user,emoji):
 c=db(); c.execute("INSERT OR IGNORE INTO reactions VALUES(?,?,?)",(message["id"],user["id"],emoji)); c.commit(); c.close()
def remove_reaction(message,user,emoji):
 c=db(); c.execute("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?",(message["id"],user["id"],emoji)); c.commit(); c.close()
def get_or_create_dm(a,b):
 x,y=sorted((a,b)); c=db(); r=c.execute("SELECT channel_id FROM dms WHERE a=? AND b=?",(x,y)).fetchone()
 if r: c.close(); return r["channel_id"]
 cur=c.execute("INSERT INTO channels(workspace_id,name,is_dm) VALUES(0,?,1)",(f"dm-{x}-{y}",)); ch=cur.lastrowid
 c.execute("INSERT INTO dms VALUES(?,?,?)",(x,y,ch)); c.execute("INSERT INTO channel_members VALUES(?,?)",(ch,a)); c.execute("INSERT INTO channel_members VALUES(?,?)",(ch,b)); c.commit(); c.close(); return ch
@app.get("/api/health")
async def health(request:Request): return {"status":"ok","node_id":int(os.getenv("NODE_ID","0"))}
@app.post("/api/auth/register")
async def register(request:Request):
 try: b=await request.json(); name=b["username"]; pw=b["password"]
 except: return err(400,"invalid")
 if not re.fullmatch(r"[A-Za-z0-9_]+",name) or len(pw)<8:return err(400,"invalid")
 c=db()
 try: c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(name,hashlib.sha256(pw.encode()).hexdigest(),b.get("display_name") or name)); uid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.commit()
 except sqlite3.IntegrityError: c.close(); return err(409,"duplicate")
 r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone(); c.close(); return JSONResponse({"user":user_obj(r),"token":token_for(uid)},201)
@app.post("/api/auth/login")
async def login(request:Request):
 try:b=await request.json()
 except:return err(400,"invalid")
 c=db(); r=c.execute("SELECT * FROM users WHERE username=? AND password=?",(b.get("username"),hashlib.sha256(str(b.get("password","")).encode()).hexdigest())).fetchone(); c.close()
 if not r:return err(401,"invalid credentials")
 return {"user":user_obj(r),"token":token_for(r["id"])}
@app.get("/api/auth/me")
async def me(request:Request):
 u=auth(request); return {"user":user_obj(u)} if u else err(401,"unauthorized")
@app.get("/api/users/{uid}")
async def getuser(uid:int,request:Request):
 if not auth(request):return err(401,"unauthorized")
 c=db(); r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone(); c.close(); return {"user":user_obj(r)} if r else err(404,"not found")
@app.patch("/api/users/me")
async def patchme(request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 b=await request.json(); allowed={"display_name","timezone","avatar_url","status_text","status_emoji"}; vals={k:v for k,v in b.items() if k in allowed}
 limits={"display_name":80,"timezone":255,"status_text":100,"status_emoji":32,"avatar_url":1024}
 if any(not isinstance(v,str) or len(v)>limits[k] for k,v in vals.items()):return err(400,"invalid")
 if "timezone" in vals:
  try: ZoneInfo(vals["timezone"])
  except Exception: return err(400,"invalid")
 c=db()
 if vals:c.execute("UPDATE users SET "+",".join(k+"=?" for k in vals)+" WHERE id=?",(*vals.values(),u["id"]));c.commit()
 r=c.execute("SELECT * FROM users WHERE id=?",(u["id"],)).fetchone()
 c.execute("INSERT INTO user_events(payload) VALUES(?)",(json.dumps({"type":"user.updated","user":user_obj(r)}),));c.commit();c.close();return {"user":user_obj(r)}
@app.post("/api/workspaces")
async def workspace(request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 try:b=await request.json(); slug=b["slug"]; name=b["name"]
 except:return err(400,"invalid")
 if not re.fullmatch(r"[a-z0-9-]{2,32}",slug):return err(400,"invalid")
 c=db()
 try:c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(slug,name,u["id"])); wid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner"));c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general"));ch=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.execute("INSERT INTO channel_members VALUES(?,?)",(ch,u["id"]));c.commit()
 except sqlite3.IntegrityError:c.close();return err(409,"duplicate")
 w=c.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone(); q=c.execute("SELECT * FROM channels WHERE id=?",(ch,)).fetchone();c.close()
 return JSONResponse({"workspace":dict(w),"general_channel":channel_obj(q)},201)
@app.get("/api/workspaces")
async def workspaces(request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db(); rows=c.execute("SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall();c.close();return {"workspaces":[dict(x) for x in rows]}
@app.get("/api/workspaces/{slug}")
async def workspace_detail(slug:str,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
 if not w:c.close();return err(404,"not found")
 rows=c.execute("SELECT c.* FROM channels c JOIN channel_members m ON m.channel_id=c.id WHERE c.workspace_id=? AND m.user_id=?",(w["id"],u["id"])).fetchall();c.close();return {"workspace":dict(w),"channels":[channel_obj(x) for x in rows],"read_state":{}}
@app.post("/api/workspaces/{slug}/channels")
async def create_channel(slug:str,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 try:b=await request.json();name=b["name"]
 except:return err(400,"invalid")
 c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone();m=c.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone() if w else None
 if not w:return err(404,"not found")
 if not m:return err(403,"forbidden")
 if not re.fullmatch(r"[a-z0-9-]{1,32}",name):return err(400,"invalid")
 if b.get("is_private") and m["role"] not in ("owner","admin"):return err(403,"forbidden")
 if c.execute("SELECT 1 FROM channels WHERE workspace_id=? AND name=?",(w["id"],name)).fetchone(): c.close(); return err(409,"duplicate")
 try:c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],name,int(bool(b.get("is_private"))),b.get("topic","")));ch=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.execute("INSERT INTO channel_members VALUES(?,?)",(ch,u["id"]));c.commit()
 except sqlite3.IntegrityError:c.close();return err(409,"duplicate")
 r=c.execute("SELECT * FROM channels WHERE id=?",(ch,)).fetchone();c.close();return JSONResponse({"channel":channel_obj(r)},201)
@app.post("/api/channels/{ch}/join")
async def join(ch:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db();r=c.execute("SELECT * FROM channels WHERE id=?",(ch,)).fetchone()
 if not r:return err(404,"not found")
 w=c.execute("SELECT * FROM workspaces WHERE id=?",(r["workspace_id"],)).fetchone()
 if w and not c.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone():c.execute("INSERT OR IGNORE INTO members VALUES(?,?,?)",(w["id"],u["id"],"member"))
 c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(ch,u["id"]));c.commit();c.close();return {"channel":channel_obj(r)}
@app.patch("/api/channels/{ch}")
async def patch_channel(ch:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 try:b=await request.json()
 except:return err(400,"invalid")
 c=db(); r=c.execute("SELECT * FROM channels WHERE id=?",(ch,)).fetchone()
 if not r:c.close();return err(404,"not found")
 ok=c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(ch,u["id"])).fetchone()
 owner=c.execute("SELECT owner_id FROM workspaces WHERE id=?",(r["workspace_id"],)).fetchone() if r["workspace_id"] else None
 if not ok and not (owner and owner["owner_id"]==u["id"]):c.close();return err(403,"forbidden")
 topic=b.get("topic",r["topic"])
 if not isinstance(topic,str) or len(topic)>250:c.close();return err(400,"invalid")
 c.execute("UPDATE channels SET topic=? WHERE id=?",(topic,ch));c.commit();r=c.execute("SELECT * FROM channels WHERE id=?",(ch,)).fetchone();c.close()
 return {"channel":channel_obj(r)}
@app.get("/api/channels/{ch}/members")
async def channel_members(ch:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db(); ok=c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(ch,u["id"])).fetchone()
 if not ok:c.close();return err(403,"forbidden")
 rows=c.execute("SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?",(ch,)).fetchall();c.close()
 return {"members":[user_obj(x) for x in rows]}
def resolve_mentions(body,ch,author):
 c=db(); cr=c.execute("SELECT workspace_id FROM channels WHERE id=?",(ch,)).fetchone()
 ids=[]
 for name in re.findall(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)",body):
  if name in ("here","channel"):
   rows=c.execute("SELECT user_id FROM presence WHERE seen>? AND user_id IN (SELECT user_id FROM channel_members WHERE channel_id=?)",(__import__("time").time()-3,ch)).fetchall()
   ids += [x["user_id"] for x in rows]
  else:
   ur=c.execute("SELECT id FROM users WHERE username=?",(name,)).fetchone()
   if ur and c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(ch,ur["id"])).fetchone(): ids.append(ur["id"])
   gr=c.execute("SELECT user_ids FROM groupsx WHERE workspace_id=? AND handle=?",(cr["workspace_id"],name)).fetchone()
   if gr:
    for gid in json.loads(gr["user_ids"] or "[]"):
     if c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(ch,gid)).fetchone(): ids.append(gid)
 ids=list(dict.fromkeys(x for x in ids if x!=author))
 for uid in ids:
  c.execute("INSERT OR IGNORE INTO readstate(user_id,channel_id) VALUES(?,?)",(uid,ch))
  c.execute("UPDATE readstate SET mention_count=mention_count+1 WHERE user_id=? AND channel_id=?",(uid,ch))
 c.commit();c.close();return ids
@app.post("/api/channels/{ch}/messages")
async def postmsg(ch:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 try:b=await request.json();body=b.get("body","")
 except:return err(400,"invalid")
 if not body:return err(400,"empty")
 file_ids=b.get("file_ids",[])
 if file_ids is None: file_ids=[]
 if not isinstance(file_ids,list): return err(400,"invalid files")
 c=db();r=c.execute("SELECT * FROM channels WHERE id=?",(ch,)).fetchone()
 if not r:return err(404,"not found")
 if not member(u["id"],ch):return err(403,"forbidden")
 wrow=c.execute("SELECT * FROM workspaces WHERE id=?",(r["workspace_id"],)).fetchone()
 owner=bool(wrow and wrow["owner_id"]==u["id"])
 parts=body.split(None,1); cmd=parts[0] if parts else ""
 if body.startswith("/") and not body.startswith("//"):
  if cmd=="/me" and len(parts)<2:return err(400,"text required")
  if cmd=="/shrug": body=(parts[1].strip()+" " if len(parts)>1 else "")+"¯\\_(ツ)_/¯"
  elif cmd=="/topic":
   if not owner and not member(u["id"],ch): c.close(); return err(403,"forbidden")
   topic=parts[1] if len(parts)>1 else ""
   if len(topic)>250:c.close();return err(400,"topic")
   c.execute("UPDATE channels SET topic=? WHERE id=?",(topic,ch));c.commit(); nr=c.execute("SELECT * FROM channels WHERE id=?",(ch,)).fetchone();c.close()
   return JSONResponse({"message":None,"channel":channel_obj(nr)},201)
  elif cmd=="/invite":
   if len(parts)<2 or not owner:return err(400,"invite")
   uname=parts[1].lstrip("@").strip(); ur=c.execute("SELECT * FROM users WHERE username=?",(uname,)).fetchone()
   if not ur:c.close();return err(404,"user")
   c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(ch,ur["id"]));c.commit();c.close()
   return JSONResponse({"message":None,"invited_user":user_obj(ur)},201)
  elif cmd in ("/archive","/unarchive"):
   if not owner:c.close();return err(403,"forbidden")
   archived=cmd=="/archive"; c.execute("UPDATE channels SET is_archived=? WHERE id=?",(int(archived),ch));c.commit();nr=c.execute("SELECT * FROM channels WHERE id=?",(ch,)).fetchone();c.close()
   return JSONResponse({"message":None,"channel":channel_obj(nr)},201)
  elif cmd not in ("/me","/shrug"):c.close();return err(400,"unknown command")
 if r["is_archived"]: c.close(); return err(423,"archived")
 if file_ids:
  marks=",".join("?" for _ in file_ids)
  fs=c.execute("SELECT * FROM files WHERE id IN ("+marks+")",tuple(file_ids)).fetchall()
  if len(fs)!=len(file_ids) or any(f["uploader_id"]!=u["id"] for f in fs): c.close(); return err(400,"invalid file")
  used=c.execute("SELECT file_id FROM message_files WHERE file_id IN ("+marks+")",tuple(file_ids)).fetchall()
  if used: c.close(); return err(400,"file already attached")
 ts=now();c.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)",(ch,u["id"],body,b.get("parent_id"),ts));mid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
 for fid in file_ids: c.execute("INSERT INTO message_files(message_id,file_id) VALUES(?,?)",(mid,fid))
 c.commit();r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone();c.close();m=msg_obj(r);m["mentions"]=resolve_mentions(body,ch,u["id"]);emit(ch,"message.reply" if r["parent_id"] else "message.created",{"message":m});return JSONResponse({"message":m},201)
@app.get("/api/channels/{ch}/messages")
async def messages(ch:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 if not member(u["id"],ch):return err(403,"forbidden")
 c=db();rows=c.execute("SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT 200",(ch,)).fetchall();c.close();return {"messages":[msg_obj(x) for x in rows],"next_cursor":None}
@app.post("/api/messages/{mid}/reactions")
async def react(mid:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 try:e=(await request.json())["emoji"]
 except:return err(400,"invalid")
 c=db();m=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone();c.close()
 if not m:return err(404,"not found")
 add_reaction(m,u,e); emit(m["channel_id"],"reaction.added",{"message_id":mid,"emoji":e,"user_id":u["id"]});return {"reactions":reactions(mid)}
@app.delete("/api/messages/{mid}/reactions/{emoji}")
async def unreact(mid:int,emoji:str,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db();m=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone();c.close()
 if not m:return err(404,"not found")
 remove_reaction(m,u,emoji);emit(m["channel_id"],"reaction.removed",{"message_id":mid,"emoji":emoji,"user_id":u["id"]});return {"reactions":reactions(mid)}
@app.get("/api/messages/{mid}/replies")
async def replies(mid:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db(); parent=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
 if not parent:c.close();return err(404,"not found")
 if not member(u["id"],parent["channel_id"]):c.close();return err(403,"forbidden")
 rs=c.execute("SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id",(mid,)).fetchall();c.close()
 return {"replies":[msg_obj(x) for x in rs],"next_cursor":None}
@app.patch("/api/messages/{mid}")
async def edit_message_route(mid:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 try:b=await request.json(); body=b.get("body")
 except:return err(400,"invalid")
 if not isinstance(body,str) or not body:return err(400,"invalid")
 c=db(); m=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
 if not m:c.close();return err(404,"not found")
 if m["deleted"]:c.close();return err(409,"deleted")
 if m["author_id"]!=u["id"]:c.close();return err(403,"forbidden")
 edited=now();c.execute("UPDATE messages SET body=?,edited_at=? WHERE id=?",(body,edited,mid));c.commit();r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone();c.close()
 obj=msg_obj(r);emit(r["channel_id"],"message.updated",{"message":obj});return {"message":obj}
@app.delete("/api/messages/{mid}")
async def delete_message_route(mid:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db();m=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone()
 if not m:c.close();return err(404,"not found")
 w=c.execute("SELECT owner_id FROM workspaces WHERE id=(SELECT workspace_id FROM channels WHERE id=?)",(m["channel_id"],)).fetchone()
 if m["author_id"]!=u["id"] and not (w and w["owner_id"]==u["id"]):c.close();return err(403,"forbidden")
 if not m["deleted"]:
  c.execute("UPDATE messages SET deleted=1 WHERE id=?",(mid,));c.commit();c.close();emit(m["channel_id"],"message.deleted",{"message_id":mid})
 else:c.close()
 return {"deleted":True}
@app.post("/api/files")
async def upload_file(request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 try:
  form=await request.form(); up=form.get("file")
  if not up or not hasattr(up,"read"): return err(400,"file required")
  data=await up.read()
 except: return err(400,"invalid upload")
 if len(data)>10*1024*1024:return err(413,"file too large")
 ct=getattr(up,"content_type",None) or "application/octet-stream"; fn=getattr(up,"filename",None) or "file"
 c=db(); c.execute("INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)",(u["id"],fn,ct,len(data),now(),data)); fid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.commit(); r=c.execute("SELECT * FROM files WHERE id=?",(fid,)).fetchone();c.close()
 return JSONResponse({"file":file_obj(r)},201)
@app.get("/api/files/{fid}")
async def file_detail(fid:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db();r=c.execute("SELECT * FROM files WHERE id=?",(fid,)).fetchone();c.close()
 return {"file":file_obj(r)} if r else err(404,"not found")
@app.get("/api/files/{fid}/download")
async def file_download(fid:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db();r=c.execute("SELECT * FROM files WHERE id=?",(fid,)).fetchone();c.close()
 if not r:return err(404,"not found")
 return Response(content=r["data"],media_type=r["content_type"],headers={"Content-Disposition":'attachment; filename="'+r["filename"]+'"'})
def group_json(r):
 return {"id":r["id"],"handle":r["handle"],"name":r["name"],"member_user_ids":json.loads(r["user_ids"] or "[]")}
@app.post("/api/workspaces/{slug}/groups")
async def group_create(slug:str,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 try:b=await request.json(); handle=b["handle"]
 except:return err(400,"invalid")
 c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
 if not w:c.close();return err(404,"not found")
 mem=c.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
 if not mem:c.close();return err(403,"forbidden")
 if not handle:c.close();return err(400,"handle")
 ids=b.get("user_ids",[])
 try:c.execute("INSERT INTO groupsx(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)",(w["id"],handle,b.get("name",""),u["id"],json.dumps(ids)));gid=c.execute("SELECT last_insert_rowid()").fetchone()[0];c.commit()
 except sqlite3.IntegrityError:c.close();return err(409,"duplicate")
 r=c.execute("SELECT * FROM groupsx WHERE id=?",(gid,)).fetchone();c.close();return JSONResponse({"group":group_json(r)},201)
@app.get("/api/workspaces/{slug}/groups")
async def group_list(slug:str,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone()
 if not w:c.close();return err(404,"not found")
 if not c.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone():c.close();return err(403,"forbidden")
 rs=c.execute("SELECT * FROM groupsx WHERE workspace_id=?",(w["id"],)).fetchall();c.close();return {"groups":[group_json(x) for x in rs]}
@app.patch("/api/workspaces/{slug}/groups/{handle}")
async def group_patch(slug:str,handle:str,request:Request):
 u=auth(request); 
 if not u:return err(401,"unauthorized")
 b=await request.json();c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone();g=c.execute("SELECT * FROM groupsx WHERE workspace_id=? AND handle=?",(w["id"],handle)).fetchone() if w else None
 if not g:c.close();return err(404,"not found")
 if g["creator_id"]!=u["id"] and w["owner_id"]!=u["id"]:c.close();return err(403,"forbidden")
 c.execute("UPDATE groupsx SET name=?,user_ids=? WHERE id=?",(b.get("name",g["name"]),json.dumps(b.get("user_ids",json.loads(g["user_ids"] or "[]"))),g["id"]));c.commit();r=c.execute("SELECT * FROM groupsx WHERE id=?",(g["id"],)).fetchone();c.close();return {"group":group_json(r)}
@app.delete("/api/workspaces/{slug}/groups/{handle}")
async def group_delete(slug:str,handle:str,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db();w=c.execute("SELECT * FROM workspaces WHERE slug=?",(slug,)).fetchone();g=c.execute("SELECT * FROM groupsx WHERE workspace_id=? AND handle=?",(w["id"],handle)).fetchone() if w else None
 if g and g["creator_id"]!=u["id"] and w["owner_id"]!=u["id"]:c.close();return err(403,"forbidden")
 if g:c.execute("DELETE FROM groupsx WHERE id=?",(g["id"],));c.commit()
 c.close();return {"deleted":True}
@app.post("/api/dms")
async def dm(request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 try:uid=(await request.json())["user_id"]
 except:return err(400,"invalid")
 c=db();r=c.execute("SELECT id FROM users WHERE id=?",(uid,)).fetchone();c.close()
 if not r:return err(404,"not found")
 return {"channel_id":get_or_create_dm(u["id"],uid)}
@app.get("/api/channels/{ch}/read")
async def get_read(ch:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 c=db();c.execute("INSERT OR IGNORE INTO readstate(user_id,channel_id) VALUES(?,?)",(u["id"],ch));r=c.execute("SELECT * FROM readstate WHERE user_id=? AND channel_id=?",(u["id"],ch)).fetchone();c.close()
 return {"read_state":{"channel_id":ch,"last_read_event_id":r["last_read_event_id"],"unread_count":0,"mention_count":r["mention_count"]}}
@app.post("/api/channels/{ch}/read")
async def post_read(ch:int,request:Request):
 u=auth(request)
 if not u:return err(401,"unauthorized")
 return await get_read(ch,request)
@app.get("/api/ws")
async def ws_http(): return err(400,"websocket required")
@app.websocket("/api/ws")
async def websocket(ws:WebSocket):
 await ws.accept(); tok=ws.query_params.get("token","");c=db();u=c.execute("SELECT user_id FROM tokens WHERE token=?",(tok,)).fetchone();c.close()
 if not u: await ws.close();return
 cid=secrets.token_urlsafe(12); pc=db(); pc.execute("INSERT OR REPLACE INTO presence VALUES(?,?,?)",(cid,u["user_id"],__import__("time").time())); pc.commit(); pc.close()
 subs={}; last_user_event=0
 try:
  while True:
   try:
    data=await asyncio.wait_for(ws.receive_json(),.15)
    ch=int(data.get("channel_id")); subs[ch]=int(data.get("since_event_id",0))
    if data["type"]=="subscribe":
     c=db();h=c.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(ch,)).fetchone()["n"];c.close();subs[ch]=h;await ws.send_json({"type":"subscribed","channel_id":ch,"head_event_id":h})
    elif data["type"]=="resume":
     c=db(); rows=c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(ch,subs[ch])).fetchall()
     for r in rows:
      pp=json.loads(r["payload"]); frame={"type":r["type"],"event_id":r["event_id"],"channel_id":ch}; frame.update(pp); await ws.send_json(frame)
     head=c.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(ch,)).fetchone()["n"]; c.close()
     subs[ch]=head
     await ws.send_json({"type":"resumed","channel_id":ch,"head_event_id":head})
   except asyncio.TimeoutError: pass
   pc=db(); pc.execute("UPDATE presence SET seen=? WHERE connection_id=?",(__import__("time").time(),cid))
   ue=pc.execute("SELECT * FROM user_events WHERE id>? ORDER BY id",(last_user_event,)).fetchall(); pc.commit(); pc.close()
   for ev in ue:
    await ws.send_json(json.loads(ev["payload"])); last_user_event=ev["id"]
   for ch,last in list(subs.items()):
    c=db();rows=c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(ch,last)).fetchall();c.close()
    for r in rows:
     p=json.loads(r["payload"]); frame={"type":r["type"],"event_id":r["event_id"],"channel_id":ch};frame.update(p);await ws.send_json(frame);subs[ch]=r["event_id"]
 except (WebSocketDisconnect,Exception):
  pc=db(); pc.execute("DELETE FROM presence WHERE connection_id=?",(cid,)); pc.commit(); pc.close()
@app.get("/")
async def root(): return FileResponse("/app/static/index.html")
