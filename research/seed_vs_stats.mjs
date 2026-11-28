#!/usr/bin/env node
/* What beats the seed line? See README.md beside this file.

   Reproduces every figure in that write-up from the season files in docs/
   plus one API call per season for regular-season-only team stats.

     export COLLEGE_FOOTBALL_API_KEY='...'
     node research/seed_vs_stats.mjs

   Stats are fetched with seasonType=regular because the figures stored in
   the season files include the tournament itself -- a team's `games` count
   correlates +0.80 with its tournament wins, so using them would be reading
   the answer off the result. */
import {ingest,engine,weighted,bracket} from '../docs/engine.js';
import {readFileSync,existsSync,mkdirSync,writeFileSync} from 'fs';
import {fileURLToPath} from 'url';
import {dirname,join} from 'path';

const HERE=dirname(fileURLToPath(import.meta.url));
const DOCS=join(HERE,'..','docs','ncaam');
const CACHE=join(HERE,'.cache');
const YEARS=[2022,2023,2024,2025,2026];
const POWER=new Set(['ACC','Big Ten','Big 12','SEC','Big East','Pac-12']);

async function regularStats(year){
  if(!existsSync(CACHE)) mkdirSync(CACHE);
  const f=join(CACHE,`reg_${year}.json`);
  if(existsSync(f)) return JSON.parse(readFileSync(f,'utf8'));
  const key=process.env.COLLEGE_FOOTBALL_API_KEY;
  if(!key){ console.error('set COLLEGE_FOOTBALL_API_KEY'); process.exit(1); }
  const r=await fetch(`https://api.collegebasketballdata.com/stats/team/season`
    +`?season=${year}&seasonType=regular`,{headers:{Authorization:`Bearer ${key}`}});
  if(!r.ok){ console.error(`stats ${year}: HTTP ${r.status}`); process.exit(1); }
  const j=await r.json();
  writeFileSync(f,JSON.stringify(j));
  return j;
}

/* Opponent-adjusted scoring margin, ridge-regularised: solve for a strength
   per team so that predicted margin best explains every regular-season game.
   Margins are capped at 25 so a blowout cannot buy more credit than winning. */
function adjustedMargin(season){
  const teams=[...new Set(season.games.flatMap(g=>[g.winner,g.loser]))];
  const idx=new Map(teams.map((t,i)=>[t,i])), n=teams.length, LAM=4;
  const gs=season.games.map(g=>({w:idx.get(g.winner),l:idx.get(g.loser),
    m:Math.min(g.margin,25)}));
  const cnt=new Float64Array(n);
  for(const g of gs){ cnt[g.w]++; cnt[g.l]++; }
  let r=new Float64Array(n);
  for(let it=0;it<300;it++){
    const num=new Float64Array(n);
    for(const g of gs){ num[g.w]+=g.m+r[g.l]; num[g.l]+=-g.m+r[g.w]; }
    const nr=new Float64Array(n);
    for(let i=0;i<n;i++) nr[i]=cnt[i]?num[i]/(cnt[i]+LAM):0;
    let mu=0; for(let i=0;i<n;i++) mu+=nr[i]; mu/=n;
    for(let i=0;i<n;i++) nr[i]-=mu;
    r=nr;
  }
  return {strength:r,idx};
}

const winOf=g=>g.homePoints>g.awayPoints?g.home:g.away;
const SIZES=[4,32,16,8,4,2,1];

