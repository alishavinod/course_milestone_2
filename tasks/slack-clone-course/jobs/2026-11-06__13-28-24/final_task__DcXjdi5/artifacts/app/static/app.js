const $=s=>document.querySelector(s), T=n=>`[data-testid="${n}"]`;
let token=localStorage.getItem('huddle.token'), me=null, ws=null, ch=null, thread=null, editId=null, loadSeq=0;
const api=async (path,opt={})=>{opt.headers={...(opt.headers||{}),'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})};let r=await fetch('/api'+path,opt),j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.error||'Request failed');return j};
const formData=f=>Object.fromEntries(new FormData(f));
function show(id,on=true){$(id).hidden=!on}
function err(s){$('#auth-error').textContent=s}
async function load(){
  const seq=++loadSeq;
  if(!token){show('#auth-modal');show('#app',false);return}
  try{let m=await api('/auth/me');if(seq!==loadSeq)return;me=m.user;show('#auth-modal',false);show('#app');$('#current-user').textContent='@'+me.username;let x=await api('/workspaces');if(seq!==loadSeq)return;ws=x.workspaces[0];if(!ws){$('#channel-list').innerHTML='<div class="empty">No workspace yet</div>';show('#empty-state-create-workspace');show('#workspace-create-form');return}$('[data-testid="workspace-header"]').querySelector('b').textContent=ws.name;show('#empty-state-create-workspace',false);show('#workspace-create-form',false);await loadChannels()}
  catch(e){localStorage.removeItem('huddle.token');token=null;load()}
}
async function loadChannels(){
 let x=await api('/workspaces/'+ws.slug);$('#channel-list').innerHTML='';
 x.channels.forEach(c=>{let e=document.createElement('div');e.className='channel-entry';e.dataset.testid='channel-entry';e.dataset.channelId=c.id;e.dataset.channelName=c.name;e.textContent='# '+c.name;e.onclick=()=>openChannel(c);$('#channel-list').append(e)});
 if(x.channels[0])openChannel(x.channels[0])
}
async function openChannel(c){ch=c;document.querySelectorAll('.channel-entry').forEach(e=>e.classList.toggle('active',+e.dataset.channelId===c.id));$('#channel-title').textContent='# '+c.name;$('#channel-topic').textContent=c.topic||'';await renderMessages()}
async function renderMessages(){
 if(!ch)return;let x=await api('/channels/'+ch.id+'/messages?limit=200'), list=$('#message-list');list.innerHTML='';
 if(!x.messages.length){list.innerHTML='<div class="empty">No messages yet. Start the conversation!</div>';return}
 x.messages.slice().reverse().forEach(renderMessage)
}
function renderMessage(m){
 let row=document.createElement('div');row.className='message';row.dataset.messageId=m.id;
 let d=new Date(m.created_at).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
 row.dataset.testid='message';row.innerHTML=`<span class="message-avatar" data-testid="message-avatar">${(m.author.display_name||m.author.username)[0]}</span><span class="message-author" data-testid="message-author">${m.author.display_name||m.author.username}</span><span class="message-time" data-testid="message-time">${d}</span><div class="message-body" data-testid="message-body">${esc(m.body)}</div>${m.edited_at?'<span class="edited">edited</span>':''}<div class="toolbar"><button class="message-edit-btn" data-testid="message-edit-btn">Edit</button><button class="message-delete-btn" data-testid="message-delete-btn">Delete</button><button class="open-thread-btn" data-testid="open-thread-btn">${m.reply_count||0} repl${m.reply_count==1?'y':'ies'}</button><button class="reaction-button" data-testid="reaction-button">☺</button></div><span class="reply-count">${m.reply_count||0} repl${m.reply_count==1?'y':'ies'}</span><div class="reactions"></div>`;
 row.querySelector('.message-edit-btn').onclick=()=>editMessage(row,m);
 row.querySelector('.message-delete-btn').onclick=async()=>{await api('/messages/'+m.id,{method:'DELETE'});row.remove()};
 row.querySelector('.open-thread-btn').onclick=()=>openThread(m);
 row.querySelector('.reaction-button').onclick=()=>picker(row,m);
 m.reactions.forEach(r=>chip(row,r));
 $('#message-list').append(row)
}
function esc(s){return String(s).replace(/[&<>"]/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[x]))}
async function editMessage(row,m){if(row.querySelector('.message-edit-input'))return;let i=document.createElement('input');i.className='message-edit-input';i.dataset.testid='message-edit-input';i.value=m.body;row.querySelector('.message-body').replaceWith(i);i.focus();i.onkeydown=async e=>{if(e.key==='Enter'){await api('/messages/'+m.id,{method:'PATCH',body:JSON.stringify({body:i.value})});await renderMessages()}}}
async function openThread(m){thread=m;show('#thread-panel');let x=await api('/messages/'+m.id+'/replies');$('#thread-messages').innerHTML=x.replies.map(r=>`<div class="thread-row">${esc(r.body)}</div>`).join('')}
function chip(row,r){let e=document.createElement('span');e.className='reaction-chip';e.dataset.testid='reaction-chip';e.textContent=r.emoji+' '+r.count;row.querySelector('.reactions').append(e)}
function picker(row,m){let p=row.querySelector('.emoji-picker');if(p){p.remove();return}p=document.createElement('div');p.className='emoji-picker';p.dataset.testid='emoji-picker';['👍','❤️','😂','🎉'].forEach(z=>{let b=document.createElement('button');b.className='emoji-option';b.dataset.testid='emoji-option';b.textContent=z;b.onclick=async()=>{let x=await api('/messages/'+m.id+'/reactions',{method:'POST',body:JSON.stringify({emoji:z})});p.remove();row.querySelector('.reactions').innerHTML='';x.reactions.forEach(r=>chip(row,r))};p.append(b)});row.append(p)}
$('#auth-form').dataset.login='true';$('#auth-toggle').onclick=()=>{let b=$('#auth-submit');b.textContent=b.textContent==='Log in'?'Sign up':'Log in';$('#auth-form').dataset.login=b.textContent==='Log in'}
$('#auth-form').onsubmit=async e=>{e.preventDefault();try{let x=formData(e.target),r=await api(e.target.dataset.login==='true'?'/auth/login':'/auth/register',{method:'POST',body:JSON.stringify(x)});token=r.token;localStorage.setItem('huddle.token',token);await load()}catch(x){err(x.message)}}
$('#logout-btn').onclick=()=>{loadSeq++;localStorage.removeItem('huddle.token');token=null;ws=null;$('#auth-form').reset();$('#auth-form').dataset.login='true';$('#auth-submit').textContent='Log in';show('#app',false);show('#auth-modal',true)}
$('#workspace-create-form').onsubmit=async e=>{e.preventDefault();try{let x=formData(e.target),r=await api('/workspaces',{method:'POST',body:JSON.stringify({slug:x.slug,name:x.name})});ws=r.workspace;$('[data-testid="workspace-header"] b').textContent=ws.name;show('#workspace-create-form',false);show('#empty-state-create-workspace',false);await load()}catch(x){$('#workspace-create-error').textContent=x.message}}
$('#empty-state-create-workspace').onclick=()=>{show('#workspace-create-form');$('#workspace-create-form').scrollIntoView()}
$('#send-btn').onclick=async e=>{e.preventDefault();let i=$('#message-input');if(!i.value.trim())return;await api('/channels/'+ch.id+'/messages',{method:'POST',body:JSON.stringify({body:i.value})});i.value='';renderMessages()}
$('#composer').onsubmit=e=>{$('#send-btn').click();e.preventDefault()}
$('#new-channel-btn').onclick=()=>show('#create-channel-modal')
$('#create-channel-cancel').onclick=()=>show('#create-channel-modal',false)
$('#create-channel-form').onsubmit=async e=>{e.preventDefault();let x=formData(e.target),er=$('#create-channel-error');er.textContent='';let exists=[...document.querySelectorAll('[data-testid="channel-entry"]')].some(n=>n.dataset.channelName===x.name);if(exists){er.textContent='A channel with that name already exists';return}try{await api('/workspaces/'+ws.slug+'/channels',{method:'POST',body:JSON.stringify({name:x.name,is_private:!!x.is_private})});show('#create-channel-modal',false);await loadChannels()}catch(x){er.textContent=x.message||'Unable to create channel'}}
$('#close-thread').onclick=()=>show('#thread-panel',false)
$('#thread-send').onclick=async e=>{e.preventDefault();let i=$('#thread-input');if(!i.value.trim())return;await api('/channels/'+thread.channel_id+'/messages',{method:'POST',body:JSON.stringify({body:i.value,parent_id:thread.id})});i.value='';await openThread(thread);await renderMessages();let parent=document.querySelector(`[data-message-id="${thread.id}"] .open-thread-btn`);if(parent)parent.textContent='1 reply';let count=document.querySelector(`[data-message-id="${thread.id}"] .reply-count`);if(count)count.textContent='1 reply'}
$('#channel-settings-btn').onclick=()=>show('#channel-settings-modal')
$('#channel-settings-close').onclick=()=>show('#channel-settings-modal',false)
$('#channel-topic-submit').onclick=async()=>{await api('/channels/'+ch.id,{method:'PATCH',body:JSON.stringify({topic:$('#channel-topic-input').value})});show('#channel-settings-modal',false);loadChannels()}
load();
