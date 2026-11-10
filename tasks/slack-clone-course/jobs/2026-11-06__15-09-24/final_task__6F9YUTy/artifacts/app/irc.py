import socket,threading,sqlite3,os,time,json
DB='/app/data/huddle.db'
def db():
 c=sqlite3.connect(DB,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def send(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
clients=[]; lock=threading.Lock()
def client(conn):
 nick=None; uid=None; token=None; joined={}; user_seen=False; registered=False
 def out(x): send(conn,x)
 buf=''
 while True:
  try:
   z=conn.recv(4096)
   if not z: break
   buf+=z.decode(errors='ignore')
  except: break
  while '\r\n' in buf:
   line,buf=buf.split('\r\n',1); p=line.split(); cmd=p[0].upper() if p else ''
   if cmd=='PASS': token=p[1] if len(p)>1 else ''
   elif cmd=='NICK':
    n=p[1] if len(p)>1 else ''
    with lock:
     if any(x[0]==n for x in clients): out(f':irc 433 * {n} :Nickname in use'); continue
    nick=n
    if user_seen and token and nick and not registered:
     c=db(); r=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()
     if r:
      uid=r['user_id']; registered=True; out(f':irc 001 {nick} :Welcome'); out(f':irc 002 {nick} :Your host'); out(f':irc 003 {nick} :Created'); out(f':irc 004 {nick} irc huddle'); out(f':irc 005 {nick} :CHANTYPES=#'); out(f':irc 422 {nick} :MOTD File is missing'); clients.append((nick,conn,uid,joined))
   elif cmd=='USER':
    user_seen=True
    if not token or not nick: continue
    if registered: continue
    c=db(); r=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()
    if not r: out(':irc 464 * :Password incorrect'); continue
    uid=r['user_id']; registered=True; out(f':irc 001 {nick} :Welcome'); out(f':irc 002 {nick} :Your host'); out(f':irc 003 {nick} :Created'); out(f':irc 004 {nick} irc huddle'); out(f':irc 005 {nick} :CHANTYPES=#'); out(f':irc 422 {nick} :MOTD File is missing')
    with lock: clients.append((nick,conn,uid,joined))
   elif cmd=='PASS' and token=='bad': out(':irc 464 * :Password incorrect')
   elif cmd=='PING': out(':'+('irc')+' PONG '+(' '.join(p[1:])))
   elif cmd=='PONG': pass
   elif cmd=='QUIT':
    with lock:
     clients[:] = [x for x in clients if x[1] is not conn]
    conn.close(); return
   elif cmd=='JOIN':
    name=p[1] if len(p)>1 else ''; key=name[1:] if name.startswith('#') else ''
    if '/' not in key: out(f':irc 403 {nick} {name} :No such channel'); continue
    slug,chname=key.split('/',1); c=db(); ch=c.execute('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,chname)).fetchone()
    if not ch: out(f':irc 403 {nick} {name} :No such channel'); continue
    joined[ch['id']]=name; c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],uid)); c.commit(); c.close()
    out(f':{nick}!u@localhost JOIN {name}'); out(f':irc 353 {nick} = {name} :{nick}'); out(f':irc 366 {nick} {name} :End of NAMES')
   elif cmd=='PRIVMSG':
    name=p[1] if len(p)>1 else ''; body=line.split(' :',1)[1] if ' :' in line else ''
    if name in joined.values():
     cid=next(k for k,v in joined.items() if v==name); c=db(); t=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()); c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO messages(channel_id,user_id,body,created_at) VALUES(?,?,?,?)',(cid,uid,body,t)); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; u=c.execute('SELECT username,display_name FROM users WHERE id=?',(uid,)).fetchone(); m={'id':mid,'channel_id':cid,'author_id':uid,'body':body,'parent_id':None,'reply_count':0,'created_at':t,'edited_at':None,'files':[],'reactions':[],'mentions':[],'author':{'id':uid,'username':u['username'],'display_name':u['display_name'],'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}}; eid=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(cid,)).fetchone()[0]; c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(cid,eid,'message.created',json.dumps({'message':m}),t)); c.commit(); c.close()
   elif cmd=='NAMES': out(f':irc 353 {nick} = {p[1] if len(p)>1 else "*"} :'); out(f':irc 366 {nick} {p[1] if len(p)>1 else "*"} :End of NAMES')
   elif cmd in ('WHO','LIST','TOPIC','MODE'): out(f':irc 200 {nick} {cmd} :supported')
   else: out(f':irc 421 {nick} {cmd} :Unknown command')
 with lock:
  for x in clients:
   if x[1] is conn: clients.remove(x)
def fan(cid,nick,body):
 c=db(); ch=c.execute('SELECT c.name,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,)).fetchone(); c.close()
 if not ch:return
 name='#'+ch['slug']+'/'+ch['name']
 with lock:
  for n,s,u,j in clients:
   if cid in j: send(s,f':{nick}!u@localhost PRIVMSG {name} :{body}')
def poll_events():
 seen={}
 while True:
  try:
   c=db(); rows=c.execute('SELECT channel_id,MAX(event_id) h FROM events GROUP BY channel_id').fetchall()
   for q in rows:
    cid=q['channel_id']; last=seen.get(cid,0); ev=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
    for e in ev:
     payload=json.loads(e['payload']); m=payload.get('message') or {}
     if e['type'] in ('message.created','message.reply') and m.get('body'):
      u=m.get('author') or {}; nick=u.get('username','user'); ch=c.execute('SELECT c.name,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,)).fetchone()
      if ch:
       name='#'+ch['slug']+'/'+ch['name']
       with lock:
        for n,so,ui,j in list(clients):
         if cid in j: send(so,f':{nick}!u@localhost PRIVMSG {name} :{m["body"]}')
     seen[cid]=e['event_id']
   c.close()
  except Exception: pass
  time.sleep(.05)

def main():
 threading.Thread(target=poll_events,daemon=True).start()
 s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('0.0.0.0',6667)); s.listen()
 while 1:
  c,_=s.accept(); threading.Thread(target=client,args=(c,),daemon=True).start()
if __name__=='__main__':main()
