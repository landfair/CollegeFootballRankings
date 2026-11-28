#!/usr/bin/env node
/* How the bracket strategies compare when each is used to fill a bracket in
   blind. Needs no API key: everything here comes from the season files.

     node research/bracket_strategies.mjs

   See README.md beside this file. The headline is that the rule which wins
   game by game LOSES as a bracket, because its edge is in the cheap early
   rounds and the underdogs it advances die before the expensive ones. */
import {ingest,engine,weighted,bracket,adjustedMargin} from '../docs/engine.js';
import {readFileSync} from 'fs';
import {fileURLToPath} from 'url';
import {dirname,join} from 'path';
const F=join(dirname(fileURLToPath(import.meta.url)),'..','docs','ncaam')+'/';
const winOf=g=>g.homePoints>g.awayPoints?g.home:g.away;
const SZ=[4,32,16,8,4,2,1],PTS=[0,1,2,4,8,16,32],YEARS=[2022,2023,2024,2025,2026];
const nameTB=(x,y)=>x<y?x:y;
const S={};
for(const y of YEARS){
  const raw=JSON.parse(readFileSync(F+`season-${y}.json`,'utf8'));
  const se=ingest(raw),e=engine(se,{});
  const H=Math.max(...e.byWeek.keys());
  const b=bracket(se,'NCAA');
  const adj=adjustedMargin(se);
  const rk=new Map(weighted(e,se,H,{mode:'B'}).rows.map(r=>[r.team,r.rank]));
  const sm=new Map();
  for(const g of b.games){if(g.homeSeed!=null&&!sm.has(g.home))sm.set(g.home,g.homeSeed);
                          if(g.awaySeed!=null&&!sm.has(g.away))sm.set(g.away,g.awaySeed);}
  const pool=b.games.slice().sort((a,c)=>String(a.date).localeCompare(String(c.date))||a.home.localeCompare(c.home));
  let i=0;const R=[];for(const n of SZ){R.push(pool.slice(i,i+n));i+=n;}
  const feed=[];for(let r=1;r<R.length;r++)feed[r]=R[r].map(g=>[g.home,g.away].map(t=>R[r-1].findIndex(x=>winOf(x)===t)));
  S[y]={sm,adj,rk,R,feed,act:R.map(r=>r.map(winOf))};
}
const fill=(s,bt)=>{const {R,feed}=s;
  const p=[R[0].map(g=>bt(g.home,g.away,0))];
  p[1]=R[1].map((g,gi)=>{const sub=t=>{const k=feed[1][gi][[g.home,g.away].indexOf(t)];return k>=0?p[0][k]:t;};return bt(sub(g.home),sub(g.away),1);});
  for(let r=2;r<R.length;r++)p[r]=R[r].map((g,gi)=>{const[a,c]=feed[r][gi].map(k=>k>=0?p[r-1][k]:null);return(a&&c)?bt(a,c,r):null;});
  return p;};
