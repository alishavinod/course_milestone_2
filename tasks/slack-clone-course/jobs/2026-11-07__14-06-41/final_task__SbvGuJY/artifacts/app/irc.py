import asyncio,sqlite3,json,datetime
DB='/app/data/app.db'; nicks=set(); clients={}; lock=asyncio.Lock()
def db():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def user_for(t):
 c=db(); r=c.execute('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def find_irc_channel(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,cn=name[1:].split('/',1); c=db(); r=c.execute('SELECT ch.* FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,cn)).fetchone(); c.close(); return r
def emit(cid,kind,payload):
 c=db(); c.execute('BEGIN IMMEDIATE'); eid=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']; c.execute('INSERT INTO events VALUES(?,?,?,?)',(cid,eid,kind,json.dumps(payload))); c.commit(); c.close()
def post(cid,uid,body):
 now=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z'); c=db(); cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,uid,body,None,now)); c.commit(); m=c.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone(); obj={'id':m['id'],'channel_id':cid,'author_id':uid,'body':body,'parent_id':None,'reply_count':0,'created_at':now,'edited_at':None,'files':[],'reactions':[],'mentions':[]}; emit(cid,'message.created',{'message':obj}); c.close()
async def bridge():
 seen={}
 while True:
  await asyncio.sleep(.1)
  for cid,arr in list(clients.items()):
   c=db(); rows=c.execute("SELECT * FROM events WHERE channel_id=? AND event_id>? AND type='message.created' ORDER BY event_id",(cid,seen.get(cid,0))).fetchall(); c.close()
   for e in rows:
    p=json.loads(e['payload'])['message']; c=db(); u=c.execute('SELECT username FROM users WHERE id=?',(p['author_id'],)).fetchone(); w=c.execute('SELECT w.slug,ch.name FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(cid,)).fetchone(); c.close()
    line=f":{u['username']}!{u['username']}@huddle PRIVMSG #{w['slug']}/{w['name']} :{p['body']}"
    for cl in arr:
     if cl.get('uid')!=p['author_id']:
      try: await cl['send'](line)
      except: pass
    seen[cid]=e['event_id']
async def handle_client(reader,writer):
 token=nick=user=None; registered=False; joined=set()
 async def send(x): writer.write((x+'\r\n').encode()); await writer.drain()
 try:
  while not reader.at_eof():
   raw=await reader.readline()
   if not raw:break
   line=raw.decode(errors='ignore').strip('\r\n'); ps=line.split(' ',2); cmd=ps[0].upper(); arg=ps[1] if len(ps)>1 else ''; rest=ps[2] if len(ps)>2 else ''
   if cmd=='PASS':
    token=arg; user=user_for(token)
    if not user: await send(':huddle 464 * :Password incorrect')
   elif cmd=='NICK':
    async with lock:
     if arg in nicks and arg!=nick: await send(f':huddle 433 * {arg} :Nickname is already in use'); continue
     if nick:nicks.discard(nick)
     nick=arg;nicks.add(nick)
   elif cmd=='USER' and token and user and nick and not registered:
    registered=True
    for n,t in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1'),('005','CHANTYPES=# :are supported'),('422','MOTD File is missing')]: await send(f':huddle {n} {nick} :{t}')
   elif cmd=='PING': await send(f':huddle PONG huddle :{arg}')
   elif cmd=='JOIN':
    ch=find_irc_channel(arg)
    if not ch: await send(f':huddle 403 {nick} {arg} :No such channel'); continue
    c=db(); c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],user['id'])); c.commit(); c.close(); joined.add(ch['id']); clients.setdefault(ch['id'],[]).append({'send':send,'uid':user['id']}); await send(f':{nick}!{user["username"]}@huddle JOIN {arg}'); await send(f':huddle 353 {nick} = {arg} :{nick}'); await send(f':huddle 366 {nick} {arg} :End of NAMES list')
   elif cmd=='PRIVMSG' and arg.startswith('#') and user:
    ch=find_irc_channel(arg)
    if ch and rest.startswith(':'): post(ch['id'],user['id'],rest[1:])
   elif cmd=='QUIT': break
   elif cmd in ('PONG','NAMES','WHO','LIST','TOPIC','MODE'): await send(f':huddle NOTICE {nick or "*"} :OK')
   elif cmd: await send(f':huddle 421 {nick or "*"} {cmd} :Unknown command')
 finally:
  for cid in joined:
   clients[cid]=[x for x in clients.get(cid,[]) if x.get('uid')!=user['id']]
  if nick:
   async with lock:nicks.discard(nick)
  writer.close()
async def main():
 asyncio.create_task(bridge()); s=await asyncio.start_server(handle_client,'0.0.0.0',6667)
 async with s: await s.serve_forever()
if __name__=='__main__':asyncio.run(main())
