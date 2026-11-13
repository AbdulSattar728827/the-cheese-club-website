/* The Cheese Club — member area: side menu, login check and charts. Needs common.js. */
const CC_COLORS={gold:'#f0b82f',green:'#69d49a',red:'#e2735f',blue:'#8fb6ff',grid:'#ffffff14',text:'#8f8b82'};

/* Load the logged-in member and draw the side menu. Sends visitors to log in first. */
async function ccMemberInit(active){
  let me=null;
  try{const r=await fetch('/api/me',{credentials:'same-origin'});me=await r.json()}catch(e){}
  if(!me||!me.logged_in){
    if(me&&me.login_enabled===false){document.getElementById('memberMain').innerHTML='<div class="m-empty">Logging in isn\'t switched on yet. Please check back soon.</div>';return null}
    location.href='/auth/login?next='+encodeURIComponent(location.pathname);return null;
  }
  const item=(key,icon,label,href)=>`<a href="${href}" class="${active===key?'on':''}"${active===key?' aria-current="page"':''}><span aria-hidden="true">${icon}</span>${label}<span></span></a>`;
  document.getElementById('memberSide').innerHTML=`
    <div class="side-me"><img src="${ccEsc(me.avatar_url||CC_DEFAULT_AVATAR)}" alt=""><div style="min-width:0"><b>${ccEsc(me.username)}</b><small id="sideVoice">${me.in_server?'Cheese Club member':'Not in the Discord server yet'}</small></div></div>
    <nav aria-label="Member menu">
      ${item('dashboard','🏠','Dashboard','/dashboard')}
      ${item('stats','📊','Stats','/stats')}
      <a href="/queue" class="${active==='queue'?'on':''}"${active==='queue'?' aria-current="page"':''}><span aria-hidden="true">⚔️</span>Queue<span class="pill" id="queuePill" hidden></span></a>
      ${me.hidden?`<span class="off" title="Your profile is hidden (see Settings)"><span aria-hidden="true">👤</span>My Profile<span class="pill">Hidden</span></span>`:item('profile','👤','My Profile',ccPlayerHref(me.user_id))}
      ${item('account','🔗','AoE4 Account','/account')}
      ${item('settings','⚙️','Settings','/settings')}
      <hr>
      <a class="logout" href="/auth/logout?next=/"><span aria-hidden="true">🚪</span>Log out<span></span></a>
    </nav>`;
  if(active!=='queue')fetch('/api/queue').then(r=>r.ok?r.json():null).then(s=>ccMemberQueueBadge(s,me)).catch(()=>{});
  return me;
}
/* Side-menu badge: your queue (green), or the fullest queue. */
function ccMemberQueueBadge(snap,me){const pill=document.getElementById('queuePill');if(!pill||!snap||!snap.available)return;
  const mine=snap.queues.find(q=>q.players.some(p=>p.user_id&&p.user_id===me.user_id));
  const best=mine||[...snap.queues].filter(q=>q.players.length).sort((a,b)=>(b.players.length/b.size)-(a.players.length/a.size))[0];
  if(!best){pill.hidden=true;return}
  pill.textContent=`${best.queue} ${best.players.length}/${best.size}`;pill.hidden=false;
  pill.style.cssText=mine?'border-color:#285a40;color:#8fe0b2':'border-color:#6e5424;color:var(--gold)';}
function ccMemberVoice(channel){const el=document.getElementById('sideVoice');if(el&&channel){el.textContent='● In voice · '+channel;el.classList.add('live')}}

/* ---- charts (inline SVG, no libraries) ----
   Every chart shows exact values on hover (mouse), tap (touch) or focus (keyboard):
   elements with data-tip get a tooltip; the rating chart follows the cursor. */
const ccTip=document.createElement('div');ccTip.className='cc-tip';ccTip.setAttribute('role','tooltip');ccTip.hidden=true;
document.addEventListener('DOMContentLoaded',()=>document.body.appendChild(ccTip));
let ccTipFrom=null;
function ccTipText(t){return t.split('\n').map((line,i)=>i===0?`<b>${ccEsc(line)}</b>`:`<span>${ccEsc(line)}</span>`).join('')}
function ccShowTip(html,x,y){ccTip.innerHTML=html;ccTip.hidden=false;const r=ccTip.getBoundingClientRect();
  let left=x+14,top=y-r.height-12;if(left+r.width>innerWidth-8)left=x-r.width-14;if(left<8)left=8;if(top<8)top=y+18;ccTip.style.left=left+'px';ccTip.style.top=top+'px'}