const sc=(s,p)=>{let pts=0;s.R.forEach((r,ri)=>r.forEach((g,gi)=>{if(p[ri][gi]===s.act[ri][gi])pts+=PTS[ri];}));return pts;};
function dist(mk,trials=400){
  const t=[];
  for(let i=0;i<trials;i++){let p=0;
    for(const y of YEARS){const coin=new Map();
      const tb=(x,y2)=>{const k=x<y2?x+'\0'+y2:y2+'\0'+x;if(!coin.has(k))coin.set(k,Math.random()<.5);return coin.get(k)?x:y2;};
      p+=sc(S[y],fill(S[y],mk(S[y],tb)));}
    t.push(p);}
  t.sort((a,b)=>a-b);
  return {mean:t.reduce((a,b)=>a+b,0)/t.length,lo:t[20],hi:t[379]};
}
const seedOf=(s,x)=>s.sm.get(x)??99;
const cands={
 'seed line':(s,tb)=>(x,y)=>{const a=seedOf(s,x),c=seedOf(s,y);return a!==c?(a<c?x:y):tb(x,y);},
 'rule everywhere (N=1)':(s,tb)=>(x,y)=>{const a=seedOf(s,x),c=seedOf(s,y);
   if(Math.abs(a-c)>1)return a!==c?(a<c?x:y):tb(x,y);
   const ax=s.adj.get(x)??-1e9,ay=s.adj.get(y)??-1e9;return ax!==ay?(ax>ay?x:y):tb(x,y);},
 'rule in R64+R32 only':(s,tb)=>(x,y,r)=>{const a=seedOf(s,x),c=seedOf(s,y);
   if(r>2||Math.abs(a-c)>1)return a!==c?(a<c?x:y):tb(x,y);
   const ax=s.adj.get(x)??-1e9,ay=s.adj.get(y)??-1e9;return ax!==ay?(ax>ay?x:y):tb(x,y);},
 'rule early, adj late':(s,tb)=>(x,y,r)=>{const a=seedOf(s,x),c=seedOf(s,y);
   if(r>=4){const ax=s.adj.get(x)??-1e9,ay=s.adj.get(y)??-1e9;return ax!==ay?(ax>ay?x:y):tb(x,y);}
   if(Math.abs(a-c)>1)return a!==c?(a<c?x:y):tb(x,y);
   const ax=s.adj.get(x)??-1e9,ay=s.adj.get(y)??-1e9;return ax!==ay?(ax>ay?x:y):tb(x,y);},
 'adj breaks all seed ties':(s,tb)=>(x,y)=>{const a=seedOf(s,x),c=seedOf(s,y);
   if(a!==c&&Math.abs(a-c)>1)return a<c?x:y;
   const ax=s.adj.get(x)??-1e9,ay=s.adj.get(y)??-1e9;return ax!==ay?(ax>ay?x:y):tb(x,y);},
};
console.log('bracket points over 5 seasons, same-seed ties flipped fairly (400 runs)\n');
const out=[];
for(const [n,f] of Object.entries(cands)) out.push({n,...dist(f)});
out.sort((a,b)=>b.mean-a.mean);
for(const r of out) console.log(`  ${r.n.padEnd(26)} ${r.mean.toFixed(0).padStart(4)}   (90% ${r.lo}-${r.hi})`);

/* The comparison above flatters the coin. A strategy that resolves same-seed
   games deterministically scores one number; the seed line gets 400 tries and
   is reported at its MEAN, which is fine -- but the right question for
   someone filling in one bracket is: how often does my single deterministic
   bracket beat the coin-flipper's single bracket? */
console.log('\nhead to head: one bracket each, 2000 pairings');
const det={
 'rule everywhere (N=1)':cands['rule everywhere (N=1)'],
 'rule in R64+R32 only':cands['rule in R64+R32 only'],
};
for(const [n,f] of Object.entries(det)){
  // the strategy's own score, with a deterministic tiebreak
  let mine=0;
  for(const y of YEARS) mine+=sc(S[y],fill(S[y],f(S[y],nameTB)));
  let win=0,tie=0;
  for(let i=0;i<2000;i++){
    let theirs=0;
    for(const y of YEARS){const coin=new Map();
      const tb=(x,y2)=>{const k=x<y2?x+'\0'+y2:y2+'\0'+x;if(!coin.has(k))coin.set(k,Math.random()<.5);return coin.get(k)?x:y2;};
      theirs+=sc(S[y],fill(S[y],cands['seed line'](S[y],tb)));}
    if(mine>theirs)win++; else if(mine===theirs)tie++;
  }
  console.log(`  ${n.padEnd(24)} scores ${mine}; beats a random seed bracket ${(100*win/2000).toFixed(0)}% of the time (ties ${(100*tie/2000).toFixed(1)}%)`);
}

/* Does the early-rounds-only variant really beat the seed line, or do the
   two distributions just overlap? Pair them on the SAME coin flips, so the
   comparison isolates the strategy rather than the luck. */
console.log('\npaired comparison, same coin flips both sides (2000 runs):');
let w=0,l=0,t=0,diff=0;
for(let i=0;i<2000;i++){
  let a=0,b=0;
  for(const y of YEARS){
    const coin=new Map();
    const tb=(x,y2)=>{const k=x<y2?x+'\0'+y2:y2+'\0'+x;
      if(!coin.has(k))coin.set(k,Math.random()<.5);return coin.get(k)?x:y2;};
    a+=sc(S[y],fill(S[y],cands['rule in R64+R32 only'](S[y],tb)));
    b+=sc(S[y],fill(S[y],cands['seed line'](S[y],tb)));
  }
  diff+=a-b;
  if(a>b)w++; else if(a<b)l++; else t++;
}
console.log(`  early-rounds rule beat the seed line in ${w}, lost ${l}, tied ${t}`);
console.log(`  mean margin ${(diff/2000).toFixed(1)} points`);
