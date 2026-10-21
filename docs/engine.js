/* Recursive Rankings — shared engine.
   Football and basketball run exactly this code. The only thing that differs is
   the bucket a game falls in: football uses a Tuesday-Monday week, basketball a
   single day, because 45% of basketball team-weeks hold more than one game and
   the model keeps one game per team per bucket. By the time a season reaches
   here that choice is already made — a game's `week` is just a bucket index. */
export const WIN=1,LOSS=0,NEUTRAL=.5,LEVELS=2;
export const esc=s=>String(s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

export function ingest(raw){
  const teamInfo=new Map();
  for(const t of (raw.teams||[])){
    const n=typeof t==='string'?t:t.name;
    if(n) teamInfo.set(n,{name:n,conference:t.conference||'',division:t.division||'FBS'});
  }
  const games=[],seen=new Set();
  for(const g of raw.games){
    const w=Number(g.week), hp=Number(g.homePoints), ap=Number(g.awayPoints);
    if(!Number.isFinite(w)||w<1||!g.home||!g.away) continue;
    if(!Number.isFinite(hp)||!Number.isFinite(ap)||hp===ap) continue;
    const win=hp>ap?g.home:g.away, lose=hp>ap?g.away:g.home;
    if(seen.has(w+'|'+win)||seen.has(w+'|'+lose)) continue;
    seen.add(w+'|'+win); seen.add(w+'|'+lose);
    for(const n of [win,lose]) if(!teamInfo.has(n)) teamInfo.set(n,{name:n,conference:'',division:'FBS'});
    games.push({week:w,winner:win,loser:lose,margin:Math.abs(hp-ap)});
  }
  const polls=[];
  for(const p of (raw.polls||[])){
    if(!p||!Array.isArray(p.ranks)) continue;
    const ranks=p.ranks.filter(r=>r&&r.team&&Number.isFinite(Number(r.rank)))
      .map(r=>({team:String(r.team),rank:Number(r.rank)})).sort((a,b)=>a.rank-b.rank);
    if(ranks.length) polls.push({poll:String(p.poll||'Poll'),week:Number(p.week)||0,
      throughWeek:Number(p.throughWeek)||0,ranks});
  }
  return {teamInfo,games,polls,maxWeek:Math.max(...games.map(g=>g.week)),
          topDivision:raw.topDivision||'FBS',sport:raw.sport||'football',
          dates:Array.isArray(raw.dates)?raw.dates:null};
}

/* Indices are built once per season. Slates are computed per (week, horizon),
   so the same machinery gives both the current table and a game's whole history:
   the worth of a week-w result seen from week w, then w+1, then w+2, and so on. */
export function engine(season){
  const byWeek=new Map(), gamesOf=new Map();
  season.games.forEach((g,i)=>{
    if(!byWeek.has(g.week)) byWeek.set(g.week,new Map());
    byWeek.get(g.week).set(g.winner,{opp:g.loser,res:WIN,id:i});
    byWeek.get(g.week).set(g.loser,{opp:g.winner,res:LOSS,id:i});
    for(const [t,o,res] of [[g.winner,g.loser,WIN],[g.loser,g.winner,LOSS]]){
      if(!gamesOf.has(t)) gamesOf.set(t,[]);
      gamesOf.get(t).push({week:g.week,opp:o,res,id:i});
    }
  });
  const memo=new Map();
  const recordOf=(x,excl,H)=>{
    const gs=gamesOf.get(x); if(!gs) return NEUTRAL;
    let n=0,won=0;
    for(const g of gs){ if(g.week>H||g.id===excl) continue; n++; if(g.res===WIN) won++; }
    return n?won/n:NEUTRAL;
  };
  const levelVal=(x,excl,k,H)=>{
    const key=x+'|'+excl+'|'+k+'|'+H, hit=memo.get(key);
    if(hit!==undefined) return hit;
    let out;
    if(k<=1) out=recordOf(x,excl,H);
    else{ let s=0,n=0;
      for(const g of (gamesOf.get(x)||[])){
        if(g.week>H||g.id===excl) continue;
        s+=levelVal(g.opp,g.id,k-1,H); n++; }
      out=n?s/n:NEUTRAL; }
    memo.set(key,out); return out;
  };
  const bvalue=(t,w,H)=>{
    const e=byWeek.get(w)&&byWeek.get(w).get(t); if(!e) return null;
    const out=[e.res];
    for(let k=1;k<=LEVELS;k++) out.push(levelVal(e.opp,e.id,k,H));
    return out;
  };
  const cmp=(a,b)=>{
    for(let i=0;i<Math.max(a.length,b.length);i++){
      const x=a[i]===undefined?NEUTRAL:a[i], y=b[i]===undefined?NEUTRAL:b[i];
      if(x!==y) return y-x;
    }
    return 0;
  };
  const pctOf=(n,rank,tied)=> n<2?50:(n-(rank-1)-tied+(tied-1)/2)/(n-1)*100;
  const slateCache=new Map();
  const slate=(w,H)=>{
    const key=w+'|'+H, hit=slateCache.get(key); if(hit) return hit;
    const wk=byWeek.get(w)||new Map();
    const rows=[...wk.keys()].map(t=>({team:t,val:bvalue(t,w,H)}));
    rows.sort((a,b)=>cmp(a.val,b.val)||a.team.localeCompare(b.team));
    const buckets=[];
    for(const r of rows){
      const last=buckets[buckets.length-1];
      if(last&&cmp(last.val,r.val)===0) last.teams.push(r.team);
      else buckets.push({val:r.val,teams:[r.team]});
    }
    let rank=1;
    for(const b of buckets){ b.rank=rank; rank+=b.teams.length; }
    const pos=new Map();
    for(const b of buckets)
      for(const t of b.teams) pos.set(t,{score:pctOf(rows.length,b.rank,b.teams.length),
        rank:b.rank,n:rows.length,val:b.val});
    const out={pos,n:rows.length};
    slateCache.set(key,out); return out;
  };
  return {byWeek,gamesOf,slate,games:season.games};
}

export function rank(eng,season,horizon){
  const top=season.topDivision||'FBS';
  const rows=[];
  for(const [t,info] of season.teamInfo){
    if(info.division!==top) continue;
    const arr=[]; let wins=0,losses=0,pd=0;
    for(let w=1;w<=horizon;w++){
      const wk=eng.byWeek.get(w); if(!wk) continue;
      const e=wk.get(t); if(!e) continue;
      arr.push(eng.slate(w,horizon).pos.get(t).score);
      e.res===WIN?wins++:losses++;
      pd+=(e.res===WIN?1:-1)*eng.games[e.id].margin;
    }
    if(!arr.length) continue;
    rows.push({team:t,conference:info.conference,wins,losses,pd,games:arr.length,
      key:arr.reduce((s,x)=>s+x,0)/arr.length});
  }
  rows.sort((a,b)=>b.key-a.key||b.pd-a.pd||a.team.localeCompare(b.team));
  /* Teams level on rating share a rank, so a tie at a tier boundary lets both
     through the gate rather than only the one sorted first. */
  let rk=1;
  rows.forEach((r,i)=>{
    if(i>0 && Math.abs(r.key-rows[i-1].key)>1e-9) rk=i+1;
    r.rank=rk;
  });
  return rows;
}

/* To be listed you must have beaten somebody worth beating. Self-consistent
   also requires that opponent to qualify, and iterates until the set settles. */
/* ---------- opponent weighting ----------
   The percentile says where a result finished among everyone who played that
   bucket. It already leans on opponent quality through L1 and L2, but weakly:
   beating an unbeaten team counts enormously whoever that team turned out to
   be. This scales each game by how good the opponent proved to be, which means
   the ranking feeds back into its own inputs, so it is iterated to a fixed
   point rather than computed in one pass.

     mode 'A'  adjusted = score x f
               Strongest. A win over a poor team can fall below a loss to a
               good one, so the rule that every win outranks every loss no
               longer holds.
     mode 'B'  adjusted = 50 + (score - 50) x f
               Scales the distance from the halfway line instead, so wins stay
               above 50 and losses below it. A win over a poor team is pulled
               back toward 50; a loss to a poor team pushed down toward 0.

   f = floor + (1 - floor) * quality^gamma, quality being the opponent's own
   rating scaled to 0..1. The floor is not optional: at zero a team can earn
   nothing, which zeroes its opponents in turn and the iteration diverges.
   Quality is fed back as the rating, not the rank — rank is a step function and
   never settles — and damped, without which it reaches a two-cycle and swaps
   the same handful of teams forever. */
export function weighted(eng,season,horizon,opts={}){
  const {mode='B',floor=.2,gamma=1,damp=.7,passes=300,tol=1e-4}=opts;
  const meta=new Map(rank(eng,season,horizon).map(r=>[r.team,r]));
  const ALL=[...eng.gamesOf.keys()];
  const raw=new Map();
  for(let w=1;w<=horizon;w++){
    if(!eng.byWeek.get(w)) continue;
    for(const [t,v] of eng.slate(w,horizon).pos) raw.set(t+'|'+w,v.score);
  }
  /* Quality covers every team, not just the top division: an opponent outside
     the ranking still has to be worth something definite, or beating one looks
     the same as beating nobody and the whole field is flattered. */
  let q=new Map(ALL.map(t=>[t,.5]));
  const fOf=qq=>t=>{const v=qq.get(t); return floor+(1-floor)*Math.pow(v==null?0:v,gamma);};
  let agg=[],it=0,settled=false;
  for(it=1;it<=passes;it++){
    const f=fOf(q);
    agg=[];
    for(const t of ALL){
      const arr=[];
      for(let w=1;w<=horizon;w++){
        const wk=eng.byWeek.get(w); if(!wk||!wk.has(t)) continue;
        const sc=raw.get(t+'|'+w),k=f(wk.get(t).opp);
        arr.push(mode==='A'?sc*k:50+(sc-50)*k);
      }
      if(arr.length) agg.push({team:t,key:arr.reduce((a,b)=>a+b,0)/arr.length});
    }
    agg.sort((a,b)=>b.key-a.key||a.team.localeCompare(b.team));
    const lo=agg[agg.length-1].key,hi=agg[0].key,span=(hi-lo)||1;
    let delta=0;const nq=new Map();
    for(const r of agg){
      const v=(r.key-lo)/span,old=q.get(r.team)??v;
      delta=Math.max(delta,Math.abs(v-old));
      nq.set(r.team,damp*v+(1-damp)*old);
    }
    q=nq;
    if(delta<tol){settled=true;break;}
  }
  const f=fOf(q);
  const rows=agg.filter(r=>meta.has(r.team)).map(r=>({...meta.get(r.team),key:r.key}));
  rows.sort((a,b)=>b.key-a.key||b.pd-a.pd||a.team.localeCompare(b.team));
  let rk=1;
  rows.forEach((r,i)=>{ if(i>0&&Math.abs(r.key-rows[i-1].key)>1e-9) rk=i+1; r.rank=rk; });
  return {rows,passes:it,settled,
          factor:f,
          adjust:(sc,opp)=>mode==='A'?sc*f(opp):50+(sc-50)*f(opp)};
}

export function applyGate(eng,rows,mode,N,horizon){
  const rankOf=new Map(rows.map(r=>[r.team,r.rank]));
  /* The engine holds the whole season, so wins after the chosen week have to be
     filtered out here or the gate would judge a team on games not yet played. */
  const winsOf=t=>(eng.gamesOf.get(t)||[])
    .filter(g=>g.res===WIN&&g.week<=horizon).map(g=>g.opp);
  /* bestWin counts only ranked opponents, because the gate is defined against
     the ranking. The averages instead score an unranked opponent as one worse
     than last: leaving them out would flatter a team that padded its schedule,
     which is the opposite of what a strength-of-schedule figure is for. */
  const UNRANKED=rows.length+1;
  const all=rows.map(r=>{
    let best=Infinity, ws=0, wn=0, ls=0, ln=0;
    for(const g of (eng.gamesOf.get(r.team)||[])){
      if(g.week>horizon) continue;
      const k=rankOf.get(g.opp);
      if(g.res===WIN){
        if(k!=null&&k<best) best=k;
        ws+=(k??UNRANKED); wn++;
      } else { ls+=(k??UNRANKED); ln++; }
    }
    return {...r,bestWin:best,avgWin:wn?ws/wn:null,avgLoss:ln?ls/ln:null};
  });
  if(mode==='off') return {kept:all,all,passes:0};
  if(mode==='one') return {kept:all.filter(r=>r.bestWin<=N),all,passes:1};
  let live=new Set(all.map(r=>r.team)),passes=0;
  for(;;){
    passes++;
    const next=new Set([...live].filter(t=>
      winsOf(t).some(o=>live.has(o)&&(rankOf.get(o)??Infinity)<=N)));
    if(next.size===live.size){ live=next; break; }
    live=next;
    if(passes>200||!live.size) break;
  }
  return {kept:all.filter(r=>live.has(r.team)),all,passes};
}

/* Spearman on the two rank orders, over the teams both lists place. Measured
   against the full ranking, not the gated view, so the number reports how much
   the two orderings agree rather than moving about as the gate opens and shuts. */
export function spearman(x,y){
  const rankOf=a=>{
    const idx=a.map((v,i)=>[v,i]).sort((p,q)=>p[0]-q[0]), r=new Array(a.length);
    idx.forEach((p,i)=>r[p[1]]=i+1);
    return r;
  };
  const rx=rankOf(x), ry=rankOf(y), n=x.length, m=(n+1)/2;
  let num=0,dx=0,dy=0;
  for(let i=0;i<n;i++){ const a=rx[i]-m, b=ry[i]-m; num+=a*b; dx+=a*a; dy+=b*b; }
  return dx&&dy?num/Math.sqrt(dx*dy):0;
}

export function agreement(a,b){
  const common=a.filter(r=>b.has(r.team));
  if(common.length<3) return null;
  return {n:common.length,
    rho:spearman(common.map(r=>r.rank),common.map(r=>b.get(r.team)))};
}

/* The latest snapshot of this poll that reflects no more football than the
   chosen week. Ties on throughWeek break to the later ballot, so a feed that
   stamps two snapshots alike still yields the newer one rather than whichever
   happens to come first in the file. */
export const pollAt=(season,name,h)=>{
  let best=null;
  for(const p of season.polls){
    if(p.poll!==name||p.throughWeek<1||p.throughWeek>h) continue;
    if(!best||p.throughWeek>best.throughWeek
       ||(p.throughWeek===best.throughWeek&&p.week>best.week)) best=p;
  }
  return best;
};

/* A game's worth, seen from every week after it was played. */
export function revision(eng,team,w,horizon){
  const out=[];
  for(let H=w;H<=horizon;H++){
    const p=eng.slate(w,H).pos.get(team);
    if(p) out.push({through:H,score:p.score});
  }
  return out;
}

/* `unit` and `label` let a sport name its own bucket: football grades a game
   again each later week, basketball each later day. */
export function sparkHTML(pts,{unit='week',label=n=>'wk '+n}={}){
  if(pts.length<2) return '<span class="val" title="This is the most recent game '+
    'played, so nothing has happened since to change what it is worth. From the '+
    'next '+unit+' it starts being re-graded.">graded once, nothing since</span>';
  const W=150,H=32,P=4;
  const vals=pts.map(p=>p.score);
  const lo=Math.min(...vals), hi=Math.max(...vals), mid=(lo+hi)/2;
  const span=Math.max(8,(hi-lo)*1.4), y0=mid-span/2, y1=mid+span/2;
  const X=i=>P+i*(W-2*P)/(pts.length-1);
  const Y=v=>H-P-((v-y0)/(y1-y0))*(H-2*P);
  const net=vals[vals.length-1]-vals[0];
  const col=net>0.05?'var(--win)':net<-0.05?'var(--loss)':'var(--muted)';
  const d=pts.map((p,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(p.score).toFixed(1)).join(' ');
  const dots=pts.map((p,i)=>'<circle cx="'+X(i).toFixed(1)+'" cy="'+Y(p.score).toFixed(1)+
    '" r="'+(i===pts.length-1?2.6:1.5)+'" fill="'+(i===pts.length-1?col:'var(--muted)')+'"/>').join('');
  const first=vals[0], last=vals[vals.length-1];
  const moved = Math.abs(net)<0.05 ? 'unchanged since it was played'
    : (net>0?'up ':'down ')+Math.abs(net).toFixed(1)+' since it was played';
  const tip=
    'HOW TO READ THIS\n'+
    'The line is what this one game has been worth, re-graded after every '+unit+'\n'+
    'since it was played. A game scores 0-100 by where the team finished among\n'+
    'everyone who played that '+unit+', so 100 is the best result of the '+unit+'.\n\n'+
    'It moves because the grade depends on what the opponent did next. Beat a\n'+
    'team that keeps winning and the line rises; beat one that collapses and it\n'+
    'falls. The last dot is where it stands now.\n\n'+
    pts.map(p=>'  through '+String(label(p.through)).padStart(9)+'   '+p.score.toFixed(1)).join('\n')+
    '\n\nFirst graded '+first.toFixed(1)+', now '+last.toFixed(1)+' \u2014 '+moved+'.';
  return '<span class="spark" title="'+esc(tip)+'"><span class="lbl">week by week</span>'+
    '<svg width="'+W+'" height="'+H+'" viewBox="0 0 '+W+' '+H+'" aria-hidden="true">'+
    '<path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="1.6" '+
    'stroke-linejoin="round" stroke-linecap="round"/>'+dots+'</svg>'+
    '<span class="val">'+vals[0].toFixed(1)+' &rarr; '+vals[vals.length-1].toFixed(1)+
    ' <b style="color:'+col+'">'+(net>0?'+':'')+net.toFixed(1)+'</b></span></span>';
}