function ccHideTip(){ccTip.hidden=true;if(ccTipFrom){ccTipFrom.classList.remove('tip-on');ccTipFrom=null}}
function ccTipOn(el,x,y){if(ccTipFrom&&ccTipFrom!==el)ccTipFrom.classList.remove('tip-on');ccTipFrom=el;el.classList.add('tip-on');ccShowTip(ccTipText(el.dataset.tip),x,y)}
document.addEventListener('pointermove',e=>{
  const ov=e.target.closest?.('.rating-hover');if(ov){ccRatingHover(ov,e);return}
  const el=e.target.closest?.('[data-tip]');if(el)ccTipOn(el,e.clientX,e.clientY);else if(!ccTip.hidden&&e.pointerType==='mouse'){ccHideTip();ccRatingClear()}});
document.addEventListener('pointerdown',e=>{if(e.pointerType==='mouse')return;
  const ov=e.target.closest?.('.rating-hover');if(ov){ccRatingHover(ov,e);return}
  const el=e.target.closest?.('[data-tip]');if(el)ccTipOn(el,e.clientX,e.clientY);else{ccHideTip();ccRatingClear()}});
document.addEventListener('focusin',e=>{const el=e.target.closest?.('[data-tip]');if(el){const r=el.getBoundingClientRect();ccTipOn(el,r.left+r.width/2,r.top)}});
document.addEventListener('focusout',()=>ccHideTip());
document.addEventListener('scroll',()=>{if(!ccTip.hidden){ccHideTip();ccRatingClear()}},{passive:true});

function ccAxis(W,H,pad,max,steps,unit){let s='';for(let i=0;i<=steps;i++){const v=max*i/steps,y=H-pad[2]-(H-pad[0]-pad[2])*i/steps;
  s+=`<line x1="${pad[3]}" x2="${W-pad[1]}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${CC_COLORS.grid}"/><text x="${pad[3]-8}" y="${(y+4).toFixed(1)}" fill="${CC_COLORS.text}" font-size="10" text-anchor="end">${+v.toFixed(1)}${unit||''}</text>`}return s}
function ccNiceMax(v,min){v=Math.max(v,min||1);const p=Math.pow(10,Math.floor(Math.log10(v))),n=v/p;return (n<=1?1:n<=2?2:n<=3?3:n<=5?5:10)*p}
function ccWeekLabel(iso){return new Date(iso+'T00:00:00Z').toLocaleDateString(undefined,{month:'short',day:'numeric',timeZone:'UTC'})}
function ccHoverCol(x,y,w,h,tip){return `<rect class="hover-col" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" data-tip="${ccEsc(tip)}" tabindex="0"/>`}

function ccChartWinLoss(weeks){
  const W=560,H=210,pad=[14,10,30,34],peak=Math.max(...weeks.map(w=>Math.max(w.wins,w.losses))),max=Math.max(3,Math.ceil(peak/3)*3);
  const cw=(W-pad[1]-pad[3])/weeks.length,bw=cw*0.3,ph=H-pad[0]-pad[2];let s=ccAxis(W,H,pad,max,3),cols='';
  weeks.forEach((w,i)=>{const x=pad[3]+cw*i+cw*0.18;
    [[w.wins,CC_COLORS.green],[w.losses,CC_COLORS.red]].forEach(([v,c],j)=>{const h=ph*v/max;
      s+=`<rect x="${(x+j*(bw+2)).toFixed(1)}" y="${(H-pad[2]-h).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(h,0).toFixed(1)}" rx="2" fill="${c}" opacity=".9"/>`});
    if(i%2===0)s+=`<text x="${(x+bw).toFixed(1)}" y="${H-10}" fill="${CC_COLORS.text}" font-size="10" text-anchor="middle">${ccWeekLabel(w.week)}</text>`;
    const games=w.wins+w.losses;
    cols+=ccHoverCol(pad[3]+cw*i,pad[0],cw,ph,`Week of ${ccWeekLabel(w.week)}\nWins: ${w.wins} · Losses: ${w.losses}\n${games?Math.round(100*w.wins/games)+'% win rate':'No club games'}`)});
  return `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="Wins and losses per week">${s}${cols}</svg>`}

