import asyncio,sqlite3,re
DB='/app/data/huddle.db'; clients={}; nicks=set(); own={}
def conn():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row; return c
def lookup(t):
 c=conn(); r=c.execute('SELECT u.id,u.username FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone();c.close();return r
def find(name):
 c=conn(); r=c.execute('SELECT ch.* FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE "#"||w.slug||"/"||ch.name=?',(name,)).fetchone();c.close();return r
def addmsg(ch,uid,body):
 c=conn(); ts=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat().replace('+00:00','Z')
 cur=c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch,uid,body,ts)); mid=cur.lastrowid
 a=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
 m={'id':mid,'channel_id':ch,'author_id':uid,'author':dict(a),'body':body,'parent_id':None,'reply_count':0,'created_at':ts,'edited_at':None,'files':[],'reactions':[],'mentions':[]}
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(ch,)).fetchone()[0]
 c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(ch,n,'message.created',__import__('json').dumps({'message':m})));c.commit();c.close();return n
async def send(w,s):
 try:w.write((s+'\r\n').encode());await w.drain()
 except:pass
async def numeric(w,code,nick,text):
 await send(w,f':huddle {code} {nick or "*"} :{text}')
async def register(w,nick):
 for code,text in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle 1.0 aio'),('005','CHANTYPES=# :are supported'),('422','MOTD File is missing')]: await numeric(w,code,nick,text)
async def bridge():
 last=0
 while True:
  await asyncio.sleep(.08); c=conn(); rows=c.execute("SELECT e.*,w.slug,ch.name FROM events e JOIN channels ch ON ch.id=e.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE e.id>? ORDER BY e.id",(last,)).fetchall()
  for r in rows:
   last=max(last,r['id'])
   if r['type']!='message.created':continue
   m=__import__('json').loads(r['payload']).get('message',{}); body=m.get('body','')
   a=c.execute('SELECT username FROM users WHERE id=?',(m.get('author_id'),)).fetchone(); uname=a['username'] if a else 'user'; target='#'+r['slug']+'/'+r['name']
   for w,info in list(clients.items()):
    if r['channel_id'] in info['channels'] and r['id'] not in own.get(w,set()):
     await send(w,f":{uname}!{uname}@huddle PRIVMSG {target} :{body}")
  c.close()
async def client(reader,w):
 user=None;nick=None;aut=False;seen=False;reg=False;info={'channels':set()};clients[w]=info;own[w]=set()
 async def num(code,text): await numeric(w,code,nick,text)
 try:
  while True:
   raw=await reader.readline()
   if not raw:break
   line=raw.decode(errors='replace').strip('\r\n'); parts=line.split(' ',1); cmd=parts[0].upper();arg=parts[1] if len(parts)>1 else ''
   if cmd=='PASS':
    user=lookup(arg.strip());aut=bool(user)
    if not aut:await num('464','Password incorrect')
   elif cmd=='NICK':
    nn=arg.strip().split()[0] if arg.strip() else ''
    if nn.lower() in nicks and nn.lower()!=str(nick).lower():await num('433','Nickname in use');continue
    if nick:nicks.discard(nick.lower())
    nick=nn;nicks.add(nn.lower())
    if aut and seen and not reg:reg=True;await register(w,nick)
   elif cmd=='USER':
    seen=True
    if aut and nick and not reg:reg=True;await register(w,nick)
   elif cmd=='PING':await send(w,':huddle PONG huddle :'+arg.lstrip(':'))
   elif cmd=='JOIN':
    name=arg.split()[0] if arg else '';ch=find(name)
    if not ch:await num('403','No such channel');continue
    c=conn();c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],user['id'],'member'));c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],user['id']));c.commit();c.close();info['channels'].add(ch['id'])
    await send(w,f':{nick}!{user["username"]}@huddle JOIN {name}');await numeric(w,'353',nick,f'= {name} :{nick}');await numeric(w,'366',nick,f'End of NAMES list')
   elif cmd=='PRIVMSG':
    target,body=(arg.split(' :',1)+[''])[:2] if ' :' in arg else ('','')
    ch=find(target)
    if ch and body:
     eid=addmsg(ch['id'],user['id'],body);own[w].add(eid)
   elif cmd=='QUIT':break
   elif cmd in ('PONG','NAMES','WHO','LIST','TOPIC','MODE'):
    if cmd=='NAMES':await num('353','= * :'+nick)
    elif cmd=='WHO':await num('315','End of WHO list')
    elif cmd=='LIST':await num('323','End of LIST')
    elif cmd=='TOPIC':await num('331','No topic is set')
   else:await num('421',cmd+' Unknown command')
 except Exception:pass
 finally:
  clients.pop(w,None);own.pop(w,None)
  if nick:nicks.discard(nick.lower())
  w.close()
async def main():
 asyncio.create_task(bridge());s=await asyncio.start_server(client,'0.0.0.0',6667)
 async with s:await s.serve_forever()
if __name__=='__main__':asyncio.run(main())
