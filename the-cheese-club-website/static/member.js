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
      <span class="off" title="Coming soon"><span aria-hidden="true">⚔️</span>Queue<span class="pill">Soon</span></span>
      ${me.hidden?`<span class="off" title="Your profile is hidden (see Settings)"><span aria-hidden="true">👤</span>My Profile<span class="pill">Hidden</span></span>`:item('profile','👤','My Profile',ccPlayerHref(me.user_id))}
      ${item('account','🔗','AoE4 Account','/account')}
      ${item('settings','⚙️','Settings','/settings')}
      <hr>
      <a class="logout" href="/auth/logout?next=/"><span aria-hidden="true">🚪</span>Log out<span></span></a>
    </nav>`;
  return me;
}
function ccMemberVoice(channel){const el=document.getElementById('sideVoice');if(el&&channel){el.textContent='● In voice · '+channel;el.classList.add('live')}}

/* ---- charts (inline SVG, no libraries) ---- */
function ccAxis(W,H,pad,max,steps,unit){let s='';for(let i=0;i<=steps;i++){const v=max*i/steps,y=H-pad[2]-(H-pad[0]-pad[2])*i/steps;
  s+=`<line x1="${pad[3]}" x2="${W-pad[1]}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${CC_COLORS.grid}"/><text x="${pad[3]-8}" y="${(y+4).toFixed(1)}" fill="${CC_COLORS.text}" font-size="10" text-anchor="end">${+v.toFixed(1)}${unit||''}</text>`}return s}
function ccNiceMax(v,min){v=Math.max(v,min||1);const p=Math.pow(10,Math.floor(Math.log10(v))),n=v/p;return (n<=1?1:n<=2?2:n<=3?3:n<=5?5:10)*p}
function ccWeekLabel(iso){return new Date(iso+'T00:00:00Z').toLocaleDateString(undefined,{month:'short',day:'numeric',timeZone:'UTC'})}
function ccChartWinLoss(weeks){
  const W=560,H=210,pad=[14,10,30,34],peak=Math.max(...weeks.map(w=>Math.max(w.wins,w.losses))),max=Math.max(3,Math.ceil(peak/3)*3);
  const cw=(W-pad[1]-pad[3])/weeks.length,bw=cw*0.3,ph=H-pad[0]-pad[2];let s=ccAxis(W,H,pad,max,3);
  weeks.forEach((w,i)=>{const x=pad[3]+cw*i+cw*0.18;
    [[w.wins,CC_COLORS.green,'wins'],[w.losses,CC_COLORS.red,'losses']].forEach(([v,c,l],j)=>{const h=ph*v/max;
      s+=`<rect x="${(x+j*(bw+2)).toFixed(1)}" y="${(H-pad[2]-h).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(h,0).toFixed(1)}" rx="2" fill="${c}" opacity=".9"><title>Week of ${ccWeekLabel(w.week)}: ${v} ${l}</title></rect>`});
    if(i%2===0)s+=`<text x="${(x+bw).toFixed(1)}" y="${H-10}" fill="${CC_COLORS.text}" font-size="10" text-anchor="middle">${ccWeekLabel(w.week)}</text>`});
  return `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="Wins and losses per week">${s}</svg>`}
function ccChartLine(series,opts){ /* series: [{values:[{x:label,y:num}], color, label}] */
  const W=opts.width||460,H=opts.height||210,pad=[16,14,30,44];const all=series.flatMap(s=>s.values.map(v=>v.y));
  if(!all.length)return '';
  let lo=opts.zero?0:Math.min(...all),hi=Math.max(...all);if(opts.zero)hi=ccNiceMax(hi,1);else{const span=Math.max(hi-lo,40);lo=Math.floor((lo-span*.15)/10)*10;hi=Math.ceil((hi+span*.15)/10)*10}
  let s='';for(let i=0;i<=3;i++){const v=lo+(hi-lo)*i/3,y=H-pad[2]-(H-pad[0]-pad[2])*i/3;s+=`<line x1="${pad[3]}" x2="${W-pad[1]}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${CC_COLORS.grid}"/><text x="${pad[3]-8}" y="${(y+4).toFixed(1)}" fill="${CC_COLORS.text}" font-size="10" text-anchor="end">${Math.round(v)}${opts.unit||''}</text>`}
  const n=Math.max(...series.map(x=>x.values.length));
  series.forEach((ser,si)=>{const pts=ser.values.map((v,i)=>[pad[3]+(W-pad[1]-pad[3])*(n>1?i/(n-1):.5),H-pad[2]-(H-pad[0]-pad[2])*(v.y-lo)/(hi-lo||1)]);
    if(opts.area&&si===0){s+=`<defs><linearGradient id="ccg${si}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${ser.color}" stop-opacity=".35"/><stop offset="1" stop-color="${ser.color}" stop-opacity="0"/></linearGradient></defs><path d="M${pts.map(p=>p[0].toFixed(1)+','+p[1].toFixed(1)).join(' L')} L${pts[pts.length-1][0].toFixed(1)},${H-pad[2]} L${pts[0][0].toFixed(1)},${H-pad[2]} Z" fill="url(#ccg${si})"/>`}
    s+=`<polyline points="${pts.map(p=>p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ')}" fill="none" stroke="${ser.color}" stroke-width="2.5" stroke-linejoin="round"/>`;
    if(opts.dots)pts.forEach((p,i)=>{s+=`<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="3.5" fill="#17171b" stroke="${ser.color}" stroke-width="2"><title>${ccEsc(ser.values[i].x)}: ${ser.values[i].y}${opts.unit||''}</title></circle>`});
    if(ser.label){const p=pts[pts.length-1];s+=`<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="4.5" fill="${ser.color}"/><text x="${(p[0]-8).toFixed(1)}" y="${(p[1]-10).toFixed(1)}" fill="${ser.color}" font-size="11" font-weight="700" text-anchor="end">${ccEsc(ser.label)} ${ser.values[ser.values.length-1].y}</text>`}});
  const labels=series[0].values.map(v=>v.x),every=Math.max(1,Math.ceil(labels.length/(opts.ticks||4)));
  labels.forEach((l,i)=>{if(i%every===0||i===labels.length-1&&opts.lastTick){const x=pad[3]+(W-pad[1]-pad[3])*(n>1?i/(n-1):.5);s+=`<text x="${x.toFixed(1)}" y="${H-10}" fill="${CC_COLORS.text}" font-size="10" text-anchor="middle">${ccEsc(l)}</text>`}});
  return `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="${ccEsc(opts.label||'Chart')}">${s}</svg>`}
function ccCivBars(civs){if(!civs.length)return '<div class="m-empty">No civilizations recorded yet. Play a few club matches and your civs show up here.</div>';
  const max=Math.max(...civs.map(c=>c.games));
  return civs.slice(0,8).map(c=>{const wr=Math.round(c.win_rate||0),col=wr>=60?CC_COLORS.green:wr>=45?CC_COLORS.gold:CC_COLORS.red;
    return `<div class="civbar"><span class="civ-name"><span class="civ-flag">${civFlag(c.civ)}</span><span>${ccEsc(c.civ)}</span></span><div class="track"><i style="width:${(100*c.games/max).toFixed(0)}%;background:${col}"></i></div><span class="g">${c.games} game${c.games===1?'':'s'}</span><span class="wr" style="color:${col}">${wr}%</span></div>`}).join('')}
function ccDonut(queues){const cols=[CC_COLORS.gold,CC_COLORS.green,CC_COLORS.blue,CC_COLORS.red,'#b48cf0'];const tot=queues.reduce((a,q)=>a+q.games,0);
  if(!tot)return '<div class="m-empty">No club matches in this period.</div>';
  const r=60,C=2*Math.PI*r;let off=0,s='';
  queues.forEach((q,i)=>{const L=C*q.games/tot;s+=`<circle r="${r}" cx="90" cy="90" fill="none" stroke="${cols[i%cols.length]}" stroke-width="24" stroke-dasharray="${Math.max(L-2,0.5).toFixed(1)} ${(C-L+2).toFixed(1)}" stroke-dashoffset="${(-off).toFixed(1)}" transform="rotate(-90 90 90)"><title>${ccEsc(q.queue)}: ${q.games} games</title></circle>`;off+=L});
  s+=`<text x="90" y="88" text-anchor="middle" fill="#f6f3eb" font-size="28" font-weight="800" font-family="Barlow Condensed">${tot}</text><text x="90" y="106" text-anchor="middle" fill="${CC_COLORS.text}" font-size="10" font-family="Inter">games</text>`;
  return `<div class="donut-wrap"><svg viewBox="0 0 180 180" width="170" height="170" role="img" aria-label="Games per queue">${s}</svg><div>${queues.map((q,i)=>`<div class="leg"><i style="background:${cols[i%cols.length]}"></i><b>${ccEsc(q.queue)}</b><span>${q.games} game${q.games===1?'':'s'} · ${Math.round(100*q.wins/q.games)}% wins</span></div>`).join('')}</div></div>`}
function ccHeatmap(times){if(!times.length)return '<div class="m-empty">No club matches in this period.</div>';
  const days=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'],slots=['12a','3a','6a','9a','12p','3p','6p','9p'],grid=days.map(()=>slots.map(()=>0));
  times.forEach(t=>{const d=new Date(t);grid[(d.getDay()+6)%7][Math.floor(d.getHours()/3)]++});
  const max=Math.max(...grid.flat());let best=[0,0];grid.forEach((row,i)=>row.forEach((v,j)=>{if(v>grid[best[0]][best[1]])best=[i,j]}));
  let s='<div class="heat"><span></span>'+slots.map(x=>`<span class="hl">${x}</span>`).join('');
  grid.forEach((row,i)=>{s+=`<span class="dl">${days[i]}</span>`+row.map((v,j)=>`<i style="background:rgba(240,184,47,${(0.06+0.85*v/max).toFixed(2)})" title="${days[i]} ${slots[j]}: ${v} game${v===1?'':'s'}"></i>`).join('')});
  const full=['Mondays','Tuesdays','Wednesdays','Thursdays','Fridays','Saturdays','Sundays'],endH=(best[1]*3+3)%24,fmtH=h=>new Date(2000,0,1,h).toLocaleTimeString([], {hour:'numeric'});
  const peak=grid[best[0]][best[1]];
  return s+`</div><p class="hint">${peak>=3?`You play most on <b>${full[best[0]]} between ${fmtH(best[1]*3)} and ${fmtH(endH)}</b> (your local time).`:'Play a few more club matches and your usual playing times will show up here.'}</p>`}

function ccNiceStep(range){const raw=Math.max(range,30)/3,p=Math.pow(10,Math.floor(Math.log10(raw)));for(const m of [1,2,2.5,5,10])if(raw<=m*p)return m*p;return 10*p}
function ccRatingChart(lines){ /* lines: [{label,color,points:[{t,rating}]}] (oldest first) */
  lines=lines.filter(l=>l.points.length>1);if(!lines.length)return '';
  const narrow=window.innerWidth<600,W=narrow?520:900,H=narrow?320:250,pad=narrow?[22,18,36,70]:[18,16,30,52],all=lines.flatMap(l=>l.points.map(p=>p.rating));
  const step=ccNiceStep(Math.max(...all)-Math.min(...all));let lo=Math.floor(Math.min(...all)/step)*step,hi=lo+3*step;while(hi<Math.max(...all))hi+=step;
  const steps=Math.round((hi-lo)/step),Y=v=>H-pad[2]-(H-pad[0]-pad[2])*(v-lo)/(hi-lo);
  const timed=lines.every(l=>l.points.every(p=>p.t));
  const t0=timed?Math.min(...lines.map(l=>+new Date(l.points[0].t))):0,t1=timed?Math.max(...lines.map(l=>+new Date(l.points[l.points.length-1].t))):1;
  const X=(l,i)=>pad[3]+(W-pad[1]-pad[3])*(timed?((+new Date(l.points[i].t)-t0)/((t1-t0)||1)):i/(l.points.length-1));
  let s='';for(let i=0;i<=steps;i++){const v=lo+step*i,y=Y(v);s+=`<line x1="${pad[3]}" x2="${W-pad[1]}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${CC_COLORS.grid}"/><text x="${pad[3]-8}" y="${(y+4).toFixed(1)}" fill="${CC_COLORS.text}" font-size="10" text-anchor="end">${ccFmt(v)}</text>`}
  lines.forEach(l=>{const pts=l.points.map((p,i)=>[X(l,i),Y(p.rating)]);const last=pts[pts.length-1];
    s+=`<polyline points="${pts.map(q=>q[0].toFixed(1)+','+q[1].toFixed(1)).join(' ')}" fill="none" stroke="${l.color}" stroke-width="2.5" stroke-linejoin="round"/>`;
    s+=`<circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="4.5" fill="${l.color}"/><text x="${(last[0]-8).toFixed(1)}" y="${(last[1]-10).toFixed(1)}" fill="${l.color}" font-size="11" font-weight="700" text-anchor="end">${ccEsc(l.label)} ${ccFmt(l.points[l.points.length-1].rating)}</text>`});
  if(timed){const n=narrow?3:4;for(let i=0;i<=n;i++){const t=t0+(t1-t0)*i/n,x=pad[3]+(W-pad[1]-pad[3])*i/n;s+=`<text x="${x.toFixed(1)}" y="${H-10}" fill="${CC_COLORS.text}" font-size="10" text-anchor="${i===0?'start':i===n?'end':'middle'}">${new Date(t).toLocaleDateString(undefined,{month:'short',day:'numeric'})}</text>`}}
  else s+=`<text x="${pad[3]}" y="${H-10}" fill="${CC_COLORS.text}" font-size="10">Older games</text><text x="${W-pad[1]}" y="${H-10}" fill="${CC_COLORS.text}" font-size="10" text-anchor="end">Most recent</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" class="chart${narrow?'':' chart-wide'}" role="img" aria-label="Official rating over time">${s}</svg>`;
}
