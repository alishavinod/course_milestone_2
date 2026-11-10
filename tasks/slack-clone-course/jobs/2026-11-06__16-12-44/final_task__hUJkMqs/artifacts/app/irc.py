import socket,threading,sqlite3,json,time
DB='/app/data/huddle.db'
def db():
 c=sqlite3.connect(DB,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('create table if not exists irc_nicks(nick text primary key)'); c.commit()
 return c
def send(s,x): s.sendall((x+'\r\n').encode())
def client(s):
 c=db(); nick=None; uid=None; user_seen=False; registered=False; chans=set(); last_events={}; buf=''
 try:
  s.settimeout(0.2)
  while True:
   try: z=s.recv(4096)
   except socket.timeout: z=b''
   except OSError: break
   if z: buf+=z.decode(errors='ignore')
   while '\r\n' in buf:
    line,buf=buf.split('\r\n',1); q=line.split(' ',2); cmd=q[0].upper()
    if cmd=='PASS':
     tok=q[1] if len(q)>1 else ''; r=c.execute('select user_id from tokens where token=?',(tok,)).fetchone(); uid=r['user_id'] if r else None
     if not uid: send(s,':huddle 464 * :Password incorrect')
    elif cmd=='NICK':
     n=q[1] if len(q)>1 else '*'
     if c.execute('select 1 from irc_nicks where nick=?',(n,)).fetchone(): send(s,f':huddle 433 * {n} :Nickname in use')
     else:
      nick=n; c.execute('insert or ignore into irc_nicks values(?)',(nick,)); c.commit()
      if user_seen and uid and not registered:
       registered=True
       for code,text in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle'),('005','CHANTYPES=#')]: send(s,f':huddle {code} {nick} :{text}')
       send(s,f':huddle 422 {nick} :MOTD File is missing')
    elif cmd=='USER':
     user_seen=True
     if nick and uid and not registered:
      registered=True
      for n,t in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle'),('005','CHANTYPES=#')]: send(s,f':huddle {n} {nick} :{t}')
      send(s,f':huddle 422 {nick} :MOTD File is missing')
    elif cmd=='PING': send(s,':huddle PONG '+(q[1] if len(q)>1 else 'huddle'))
    elif cmd=='PONG': pass
    elif cmd=='QUIT':
     try: s.shutdown(socket.SHUT_RDWR)
     except: pass
     return
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'): 
     if cmd=='NAMES': send(s,f':huddle 353 {nick} = {q[1] if len(q)>1 else "*"} :{nick}'); send(s,f':huddle 366 {nick} {q[1] if len(q)>1 else "*"} :End of NAMES list')
    elif cmd=='JOIN':
     name=q[1] if len(q)>1 else ''; parts=name[1:].split('/',1) if name.startswith('#') else []
     r=c.execute('select c.*,w.slug from channels c join workspaces w on w.id=c.workspace_id where w.slug=? and c.name=?',(parts[0],parts[1] if len(parts)>1 else '')).fetchone() if len(parts)==2 else None
     if not r: send(s,f':huddle 403 {nick} {name} :No such channel')
     else:
      chans.add(r['id']); c.execute('insert or ignore into channel_members values(?,?)',(r['id'],uid)); c.commit(); send(s,f':{nick}!u@huddle JOIN {name}'); send(s,f':huddle 353 {nick} = {name} :{nick}'); send(s,f':huddle 366 {nick} {name} :End of NAMES list')
    elif cmd=='PRIVMSG' and len(q)>2:
     name=q[1]; body=q[2].lstrip(':'); parts=name[1:].split('/',1) if name.startswith('#') else []
     r=c.execute('select c.* from channels c join workspaces w on w.id=c.workspace_id where w.slug=? and c.name=?',(parts[0],parts[1])).fetchone() if len(parts)==2 else None
     if r and uid:
      ts=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()); e=c.execute('select coalesce(max(event_id),0)+1 n from events where channel_id=?',(r['id'],)).fetchone()['n']
      c.execute('insert into messages(channel_id,author_id,body,created_at,event_id) values(?,?,?,?,?)',(r['id'],uid,body,ts,e)); mid=c.execute('select last_insert_rowid() x').fetchone()['x']; payload={'message':{'id':mid,'channel_id':r['id'],'author_id':uid,'body':body}}; c.execute('insert into events values(?,?,?,?,?)',(r['id'],e,'message.created',json.dumps(payload),ts)); c.commit()
    else: send(s,f':huddle 421 {nick or "*"} {cmd} :Unknown command')
   if uid and chans:
    for cc in list(chans):
     last=last_events.get(cc,0)
     for ev in c.execute('select e.*,u.username from events e join messages m on m.channel_id=e.channel_id and m.event_id=e.event_id join users u on u.id=m.author_id where e.channel_id=? and e.event_id>? and e.kind="message.created" order by e.event_id',(cc,last)):
      data=json.loads(ev['payload']); body=(data.get('message') or {}).get('body',data.get('body',''))
      row=c.execute('select w.slug,ch.name from channels ch join workspaces w on w.id=ch.workspace_id where ch.id=?',(cc,)).fetchone()
      if row and ev['username']!=nick: send(s,f':{ev["username"]}!u@huddle PRIVMSG #{row["slug"]}/{row["name"]} :{body}')
      last_events[cc]=ev['event_id']
 finally:
  if nick:
   try:c.execute('delete from irc_nicks where nick=?',(nick,));c.commit()
   except:pass
  s.close()
def main():
 x=socket.socket(); x.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); x.bind(('0.0.0.0',6667)); x.listen()
 while True:
  s,_=x.accept(); threading.Thread(target=client,args=(s,),daemon=True).start()
if __name__=='__main__':main()