function ccChartLine(series,opts){ /* series: [{values:[{x:label,y:num}], color, label}]; opts.tip(value,index) */
  const W=opts.width||460,H=opts.height||210,pad=[16,14,30,44];const all=series.flatMap(s=>s.values.map(v=>v.y));
  if(!all.length)return '';
  let lo=opts.zero?0:Math.min(...all),hi=Math.max(...all);if(opts.zero)hi=ccNiceMax(hi,1);else{const span=Math.max(hi-lo,40);lo=Math.floor((lo-span*.15)/10)*10;hi=Math.ceil((hi+span*.15)/10)*10}
  let s='';for(let i=0;i<=3;i++){const v=lo+(hi-lo)*i/3,y=H-pad[2]-(H-pad[0]-pad[2])*i/3;s+=`<line x1="${pad[3]}" x2="${W-pad[1]}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${CC_COLORS.grid}"/><text x="${pad[3]-8}" y="${(y+4).toFixed(1)}" fill="${CC_COLORS.text}" font-size="10" text-anchor="end">${Math.round(v)}${opts.unit||''}</text>`}
  const n=Math.max(...series.map(x=>x.values.length)),xAt=i=>pad[3]+(W-pad[1]-pad[3])*(n>1?i/(n-1):.5);
  series.forEach((ser,si)=>{const pts=ser.values.map((v,i)=>[xAt(i),H-pad[2]-(H-pad[0]-pad[2])*(v.y-lo)/(hi-lo||1)]);
    if(opts.area&&si===0){s+=`<defs><linearGradient id="ccg${si}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${ser.color}" stop-opacity=".35"/><stop offset="1" stop-color="${ser.color}" stop-opacity="0"/></linearGradient></defs><path d="M${pts.map(p=>p[0].toFixed(1)+','+p[1].toFixed(1)).join(' L')} L${pts[pts.length-1][0].toFixed(1)},${H-pad[2]} L${pts[0][0].toFixed(1)},${H-pad[2]} Z" fill="url(#ccg${si})"/>`}
    s+=`<polyline points="${pts.map(p=>p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ')}" fill="none" stroke="${ser.color}" stroke-width="2.5" stroke-linejoin="round"/>`;
    if(opts.dots)pts.forEach(p=>{s+=`<circle class="dot" cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="3.5" fill="#17171b" stroke="${ser.color}" stroke-width="2"/>`})});
  const labels=series[0].values.map(v=>v.x),every=Math.max(1,Math.ceil(labels.length/(opts.ticks||4)));
  labels.forEach((l,i)=>{if(i%every===0)s+=`<text x="${xAt(i).toFixed(1)}" y="${H-10}" fill="${CC_COLORS.text}" font-size="10" text-anchor="middle">${ccEsc(l)}</text>`});
  if(opts.tip){const cw=(W-pad[1]-pad[3])/Math.max(n-1,1);series[0].values.forEach((v,i)=>{s+=ccHoverCol(Math.max(pad[3],xAt(i)-cw/2),pad[0],Math.min(cw,W-pad[1]-Math.max(pad[3],xAt(i)-cw/2)),H-pad[0]-pad[2],opts.tip(v,i))})}
  return `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="${ccEsc(opts.label||'Chart')}">${s}</svg>`}

function ccCivBars(civs){if(!civs.length)return '<div class="m-empty">No civilizations recorded yet. Play a few club matches and your civs show up here.</div>';
  const max=Math.max(...civs.map(c=>c.games));
  return civs.slice(0,8).map(c=>{const wr=Math.round(c.win_rate||0),col=wr>=60?CC_COLORS.green:wr>=45?CC_COLORS.gold:CC_COLORS.red;
    const tip=`${c.civ}\n${c.games} game${c.games===1?'':'s'} · ${c.wins} win${c.wins===1?'':'s'}, ${c.losses} loss${c.losses===1?'':'es'}\n${wr}% win rate`;
    return `<div class="civbar" data-tip="${ccEsc(tip)}" tabindex="0"><span class="civ-name"><span class="civ-flag">${civFlag(c.civ)}</span><span>${ccEsc(c.civ)}</span></span><div class="track"><i style="width:${(100*c.games/max).toFixed(0)}%;background:${col}"></i></div><span class="g">${c.games} game${c.games===1?'':'s'}</span><span class="wr" style="color:${col}">${wr}%</span></div>`}).join('')}