async function load(){
  const games=[], feat={};
  for(const y of YEARS){
    const raw=JSON.parse(readFileSync(join(DOCS,`season-${y}.json`),'utf8'));
    const se=ingest(raw), e=engine(se,{});
    const H=Math.max(...e.byWeek.keys());
    const rows=weighted(e,se,H,{mode:'B'}).rows;
    const rank=new Map(rows.map(r=>[r.team,r.rank]));
    const b=bracket(se,'NCAA');
    const seed=new Map();
    for(const g of b.games){
      if(g.homeSeed!=null&&!seed.has(g.home)) seed.set(g.home,g.homeSeed);
      if(g.awaySeed!=null&&!seed.has(g.away)) seed.set(g.away,g.awaySeed);
    }
    const key=new Map(rows.map(r=>[r.team,r.key]));
    const floor=Math.min(...rows.map(r=>r.key))-3;
    const opp=new Map();
    for(const g of se.games) for(const [a,c] of [[g.winner,g.loser],[g.loser,g.winner]]){
      if(!opp.has(a)) opp.set(a,[]);
      opp.get(a).push(key.has(c)?key.get(c):floor);
    }
    const {strength,idx}=adjustedMargin(se);
    const st=new Map((await regularStats(y)).map(r=>[r.team,r]));
    feat[y]={};
    for(const t of b.field){
      const x=st.get(t); if(!x) continue;
      const o=opp.get(t)||[];
      feat[y][t]={
        seed:seed.get(t)??null, rank:rank.get(t)??null, key:key.get(t)??null,
        net:x.teamStats.rating-x.opponentStats.rating,
        adj:idx.has(t)?strength[idx.get(t)]:0,
        sos:o.length?o.reduce((a,c)=>a+c,0)/o.length:floor,
        conference:x.conference,
      };
    }
    const pool=b.games.slice().sort((a,c)=>
      String(a.date).localeCompare(String(c.date))||a.home.localeCompare(c.home));
    let i=0;
    SIZES.forEach(n=>{ for(const g of pool.slice(i,i+n))
      games.push({y,home:g.home,away:g.away,win:winOf(g),
        sH:seed.get(g.home)??null,sA:seed.get(g.away)??null}); i+=n; });
  }
  return {games,feat};
}

function mcnemar(A,B){
  let b=0,c=0;
  for(const [k,va] of A){ const vb=B.get(k); if(va&&!vb)b++; else if(!va&&vb)c++; }
  const n=b+c; if(!n) return {b,c,n,p:1};
  const z=(b-c)/Math.sqrt(n);
  const erf=x=>{const t=1/(1+.3275911*x);
    return 1-((((1.061405429*t-1.453152027)*t+1.421413741)*t-.284496736)*t
      +.254829592)*t*Math.exp(-x*x);};
  return {b,c,n,z,p:2*(1-0.5*(1+erf(Math.abs(z)/Math.SQRT2)))};
}

const {games,feat}=await load();
/* Same-seed games are dropped: the committee expressed no preference there,
   so scoring anything against it would be a comparison with a shrug. */
const EVAL=games.filter(g=>g.sH!=null&&g.sA!=null&&g.sH!==g.sA
  &&feat[g.y][g.home]&&feat[g.y][g.away]);
const fav=g=>g.sH<g.sA?g.home:g.away;
const pickBy=f=>g=>{const a=feat[g.y][g.home][f],b=feat[g.y][g.away][f];
  return (a??-1e9)>(b??-1e9)?g.home:g.away;};
const score=pick=>{let ok=0;const hit=new Map();
  EVAL.forEach((g,i)=>{const good=pick(g)===g.win;if(good)ok++;hit.set(i,good);});
  return {ok,pct:100*ok/EVAL.length,hit};};

const SEED=score(fav);
const RULE=g=>Math.abs(g.sH-g.sA)>1?fav(g):pickBy('net')(g);

console.log(`${EVAL.length} games, 2021-22 to 2025-26 (same-seed games excluded)\n`);
const table=[
  ['Raw net rating',           score(pickBy('net'))],
  ['This site’s ranking', score(g=>feat[g.y][g.home].rank<feat[g.y][g.away].rank?g.home:g.away)],
  ['Schedule-adjusted margin', score(pickBy('adj'))],
  ['The seed line',            SEED],
  ['Seed + net when 1 apart',  score(RULE)],
];
for(const [n,r] of table) console.log(`  ${n.padEnd(28)} ${r.pct.toFixed(1)}%`);
const m=mcnemar(table[4][1].hit,SEED.hit);
console.log(`\n  the rule differs from the seed line on ${m.n} games: `
  +`right on ${m.b}, wrong on ${m.c}, p = ${m.p.toFixed(3)}`);

console.log('\nper season:');
for(const y of YEARS){
  const gs=EVAL.filter(g=>g.y===y);
  const a=gs.filter(g=>RULE(g)===g.win).length, b=gs.filter(g=>fav(g)===g.win).length;
  console.log(`  ${y}   rule ${String(a).padStart(2)}/${gs.length}   `
    +`seed ${String(b).padStart(2)}/${gs.length}   ${a-b>0?'+':''}${a-b}`);
}

