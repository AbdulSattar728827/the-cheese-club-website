/* ---- Logged-in hint: set before the page paints, so the header never flashes the logged-out buttons
   ("Log in", "Join Discord") while the real answer is still loading. ---- */
(function(){try{if(localStorage.getItem('cc_me')||/(?:^|; )cc_in=1/.test(document.cookie))document.documentElement.classList.add('cc-in')}catch(e){}})();

/* ---- The cheese loader ----
   1. The page-load cover (#ccLoader, added to every page by the server) fades away once the page and its first
      data requests are done. It only becomes visible if loading takes longer than ~0.12 s.
   2. A small cheese (#ccBusy) shows in the corner while a request to the site takes longer than ~0.7 s.
   3. Clicking a link to another page of the site shows the cover straight away, so the wait never looks frozen. */
const CC_CHEESE='<svg viewBox="0 0 80 80" class="cc-cheese" aria-hidden="true"><path d="M8 38 L72 16 V52 a6 6 0 0 1-6 6 H14 a6 6 0 0 1-6-6 Z" fill="#f0b82f"/><path d="M8 38 L72 16 V27 L8 49 Z" fill="#ffda70"/><circle cx="24" cy="50" r="4.5" fill="#c98713"/><circle cx="43" cy="46" r="3.2" fill="#c98713"/><circle cx="58" cy="43" r="4" fill="#c98713"/><circle cx="34" cy="31" r="2.6" fill="#e0a62a"/><circle cx="55" cy="26" r="2.2" fill="#e0a62a"/></svg>';
const ccLoader=(function(){
  let inflight=0,slowTimer=null,startedAt=Date.now(),cover=null,busy=null,navTimer=null;
  const origFetch=window.fetch.bind(window);
  const isApi=u=>/^(https?:\/\/[^/]+)?\/api\//.test(u);
  function showBusy(on){
    if(on&&!busy){busy=document.createElement('div');busy.id='ccBusy';busy.setAttribute('role','status');busy.setAttribute('aria-label','Working');busy.innerHTML=CC_CHEESE;(document.body||document.documentElement).appendChild(busy)}
    if(busy)busy.hidden=!on;
  }
  window.fetch=function(input,init){
    const url=typeof input==='string'?input:(input&&input.url)||'';
    if(!isApi(url)||/\/notifications/.test(url))return origFetch(input,init);
    inflight++;clearTimeout(slowTimer);slowTimer=setTimeout(()=>{if(inflight>0&&!cover)showBusy(true)},700);
    return origFetch(input,init).finally(()=>{inflight=Math.max(0,inflight-1);if(!inflight){clearTimeout(slowTimer);showBusy(false)}});
  };
  function finish(){
    if(!cover)return;const c=cover;cover=null;c.classList.add('cc-done');setTimeout(()=>c.remove(),360);
  }
  function init(){
    cover=document.getElementById('ccLoader');
    if(!cover)return;
    let quiet=0;
    (function poll(){
      if(document.readyState==='complete'&&inflight===0){if(++quiet>=2)return finish()}else quiet=0;
      if(Date.now()-startedAt>8000)return finish();
      setTimeout(poll,60);
    })();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
  /* Leaving the page (a link, the back button, reload): the cover fades in over THIS page while the next one loads, and
     the next page starts covered too, so it is one continuous cover instead of two with a bright gap between. This only
     fires when the page really is being left, so a link that the page handles itself never leaves a stuck cover. */
  let leaveTimer=null;
  window.addEventListener('beforeunload',()=>{
    let c=document.getElementById('ccLoader');
    if(!c){c=document.createElement('div');c.id='ccLoader';c.setAttribute('role','status');c.setAttribute('aria-label','Loading');c.className='cc-nav';c.innerHTML='<div class="cc-load">'+CC_CHEESE+'<span>Loading…</span></div>';(document.body||document.documentElement).appendChild(c);void c.offsetWidth}
    c.classList.remove('cc-done');c.classList.add('cc-nav','cc-on');cover=null;
    clearTimeout(leaveTimer);leaveTimer=setTimeout(()=>{c.classList.remove('cc-on')},8000);   /* e.g. a download: the page stays, so uncover it */
  });
  window.addEventListener('pageshow',e=>{if(e.persisted){clearTimeout(leaveTimer);const c=document.getElementById('ccLoader');if(c)c.remove()}});
  return {busy:showBusy};
})();

/* Script errors on a page are sent to the admins (#website-exception-logs) so they can be fixed.
   Only errors from this site's own scripts are sent, at most 3 per page load, and never the same one twice. */
(function(){
  let sent=0;const seen=new Set();
  const IGNORE=/^Script error\.?$|ResizeObserver loop|Non-Error promise rejection|Failed to fetch|NetworkError|Load failed|AbortError|The operation was aborted/i;
  function report(message,source,line,col,stack){
    try{
      message=String(message||'').slice(0,300);
      if(!message||sent>=3||IGNORE.test(message))return;
      source=String(source||'');stack=String(stack||'');
      if(/extension:\/\//i.test(source+stack))return;
      if(source&&source.indexOf(location.origin)!==0)return;
      const key=message+'|'+source+'|'+line;if(seen.has(key))return;seen.add(key);sent++;
      fetch('/api/client-error',{method:'POST',credentials:'same-origin',keepalive:true,headers:{'Content-Type':'application/json'},
        body:JSON.stringify({message,source:source.replace(location.origin,''),line:line||0,col:col||0,stack:stack.slice(0,1500),page:location.pathname})}).catch(()=>{});
    }catch(e){}
  }
  window.addEventListener('error',e=>report(e.message,e.filename,e.lineno,e.colno,e.error&&e.error.stack));
  window.addEventListener('unhandledrejection',e=>{const r=e.reason;report('Unhandled promise rejection: '+((r&&r.message)||r),'',0,0,r&&r.stack)});
})();

/* The Cheese Club — shared helpers loaded by every page.
   Keep match, civilization and navigation rendering here so every page shows
   the same thing. Page scripts still define their own small helpers (esc, fmt,
   duration…); the names below are chosen not to clash with them. */

function ccEsc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function ccFmt(n){return Number(n||0).toLocaleString()}
const CC_DEFAULT_AVATAR='https://cdn.discordapp.com/embed/avatars/0.png';

/* Civilization name (lower case, straight apostrophes) -> file in /static/civ-flags */
const CIV_FLAG_FILES={
  "english":"English.png",
  "french":"French.png",
  "holy roman empire":"Roman empire.png","hre":"Roman empire.png","roman empire":"Roman empire.png",
  "rus":"Rus.png",
  "abbasid dynasty":"Abbasid.png","abbasid":"Abbasid.png",
  "chinese":"Chinese (2).png",
  "delhi sultanate":"Delhi.png","delhi":"Delhi.png",
  "mongols":"Mongal.png","mongol":"Mongal.png",
  "japanese":"Japnese.png",
  "byzantines":"Byzantine.png","byzantine":"Byzantine.png",
  "ottomans":"Ottoman.png","ottoman":"Ottoman.png",
  "malians":"Malians.png","malian":"Malians.png",
  "jeanne d'arc":"Jenne d arc.png","jeanne darc":"Jenne d arc.png",
  "ayyubids":"Ayyubid.png","ayyubid":"Ayyubid.png",
  "order of the dragon":"Order.png",
  "zhu xi's legacy":"Zi Legacy.png","zhu xis legacy":"Zi Legacy.png",
  "tughlaq dynasty":"Tughlaq.png","tughlaq":"Tughlaq.png",
  "sengoku daimyo":"Sengoku.png","sengoku":"Sengoku.png",
  "macedonian dynasty":"Maccedonian.png","macedonian":"Maccedonian.png",
  "knights templar":"Templar.png","templar knights":"Templar.png","templar":"Templar.png","templars":"Templar.png",
  "golden horde":"Horde.png",
  "house of lancaster":"Lancaster.png","lancaster":"Lancaster.png",
  "jin dynasty":"Jin.png","jin":"Jin.png",
  "vikings":"Vikings.webp","viking":"Vikings.webp",
  "scots":"Scots.webp","scottish":"Scots.webp",
  "random":"Random.png"
};
function civKey(c){return String(c||'').trim().toLowerCase().replace(/[\u2018\u2019\u02bc`]/g,"'").replace(/\s+/g,' ')}
/* Exact name first; otherwise the longest known name that appears as whole words in it,
   so "Templar Knights" or "The Order of the Dragon" still find their flag. */
const CIV_KEYS_LONGEST_FIRST=Object.keys(CIV_FLAG_FILES).sort((a,b)=>b.length-a.length);
function civFlagFile(c){
  const key=civKey(c);if(!key)return null;
  if(CIV_FLAG_FILES[key])return CIV_FLAG_FILES[key];
  const words=` ${key.replace(/[^a-z0-9' ]+/g,' ').replace(/\s+/g,' ').trim()} `;
  const hit=CIV_KEYS_LONGEST_FIRST.find(k=>k.length>=3&&words.includes(` ${k} `));
  return hit?CIV_FLAG_FILES[hit]:null;
}
function civFlag(c){
  const file=civFlagFile(c);
  return file?`<img class="civ-flag-image" src="/static/civ-flags/${encodeURIComponent(file)}" alt="${ccEsc(c)} emblem" title="${ccEsc(c)}" loading="lazy" onerror="this.style.display='none'">`:'⚑';
}

/* The civ's flag and name, for any page. */
function ccCivCell(n){const f=n?civFlag(n):'';return `<span class="cf">${f&&f!=='?'?f:''}<span>${ccEsc(n)}</span></span>`}

function ccPlayerHref(publicId){return '/player/'+encodeURIComponent(publicId)}

/* One player row inside the match-detail modal. */
function matchPlayer(p){
  const player=p||{};
  const username=player.username||'Unknown player';
  const avatar=player.avatar_url||CC_DEFAULT_AVATAR;
  const civ=player.civ||'Not recorded';
  /* Official AoE4World rating (TG for team games, Solo for 1v1) and the club's own rating for this queue. */
  const ratings=[];
  if(player.official_elo!=null)ratings.push(`${ccEsc(player.official_elo_type||'TG')} ELO ${ccFmt(player.official_elo)}${player.official_elo_at_match?' at queue pop':''}`);
  if(player.elo!=null&&player.elo!=='')ratings.push(`Community ${ccFmt(player.elo)}`);
  const elo=ratings.join(' · ')||'Ratings unavailable';
  const hint=player.id_hint||'Unavailable';
  const nameHtml=(player.user_id?`<a class="player-page-link" href="${ccPlayerHref(player.user_id)}">${ccEsc(username)}</a>`:ccEsc(username))+(player.captain?' <span title="Captain">👑</span>':'');
  const flagHtml=player.civ_status==='ready'?'<span title="Civ chosen, hidden until both captains lock in">✅</span>':player.civ_status==='picking'?'<span title="Choosing a civ">⏳</span>':civFlag(civ);
  return `<div class="detail-player"><img src="${ccEsc(avatar)}" alt="" loading="lazy" onerror="this.onerror=null;this.src='${CC_DEFAULT_AVATAR}'"><div class="detail-player-info"><b>${nameHtml}</b><small>${ccEsc(civ)}</small><small>${elo}</small><small>Discord ID: ${ccEsc(hint)}</small></div><span class="civ-flag" title="${ccEsc(civ)}">${flagHtml}</span></div>`;
}

/* Plain-text description of a match's civilization bans (escape before inserting). */
function formatCivBans(bans){
  if(!bans||typeof bans!=='object')return 'No ban information recorded';
  if(bans.skipped)return 'Skipped — no civilizations were banned';
  const list=v=>Array.isArray(v)?v.filter(Boolean).map(String):(v?[String(v)]:[]);
  if(Array.isArray(bans)){const all=list(bans);return all.length?all.join(', '):'No civilizations were banned'}
  const parts=[];
  for(const [key,label] of [['team1','Team 1'],['team2','Team 2']]){const civs=list(bans[key]);if(civs.length)parts.push(`${label}: ${civs.join(', ')}`)}
  for(const [key,value] of Object.entries(bans)){if(['team1','team2','skipped'].includes(key))continue;const civs=list(value);if(civs.length)parts.push(`${key.replaceAll('_',' ')}: ${civs.join(', ')}`)}
  return parts.join(' · ')||'No ban information recorded';
}

function matchResultText(result){
  const r=String(result||'').toLowerCase();
  return {team1:'Team 1 Victory',team2:'Team 2 Victory',pending:'In progress',cancelled:'Cancelled',canceled:'Cancelled'}[r]||(r?r.replaceAll('_',' '):'Result unavailable');
}
function matchWinner(result){const r=String(result||'').toLowerCase();return r==='team1'?1:r==='team2'?2:0}
function ccDateTime(d){return d?new Date(d).toLocaleString(undefined,{year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'Date unavailable'}

/* Load one match. Copes with error pages that are not JSON and with malformed responses. */
async function fetchMatchDetail(id){
  const r=await fetch('/api/matches/'+encodeURIComponent(id));
  const data=await r.json().catch(()=>null);
  if(!r.ok)throw new Error(data?.detail||`Match details could not be loaded (error ${r.status})`);
  if(!data||typeof data!=='object')throw new Error('The server sent an invalid match record');
  return data;
}

/* Full inner HTML for the match-detail modal body. */
function matchDetailHtml(m){
  const winner=matchWinner(m.result);
  const t1=Array.isArray(m.team1)?m.team1:[],t2=Array.isArray(m.team2)?m.team2:[],everyone=Array.isArray(m.players)?m.players:[];
  /* Before the bot saves teams (coin flip, veto, civ picks), list everyone in the match instead. */
  const lineup=(!t1.length&&!t2.length&&everyone.length)
    ?`<div class="detail-teams"><div class="detail-team" style="grid-column:1/-1"><h3>Players</h3><p class="detail-note" style="margin:0 0 6px;color:#9d998f;font-size:11px">Teams appear here once they're set up in Discord.</p>${everyone.map(matchPlayer).join('')}</div></div>`
    :null;
  const live=m.live||null;
  let liveHtml='';
  if(live){
    const bits=[`<b>${ccEsc(live.phase_label||'In progress')}</b>`];
    if(live.map)bits.push(`Map: ${ccEsc(live.map)}`);
    if(live.phase==='civ_select')bits.push(`Team 1 ${live.team1_locked?'locked in ✅':'picking ⏳'} · Team 2 ${live.team2_locked?'locked in ✅':'picking ⏳'}`);
    if(live.bans&&live.bans.status==='choosing')bits.push(`Bans: Team 1 ${live.bans.team1_locked?'locked ✅':'choosing ⏳'} · Team 2 ${live.bans.team2_locked?'locked ✅':'choosing ⏳'}`);
    liveHtml=`<div class="cc-live-banner"><i class="cc-live-dot" aria-hidden="true"></i><span>${bits.join(' · ')}</span><small>Updates automatically</small></div>`;
  }
  const pool=Array.isArray(m.pool)?m.pool:[];
  const poolHtml=pool.length?`<div class="detail-team" style="margin-top:12px"><h3>Waiting to be drafted</h3>${pool.map(matchPlayer).join('')}</div>`:'';
  const team=n=>{const raw=n===1?m.team1:m.team2;const players=Array.isArray(raw)?raw:[];return `<div class="detail-team"><h3>${winner===n?'🏆 ':''}Team ${n}${winner===n?' · Winner':''}</h3>${players.length?players.map(matchPlayer).join(''):'<div class="empty">No players were saved for this team.</div>'}</div>`};
  const html=`${liveHtml}<p class="detail-meta"><b>${ccEsc(matchResultText(m.result))}</b> · ${ccEsc(m.queue_type)}${m.tournament?' <span class="t-label">🏆 Tournament · not counted in club stats</span>':''} · ${ccDateTime(m.created_at)} · ${ccFmt(m.player_count)} players</p>${lineup||`<div class="detail-teams">${team(1)}${team(2)}</div>`}${poolHtml}<div class="detail-meta">${m.map?`<b>Map:</b> ${ccEsc(m.map)}${(m.map_bans||[]).length?` <span style="color:#8f8b82">(banned: ${m.map_bans.map(ccEsc).join(', ')})</span>`:''}<br>`:''}<b>Civilization bans:</b> ${live&&live.bans&&live.bans.status==='choosing'?'Hidden until both sides lock in':live&&!live.bans?'Not decided yet':ccEsc(formatCivBans(m.civ_bans))}<br><b>Match ID:</b> #${ccEsc(m.id)}<br><b>Finished:</b> ${m.finished_at?ccDateTime(m.finished_at):'Not finished'}</div>`;
  /* Running matches refresh themselves while their pop-up is open (see below). */
  return m.result==='pending'?`<div data-live-match="${Number(m.id)}">${html}</div>`:html;
}

/* Every 15 seconds, reload any running match whose details are on screen. Works on
   every page with match pop-ups; stops by itself once the pop-up closes or the match ends. */
let ccLiveRefreshBusy=false;
setInterval(async()=>{
  if(ccLiveRefreshBusy||document.hidden)return;
  const boxes=[...document.querySelectorAll('[data-live-match]')].filter(el=>el.offsetParent!==null);
  if(!boxes.length)return;
  ccLiveRefreshBusy=true;
  try{for(const el of boxes){try{const m=await fetchMatchDetail(el.dataset.liveMatch);if(el.isConnected)el.outerHTML=matchDetailHtml(m)}catch(e){}}}
  finally{ccLiveRefreshBusy=false}
},15000);

/* Navigation: highlight the current page and grey out pages that are not live yet. */
const CC_COMING_SOON=['/contact'];
document.addEventListener('DOMContentLoaded',()=>{
  const clean=p=>{p=(p||'/').replace(/\/+$/,'');return p||'/'};
  const path=clean(location.pathname);
  document.querySelectorAll('.nav a').forEach(a=>{
    const href=clean(new URL(a.getAttribute('href'),location.origin).pathname);
    const active=href===path;
    a.classList.toggle('active',active);
    if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');
    if(CC_COMING_SOON.includes(href)){a.classList.add('nav-coming-soon');a.title='Coming soon'}
  });
  /* Footer: every page links to the privacy page. */
  let footer=document.querySelector('.footer');
  if(!footer){const shell=document.querySelector('.shell');if(shell){footer=document.createElement('footer');footer.className='footer cc-footer';footer.innerHTML='<span>© '+new Date().getFullYear()+' THE CHEESE CLUB</span>';shell.appendChild(footer)}}
  if(footer&&!footer.querySelector('a[href="/privacy"]')){const span=document.createElement('span');span.innerHTML='<a class="footer-link" href="/privacy">Privacy</a>';footer.appendChild(span)}
  ccSetupAccountNav();
});

/* Account menu in the navigation: "Log in" for visitors; name, avatar and a menu
   (My profile, Account, Log out) for members who logged in with Discord. */
function ccCachedMe(){try{return JSON.parse(localStorage.getItem('cc_me')||'null')}catch(e){return null}}
function ccSaveMe(me){try{localStorage.setItem('cc_me',JSON.stringify({username:me.username,avatar_url:me.avatar_url,user_id:me.user_id,in_server:me.in_server,hidden:me.hidden,aoe4:me.aoe4}))}catch(e){};document.cookie='cc_in=1; path=/; max-age=2592000; samesite=lax'}
function ccForgetMe(){try{localStorage.removeItem('cc_me')}catch(e){};document.cookie='cc_in=; path=/; max-age=0';document.documentElement.classList.remove('cc-in')}
let ccAccount=null;
function ccBuildAccount(me){
  const link=document.querySelector('.nav a[href="/login"]');
  const here=location.pathname+location.search;
  const wrap=document.createElement('div');wrap.className='nav-user';
  wrap.innerHTML=`<button type="button" class="nav-user-btn" aria-haspopup="true" aria-expanded="false"><img src="${ccEsc(me.avatar_url||CC_DEFAULT_AVATAR)}" alt=""><span>${ccEsc(me.username||'Account')}</span><i aria-hidden="true">▾</i></button>
  <div class="nav-menu" role="menu" hidden>
    <a role="menuitem" href="/dashboard">🏠 Dashboard</a>
    <a role="menuitem" href="/stats">📊 Stats</a>
    <a role="menuitem" href="/my-tournaments">🏆 My tournaments</a>
    <a role="menuitem" href="/queue">⚔️ Queue</a>
    ${me.hidden?'':`<a role="menuitem" href="${ccPlayerHref(me.user_id)}">👤 My profile</a>`}
    <a role="menuitem" href="/account">🔗 AoE4 account</a>
    <a role="menuitem" href="/settings">⚙️ Settings</a>
    <a role="menuitem" href="/auth/logout?next=${encodeURIComponent(['/account','/dashboard','/stats','/settings','/queue','/my-tournaments'].some(x=>here.startsWith(x))?'/':here)}">🚪 Log out</a>
  </div>`;
  if(['/account','/dashboard','/stats','/settings','/queue','/my-tournaments'].includes(location.pathname))wrap.classList.add('active');
  /* The bell and account button live on the right of the header, next to (or
     instead of) "Join Discord", not in the row of links, so long names never
     push them onto a second line. Members already in the server don't need the
     Join Discord button. */
  document.documentElement.classList.add('cc-in');            /* hides "Log in" in the row of links */
  const topbar=document.querySelector('.topbar'),discord=topbar&&topbar.querySelector('.discord');
  const actions=document.createElement('div');actions.className='top-actions';
  if(discord){discord.before(actions);if(me.in_server)discord.hidden=true;else{actions.appendChild(discord);actions.classList.add('with-discord')}}
  else if(topbar)topbar.appendChild(actions);
  actions.prepend(wrap);
  ccAccount={wrap,actions,discord,topbar};
  const lo=wrap.querySelector('a[href^="/auth/logout"]');if(lo)lo.addEventListener('click',ccForgetMe);
  ccSetupBell(wrap);
  ccWelcomeStrip(me);
  const btn=wrap.querySelector('button'),menu=wrap.querySelector('.nav-menu');
  const set=open=>{menu.hidden=!open;btn.setAttribute('aria-expanded',String(open))};
  btn.addEventListener('click',e=>{e.stopPropagation();set(menu.hidden)});
  document.addEventListener('click',e=>{if(!wrap.contains(e.target))set(false)});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')set(false)});
}
function ccUpdateAccount(me){
  if(!ccAccount)return;
  const img=ccAccount.wrap.querySelector('.nav-user-btn img'),name=ccAccount.wrap.querySelector('.nav-user-btn span');
  if(img&&(me.avatar_url||CC_DEFAULT_AVATAR)!==img.getAttribute('src'))img.src=me.avatar_url||CC_DEFAULT_AVATAR;
  if(name&&name.textContent!==(me.username||'Account'))name.textContent=me.username||'Account';
  if(ccAccount.discord){const inServer=!!me.in_server;ccAccount.discord.hidden=inServer;if(!inServer&&ccAccount.discord.parentElement!==ccAccount.actions){ccAccount.actions.appendChild(ccAccount.discord);ccAccount.actions.classList.add('with-discord')}}
}
function ccRevertAccount(){
  /* the server says we're logged out after all (e.g. the session expired): back to the visitor header */
  if(!ccAccount)return;
  const a=ccAccount;ccAccount=null;
  if(a.discord&&a.topbar){a.discord.hidden=false;a.topbar.appendChild(a.discord)}
  a.actions.remove();
}
async function ccSetupAccountNav(){
  const link=document.querySelector('.nav a[href="/login"]');
  if(!link)return;
  const cached=ccCachedMe();
  if(cached&&cached.username!==undefined)ccBuildAccount(cached);       /* draw the member header at once */
  let me=null,failed=false;
  try{const r=await fetch('/api/me',{credentials:'same-origin'});if(r.ok)me=await r.json();else failed=true}catch(e){failed=true}
  if(me&&me.logged_in){ccSaveMe(me);ccAccount?ccUpdateAccount(me):ccBuildAccount(me)}
  else if(me){ccForgetMe();ccRevertAccount();link.textContent='Log in'}
  /* if the request failed we keep whatever we are showing */
}

/* Home page: a small "Welcome back" strip for logged-in members. */
async function ccWelcomeStrip(me){
  const box=document.getElementById('welcomeStrip');
  if(!box)return;
  let d=null;try{const r=await fetch('/api/me/dashboard',{credentials:'same-origin'});if(r.ok)d=await r.json()}catch(e){}
  const bits=[];
  if(d){bits.push(`${ccFmt(d.record.wins)}–${ccFmt(d.record.losses)}`);
    const s=d.current_streak;if(s&&s.count&&s.type==='win')bits.push(`🔥 ${s.count} win streak`);
    if(d.rank)bits.push(`#${ccFmt(d.rank)} in the club`);
    if(d.in_voice)bits.push(`🎙️ ${ccEsc(d.in_voice)}`);}
  box.innerHTML=`<img src="${ccEsc(me.avatar_url||CC_DEFAULT_AVATAR)}" alt=""><div class="ws-text"><b>Welcome back, ${ccEsc(me.username)}</b>${bits.length?`<span>${bits.join(' · ')}</span>`:''}</div><div class="ws-actions">${me.aoe4?'':'<a class="btn secondary" href="/account">Link AoE4 account</a>'}<a class="btn primary" href="/dashboard">Dashboard</a></div>`;
  box.hidden=false;
}

/* Notification bell (logged-in members): queue alerts, matches, results and
   achievements. Refreshes every 20 seconds; optional browser pop-ups. */
let ccBellItems=[],ccBellTimer=null;
function ccAgo(d){const s=Math.max(0,(Date.now()-new Date(d))/1000);return s<60?'just now':s<3600?Math.floor(s/60)+' min ago':s<86400?Math.floor(s/3600)+'h ago':new Date(d).toLocaleDateString(undefined,{month:'short',day:'numeric'})}
function ccSetupBell(anchor){
  const bell=document.createElement('div');bell.className='nav-bell';
  bell.innerHTML=`<button type="button" class="bell-btn" aria-label="Notifications" aria-haspopup="true" aria-expanded="false"><span aria-hidden="true">🔔</span><b class="bell-badge" hidden></b></button>
  <div class="bell-panel" role="dialog" aria-label="Notifications" hidden><div class="bell-head"><b>Notifications</b><button type="button" class="bell-readall">Mark all as read</button></div><div class="bell-list"><div class="bell-empty">Loading…</div></div><a class="bell-foot" href="/settings#alerts">🔔 Queue alert settings</a></div>`;
  anchor.before(bell);
  const btn=bell.querySelector('.bell-btn'),panel=bell.querySelector('.bell-panel');
  const set=open=>{panel.hidden=!open;btn.setAttribute('aria-expanded',String(open));if(open)ccRenderBell(bell)};
  btn.addEventListener('click',e=>{e.stopPropagation();set(panel.hidden)});
  document.addEventListener('click',e=>{if(!bell.contains(e.target))set(false)});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')set(false)});
  bell.querySelector('.bell-readall').addEventListener('click',async()=>{await ccMarkRead({all:true});ccBellItems.forEach(i=>i.read_at=i.read_at||new Date().toISOString());ccSetBadge(bell,0);ccRenderBell(bell)});
  bell.querySelector('.bell-list').addEventListener('click',async e=>{const a=e.target.closest('[data-nid]');if(!a)return;const it=ccBellItems.find(i=>String(i.id)===a.dataset.nid);
    if(it&&!it.read_at){e.preventDefault();await ccMarkRead({ids:[it.id]});location.href=a.getAttribute('href')}});
  ccPollBell(bell);ccBellTimer=setInterval(()=>{if(!document.hidden)ccPollBell(bell)},20000);
}
async function ccMarkRead(body){try{await fetch('/api/me/notifications/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),credentials:'same-origin'})}catch(e){}}
function ccSetBadge(bell,n){const badge=bell.querySelector('.bell-badge');badge.textContent=n>9?'9+':String(n);badge.hidden=!n;
  bell.querySelector('.bell-btn').setAttribute('aria-label',n?`Notifications, ${n} unread`:'Notifications')}
async function ccPollBell(bell){
  let d;try{const r=await fetch('/api/me/notifications',{credentials:'same-origin'});if(!r.ok)return;d=await r.json()}catch(e){return}
  ccBellItems=d.items||[];
  ccSetBadge(bell,d.unread);
  if(!bell.querySelector('.bell-panel').hidden)ccRenderBell(bell);
  /* Browser pop-ups for new notices (only after the first check, so old ones don't pop up) */
  try{
    const stored=localStorage.getItem('cc_last_notice'),last=Number(stored||0);
    const newest=ccBellItems.reduce((m,i)=>Math.max(m,Number(i.id)),last);
    if(stored!==null&&d.browser_popups&&'Notification' in window&&Notification.permission==='granted')
      ccBellItems.filter(i=>Number(i.id)>last&&!i.read_at).slice(0,3).forEach(i=>{const n=new Notification(i.title,{body:i.body||'',tag:'cc-'+i.id,icon:'/static/cheese-club-logo.png'});n.onclick=()=>{window.focus();location.href=i.link||'/dashboard'}});
    if(stored===null||newest>last)localStorage.setItem('cc_last_notice',String(newest));
  }catch(e){}
}
function ccRenderBell(bell){
  const list=bell.querySelector('.bell-list');
  list.innerHTML=ccBellItems.length?ccBellItems.map(i=>`<a class="bell-item ${i.read_at?'':'unread'}" href="${ccEsc(i.link||'/dashboard')}" data-nid="${i.id}"><span class="bell-dot" aria-hidden="true"></span><span class="bell-text"><b>${ccEsc(i.title)}</b>${i.body?`<span>${ccEsc(i.body)}</span>`:''}<small>${ccAgo(i.created_at)}</small></span></a>`).join(''):'<div class="bell-empty">You\'re all caught up. 🧀<br><small>Queue alerts, match results and new achievements show up here.</small></div>';
}


/* ---- Sortable tables -------------------------------------------------------
   Every table with a header row becomes sortable: click a column header to sort
   (high to low first, click again for low to high). The choice is remembered for
   the page, so it survives the automatic refreshes. Tables that sort themselves
   (header buttons with data-sort) are left alone. */
const ccSortState={};
function ccCellValue(td){
  if(!td)return {n:null,t:''};
  if(td.dataset.sort!==undefined){const v=Number(td.dataset.sort);return isNaN(v)?{n:null,t:td.dataset.sort.toLowerCase()}:{n:v,t:''}}
  const t=(td.innerText||td.textContent||'').trim();
  if(!t||t==='—'||t==='-'||t==='–'||t==='N/A')return {n:-Infinity,t:''};
  const dur=t.replace(/,/g,'').match(/^(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s)?$/i);
  if(dur&&dur.slice(1).some(Boolean))return {n:((+dur[1]||0)*1440+(+dur[2]||0)*60+(+dur[3]||0))*60+(+dur[4]||0),t:''};
  const num=t.replace(/[,\s]/g,'').match(/^[#+]?(-?\d+(?:\.\d+)?)/);
  if(num)return {n:parseFloat(num[1]),t:''};
  const any=t.replace(/,/g,'').match(/-?\d+(?:\.\d+)?/);
  if(any&&/^[^a-z]*\d/i.test(t))return {n:parseFloat(any[0]),t:''};
  return {n:null,t:t.toLowerCase()};
}
function ccTableKey(table){return location.pathname+'|'+(table.id||'')+'|'+[...table.querySelectorAll('thead th')].map(th=>th.textContent.trim()).join('/')}
let ccSorting=false;
function ccApplySort(table){
  const st=ccSortState[ccTableKey(table)];const ths=[...table.querySelectorAll('thead th')];
  ths.forEach((th,i)=>{if(st&&st.col===i)th.setAttribute('aria-sort',st.dir>0?'ascending':'descending');else th.removeAttribute('aria-sort')});
  if(!st)return;
  const body=table.tBodies[0];if(!body)return;
  const rows=[...body.rows].filter(r=>r.cells.length>1);if(rows.length<2)return;
  const keyed=rows.map((r,i)=>({r,i,v:ccCellValue(r.cells[st.col])}));
  keyed.sort((a,b)=>{
    const an=a.v.n!==null,bn=b.v.n!==null;
    if(an&&bn&&a.v.n!==b.v.n){if(a.v.n===-Infinity)return 1;if(b.v.n===-Infinity)return -1;return st.dir*(a.v.n-b.v.n)}
    if(!an&&!bn&&a.v.t!==b.v.t)return st.dir*a.v.t.localeCompare(b.v.t);
    if(an!==bn)return an?-1:1;
    return a.i-b.i});
  if(keyed.every((k,i)=>k.r===rows[i]))return;
  ccSorting=true;keyed.forEach(k=>body.appendChild(k.r));ccSorting=false;
  if(window.ccSortObserver)window.ccSortObserver.takeRecords();
}
function ccSortTables(root){
  (root||document).querySelectorAll('table').forEach(table=>{
    if(table.dataset.ccSortable||!table.tHead||table.querySelector('thead th button[data-sort]'))return;
    const ths=[...table.querySelectorAll('thead th')];if(!ths.length)return;
    table.dataset.ccSortable='1';
    ths.forEach((th,i)=>{
      if(!th.textContent.trim()||th.dataset.nosort!==undefined)return;
      th.classList.add('cc-sort');th.tabIndex=0;th.setAttribute('role','button');th.title='Sort by '+th.textContent.trim();
      const go=()=>{const k=ccTableKey(table),cur=ccSortState[k];
        const textCol=[...table.tBodies[0]?.rows||[]].slice(0,5).every(r=>ccCellValue(r.cells[i]).n===null);
        ccSortState[k]={col:i,dir:cur&&cur.col===i?-cur.dir:(textCol?1:-1)};ccApplySort(table)};
      th.addEventListener('click',go);th.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();go()}});
    });
    ccApplySort(table);
  });
}
if(typeof MutationObserver!=='undefined'){
  const ccObs=window.ccSortObserver=new MutationObserver(muts=>{if(ccSorting)return;
    const tables=new Set();
    muts.forEach(m=>{const el=m.target.nodeType===1?m.target:m.target.parentElement;const t=el&&el.closest&&el.closest('table');if(t)tables.add(t);
      m.addedNodes.forEach(n=>{if(n.nodeType===1){if(n.tagName==='TABLE')tables.add(n);n.querySelectorAll&&n.querySelectorAll('table').forEach(x=>tables.add(x))}})});
    tables.forEach(t=>{if(!t.dataset.ccSortable)ccSortTables(t.parentElement||document);else ccApplySort(t)});
  });
  const ccStart=()=>{ccSortTables(document);ccObs.observe(document.body,{childList:true,subtree:true})};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',ccStart);else ccStart();
}
