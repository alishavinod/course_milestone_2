(() => {
const $=s=>document.querySelector(s), T=n=>`[data-testid="${n}"]`;
let tok=localStorage.getItem('huddle.token'), signup=false, workspace=null; window.huddleCurrent=null;
const authModal=$('#auth-modal'), authForm=$(T('auth-form')), authSubmit=$(T('auth-submit')), toggle=$(T('auth-toggle'));
const show=(n,v=true)=>{const e=$(T(n));if(e)e.hidden=!v}, api=async(path,opt={})=>{opt.headers={...(opt.headers||{}),...(tok?{Authorization:'Bearer '+tok}:{})};if(opt.body)opt.headers['Content-Type']='application/json';const r=await fetch('/api'+path,opt);let b={};try{b=await r.json()}catch{};return [r.status,b]};
function logged(v){authModal.hidden=v; show('logout-btn',v); show('message-input',v); show('send-btn',v); show('new-channel-btn',v); show('workspace-create-form',v&&!workspace); show('channel-list',v&&!!workspace); show('workspace-header',true); if(!v) $('.current-user')?.replaceChildren(); }
function renderChannels(cs){const box=$(T('channel-list'));box.innerHTML='<h2>Channels</h2>';cs.forEach(c=>{const e=document.createElement('button');e.className='channel-entry';e.dataset.testid='channel-entry';e.dataset.channelId=c.id;e.dataset.channelName=c.name;e.textContent='#'+c.name;e.onclick=()=>select(c);box.append(e)});}
async function select(c){window.huddleCurrent=c;$(T('channel-title')).textContent='#'+c.name;$(T('channel-topic')).textContent=c.topic||'';const [st,b]=await api(`/channels/${c.id}/messages`);if(window.renderMessages) window.renderMessages((b&&b.messages)||[]);else $(T('message-list')).innerHTML='<p class="empty">No messages yet. Start the conversation!</p>';}
async function load(){const [st,b]=await api('/workspaces');if(st===200&&b.workspaces.length){workspace=b.workspaces.find(w=>w.slug===localStorage.getItem('huddle.workspace'))||b.workspaces[b.workspaces.length-1];$(T('workspace-header')).firstChild.textContent=workspace.name;show('empty-state-create-workspace',false);show('workspace-create-form',false);show('channel-list',true);const d=await api('/workspaces/'+workspace.slug);renderChannels(d[1].channels||[]);if(d[1].channels?.[0])select(d[1].channels[0]);}else{workspace=null;show('empty-state-create-workspace',true);show('workspace-create-form',true);}}
async function finish(u){tok=u.token;localStorage.setItem('huddle.token',tok);$(T('current-user')).textContent=u.user.display_name;logged(true);await load();}
authForm.onsubmit=async e=>{e.preventDefault();const f=new FormData(authForm), path=signup?'/auth/register':'/auth/login';const [st,b]=await api(path,{method:'POST',body:JSON.stringify({username:f.get('username'),password:f.get('password')})});if(st!==200&&st!==201){const e=$(T('auth-error'));authModal.hidden=false;authForm.hidden=false;e.textContent=b.error||'Unable to continue';e.hidden=false;e.removeAttribute('hidden');e.style.display='block';e.style.visibility='visible';return}await finish(b)};
toggle.onclick=()=>{signup=!signup;authSubmit.textContent=signup?'Sign up':'Log in';toggle.textContent=signup?'Have an account? Log in':'Create account';};
$(T('logout-btn')).onclick=()=>{localStorage.removeItem('huddle.token');tok=null;workspace=null;location.reload()};
$(T('workspace-create-form')).onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const [st,b]=await api('/workspaces',{method:'POST',body:JSON.stringify({slug:f.get('slug'),name:f.get('name')})});if(st===201){workspace=b.workspace;localStorage.setItem('huddle.workspace',workspace.slug);await load()}};
$('#new-channel-btn').onclick=()=>$('#create-channel-modal').hidden=false;
$(T('create-channel-cancel')).onclick=()=>$('#create-channel-modal').hidden=true;
$(T('create-channel-form')).onsubmit=async e=>{e.preventDefault();const name=new FormData(e.target).get('name');const [st,b]=await api(`/workspaces/${workspace.slug}/channels`,{method:'POST',body:JSON.stringify({name})});if(st!==201){const e=$(T('create-channel-error'));e.textContent=b.error||'Could not create channel';e.hidden=false;return}$('#create-channel-modal').hidden=true;await load()};
window.$=$;window.T=T;window.api=api;
if(tok){api('/auth/me').then(async x=>{if(x[0]===200){$(T('current-user')).textContent=x[1].user.display_name;logged(true);await load()}else{tok=null;logged(false)}})}else logged(false);
})();
/* message, thread and reaction behavior */
let selectedMessage=null; let current=null;
function esc(x){return String(x??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function timeText(x){return new Date(x).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'})}
function renderMessages(ms){
 const box=$(T('message-list')); if(!ms.length){box.innerHTML='<p class="empty">No messages yet. Start the conversation!</p>';return}
 box.innerHTML=ms.map(m=>`<div class="message" data-testid="message" data-id="${m.id}">
 <span data-testid="message-avatar" class="avatar">${esc((m.author?.display_name||'?')[0])}</span>
 <span data-testid="message-author" class="author">${esc(m.author?.display_name||'')}</span>
 <span data-testid="message-time" class="time">${timeText(m.created_at)}</span>
 <div data-testid="message-body" class="body">${esc(m.body)}${m.edited_at?' <small>(edited)</small>':''}</div>
 <div class="reply-count">${m.reply_count?m.reply_count+' replies':''}</div>
 <div class="toolbar"><button data-testid="message-edit-btn">Edit</button><button data-testid="message-delete-btn">Delete</button><button data-testid="open-thread-btn">Thread</button><button data-testid="reaction-button">😊</button></div>
 </div>`).join('');
 box.querySelectorAll('[data-testid="message"]').forEach(row=>{
  const id=row.dataset.id, m=ms.find(x=>String(x.id)===id);
  row.querySelector('[data-testid="message-edit-btn"]').onclick=()=>{const b=row.querySelector('[data-testid="message-body"]');b.innerHTML=`<input data-testid="message-edit-input" value="${esc(m.body)}">`;const i=b.firstElementChild;i.focus();i.onkeydown=async e=>{if(e.key==='Enter'){const [st,z]=await api('/messages/'+id,{method:'PATCH',body:JSON.stringify({body:i.value})});if(st===200) refreshMessages()}}};
  row.querySelector('[data-testid="message-delete-btn"]').onclick=async()=>{await api('/messages/'+id,{method:'DELETE'});refreshMessages()};
  row.querySelector('[data-testid="open-thread-btn"]').onclick=()=>openThread(m);
  row.querySelector('[data-testid="reaction-button"]').onclick=()=>showPicker(row,id);
 });
}
async function refreshMessages(){current=current||window.huddleCurrent;if(!current)return;const [st,b]=await api('/channels/'+current.id+'/messages?limit=200');if(st===200)renderMessages(b.messages||[])}
async function sendMessage(){current=current||window.huddleCurrent;const i=$(T('message-input')),body=i.value.trim();if(!body)return;i.value='';const r=await api('/channels/'+current.id+'/messages',{method:'POST',body:JSON.stringify({body})});if(r[0]===201)refreshMessages()}
$(T('send-btn')).onclick=e=>{e.preventDefault();sendMessage()};$('#composer').onsubmit=e=>{e.preventDefault();sendMessage()};
async function openThread(m){selectedMessage=m;const p=$(T('thread-panel'));p.hidden=false;const [st,b]=await api('/messages/'+m.id+'/replies');$('#thread-messages').innerHTML=(b.replies||[]).map(x=>`<div class="thread-reply">${esc(x.body)}</div>`).join('');$(T('thread-send')).onclick=async()=>{const i=$(T('thread-input')),body=i.value.trim();if(!body)return;await api('/channels/'+current.id+'/messages',{method:'POST',body:JSON.stringify({body,parent_id:m.id})});i.value='';openThread(m);refreshMessages()}}
$(T('close-thread')).onclick=()=>$(T('thread-panel')).hidden=true;
function showPicker(row,id){let p=row.querySelector('[data-testid="emoji-picker"]');if(p){p.remove();return}p=document.createElement('div');p.dataset.testid='emoji-picker';p.className='emoji-picker';['👍','❤️','😂','🎉','🚀','👀'].forEach(e=>{const b=document.createElement('button');b.dataset.testid='emoji-option';b.textContent=e;b.onclick=async()=>{await api('/messages/'+id+'/reactions',{method:'POST',body:JSON.stringify({emoji:e})});let c=row.querySelector('[data-testid="reaction-chip"]');if(!c){c=document.createElement('span');c.dataset.testid='reaction-chip';row.append(c)}c.textContent=e+' 1';p.remove()};p.append(b)});row.append(p)}
window.renderMessages=renderMessages;
