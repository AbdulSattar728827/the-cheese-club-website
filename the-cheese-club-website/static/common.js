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

function ccPlayerHref(publicId){return '/player/'+encodeURIComponent(publicId)}

/* One player row inside the match-detail modal. */
function matchPlayer(p){
  const player=p||{};
  const username=player.username||'Unknown player';
  const avatar=player.avatar_url||CC_DEFAULT_AVATAR;
  const civ=player.civ||'Not recorded';
  const elo=player.elo!=null&&player.elo!==''?`${ccFmt(player.elo)} ELO`:'ELO unavailable';
  const hint=player.id_hint||'Unavailable';
  const nameHtml=player.user_id?`<a class="player-page-link" href="${ccPlayerHref(player.user_id)}">${ccEsc(username)}</a>`:ccEsc(username);
  return `<div class="detail-player"><img src="${ccEsc(avatar)}" alt="" loading="lazy" onerror="this.onerror=null;this.src='${CC_DEFAULT_AVATAR}'"><div class="detail-player-info"><b>${nameHtml}</b><small>${ccEsc(civ)} · ${elo}</small><small>Discord ID: ${ccEsc(hint)}</small></div><span class="civ-flag" title="${ccEsc(civ)}">${civFlag(civ)}</span></div>`;
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
  const team=n=>{const raw=n===1?m.team1:m.team2;const players=Array.isArray(raw)?raw:[];return `<div class="detail-team"><h3>${winner===n?'🏆 ':''}Team ${n}${winner===n?' · Winner':''}</h3>${players.length?players.map(matchPlayer).join(''):'<div class="empty">No players were saved for this team.</div>'}</div>`};
  return `<p class="detail-meta"><b>${ccEsc(matchResultText(m.result))}</b> · ${ccEsc(m.queue_type)} · ${ccDateTime(m.created_at)} · ${ccFmt(m.player_count)} players</p><div class="detail-teams">${team(1)}${team(2)}</div><div class="detail-meta"><b>Civilization bans:</b> ${ccEsc(formatCivBans(m.civ_bans))}<br><b>Match ID:</b> #${ccEsc(m.id)}<br><b>Finished:</b> ${m.finished_at?ccDateTime(m.finished_at):'Not finished'}</div>`;
}

/* Navigation: highlight the current page and grey out pages that are not live yet. */
const CC_COMING_SOON=['/tournament','/contact','/login','/signup'];
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
});
