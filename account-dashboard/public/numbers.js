'use strict';
(function(root){
 const number=v=>v==null||v===''||!Number.isFinite(Number(v))?null:Number(v);
 function format(v,digits=2){
  const n=number(v);if(n===null)return '—';
  if(n!==0&&Math.abs(n)<10**(-digits))return n.toLocaleString('zh-CN',{maximumSignificantDigits:8});
  return n.toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits});
 }
 function precise(v){const n=number(v);return n===null?'—':n.toLocaleString('zh-CN',{maximumSignificantDigits:12});}
 function contributions(rows,limit=8){
  const sorted=[...rows].sort((a,b)=>Math.abs(b['期间净盈亏'])-Math.abs(a['期间净盈亏']));
  const selected=sorted.slice(0,limit).map(r=>({...r}));
  if(sorted.length>limit)selected.push({'合约':`其他 ${sorted.length-limit} 个合约`,'期间净盈亏':sorted.slice(limit).reduce((n,r)=>n+Number(r['期间净盈亏']),0)});
  return selected;
 }
 function rollingDigits(previous,next){
  const old=String(previous).padStart(String(next).length,' '),offset=old.length-String(next).length;
  return [...String(next)].map((glyph,i)=>({previous:old[offset+i],next:glyph,changed:/[0-9]/.test(glyph)&&old[offset+i]!==glyph}));
 }
 function roundStats(rows){
  const eligible=(rows||[]).filter(r=>!r.跨期&&number(r.净盈亏)!==null).slice().sort((a,b)=>String(a.平仓时间).localeCompare(String(b.平仓时间)));
  const wins=eligible.filter(r=>r.净盈亏>0),losses=eligible.filter(r=>r.净盈亏<0),hours=eligible.map(r=>number(r.持仓小时)).filter(v=>v!==null);
  let streak=0,maxLossStreak=0;for(const r of eligible){streak=r.净盈亏<0?streak+1:0;maxLossStreak=Math.max(maxLossStreak,streak);}
  const mean=(rs,key)=>rs.length?rs.reduce((n,r)=>n+Number(r[key]),0)/rs.length:null;
  return {count:eligible.length,winRate:eligible.length?wins.length/eligible.length:null,averageWin:mean(wins,'净盈亏'),averageLoss:mean(losses,'净盈亏'),averageHours:hours.length?hours.reduce((n,v)=>n+v,0)/hours.length:null,maxLossStreak:eligible.length?maxLossStreak:null};
 }
 const api={format,precise,contributions,rollingDigits,roundStats};
 if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.DashboardNumbers=api;
})(globalThis);
