import asyncio, sqlite3, os, time, json
DB="/app/data/huddle.db"; clients=[]; nicks=set()
def conn():
 c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; c.execute("PRAGMA busy_timeout=30000"); return c
def token_user(t):
 c=conn(); r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(t,)).fetchone(); c.close(); return r
def chan(name):
 if not name.startswith("#") or "/" not in name[1:]: return None
 slug,cname=name[1:].split("/",1); c=conn(); r=c.execute("SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?",(slug,cname)).fetchone(); c.close(); return r
def add_member(ch,uid):
 c=conn(); c.execute("INSERT OR IGNORE INTO members(workspace_id,user_id,role) SELECT workspace_id,?, 'member' FROM channels WHERE id=?",(uid,ch["id"])); c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(ch["id"],uid)); c.commit(); c.close()
def make_msg(ch,u,body):
 import server
 c=conn(); ts=server.now(); c.execute("INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)",(ch["id"],u["id"],body,ts)); mid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.commit(); r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone(); c.close()
 m=server.msg_obj(r); server.emit(ch["id"],"message.created",{"message":m}); return mid
async def send(w,line):
 try: w.write((line+"\r\n").encode()); await w.drain()
 except: pass
async def bridge(st):
 while not st["closed"]:
  await asyncio.sleep(.08)
  for ch in list(st["joined"]):
   c=conn(); rows=c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(ch["id"],st["last"].get(ch["id"],0))).fetchall(); c.close()
   for e in rows:
    st["last"][ch["id"]]=e["event_id"]
    if e["type"]!="message.created": continue
    p=json.loads(e["payload"]); m=p.get("message") or {}; author=(m.get("author") or {}).get("username","user")
    if m.get("author_id")==st["user"]["id"] and m.get("id") in st["own"]: continue
    await send(st["writer"],f":{author}!{author}@localhost PRIVMSG #{ch['slug']}/{ch['name']} :{m.get('body','')}")
async def handle_client(reader,writer):
 st={"writer":writer,"user":None,"nick":None,"joined":[],"last":{},"own":set(),"closed":False}; clients.append(st); task=asyncio.create_task(bridge(st))
 async def registration():
  for num,text in [("001","Welcome"),("002","Your host is huddle"),("003","This server was created today"),("004","huddle huddle 1 1"),("005","CHANTYPES=# :are supported by this server"),("422","MOTD File is missing")]: await send(writer,f":huddle {num} {st['nick']} :{text}")
 try:
  while True:
   line=await reader.readline()
   if not line: break
   x=line.decode(errors="ignore").rstrip("\r\n"); parts=x.split(" ",2); cmd=parts[0].upper() if parts else ""; args=parts[1:]
   if cmd=="PASS":
    st["user"]=token_user(args[0] if args else "")
    if not st["user"]: await send(writer,":huddle 464 * :Password incorrect")
   elif cmd=="NICK":
    nn=args[0].lstrip(":") if args else ""
    if any((q["nick"] or "").lower()==nn.lower() for q in clients if q is not st and not q["closed"]): await send(writer,f":huddle 433 * {nn} :Nickname is already in use")
    else:
     st["nick"]=nn
     if st["user"] and st.get("user_seen") and not st.get("registered"): st["registered"]=True; await registration()
   elif cmd=="USER":
    st["user_seen"]=True
    if st["user"] and st["nick"] and not st.get("registered"): st["registered"]=True; await registration()
   elif cmd=="PING": await send(writer,f":huddle PONG huddle :{args[0].lstrip(':') if args else ''}")
   elif cmd=="QUIT": break
   elif cmd=="JOIN":
    name=args[0] if args else ""; ch=chan(name)
    if not ch: await send(writer,f":huddle 403 {st['nick']} {name} :No such channel"); continue
    add_member(ch,st["user"]["id"])
    if ch["id"] not in [x["id"] for x in st["joined"]]:
     st["joined"].append(ch); c=conn(); h=c.execute("SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?",(ch["id"],)).fetchone()["n"]; c.close(); st["last"][ch["id"]]=h
    await send(writer,f":{st['nick']}!{st['user']['username']}@localhost JOIN {name}")
    await send(writer,f":huddle 353 {st['nick']} = {name} :{st['nick']}")
    await send(writer,f":huddle 366 {st['nick']} {name} :End of /NAMES list")
   elif cmd=="PRIVMSG" and len(args)>=2:
    name=args[0]; body=args[1].lstrip(":"); ch=chan(name)
    if ch and st["user"] and ch["id"] in [x["id"] for x in st["joined"]]: st["own"].add(make_msg(ch,st["user"],body))
   elif cmd in ("PONG","NAMES","WHO","LIST","TOPIC","MODE"): pass
   elif cmd in ("","PASS","NICK","USER"): pass
   else: await send(writer,f":huddle 421 {st['nick'] or '*'} {cmd} :Unknown command")
 finally:
  st["closed"]=True; clients.remove(st) if st in clients else None; task.cancel()
  try: writer.close(); await writer.wait_closed()
  except: pass
async def main():
 s=await asyncio.start_server(handle_client,"0.0.0.0",6667)
 async with s: await s.serve_forever()
if __name__=="__main__": asyncio.run(main())
