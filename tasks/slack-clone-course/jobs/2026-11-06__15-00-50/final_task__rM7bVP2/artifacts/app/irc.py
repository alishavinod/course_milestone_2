import asyncio,json,re,urllib.request
BASE='http://127.0.0.1:8000'; clients=[]; nicks={}
def api(method,path,token,data=None):
 d=None
 if data is not None: d=json.dumps(data).encode()
 for base in ("http://127.0.0.1:8000","http://127.0.0.1:8001","http://127.0.0.1:8002"):
  q=urllib.request.Request(base+path,data=d,method=method,headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
  try:
   with urllib.request.urlopen(q,timeout=1) as r:return json.loads(r.read())
  except Exception: continue
 return None

async def send(w,s):
 try:w.write((s+'\r\n').encode()); await w.drain()
 except:pass
async def client(rd,wr):
 state={'token':None,'nick':None,'user':None,'joined':set(),'registered':False}; clients.append((wr,state))
 async def registration():
  if state['token'] and state['nick'] and state.get('user') and not state['registered']:
   state['registered']=True
   for x in ['001','002','003','004','005','422']: await send(wr,':huddle '+x+' '+state['nick']+' :welcome')
 while not rd.at_eof():
  line=(await rd.readline()).decode(errors='ignore').strip('\r\n')
  if not line: continue
  parts=line.split(' ',2); cmd=parts[0].upper(); arg=parts[1] if len(parts)>1 else ''; rest=parts[2] if len(parts)>2 else ''
  if cmd=='PASS':
   state['token']=arg
   if not api('GET','/api/auth/me',arg): await send(wr,':huddle 464 * :Password incorrect'); state['token']=None
  elif cmd=='NICK':
   if arg in nicks and nicks[arg] is not wr: await send(wr,':huddle 433 * '+arg+' :Nickname in use')
   else: state['nick']=arg; nicks[arg]=wr; state['user']=api('GET','/api/auth/me',state['token']) if state['token'] else None; await registration()
  elif cmd=='USER':
   state['user']=state.get('user') or (api('GET','/api/auth/me',state['token']) if state['token'] else None); await registration()
  elif cmd=='PING': await send(wr,':huddle PONG huddle :'+arg)
  elif cmd=='PONG': pass
  elif cmd=='JOIN':
   name=arg; m=re.match(r'^#([^/]+)/([^/]+)$',name)
   if not m: await send(wr,':huddle 403 '+state['nick']+' '+name+' :No such channel'); continue
   ws,ch=m.groups(); info=api('GET','/api/workspaces/'+ws,state['token']); found=next((x for x in (info or {}).get('channels',[]) if x['name']==ch),None)
   if not found: await send(wr,':huddle 403 '+state['nick']+' '+name+' :No such channel'); continue
   api('POST',f'/api/channels/{found["id"]}/join',state['token'],{}); state['joined'].add((name,found['id'])); await send(wr,f':{state["nick"]}!{state["user"]["user"]["username"]}@localhost JOIN {name}'); await send(wr,':huddle 353 '+state['nick']+' = '+name+' :'+state['nick']); await send(wr,':huddle 366 '+state['nick']+' '+name+' :End of NAMES list')
  elif cmd=='PRIVMSG':
   text=rest[1:] if rest.startswith(':') else rest
   item=next((x for x in state['joined'] if x[0]==arg),None)
   if item: api('POST',f'/api/channels/{item[1]}/messages',state['token'],{'body':text})
  elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'): pass
  elif cmd=='QUIT': break
  else: await send(wr,':huddle 421 '+state.get('nick','*')+' '+cmd+' :Unknown command')
 
 try: clients.remove((wr,state))
 except ValueError: pass
 if state.get('nick'): nicks.pop(state['nick'],None)
 wr.close(); await wr.wait_closed()
async def bridge():
 seen={}
 while True:
  for wr,st in list(clients):
   for name,cid in list(st['joined']):
    q=api('GET',f'/api/channels/{cid}/messages?limit=100',st['token']) or {}
    for m in reversed(q.get('messages',[])):
     if m['id']<=seen.get(cid,0): continue
     seen[cid]=m['id']
     if m['author']['username']!=st.get('nick'): await send(wr,f':{m["author"]["username"]}!{m["author"]["username"]}@localhost PRIVMSG {name} :{m["body"]}')
  await asyncio.sleep(.15)
async def main():
 s=await asyncio.start_server(client,'0.0.0.0',6667); asyncio.create_task(bridge())
 async with s: await s.serve_forever()
asyncio.run(main())