function ccDonut(queues){const cols=[CC_COLORS.gold,CC_COLORS.green,CC_COLORS.blue,CC_COLORS.red,'#b48cf0'];const tot=queues.reduce((a,q)=>a+q.games,0);
  if(!tot)return '<div class="m-empty">No club matches in this period.</div>';
  const r=60,C=2*Math.PI*r;let off=0,s='';
  queues.forEach((q,i)=>{const L=C*q.games/tot,tip=`${q.queue}\n${q.games} game${q.games===1?'':'s'} (${Math.round(100*q.games/tot)}% of your games)\n${q.wins} win${q.wins===1?'':'s'} · ${Math.round(100*q.wins/q.games)}% win rate`;
    s+=`<circle class="seg" r="${r}" cx="90" cy="90" fill="none" stroke="${cols[i%cols.length]}" stroke-width="24" stroke-dasharray="${Math.max(L-2,0.5).toFixed(1)} ${(C-L+2).toFixed(1)}" stroke-dashoffset="${(-off).toFixed(1)}" transform="rotate(-90 90 90)" data-tip="${ccEsc(tip)}" tabindex="0"/>`;off+=L});
  s+=`<text x="90" y="88" text-anchor="middle" fill="#f6f3eb" font-size="28" font-weight="800" font-family="Barlow Condensed" pointer-events="none">${tot}</text><text x="90" y="106" text-anchor="middle" fill="${CC_COLORS.text}" font-size="10" font-family="Inter" pointer-events="none">games</text>`;
  return `<div class="donut-wrap"><svg viewBox="0 0 180 180" width="170" height="170" role="img" aria-label="Games per queue">${s}</svg><div>${queues.map((q,i)=>`<div class="leg"><i style="background:${cols[i%cols.length]}"></i><b>${ccEsc(q.queue)}</b><span>${q.games} game${q.games===1?'':'s'} · ${Math.round(100*q.wins/q.games)}% wins</span></div>`).join('')}</div></div>`}

function ccHeatmap(times){if(!times.length)return '<div class="m-empty">No club matches in this period.</div>';
  const days=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'],full=['Mondays','Tuesdays','Wednesdays','Thursdays','Fridays','Saturdays','Sundays'],slots=['12a','3a','6a','9a','12p','3p','6p','9p'],grid=days.map(()=>slots.map(()=>0));
  times.forEach(t=>{const d=new Date(t);grid[(d.getDay()+6)%7][Math.floor(d.getHours()/3)]++});
  const fmtH=h=>new Date(2000,0,1,h%24).toLocaleTimeString([], {hour:'numeric'});
  const max=Math.max(...grid.flat());let best=[0,0];grid.forEach((row,i)=>row.forEach((v,j)=>{if(v>grid[best[0]][best[1]])best=[i,j]}));
  let s='<div class="heat"><span></span>'+slots.map(x=>`<span class="hl">${x}</span>`).join('');
  grid.forEach((row,i)=>{s+=`<span class="dl">${days[i]}</span>`+row.map((v,j)=>`<i style="background:rgba(240,184,47,${(0.06+0.85*v/max).toFixed(2)})" data-tip="${ccEsc(`${full[i]}, ${fmtH(j*3)}–${fmtH(j*3+3)}\n${v} game${v===1?'':'s'} in this period`)}" tabindex="0"></i>`).join('')});
  const peak=grid[best[0]][best[1]];
  return s+`</div><p class="hint">${peak>=3?`You play most on <b>${full[best[0]]} between ${fmtH(best[1]*3)} and ${fmtH(best[1]*3+3)}</b> (your local time).`:'Play a few more club matches and your usual playing times will show up here.'}</p>`}

