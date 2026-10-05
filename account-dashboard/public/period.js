/* 全局分析区间：账单按北京时间自然日过滤，风险从权益与划转重新计算。 */
(function(root){
 'use strict';
 const day=s=>String(s||'').slice(0,10),DAY=86400000;
 const valid=s=>{if(!/^\d{4}-\d{2}-\d{2}$/.test(s))return false;const d=new Date(s+'T00:00:00Z');return Number.isFinite(d.getTime())&&d.toISOString().slice(0,10)===s;};
 const shift=(s,n)=>new Date(Date.parse(s+'T00:00:00Z')+n*DAY).toISOString().slice(0,10);
 function bounds(a){const dates=[...(a.daily||[]).map(r=>r.日期),...(a.ledger||[]).map(r=>day(r.时间)),...(a.fills||[]).map(r=>day(r.时间)),...Object.values(a.periodCoverage||{}).flat().map(r=>day(r.start))].filter(valid).sort();return {start:dates[0],end:(a.daily||[]).at(-1)?.日期||day(a.range?.end)||dates.at(-1)};}
 function preset(a,months,endOverride){const b=bounds(a);if(endOverride)b.end=endOverride;if(!b.end||!b.start)return null;if(months==='all')return b;const dt=new Date(b.end+'T00:00:00Z'),d=dt.getUTCDate();dt.setUTCDate(1);dt.setUTCMonth(dt.getUTCMonth()-Number(months));dt.setUTCDate(Math.min(d,new Date(Date.UTC(dt.getUTCFullYear(),dt.getUTCMonth()+1,0)).getUTCDate()));return {start:shift(dt.toISOString().slice(0,10),1),end:b.end};}
 function today(now=Date.now()){return new Date(Number(now)+8*3600000).toISOString().slice(0,10);}
 function readPreference(raw){try{const p=JSON.parse(raw);if(p?.version!==1||!['all','1','3','6','12','custom'].includes(p.preset)||!['fixed','today'].includes(p.endMode))return null;if(p.preset==='custom'&&(!valid(p.start)||(p.endMode==='fixed'&&(!valid(p.end)||p.start>p.end))))return null;return {version:1,preset:p.preset,endMode:p.endMode,...(p.preset==='custom'?{start:p.start,...(p.endMode==='fixed'?{end:p.end}:{})}:{})};}catch{return null;}}
 function resolvePreference(p,a,now=Date.now()){if(!p)return null;const end=p.endMode==='today'?today(now):p.end;if(p.preset==='custom')return {start:p.start,end};return preset(a,p.preset,p.endMode==='today'?end:undefined);}
 function covered(windows,start,end){let cursor=start+' 00:00:00',limit=shift(end,1)+' 00:00:00';for(const w of [...(windows||[])].sort((a,b)=>a.start.localeCompare(b.start))){if(w.start>cursor)return false;if(w.end>cursor)cursor=w.end;if(cursor>=limit)return true;}return cursor>=limit;}
 function equityDrawdown(rows,initialPeak=null){let peak=Number.isFinite(initialPeak)&&initialPeak>0?initialPeak:null;return (rows||[]).map(r=>{const v=r.重建权益;let drawdown=null;if(v==null||!Number.isFinite(v))peak=null;else{if(v>0)peak=peak==null?v:Math.max(peak,v);if(peak>0)drawdown=v/peak-1;}return {...r,权益回撤:drawdown};});}
 function calculate(a,scope){if(!scope)return a;const {start,end}=scope;if(!valid(start)||!valid(end)||start>end)throw Error('请选择有效日期，开始日期不能晚于结束日期。');
  const inside=s=>day(s)>=start&&day(s)<=end,ledger=(a.ledger||[]).filter(r=>inside(r.时间)),fills=(a.fills||[]).filter(r=>inside(r.时间));
  const summary={},groups=new Map(),months=new Map(),warnings=[];
  const cols=['平仓毛盈亏','手续费','资金费','其他调整'];
  function entry(map,key){if(!map.has(key))map.set(key,Object.fromEntries([...cols.map(c=>[c,0]),['成交笔数',0]]));return map.get(key);}
  for(const r of ledger)for(const g of [entry(groups,r.合约),entry(months,day(r.时间).slice(0,7))])for(const c of cols)g[c]+=Number(r[c]||0);
  const swap=fills.filter(r=>r.产品==='SWAP'&&r.合约.endsWith('-USDT-SWAP'));
  for(const r of swap)for(const g of [entry(groups,r.合约),entry(months,day(r.时间).slice(0,7))])g.成交笔数++;
  const rows=(map,key,net)=>[...map].map(([name,g])=>({[key]:name,...g,[net]:cols.reduce((n,c)=>n+g[c],0)}));
  const symbols=rows(groups,'合约','期间净盈亏').sort((a,b)=>b.期间净盈亏-a.期间净盈亏),monthly=rows(months,'月份','净盈亏').sort((a,b)=>a.月份.localeCompare(b.月份));
  for(const [c,key] of [['平仓毛盈亏','合约平仓毛盈亏USDT'],['手续费','合约手续费USDT'],['资金费','合约资金费USDT'],['其他调整','合约其他调整USDT'],['期间净盈亏','合约期间已实现净盈亏USDT']])summary[key]=a.ledger?symbols.reduce((n,r)=>n+(r[c]||0),0):null;
  const billsComplete=covered(a.periodCoverage?.bills,start,end),fillsComplete=covered(a.periodCoverage?.fills_SWAP,start,end);
  const ongoing=kind=>end===today()&&(a.periodCoverage?.[kind]||[]).some(w=>day(w.end)===end)&&covered(a.periodCoverage?.[kind],start,shift(end,-1));
  const billsOngoing=ongoing('bills'),fillsOngoing=ongoing('fills_SWAP');
  if(!billsComplete)warnings.push(billsOngoing?'今日尚未结束，盈亏与成本统计到最近同步时点。':'所选区间账单覆盖不足，盈亏与成本仅汇总已保存记录。');
  if(!a.ledger)warnings.push('旧缓存缺少逐笔分析字段，请同步 API 后重新计算。');
  if(!billsComplete&&!billsOngoing&&!ledger.length)for(const k of Object.keys(summary))summary[k]=null;
  if(!fillsComplete&&!fillsOngoing)warnings.push('所选区间成交覆盖不足，明细及成交占比仅代表已有记录。');
  summary['合约成交笔数']=fillsComplete||fillsOngoing||swap.length?swap.length:null;
  const liquidity=swap.filter(r=>r.流动性==='T'||r.流动性==='M');summary['Taker成交占比']=liquidity.length===swap.length&&swap.length?liquidity.filter(r=>r.流动性==='T').length/swap.length:null;
  // 用起日前一日收盘作分母；净值指数从 1 开始，不继承区间外高点。
  const source=(a.daily||[]).slice().sort((a,b)=>a.日期.localeCompare(b.日期));const daily=source.filter(r=>inside(r.日期)).map(r=>({...r,日收益率:null,净值指数:null,回撤:null}));
  const before=source.find(r=>r.日期===shift(start,-1));let previous=before,index=1,peak=1,segment=[],segments=[];
  for(const r of daily){const v=r.重建权益;
   if(v==null||v<=0){if(segment.length)segments.push(segment);segment=[];previous=null;index=peak=1;continue;}
   let ret=null;if(previous?.重建权益>0&&previous.日期===shift(r.日期,-1)&&r.外部划转估值!=null){const weighted=r.资金流加权估值??(r.外部划转估值===0?0:null),capital=weighted==null?null:previous.重建权益+weighted;ret=capital>0?(v-previous.重建权益-r.外部划转估值)/capital:null;}
   if(ret==null||ret<=-1){if(segment.length)segments.push(segment);segment=[];index=peak=1;ret=null;}
   if(ret!=null)index*=1+ret;r.日收益率=ret;r.净值指数=index;peak=Math.max(peak,index);r.回撤=index/peak-1;segment.push(r);previous=r;
  }
  if(segment.length)segments.push(segment);
  const selected=segments.at(-1)?.at(-1)===daily.at(-1)?segments.at(-1):[];
  const returns=(selected||[]).filter(r=>r.日收益率!=null).map(r=>r.日收益率),riskRange=selected?.length?{start:selected[0].日期,end:selected.at(-1).日期}:null;
  const keep=new Set((selected||[]).map(r=>r.日期));for(const r of daily)if(!keep.has(r.日期))Object.assign(r,{日收益率:null,净值指数:null,回撤:null});
  const mean=returns.length?returns.reduce((n,v)=>n+v,0)/returns.length:0,sd=returns.length>1?Math.sqrt(returns.reduce((n,v)=>n+(v-mean)**2,0)/(returns.length-1)):0;
  summary['重建年化夏普']=sd>0?mean/sd*Math.sqrt(365):null;summary['重建日收益样本数']=returns.length;
  summary['重建日收盘最大回撤']=selected?.length?Math.min(...selected.map(r=>r.回撤)):null;
  summary['重建期间累计收益率']=selected?.length?selected.at(-1).净值指数-1:null;
  const roundRange=a.roundRange,roundAvailable=!!a.roundAvailable&&roundRange&&start>=day(roundRange.start)&&end<=day(roundRange.end);
  const rounds=(a.rounds||[]).filter(r=>inside(r.平仓时间)).map(r=>({...r,跨期:!!r.跨期||day(r.开仓时间)<start}));
  const complete=rounds.filter(r=>!r.跨期&&inside(r.开仓时间));const wins=complete.filter(r=>r.净盈亏>0),profit=wins.reduce((n,r)=>n+r.净盈亏,0),loss=-complete.filter(r=>r.净盈亏<0).reduce((n,r)=>n+r.净盈亏,0);
  summary['完整闭合交易轮次']=roundAvailable?complete.length:null;summary['完整轮次胜率']=roundAvailable&&complete.length?wins.length/complete.length:null;summary['完整轮次盈利因子']=roundAvailable&&loss>0?profit/loss:null;
  if(!roundAvailable)warnings.push('所选区间完整轮次尚未全部复核，胜率暂不显示。');
  if(daily.length!==Math.round((Date.parse(end)-Date.parse(start))/DAY)+1||daily.some(r=>r.重建权益==null)||riskRange?.start!==start||riskRange?.end!==end)warnings.push('风险指标仅使用所选日期内最后一段连续有效日净值；缺口不连接。');
  return {...a,scope:{start,end,timezone:'UTC+8',billsComplete,fillsComplete,billsOngoing,fillsOngoing},range:{start,end},ledger,fills,symbols,months:monthly,summary,daily,rounds,groups:[],roundAvailable:!!roundAvailable,roundRange:roundAvailable?{start,end}:roundRange,riskAvailable:returns.length>1,riskRange,equityRange:daily.length?{start:daily[0].日期,end:daily.at(-1).日期}:null,periodWarnings:warnings,riskReason:'该区间连续有效日收益不足两个样本',equityHistory:{...a.equityHistory,segments:segments.length,missing:(a.equityHistory?.missing||[]).filter(r=>inside(r.日期)),message:'所选区间权益与风险已重新计算；净值指数以区间起点为基准，资金划转不计收益。'}};
 }
 function drawdownSummary(a,scope,basis='equity'){
  if(!scope?.start||!scope?.end)return {basis,scope,value:null,range:null,incomplete:true};
  let rows,key;
  if(basis==='adjusted'){rows=calculate(a,scope).daily;key='回撤';}
  else{const selected=(a.daily||[]).filter(r=>r.日期>=scope.start&&r.日期<=scope.end),previous=(a.daily||[]).find(r=>r.日期===shift(scope.start,-1));rows=equityDrawdown(selected,previous?.重建权益);key='权益回撤';}
  const values=rows.filter(r=>Number.isFinite(r[key])),closedEnd=scope.end===today()?shift(scope.end,-1):scope.end;
  return {basis,scope,value:values.length?Math.min(...values.map(r=>r[key])):null,range:values.length?{start:values[0].日期,end:values.at(-1).日期}:null,incomplete:rows.length!==Math.round((Date.parse(closedEnd)-Date.parse(scope.start))/DAY)+1||values.length!==rows.length};
 }
 const api={valid,shift,bounds,preset,today,readPreference,resolvePreference,covered,calculate,equityDrawdown,drawdownSummary};if(typeof module==='object'&&module.exports)module.exports=api;else root.DashboardPeriod=api;
})(typeof globalThis==='object'?globalThis:this);