/* The mechanism: the committee's ordering is near-worthless at one seed
   apart, and that is exactly where raw stats are safe to use, because the
   schedules have already been matched. */
const close=EVAL.filter(g=>Math.abs(g.sH-g.sA)<=1);
const gap=gs=>gs.reduce((t,g)=>t+Math.abs(feat[g.y][g.home].sos-feat[g.y][g.away].sos),0)/gs.length;
console.log(`\nwhy it works, on the ${close.length} games seeded one apart:`);
console.log(`  higher seed wins      ${(100*close.filter(g=>fav(g)===g.win).length/close.length).toFixed(1)}%  (a coin flip)`);
console.log(`  better net rating wins ${(100*close.filter(g=>pickBy('net')(g)===g.win).length/close.length).toFixed(1)}%`);
console.log(`  mean |SOS difference|  ${gap(close).toFixed(2)} here vs `
  +`${gap(EVAL.filter(g=>Math.abs(g.sH-g.sA)>1)).toFixed(2)} elsewhere`);

let pw=0,pn=0,mw=0,mn=0;
for(const y of YEARS) for(const f of Object.values(feat[y]))
  if(POWER.has(f.conference)){pn++;pw+=f.sos;} else {mn++;mw+=f.sos;}
console.log(`\nwhy raw net rating fails elsewhere:`);
console.log(`  mean strength of schedule: power ${(pw/pn).toFixed(1)}, `
  +`mid-major ${(mw/mn).toFixed(1)}`);
const dis=EVAL.filter(g=>pickBy('net')(g)!==fav(g));
console.log(`  when it disagrees with the seed line (${dis.length} games) it is right `
  +`${(100*dis.filter(g=>pickBy('net')(g)===g.win).length/dis.length).toFixed(1)}% of the time`);

console.log('\nplacebo: coin-flip the close games instead');
let beat=0;
for(let i=0;i<2000;i++){
  let k=0;
  for(const g of EVAL) if((Math.abs(g.sH-g.sA)>1?fav(g)
    :(Math.random()<.5?g.home:g.away))===g.win) k++;
  if(k>=table[4][1].ok) beat++;
}
console.log(`  matched or beat the rule in ${beat} of 2000 runs (${(beat/20).toFixed(1)}%)`);

/* ---- what upsets have in common ---------------------------------------- */
const dog=g=>g.sH<g.sA?g.away:g.home;
const upsets=EVAL.filter(g=>fav(g)!==g.win);
const A=-0.033,B=0.171,pFav=d=>1/(1+Math.exp(-(A+B*d)));
const cor=(xs,ys)=>{const n=xs.length,mx=xs.reduce((a,b)=>a+b,0)/n,
  my=ys.reduce((a,b)=>a+b,0)/n;let sxy=0,sxx=0,syy=0;
  for(let i=0;i<n;i++){sxy+=(xs[i]-mx)*(ys[i]-my);sxx+=(xs[i]-mx)**2;syy+=(ys[i]-my)**2;}
  const r=sxy/Math.sqrt(sxx*syy);return {r,t:r*Math.sqrt((n-2)/(1-r*r)),n};};
const surp=upsets.map(g=>pFav(Math.abs(g.sH-g.sA)));
const agap=upsets.map(g=>feat[g.y][fav(g)].adj-feat[g.y][dog(g)].adj);
const c=cor(surp,agap);
console.log(`\nupsets: ${upsets.length}`);
console.log(`  corr(how unlikely by seed, favourite's edge in adjusted margin)`);
console.log(`  = ${c.r.toFixed(3)}  t = ${c.t.toFixed(2)}`);
console.log(`  => the more surprising the upset, the MORE the favourite deserved`);
console.log(`     to win. Big upsets are random; small ones are mis-seeds.`);
const sgap=EVAL.map(g=>Math.abs(g.sH-g.sA));
const ssos=EVAL.map(g=>feat[g.y][fav(g)].sos-feat[g.y][dog(g)].sos);
console.log(`\n  corr(seed gap, schedule gap) = ${cor(sgap,ssos).r.toFixed(3)}`);
console.log(`  => a schedule-gap "finding" about upsets is mostly the seed gap.`);
