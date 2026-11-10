import asyncio,sqlite3,os,json,time
DB='/app/data/huddle.db'; nicks=set(); clients=[]
def con(): c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; return c
def user_for(t):
 c=con(); r=c.execute('select user_id from tokens where token=?',(t,)).fetchone(); c.close(); return r['user_id'] if r else None
def channel(name):
 if not name.startswith('#') or '/' not in name[1:]: return None
 slug,cn=name[1:].split('/',1); c=con(); r=c.execute('select ch.*,w.slug,w.owner_id from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(slug,cn)).fetchone(); c.close(); return r
async def send(w,line):
 try: w.write((line+'\r\n').encode()); await w.drain()
 except: pass
async def broadcast():
 while True:
  await asyncio.sleep(.05)
  for cl in list(clients):
   if not cl.get('joined'): continue
   c=con()
   rows=c.execute('select event_id,payload from channel_events where channel_id=? and event_id>? order by event_id',(cl['joined']['id'],cl['last'])).fetchall(); c.close()
   for x in rows:
    cl['last']=x['event_id']; e=json.loads(x['payload'])
    if e.get('type')=='message.created':
     c=con(); a=c.execute('select username from users where id=?',(e['message']['author_id'],)).fetchone(); c.close()
     if a: await send(cl['writer'],f":{a['username']}!user@host PRIVMSG {cl['name']} :{e['message']['body']}")
async def handle_client(reader,writer):
 token=nick=None; registered=False; user_seen=False; joined=None
 async def finish():
  nonlocal registered
  if token and user_seen and nick and user_for(token) and not registered:
   registered=True
   for code,text in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle'),('005','CHANTYPES=#'),('422','MOTD File is missing')]: await send(writer,f':huddle {code} {nick} :{text}')
 state={'writer':writer,'joined':None,'last':0}
 try:
  while True:
   raw=await reader.readline()
   if not raw: break
   line=raw.decode(errors='ignore').strip('\r\n'); p=line.split(' ',1); cmd=p[0].upper(); arg=p[1] if len(p)>1 else ''
   if cmd=='PASS':
    token=arg.strip()
    if not user_for(token): await send(writer,':huddle 464 * :Password incorrect')
   elif cmd=='NICK':
    nn=arg.split()[0] if arg else ''
    if nn in nicks and nn!=nick: await send(writer,f':huddle 433 * {nn} :Nickname is already in use')
    else:
     if nick: nicks.discard(nick)
     nick=nn; nicks.add(nick); await finish()
   elif cmd=='USER': user_seen=True; await finish()
   elif cmd=='PING': await send(writer,':huddle PONG '+arg.lstrip(':'))
   elif cmd=='JOIN':
    ch=channel(arg.strip())
    if not ch: await send(writer,f':huddle 403 {nick} {arg} :No such channel'); continue
    uid=user_for(token); c=con(); c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],uid,'member')); c.execute('insert or ignore into cmembers values(?,?)',(ch['id'],uid)); c.commit(); head=c.execute('select coalesce(max(event_id),0) from channel_events where channel_id=?',(ch['id'],)).fetchone()[0]; c.close()
    joined=ch; state['joined']=ch; state['name']=arg.strip(); state['last']=head; clients.append(state)
    await send(writer,f':{nick}!user@host JOIN {arg.strip()}'); await send(writer,f':huddle 353 {nick} = {arg.strip()} :{nick}'); await send(writer,f':huddle 366 {nick} {arg.strip()} :End of NAMES list')
   elif cmd=='PRIVMSG' and joined:
    target,body=(arg.split(' :',1)+[''])[:2] if ' :' in arg else (arg,'')
    if target==state.get('name') and body:
     uid=user_for(token); c=con(); c.execute('BEGIN IMMEDIATE'); ev=c.execute('select coalesce(max(event_id),0)+1 from channel_events where channel_id=?',(joined['id'],)).fetchone()[0]; c.execute('insert into messages(channel_id,author_id,body,created_at,event_id) values(?,?,?,?,?)',(joined['id'],uid,body,datetime(),ev)); mid=c.execute('select last_insert_rowid()').fetchone()[0]; m=dict(c.execute('select * from messages where id=?',(mid,)).fetchone()); e={'type':'message.created','event_id':ev,'channel_id':joined['id'],'message':m}; c.execute('insert into channel_events(channel_id,event_id,payload) values(?,?,?)',(joined['id'],ev,json.dumps(e))); c.commit(); c.close()
   elif cmd in ('PONG','NAMES','WHO','LIST','TOPIC','MODE'): pass
   elif cmd=='QUIT': break
   else: await send(writer,f':huddle 421 {nick or "*"} {cmd} :Unknown command')
 finally:
  if state in clients: clients.remove(state)
  if nick: nicks.discard(nick)
  writer.close()
def datetime(): return time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
async def main():
 s=await asyncio.start_server(handle_client,'0.0.0.0',6667); asyncio.create_task(broadcast())
 async with s: await s.serve_forever()
if __name__=='__main__': asyncio.run(main())
