import asyncio,re,sqlite3,json,os
from app import DB,token_user
clients=set(); joined={}; active_nicks=set()
def conn():
 c=sqlite3.connect(DB,timeout=10);c.row_factory=sqlite3.Row;return c
async def send(w,s):
 try:w.write((s+'\r\n').encode());await w.drain()
 except:pass
async def bridge():
 seen={}
 while True:
  try:
   c=conn()
   for e in c.execute('SELECT e.*,ch.name cn,w.slug FROM events e JOIN channels ch ON ch.id=e.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE e.type IN ("message.created","message.reply") ORDER BY e.id'):
    if e['id']<=seen.get(e['channel_id'],0):continue
    seen[e['channel_id']]=e['id'];p=json.loads(e['payload']);m=p.get('message',{});name='#'+e['slug']+'/'+e['cn']
    for w,u in list(clients):
     if e['channel_id'] in joined.get(w,set()) and m.get('author_id')!=u['id']:
      await send(w,f":{m.get('author',{}).get('username','user')}!{m.get('author',{}).get('username','user')}@localhost PRIVMSG {name} :{m.get('body','')}")
   c.close()
  except:pass
  await asyncio.sleep(.1)
async def client(r,w):
 clients.add((w,None)); user=None;nick=None; reg=False; user_sent=False
 async def finish():
  nonlocal reg
  if user and nick and user_sent and not reg:
   reg=True; active_nicks.add(nick); clients.discard((w,None));clients.add((w,user))
   for n in ('001','002','003','004','005'): await send(w,f':irc {n} {nick} :huddle')
   await send(w,':irc 422 * :MOTD File is missing')
 try:
  while True:
   line=await r.readline()
   if not line:break
   s=line.decode(errors='ignore').strip(); parts=s.split(' ',2);cmd=parts[0].upper();arg=parts[1] if len(parts)>1 else ''
   if cmd=='PASS':
    user=token_user(arg); 
    if not user:await send(w,':irc 464 * :Password incorrect')
   elif cmd=='NICK':
    if arg in active_nicks: await send(w,f':irc 433 {nick or "*"} {arg} :Nickname is already in use')
    else:
     nick=arg
     await finish()
   elif cmd=='USER' and user and not reg:
    user_sent=True
    await finish()
   elif cmd=='PING':await send(w,':irc PONG '+(arg or ''))
   elif cmd=='QUIT':break
   elif cmd=='JOIN' and reg:
    name=arg;c=conn();x=c.execute('SELECT ch.* ,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ("#"||w.slug||"/"||ch.name)=?',(name,)).fetchone()
    if not x:await send(w,f':irc 403 {nick} {name} :No such channel')
    else:
     c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(x['id'],user['id']));c.commit();c.close();joined.setdefault(w,set()).add(x['id'])
     await send(w,f':{nick}!{user["username"]}@localhost JOIN {name}');await send(w,f':irc 353 {nick} = {name} :{nick}');await send(w,f':irc 366 {nick} {name} :End of NAMES list')
   elif cmd=='PRIVMSG' and reg and len(parts)>2:
    name=arg;body=parts[2].lstrip(':');c=conn();x=c.execute('SELECT ch.* FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ("#"||w.slug||"/"||ch.name)=?',(name,)).fetchone()
    if x:
     c.execute('INSERT INTO messages(channel_id,author_id,body,created) VALUES(?,?,?,datetime("now"))',(x['id'],user['id'],body));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];r2=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
     from app import msg_obj,emit
     emit(c,x['id'],'message.created',{'message':msg_obj(c,r2)});c.commit();c.close()
   elif cmd in ('PONG','NAMES','WHO','LIST','TOPIC','MODE'):pass
   elif cmd not in ('PASS','NICK','USER'):await send(w,f':irc 421 {nick or "*"} {cmd} :Unknown command')
 finally:
  clients.discard((w,user)); clients.discard((w,None)); active_nicks.discard(nick); joined.pop(w,None);w.close()
async def main():
 asyncio.create_task(bridge());server=await asyncio.start_server(client,'0.0.0.0',6667);await server.serve_forever()
if __name__=='__main__':asyncio.run(main())
