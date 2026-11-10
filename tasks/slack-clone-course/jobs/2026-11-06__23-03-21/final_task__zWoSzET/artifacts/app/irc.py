import asyncio,sqlite3,json
DB='/app/data/app.db'; nicks=set(); clients=[]
def db():
 d=sqlite3.connect(DB,timeout=30); d.row_factory=sqlite3.Row; d.execute('PRAGMA busy_timeout=30000'); return d
def channel(d,name):
 if not name.startswith('#') or '/' not in name: return None
 slug,cname=name[1:].split('/',1)
 return d.execute('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,cname)).fetchone()
async def client(r,w):
 nick=None; uid=None; authed=False; registered=False; user_seen=False; joined=set(); seen={}
 async def send(x): w.write((x+'\r\n').encode()); await w.drain()
 clients.append((w,lambda: joined,nick))
 try:
  while True:
   try: line=await asyncio.wait_for(r.readline(),.2)
   except asyncio.TimeoutError:
    d=db()
    for cid in list(joined):
     last=seen.get(cid,0)
     for e in d.execute('SELECT * FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)):
      if e['kind']!='message.created': continue
      x=json.loads(e['payload']); m=d.execute('SELECT m.*,u.username FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?',(x['message_id'],)).fetchone()
      if not m: continue
      c=d.execute('SELECT c.name,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,)).fetchone()
      if m['author_id']!=uid: await send(f":{m['username']}!{m['username']}@huddle PRIVMSG #{c['slug']}/{c['name']} :{m['body']}")
      seen[cid]=e['event_id']
    continue
   if not line: break
   p=line.decode(errors='ignore').strip().split(' ',2); cmd=p[0].upper() if p else ''; arg=p[1] if len(p)>1 else ''; rest=p[2] if len(p)>2 else ''
   if cmd=='PASS':
    d=db(); z=d.execute('SELECT user_id FROM tokens WHERE token=?',(arg,)).fetchone(); d.close(); authed=bool(z); uid=z['user_id'] if z else None
    if not authed: await send(':huddle 464 * :Password incorrect')
   elif cmd=='NICK':
    if arg in nicks and arg!=nick: await send(':huddle 433 * '+arg+' :Nickname is already in use'); continue
    if nick: nicks.discard(nick)
    nick=arg; nicks.add(nick)
    if authed and user_seen and not registered: registered=True; await register(send,nick)
   elif cmd=='USER':
    user_seen=True
    if not authed: await send(':huddle 464 * :Password incorrect'); continue
    if nick and not registered: registered=True; await register(send,nick)
   elif cmd=='PING': await send(':huddle PONG :'+(rest or arg))
   elif cmd in ('PONG','WHO','LIST','TOPIC','MODE'): pass
   elif cmd=='JOIN':
    d=db(); c=channel(d,arg)
    if not c: await send(f':huddle 403 {nick} {arg} :No such channel'); continue
    cid=c['id']; joined.add(cid); seen[cid]=d.execute('SELECT COALESCE(MAX(event_id),0) n FROM channel_events WHERE channel_id=?',(cid,)).fetchone()['n']
    d.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,uid)); d.commit()
    await send(f':{nick}!{nick}@huddle JOIN {arg}')
    names=[nick]+[z['username'] for z in d.execute('SELECT u.username FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=? AND u.id<>?',(cid,uid))]
    await send(f':huddle 353 {nick} = {arg} :{" ".join(names)}'); await send(f':huddle 366 {nick} {arg} :End of /NAMES list')
   elif cmd=='PRIVMSG':
    d=db(); c=channel(d,arg)
    if not c: await send(f':huddle 403 {nick} {arg} :No such channel'); continue
    body=rest[1:] if rest.startswith(':') else rest
    q=d.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,datetime("now"))',(c['id'],uid,body)); mid=q.lastrowid
    seq=d.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(c['id'],)).fetchone()['n']; d.execute('INSERT INTO channel_events VALUES(?,?,?,?)',(c['id'],seq,'message.created',json.dumps({'message_id':mid}))); d.commit()
   elif cmd=='QUIT': break
   elif cmd: await send(f':huddle 421 {nick or "*"} {cmd} :Unknown command')
 finally:
  if nick: nicks.discard(nick)
  try: clients.remove((w,lambda: joined,nick))
  except: pass
  w.close()
async def register(send,nick):
 for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1 0'),('005','CHANTYPES=# PREFIX=(ov)@+'),('422','MOTD File is missing')]: await send(f':huddle {code} {nick} :{text}')
async def main():
 s=await asyncio.start_server(client,'0.0.0.0',6667)
 async with s: await s.serve_forever()
asyncio.run(main())
