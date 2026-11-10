import socket,threading,sqlite3,os,json,time,fcntl
DB='/app/data/huddle.db'
def db():
 c=sqlite3.connect(DB,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def send(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
def client(s,o):
 nick=None; uid=None; aut=False; joined=o.joined; user_seen=False; registered=False
 def prefix(): return f':{nick}!{nick}@localhost'
 try:
  buf=''
  while True:
   z=s.recv(4096)
   if not z:return
   buf+=z.decode(errors='ignore')
   while '\r\n' in buf:
    line,buf=buf.split('\r\n',1); a=line.split(); cmd=a[0].upper() if a else ''
    if cmd=='PASS':
     r=db().execute('SELECT user_id FROM tokens WHERE token=?',(a[1] if len(a)>1 else '',)).fetchone()
     if r: uid=r['user_id']; aut=True
     else: send(s,':huddle 464 * :Password incorrect')
    elif cmd=='NICK' and len(a)>1:
     n=a[1]
     with nick_lock:
      used=n in active_nicks and n != nick
      if used:
       send(s,f':huddle 433 * {n} :Nickname is already in use')
       continue
      if nick: active_nicks.discard(nick)
      nick=n; active_nicks.add(nick)
     if aut and user_seen and not registered:
      send(s,f':huddle 001 {nick} :Welcome to Huddle');send(s,f':huddle 002 {nick} :Your host is huddle');send(s,f':huddle 003 {nick} :This server');send(s,f':huddle 004 {nick} huddle');send(s,f':huddle 005 {nick} CHANTYPES=# :are supported');send(s,f':huddle 422 {nick} :MOTD File is missing'); registered=True
    elif cmd=='USER' and aut:
     user_seen=True
     if not registered:
      send(s,f':huddle 001 {nick} :Welcome to Huddle');send(s,f':huddle 002 {nick} :Your host is huddle');send(s,f':huddle 003 {nick} :This server');send(s,f':huddle 004 {nick} huddle');send(s,f':huddle 005 {nick} CHANTYPES=# :are supported');send(s,f':huddle 422 {nick} :MOTD File is missing'); registered=True
    elif cmd=='PING':send(s,':huddle PONG huddle :'+(a[1] if len(a)>1 else ''))
    elif cmd=='PONG': pass
    elif cmd=='QUIT':return
    elif cmd=='JOIN' and len(a)>1:
     name=a[1]; q=db().execute("SELECT c.*,w.slug FROM channels c LEFT JOIN workspaces w ON w.id=c.workspace_id WHERE '#'||w.slug||'/'||c.name=?",(name,)).fetchone()
     if not q: send(s,f':huddle 403 {nick} {name} :No such channel');continue
     cjoin=db(); cjoin.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(q['id'],uid)); cjoin.commit()
     joined.append((name,q['id'])); o.nick=nick; o.uid=uid; send(s,f'{prefix()} JOIN {name}')
     rows=cjoin.execute('SELECT u.username FROM cmembers m JOIN users u ON u.id=m.user_id WHERE m.channel_id=?',(q['id'],)).fetchall()
     names=' '.join([x['username'] for x in rows]);send(s,f':huddle 353 {nick} = {name} :{names}');send(s,f':huddle 366 {nick} {name} :End of NAMES list')
     cjoin.close()
    elif cmd=='NAMES' and len(a)>1: send(s,f':huddle 366 {nick} {a[1]} :End of NAMES list')
    elif cmd=='PRIVMSG' and len(a)>2:
     name=a[1]; body=line.split(' :',1)[1] if ' :' in line else ' '.join(a[2:])
     q=db().execute("SELECT c.id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE '#'||w.slug||'/'||c.name=?",(name,)).fetchone()
     if q:
      c=db(); c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(q['id'],uid,body,__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat().replace('+00:00','Z'))); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
      payload={'id':mid,'channel_id':q['id'],'author_id':uid,'body':body,'parent_id':None,'created_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat().replace('+00:00','Z'),'edited_at':None,'deleted':0,'reactions':[],'files':[],'mentions':[]}
      with open('/tmp/huddle-events.lock','w') as lk:
       fcntl.flock(lk,fcntl.LOCK_EX)
       n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(q['id'],)).fetchone()[0]
       c.execute('INSERT INTO events VALUES(?,?,?,?)',(q['id'],n,'message.created',json.dumps(payload))); c.commit()
      for cs,nm in list(clients):
       if cs is not s and any(j[1]==q['id'] for j in nm.joined): send(cs,f':{nick}!{nick}@localhost PRIVMSG {name} :{body}')
    elif cmd=='WHO': send(s,f':huddle 315 {nick} {a[1] if len(a)>1 else "*"} :End of WHO list')
    elif cmd=='LIST': send(s,f':huddle 323 {nick} :End of LIST')
    elif cmd=='TOPIC': send(s,f':huddle 331 {nick} {a[1] if len(a)>1 else "#"} :No topic')
    elif cmd=='MODE': send(s,f':huddle 324 {nick} {a[1] if len(a)>1 else nick} +')
    elif cmd not in ('PASS','NICK','USER'): send(s,f':huddle 421 {nick or "*"} {cmd} :Unknown command')
 except:pass
 finally:
  with nick_lock:
   if nick: active_nicks.discard(nick)
  try:s.close()
  except:pass
clients=[]
active_nicks=set()
nick_lock=threading.Lock()
class C:
 pass
def bridge():
 last={}
 while True:
  try:
   c=db()
   for e in c.execute("SELECT * FROM events WHERE event_id IN (SELECT MAX(event_id) FROM events GROUP BY channel_id)"):
    pass
   for cs,o in list(clients):
    for name,cid in list(o.joined):
     n=last.get(cid,0)
     for e in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,n)):
      last[cid]=e['event_id']
      if e['type']=='message.created':
       q=json.loads(e['payload'])
       if q.get('author_id')!=o.uid:
        au=db().execute('SELECT username FROM users WHERE id=?',(q.get('author_id'),)).fetchone(); an=au['username'] if au else 'web'; send(cs,f":{an}!{an}@localhost PRIVMSG {name} :{q.get('body','')}")
   c.close()
  except Exception: pass
  time.sleep(0.05)

def main():
 global clients
 threading.Thread(target=bridge,daemon=True).start()
 while True:
  try:
   x=socket.socket(); x.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
   try: x.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEPORT,1)
   except OSError: pass
   x.bind(('0.0.0.0',6667)); x.listen(); break
  except OSError:
   try: x.close()
   except Exception: pass
   time.sleep(0.5)
 while True:
  s,_=x.accept(); o=C();o.joined=[];o.nick=None;o.uid=None;clients.append((s,o));threading.Thread(target=client,args=(s,o),daemon=True).start()
if __name__=='__main__':main()
