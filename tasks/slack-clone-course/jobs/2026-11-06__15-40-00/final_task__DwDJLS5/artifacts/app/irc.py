import asyncio,sqlite3,json,re,time,os
DB='/app/data/huddle.db'; clients=set(); nicks=set()
def db():
 c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=30000'); return c
def q(sql,a=(),one=False):
 c=db(); x=c.execute(sql,a); r=x.fetchone() if one else x.fetchall(); c.commit(); return r
def user(token):
 return q('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(token,),True)
def chan(name):
 if not name.startswith('#') or '/' not in name[1:]: return None
 slug,cn=name[1:].split('/',1)
 return q('SELECT c.*,w.slug,w.owner_id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,cn),True)
def cobj(c): return '#%s/%s'%(c['slug'],c['name'])
def send(w,s):
 if hasattr(w,'w'): w=w.w
 w.write((s+'\r\n').encode())
async def register(w):
 for n in ('001','002','003','004','005'):
  send(w,':huddle '+n+' '+w.nick+' :Welcome')
 send(w,':huddle 422 '+w.nick+' :MOTD File is missing')
 w.registered=True
async def handle_client(r,w):
 x=IRC(r,w); clients.add(x); token=None
 try:
  while True:
   line=(await r.readline()).decode(errors='ignore').strip('\r\n')
   if not line: continue
   a=line.split(' ',1); cmd=a[0].upper(); rest=a[1] if len(a)>1 else ''
   if cmd=='PASS':
    token=rest.strip(); x.user=user(token)
    if not x.user: send(x,':huddle 464 * :Password incorrect')
   elif cmd=='NICK':
    nick=rest.strip().lstrip(':')
    if nick in nicks and nick!=getattr(x,'nick',None): send(w,':huddle 433 * '+nick+' :Nickname is already in use'); continue
    if getattr(x,'nick',None): nicks.discard(x.nick)
    x.nick=nick; nicks.add(nick)
    if x.user and getattr(x,'real',None) and not x.registered: await register(x)
   elif cmd=='USER':
    x.real=rest
    if x.user and getattr(x,'nick',None) and not x.registered: await register(x)
   elif cmd=='PING': send(w,':huddle PONG huddle :'+rest.lstrip(':'))
   elif cmd=='QUIT': break
   elif cmd in ('PONG','WHO','LIST','TOPIC','MODE'): 
    if cmd=='TOPIC' and rest: send(w,':huddle 331 '+x.nick+' '+rest.split()[0]+' :No topic')
   elif cmd=='NAMES':
    c=chan(rest.split()[0] if rest else '')
    if c: await names(x,c)
   elif cmd=='JOIN':
    c=chan(rest.split()[0])
    if not c: send(w,':huddle 403 '+x.nick+' '+rest+' :No such channel'); continue
    q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c['workspace_id'],x.user['id'],'member'))
    q('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(c['id'],x.user['id'])); x.joined.add(c['id'])
    ir=cobj(c); send(w,f':{x.nick}!{x.user["username"]}@localhost JOIN {ir}'); await names(x,c)
   elif cmd=='PRIVMSG':
    z=rest.split(' :',1); target=z[0].strip(); body=z[1] if len(z)>1 else ''
    c=chan(target)
    if c and c['id'] in x.joined: await post(c,x.user,body)
   else: send(w,':huddle 421 '+x.nick+' '+cmd+' :Unknown command')
 finally:
  clients.discard(x); nicks.discard(getattr(x,'nick',''))
  try: w.close(); await w.wait_closed()
  except: pass
async def names(x,c):
 rows=q('SELECT u.username FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(c['id'],))
 send(x.w,':huddle 353 '+x.nick+' = '+cobj(c)+' :'+ ' '.join('@'+z['username'] for z in rows))
 send(x.w,':huddle 366 '+x.nick+' '+cobj(c)+' :End of NAMES list')
async def post(c,u,body):
 if not body: return
 q('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(c['id'],u['id'],body,time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())))
 m=q('SELECT * FROM messages WHERE channel_id=? AND author_id=? ORDER BY id DESC LIMIT 1',(c['id'],u['id']),True)
 with db() as z:
  n=z.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(c['id'],)).fetchone()[0]
  payload={'message':{'id':m['id'],'channel_id':c['id'],'author_id':u['id'],'body':body,'parent_id':None,'reply_count':0,'created_at':m['created_at'],'edited_at':None,'files':[],'reactions':[],'mentions':[]}}
  z.execute('INSERT INTO events VALUES(?,?,?,?,?)',(c['id'],n,'message.created',json.dumps(payload),m['created_at']))
async def bridge():
 seen={}
 while True:
  await asyncio.sleep(.1)
  for c in q('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id'):
   last=seen.get(c['id'],0)
   for e in q('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(c['id'],last)):
    seen[c['id']]=e['event_id']; p=json.loads(e['payload'])
    if e['type']=='message.created':
     m=p['message']; a=q('SELECT username FROM users WHERE id=?',(m['author_id'],),True)
     for x in list(clients):
      if c['id'] in x.joined:
       send(x.w,':%s!%s@localhost PRIVMSG %s :%s'%(a['username'],a['username'],cobj(c),m['body']))
class IRC:
 def __init__(self,r,w): self.r=r; self.w=w; self.joined=set(); self.registered=False; self.nick='*'; self.user=None; self.real=None
async def main():
 server=await asyncio.start_server(handle_client,'0.0.0.0',6667)
 asyncio.create_task(bridge())
 await asyncio.Event().wait()
 server.close()
 await server.wait_closed()
asyncio.run(main())