/* Rating over time: a guide line follows the cursor and the tooltip shows each line's exact rating. */
const ccRatingCharts={};let ccRatingSeq=0;
function ccNiceStep(range){const raw=Math.max(range,30)/3,p=Math.pow(10,Math.floor(Math.log10(raw)));for(const m of [1,2,2.5,5,10])if(raw<=m*p)return m*p;return 10*p}
function ccRatingChart(lines){ /* lines: [{label,color,points:[{t,rating}]}] (oldest first) */
  lines=lines.filter(l=>l.points.length>1);if(!lines.length)return '';
  const narrow=window.innerWidth<600,W=narrow?520:900,H=narrow?320:250,pad=narrow?[22,18,36,70]:[18,16,30,52],all=lines.flatMap(l=>l.points.map(p=>p.rating));
  const step=ccNiceStep(Math.max(...all)-Math.min(...all));let lo=Math.floor(Math.min(...all)/step)*step,hi=lo+3*step;while(hi<Math.max(...all))hi+=step;
  const steps=Math.round((hi-lo)/step),Y=v=>H-pad[2]-(H-pad[0]-pad[2])*(v-lo)/(hi-lo);
  const timed=lines.every(l=>l.points.every(p=>p.t));
  const t0=timed?Math.min(...lines.map(l=>+new Date(l.points[0].t))):0,t1=timed?Math.max(...lines.map(l=>+new Date(l.points[l.points.length-1].t))):1;
  const X=(l,i)=>pad[3]+(W-pad[1]-pad[3])*(timed?((+new Date(l.points[i].t)-t0)/((t1-t0)||1)):i/(l.points.length-1));
  const id='rc'+(++ccRatingSeq);let s='';
  for(let i=0;i<=steps;i++){const v=lo+step*i,y=Y(v);s+=`<line x1="${pad[3]}" x2="${W-pad[1]}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${CC_COLORS.grid}"/><text x="${pad[3]-8}" y="${(y+4).toFixed(1)}" fill="${CC_COLORS.text}" font-size="10" text-anchor="end">${ccFmt(v)}</text>`}
  const data=lines.map(l=>({label:l.label,color:l.color,pts:l.points.map((p,i)=>({x:X(l,i),y:Y(p.rating),rating:p.rating,t:p.t,n:i+1}))}));
  data.forEach(l=>{const last=l.pts[l.pts.length-1];
    s+=`<polyline points="${l.pts.map(q=>q.x.toFixed(1)+','+q.y.toFixed(1)).join(' ')}" fill="none" stroke="${l.color}" stroke-width="2.5" stroke-linejoin="round"/>`;
    s+=`<circle cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="4.5" fill="${l.color}"/><text x="${(last.x-8).toFixed(1)}" y="${(last.y-10).toFixed(1)}" fill="${l.color}" font-size="11" font-weight="700" text-anchor="end">${ccEsc(l.label)} ${ccFmt(last.rating)}</text>`});
  if(timed){const nT=narrow?3:4;for(let i=0;i<=nT;i++){const t=t0+(t1-t0)*i/nT,x=pad[3]+(W-pad[1]-pad[3])*i/nT;s+=`<text x="${x.toFixed(1)}" y="${H-10}" fill="${CC_COLORS.text}" font-size="10" text-anchor="${i===0?'start':i===nT?'end':'middle'}">${new Date(t).toLocaleDateString(undefined,{month:'short',day:'numeric'})}</text>`}}
  else s+=`<text x="${pad[3]}" y="${H-10}" fill="${CC_COLORS.text}" font-size="10">Older games</text><text x="${W-pad[1]}" y="${H-10}" fill="${CC_COLORS.text}" font-size="10" text-anchor="end">Most recent</text>`;
  s+=`<g class="rating-guide" id="${id}g" style="display:none"><line y1="${pad[0]}" y2="${H-pad[2]}" stroke="#ffffff55" stroke-dasharray="3 3"/>${data.map((l,k)=>`<circle r="5" fill="#17171b" stroke="${l.color}" stroke-width="2.5" data-k="${k}"/>`).join('')}</g>`;
  s+=`<rect class="rating-hover" data-chart="${id}" x="${pad[3]}" y="${pad[0]}" width="${W-pad[1]-pad[3]}" height="${H-pad[0]-pad[2]}" fill="transparent"/>`;
  ccRatingCharts[id]={data,timed};
  return `<svg viewBox="0 0 ${W} ${H}" class="chart${narrow?'':' chart-wide'}" role="img" aria-label="Official rating over time">${s}</svg>`;
}
function ccRatingHover(rect,e){
  const chart=ccRatingCharts[rect.dataset.chart];if(!chart)return;const svg=rect.ownerSVGElement;
  const pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;const x=pt.matrixTransform(svg.getScreenCTM().inverse()).x;
  const near=chart.data.map(l=>l.pts.reduce((a,p)=>Math.abs(p.x-x)<Math.abs(a.x-x)?p:a));
  const lead=near.reduce((a,p)=>Math.abs(p.x-x)<Math.abs(a.x-x)?p:a);
  const g=document.getElementById(rect.dataset.chart+'g');g.style.display='';g.querySelector('line').setAttribute('x1',lead.x);g.querySelector('line').setAttribute('x2',lead.x);
  g.querySelectorAll('circle').forEach(c=>{const p=near[+c.dataset.k];c.setAttribute('cx',p.x);c.setAttribute('cy',p.y)});
  const head=lead.t?new Date(lead.t).toLocaleDateString(undefined,{weekday:'short',year:'numeric',month:'short',day:'numeric'}):`Ranked game ${lead.n}`;
  const rows=chart.data.map((l,k)=>{const p=near[k];return `<span><i style="background:${l.color}"></i>${ccEsc(l.label)}: <b>${ccFmt(p.rating)}</b>${chart.timed&&p.t&&p!==lead&&new Date(p.t).toDateString()!==new Date(lead.t).toDateString()?` <em>(${new Date(p.t).toLocaleDateString(undefined,{month:'short',day:'numeric'})})</em>`:''}</span>`}).join('');
  ccShowTip(`<b>${ccEsc(head)}</b>${rows}`,e.clientX,e.clientY);
}
function ccRatingClear(){document.querySelectorAll('.rating-guide').forEach(g=>g.style.display='none')}
