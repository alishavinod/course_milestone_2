import socketserver, sqlite3, threading, time, json, os
DB='/app/data/huddle.db'; nicks=set(); clients=[]; lock=threading.RLock()
def db():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def token_user(t):
 c=db(); r=c.execute('select user_id from tokens where token=?',(t,)).fetchone(); c.close(); return r
def lookup(name):
 if not name.startswith('#') or '/' not in name: return None
 slug,ch=name[1:].split('/',1); c=db()
 r=c.execute('select c.*,w.slug,w.owner_id from channels c join workspaces w on w.id=c.workspace_id where w.slug=? and c.name=?',(slug,ch)).fetchone(); c.close(); return r
def channel_name(cid):
 c=db(); r=c.execute('select w.slug,c.name from channels c join workspaces w on w.id=c.workspace_id where c.id=?',(cid,)).fetchone(); c.close()
 return '#%s/%s'%(r['slug'],r['name']) if r else ''
def insert_message(cid,uid,body):
 c=db(); ts=time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime())
 c.execute('insert or ignore into cmembers values(?,?)',(cid,uid))
 c.execute('insert into messages(channel_id,author_id,body,created_at,mention_json) values(?,?,?,?,?)',(cid,uid,body,ts,'[]')); mid=c.execute('select last_insert_rowid()').fetchone()[0]
 row=c.execute('select * from messages where id=?',(mid,)).fetchone()
 n=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(cid,)).fetchone()[0]
 payload={'message':dict(row)}
 c.execute('insert into events values(?,?,?,?,?)',(cid,n,'message.created',json.dumps(payload),ts)); c.commit(); c.close(); return mid
class IRC(socketserver.StreamRequestHandler):
 def setup(self):
  super().setup(); self.token=None; self.uid=None; self.nick=None; self.registered=False; self.joined={}; self.stop=False; self.last={}
 def send(self,s):
  try:self.wfile.write((s+'\r\n').encode()); self.wfile.flush()
  except: pass
 def numeric(self,code,text=''): self.send(f':huddle {code} {self.nick or "*"} {text}')
 def register(self):
  if self.registered or not self.token or not self.nick or not self.uid:return
  with lock:
   if self.nick in nicks:self.numeric('433','Nickname in use'); self.nick=None; return
   nicks.add(self.nick)
  self.registered=True
  for code,text in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1 1'),('005','CHANTYPES=# :are supported by this server'),('422','MOTD File is missing')]: self.numeric(code,text)
 def pump(self):
  while not self.stop:
   try:
    for name,cid in list(self.joined.items()):
     c=db(); rows=c.execute("select e.*,m.body,m.author_id,u.username from events e join messages m on json_extract(e.payload,'$.message.id')=m.id join users u on u.id=m.author_id where e.channel_id=? and e.event_id>? and e.type='message.created' order by e.event_id",(cid,self.last.get(cid,0))).fetchall(); self.last[cid]=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(cid,)).fetchone()[0]; c.close()
     for r in rows:
      if r['author_id']==self.uid: continue
      self.send(f":{r['username']}!user@host PRIVMSG {name} :{r['body']}")
   except: pass
   time.sleep(.05)
 def handle(self):
  threading.Thread(target=self.pump,daemon=True).start()
  try:
   for raw in self.rfile:
    z=raw.decode(errors='replace').strip(); parts=z.split(' ',2); cmd=parts[0].upper(); args=parts[1:]
    if cmd=='PASS':
     r=token_user(args[0] if args else '')
     if not r:self.numeric('464','Password incorrect')
     else:self.token=args[0]; self.uid=r['user_id']; self.register()
    elif cmd=='NICK':
     self.nick=args[0] if args else ''; self.register()
    elif cmd=='USER': self.register()
    elif cmd=='PING': self.send(':huddle PONG huddle :'+(args[-1].lstrip(':') if args else ''))
    elif cmd=='QUIT': return
    elif cmd=='JOIN':
     name=args[0] if args else ''; ch=lookup(name)
     if not ch:self.numeric('403',f'{name} :No such channel'); continue
     c=db(); c.execute('insert or ignore into cmembers values(?,?)',(ch['id'],self.uid)); c.commit(); c.close(); self.joined[name]=ch['id']; self.last[ch['id']]=0
     self.send(f':{self.nick}!user@host JOIN {name}'); self.numeric('353',f'= {name} :{self.nick}'); self.numeric('366',f'{name} :End of NAMES list')
    elif cmd=='PRIVMSG' and len(parts)>=3:
     ch=lookup(args[0] if args else '')
     if ch: insert_message(ch['id'],self.uid,parts[2].lstrip(':'))
    elif cmd=='NAMES':
     self.numeric('353',f'= {args[0] if args else "*"} :{self.nick}'); self.numeric('366',f'{args[0] if args else "*"} :End of NAMES list')
    elif cmd=='TOPIC': self.numeric('331',f'{args[0] if args else "*"} :No topic is set')
    elif cmd in ('PONG','WHO','LIST','MODE'): pass
    elif self.registered:self.numeric('421',f'{cmd} :Unknown command')
  finally:
   self.stop=True
   with lock:
    if self.nick:nicks.discard(self.nick)
class Server(socketserver.ThreadingTCPServer): allow_reuse_address=True
with Server(('0.0.0.0',6667),IRC) as srv: srv.serve_forever()
