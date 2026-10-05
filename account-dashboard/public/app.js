'use strict';
const $ = id => document.getElementById(id);
let preparedImport=null,preparedBackup=null;
let state = {connected:false}, demoMode=false, offlineMode=false, page=1, view='overview', timer=null, busy=false, autoRestoreAttempted=false, openedSyncAttempted=false;
const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = DashboardNumbers.format;
const precise = DashboardNumbers.precise;
const pct = v => v == null ? '—' : `${fmt(Number(v)*100,Math.abs(Number(v))<1&&Math.abs(Number(v))>=.9999?4:2)}%`;
const sign = v => v == null ? '' : Number(v)>0 ? 'positive' : Number(v)<0 ? 'negative' : '';
function notice(text, demo=false){$('notice').textContent=text;$('notice').className=`notice${demo?' demo':''}${text?'':' hidden'}`;}
async function request(path, data){const response=await fetch(path,{method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':state.csrf||''},body:data===undefined?undefined:JSON.stringify(data),credentials:'same-origin'});const result=await response.json();if(!response.ok)throw Error(result.error||'请求失败');return result;}
async function sync(){try{const next=await request('/api/state');if(!demoMode&&!offlineMode){state=next;render();if(!autoRestoreAttempted){autoRestoreAttempted=true;if(state.savedAccount&&!state.connected)await restoreSaved();else if(state.connected&&!openedSyncAttempted&&state.status!=='running'){openedSyncAttempted=true;await request('/api/analyze',{});await sync();}}}else state.csrf=next.csrf;}catch(e){notice(e.message);}clearTimeout(timer);if(!demoMode&&!offlineMode&&state.connected)timer=setTimeout(sync,state.status==='running'?2000:30000);}
const SNAPSHOT_INTERVAL=30000;
let snapshotTimer=null,snapshotInFlight=false,lastSnapshotAttempt=Date.now(),snapshotError=false,metricsMarkup='';
function scheduleSnapshot(){
 clearTimeout(snapshotTimer);snapshotTimer=null;
 if(demoMode||!state.connected||document.hidden)return;
 snapshotTimer=setTimeout(autoRefreshSnapshot,Math.max(1000,SNAPSHOT_INTERVAL-(Date.now()-lastSnapshotAttempt)));
}
async function autoRefreshSnapshot(){
 if(demoMode||!state.connected||document.hidden){scheduleSnapshot();return;}
 if(busy||snapshotInFlight||state.status==='running'){lastSnapshotAttempt=Date.now();scheduleSnapshot();return;}
 lastSnapshotAttempt=Date.now();snapshotInFlight=true;render();
 try{await request('/api/refresh',{});snapshotError=false;await sync();}
 catch{snapshotError=true;}
 finally{snapshotInFlight=false;render();scheduleSnapshot();}
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.connected&&!demoMode&&Date.now()-lastSnapshotAttempt>=SNAPSHOT_INTERVAL)autoRefreshSnapshot();else scheduleSnapshot();});
function updateMetrics(markup){
 if(markup===metricsMarkup)return;
 const previous=new Map([...$('metrics').querySelectorAll('[data-money]')].map(el=>[el.dataset.money,el.dataset.target]));
 metricsMarkup=markup;$('metrics').innerHTML=markup;
 if(window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;
 for(const el of $('metrics').querySelectorAll('[data-money]')){
  const old=previous.get(el.dataset.money),next=el.dataset.target;
  if(!old||old===next||old==='—'||next==='—')continue;
  el.innerHTML=DashboardNumbers.rollingDigits(old,next).map(d=>d.changed?`<span class="rolling-glyph" aria-hidden="true"><span class="rolling-track"><span>${esc(d.previous)}</span><span>${esc(d.next)}</span></span></span>`:`<span aria-hidden="true">${esc(d.next)}</span>`).join('');
 }
}
function empty(text='接入账户后显示数据'){return `<div class="table-empty">${esc(text)}</div>`;}
function table(rows, cols, limit=100){if(!rows?.length)return empty(state.connected||demoMode||offlineMode?'暂无记录':'接入账户后显示数据');return `<div class="table-scroll"><table><thead><tr>${cols.map(c=>`<th scope="col">${esc(c.label||c.key)}</th>`).join('')}</tr></thead><tbody>${rows.slice(0,limit).map(r=>`<tr>${cols.map(c=>{const v=r[c.key];return `<td title="${esc(r[c.rawKey||c.key]??'未提供')}" class="${c.color?sign(v):''}">${c.format?esc(c.format(v)):esc(v??'—')}</td>`;}).join('')}</tr>`).join('')}</tbody></table></div>`;}
function metric(label,value,detail,cls='',unit=''){return `<div class="metric"><div class="metric-label">${label}</div><div class="metric-value ${cls}">${['USD','USDT'].includes(unit)?`<span class="rolling-number" data-money="${esc(label)}" data-target="${esc(value)}" aria-label="${esc(value)}">${esc(value)}</span>`:value} ${unit?`<small>${unit}</small>`:''}</div><div class="metric-detail">${detail}</div></div>`;}
const chartInstances = new Map();
let chartRange = 'all';
const PERIOD_STORAGE_KEY='okx-dashboard-analysis-period-v1';
let periodScope=null,periodPreset='all',displayAnalysis={},periodDraft=false,periodToday=false,periodStorageError='';
let savedPeriod=null;try{savedPeriod=DashboardPeriod.readPreference(localStorage.getItem(PERIOD_STORAGE_KEY));}catch{periodStorageError='浏览器未允许保存日期，刷新后可能需要重新选择。';}
if(savedPeriod){periodPreset=savedPeriod.preset;periodToday=savedPeriod.endMode==='today';if(periodPreset==='custom')periodScope=DashboardPeriod.resolvePreference(savedPeriod,{});}
const DRAWDOWN_STORAGE_KEY='okx-dashboard-drawdown-scope-v1';
let drawdownScopeMode='follow';try{const stored=localStorage.getItem(DRAWDOWN_STORAGE_KEY);if(['follow','3','6','all'].includes(stored))drawdownScopeMode=stored;}catch{}
$('drawdown-scope').value=drawdownScopeMode;
function drawdownStatistic(){const source=state.analysis||{},scope=drawdownScopeMode==='follow'?periodScope:DashboardPeriod.preset(source,drawdownScopeMode,drawdownScopeMode==='all'?undefined:DashboardPeriod.today());return {...DashboardPeriod.drawdownSummary(source,scope,$('drawdown-mode').value),selection:drawdownScopeMode};}
function periodPreference(){return {version:1,preset:periodPreset,endMode:periodToday?'today':'fixed',...(periodPreset==='custom'?{start:periodScope.start,...(!periodToday?{end:periodScope.end}:{})}:{})};}
function savePeriod(){if(demoMode||offlineMode)return;try{savedPeriod=periodPreference();localStorage.setItem(PERIOD_STORAGE_KEY,JSON.stringify(savedPeriod));periodStorageError='';}catch{periodStorageError='浏览器未允许保存日期，刷新后可能需要重新选择。';}}
function updatePeriod(){const base=state.analysis||{},b=DashboardPeriod.bounds(base);
 if(periodPreset==='custom'&&periodScope){if(periodToday)periodScope={...periodScope,end:DashboardPeriod.today()};}
 else if(b.start&&b.end)periodScope=DashboardPeriod.preset(base,periodPreset,periodToday?DashboardPeriod.today():undefined);
 if(periodScope&&periodScope.start>periodScope.end){periodScope=null;periodPreset='all';periodToday=false;periodStorageError='已保存的开始日期晚于今天，已恢复全部历史，请重新选择。';}
 displayAnalysis=DashboardPeriod.calculate(base,periodScope);
 $('period-summary').textContent=periodScope?`${periodScope.start} — ${periodScope.end} · 北京时间${periodToday?' · 自动到今天':''}`:'历史分析完成后可选择日期';
 for(const id of ['period-start','period-today'])$(id).disabled=!b.start;for(const button of document.querySelectorAll('[data-period]')){button.disabled=!b.start;button.classList.toggle('active',button.dataset.period===periodPreset);button.setAttribute('aria-pressed',String(button.dataset.period===periodPreset));}
 if(!periodDraft){$('period-today').checked=periodToday;$('period-end').disabled=!b.start||periodToday;if(periodScope&&document.activeElement!==$('period-start')&&document.activeElement!==$('period-end')){$('period-start').value=periodScope.start;$('period-end').value=periodScope.end;}}
 $('period-caption').textContent=periodScope?`${periodScope.start} 至 ${periodScope.end} · ${b.start||'—'} 至 ${b.end||'—'} 有已保存数据 · ${periodToday?'结束日期每天自动更新为今天；今日统计截至最近同步，日净值截至最近收盘。':'预设截至最近日收盘；自定义日期保持固定。'} 当前资产仍为实时快照。 ${displayAnalysis.periodWarnings?.join(' ')||''} ${periodStorageError||(!demoMode&&savedPeriod?'日期设置已保存到当前浏览器。':'')}`:'历史分析完成后可选择日期';
}
for(const button of document.querySelectorAll('[data-period]'))button.onclick=()=>{periodDraft=false;periodPreset=button.dataset.period;periodScope=DashboardPeriod.preset(state.analysis||{},periodPreset,periodToday?DashboardPeriod.today():undefined);page=1;$('period-error').classList.add('hidden');savePeriod();render();};
function applyPeriodDates(){
 const todayMode=$('period-today').checked,scope={start:$('period-start').value,end:todayMode?DashboardPeriod.today():$('period-end').value};
 if(!DashboardPeriod.valid(scope.start)||!DashboardPeriod.valid(scope.end)||scope.start>scope.end){periodDraft=true;$('period-error').textContent='请选择完整有效的日期，开始日期不能晚于结束日期。当前仍显示上一次有效区间。';$('period-error').classList.remove('hidden');return;}
 periodDraft=false;periodToday=todayMode;periodScope=scope;periodPreset='custom';page=1;$('period-error').classList.add('hidden');savePeriod();render();
}
for(const id of ['period-start','period-end']){const field=$(id);field.oninput=()=>{periodDraft=true;};field.onchange=applyPeriodDates;field.onblur=()=>{if(periodDraft)applyPeriodDates();};}
$('period-today').onchange=()=>{periodDraft=true;$('period-end').disabled=$('period-today').checked;if($('period-today').checked)$('period-end').value=DashboardPeriod.today();applyPeriodDates();};
$('period-form').onsubmit=event=>{event.preventDefault();applyPeriodDates();};
function applyChartRange(item){
 if(!item.data?.length)return;
 if(chartRange==='all'){item.api.timeScale().fitContent();return;}
 const end=item.data.at(-1)?.time;if(!end)return;
 const from=new Date(end+'T00:00:00Z');from.setUTCDate(from.getUTCDate()-Number(chartRange)+1);
 item.api.timeScale().setVisibleRange({from:from.toISOString().slice(0,10),to:end});
}
function chart(rows,key,target,color,percent=false){
 const host=$(target),valid=(rows||[]).filter(r=>/^\d{4}-\d{2}-\d{2}$/.test(r.日期)&&r[key]!=null&&r[key]!==''&&Number.isFinite(Number(r[key])));
 const points=new Map((rows||[]).filter(r=>/^\d{4}-\d{2}-\d{2}$/.test(r.日期)).map(r=>[r.日期,r[key]!=null&&r[key]!==''&&Number.isFinite(Number(r[key]))?Number(r[key]):null]));
 const data=[...points].sort(([a],[b])=>a.localeCompare(b)).map(([time,value])=>value===null?{time}:{time,value});
 let item=chartInstances.get(target);
 if(!valid.length){
  if(item){item.observer?.disconnect();item.api.remove();chartInstances.delete(target);}
  host.innerHTML=`<div class="chart-empty"><strong>${state.connected?'日净值暂不可用':'还没有净值数据'}</strong><span>${state.status==='running'?'历史数据正在获取与复核':state.connected?'请查看“数据与计算口径”中的原因':'接入账户，或体验示例数据'}</span></div>`;return;
 }
 if(!window.LightweightCharts){host.innerHTML=empty('图表组件加载失败，请刷新页面');return;}
 const fingerprint=JSON.stringify([key,data]);
 if(item?.fingerprint===fingerprint)return;
 if(!item){
  host.innerHTML='<div class="chart-canvas"></div><div class="chart-legend" aria-live="off"></div>';
  const api=LightweightCharts.createChart(host.querySelector('.chart-canvas'),{
   autoSize:true,layout:{background:{type:'solid',color:'#ffffff'},textColor:'#738297',fontFamily:'-apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif',fontSize:11,attributionLogo:true},
   grid:{vertLines:{visible:false},horzLines:{color:'#edf1f6'}},
   rightPriceScale:{borderVisible:false,scaleMargins:{top:.2,bottom:.12}},
   timeScale:{borderVisible:false,timeVisible:false,rightOffset:2,minBarSpacing:.5},
   localization:{locale:'zh-CN'},crosshair:{mode:LightweightCharts.CrosshairMode.Magnet,vertLine:{color:'#9baec6',labelBackgroundColor:'#425b7a'},horzLine:{color:'#9baec6',labelBackgroundColor:'#425b7a'}},
   handleScroll:{mouseWheel:false,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:false},handleScale:{mouseWheel:true,pinch:true,axisPressedMouseMove:true}
  });
  item={api,legend:host.querySelector('.chart-legend')};chartInstances.set(target,item);
  let previousWidth=host.clientWidth;item.observer=new ResizeObserver(()=>{const width=host.clientWidth;if(width>0&&width!==previousWidth){previousWidth=width;requestAnimationFrame(()=>{if(chartInstances.get(target)===item)applyChartRange(item);});}});item.observer.observe(host);
  api.subscribeCrosshairMove(param=>{
   const point=[...(item.seriesList||[])].map(s=>param.seriesData.get(s)).find(p=>p?.value!=null);
   if(!param.time){item.legend.textContent=item.latest;return;}
   const time=typeof param.time==='string'?param.time:`${param.time.year}-${String(param.time.month).padStart(2,'0')}-${String(param.time.day).padStart(2,'0')}`;
   item.legend.textContent=`${time} · ${item.key} ${point?item.format(point.value):'未取得 / 待核对'}`;
  });
 }
 const mode=target==='drawdown-chart'?'drawdown':key==='日收益率'?'returns':'area';
 if(item.seriesList)for(const series of item.seriesList)item.api.removeSeries(series);
 const options=(mode==='returns'||mode==='drawdown')?{baseValue:{type:'price',price:0},baseLineVisible:mode==='drawdown',baseLineColor:'#a8b4c4',topLineColor:'#11816e',topFillColor1:'rgba(17,129,110,.2)',topFillColor2:'rgba(17,129,110,.02)',bottomLineColor:'#c64952',bottomFillColor1:'rgba(198,73,82,.02)',bottomFillColor2:'rgba(198,73,82,.2)'}:{lineColor:color,topColor:target==='drawdown-chart'?'rgba(198,73,82,.04)':'rgba(49,106,222,.24)',bottomColor:target==='drawdown-chart'?'rgba(198,73,82,.24)':'rgba(49,106,222,.01)',lineWidth:2};
 item.format=percent?pct:v=>key==='净值指数'?fmt(v,4):fmt(v);
 const segments=[];let segment=[];
 for(const row of data){if(row.value==null){if(segment.length)segments.push(segment);segment=[];}else segment.push(row);}
 if(segment.length)segments.push(segment);
 item.seriesList=segments.map((part,i)=>{
  const series=item.api.addSeries((mode==='returns'||mode==='drawdown')?LightweightCharts.BaselineSeries:LightweightCharts.AreaSeries,{...options,...(mode==='drawdown'?{autoscaleInfoProvider:original=>{const info=original();return info?{...info,priceRange:{minValue:Math.min(info.priceRange.minValue,-.01),maxValue:0}}:null;}}:{}),priceFormat:{type:'custom',formatter:item.format,minMove:(key==='净值指数'||percent)?0.0001:0.01},priceLineVisible:false,lastValueVisible:i===segments.length-1});
  if(i===segments.length-1){const selected=new Set(part.map(r=>r.time));series.setData(data.map(r=>selected.has(r.time)?r:{time:r.time}));}else series.setData(part);
  return series;
 });
 item.series=item.seriesList.at(-1);item.mode=mode;
 item.api.applyOptions({localization:{priceFormatter:item.format},rightPriceScale:{scaleMargins:{top:mode==='drawdown'?0:.2,bottom:mode==='drawdown'?.05:.12}}});
 const last=data.findLast(r=>r.value!=null);item.key=key;item.data=data;item.latest=`${last.time} · ${key} ${item.format(last.value)}`;item.legend.textContent=item.latest;
 item.fingerprint=fingerprint;applyChartRange(item);
}
function render(){scheduleSnapshot();updatePeriod();const connected=state.connected||demoMode||offlineMode,s=state.snapshot||{},a=displayAnalysis,m=a.summary||{};const risk=a.riskAvailable,roundRisk=a.roundAvailable??risk;
 const local=state.local||a.local||{},rr=a.riskRange||(a.riskAvailable?a.range:null);
 const syncInfo=a.sync||{},cachedAt=syncInfo.statisticsAt||local.lastSync;
 const syncText=offlineMode?(syncInfo.state==='partial'?'备份保存时有未完整获取的数据，当前展示上次成功统计。'+(cachedAt?' 统计成功时间：'+cachedAt:''):''):state.status==='running'&&a.fills?'正在同步 API，当前显示上次保存的统计。'+(cachedAt?' 统计成功时间：'+cachedAt:''):syncInfo.state==='partial'?syncInfo.message+(cachedAt?' 统计成功时间：'+cachedAt:''):'';
 $('sync-notice').textContent=syncText;$('sync-notice').classList.toggle('hidden',!syncText||demoMode);
 $('snapshot-notice').textContent=(s.warnings||[]).join(' ');$('snapshot-notice').classList.toggle('hidden',!(s.warnings||[]).length||demoMode);
 $('gap-notice').textContent=local.message?local.message+' '+(local.gaps||[]).map(g=>g.start+' 至 '+g.end).join('；'):'';
 $('gap-notice').classList.toggle('hidden',!local.message||demoMode);
 $('local-status').textContent=connected?`上次成功同步：${local.lastSync||'尚未完成'} · 本机保存 ${local.storedRecords||0} 条原始记录（含用于去重的重叠记录）`:'连接后读取已保存历史，每次打开自动同步 API';
 $('imports').innerHTML=table(local.imports,[{key:'文件类型'},{key:'记录数'},{key:'有效新增'},{key:'重复记录'},{key:'API覆盖'},{key:'覆盖开始'},{key:'覆盖结束'},{key:'导入时间'}]);$('history-coverage').innerHTML=table(local.coverage,[{key:'数据集'},{key:'来源'},{key:'开始'},{key:'结束'}],local.coverage?.length||100);
 const dataBusy=!state.connected||demoMode||state.status==='running'||busy||snapshotInFlight;for(const id of ['import-preview','backup-export','backup-preview'])$(id).disabled=dataBusy;$('import-history').disabled=dataBusy||!preparedImport;$('backup-restore').disabled=dataBusy||!preparedBackup;
 $('forget').classList.toggle('hidden',!state.savedAccount||demoMode);$('saved-panel').classList.toggle('hidden',!state.savedAccount);$('remember').disabled=state.keychainAvailable===false;const credentialName=state.credentialStore?.name||'系统凭据存储';$('remember-caption').textContent=`记住此账户（保存在本机 ${credentialName}）`;$('saved-caption').textContent=`本机 ${credentialName} 已保存账户凭据`;$('credential-note').textContent=state.keychainAvailable===false?'本机系统凭据存储暂不可用，仍可不勾选保存，仅在当前会话连接。':`未勾选时仅在当前会话保留；勾选后保存到 ${credentialName}，下次打开自动连接。断开连接保留凭据，“忘记账户”会删除它们。`;
 $('connect-panel').classList.toggle('hidden',connected);$('disconnect').classList.toggle('hidden',!connected);$('disconnect').textContent=offlineMode?'退出离线查看':demoMode?'接入我的账户':'断开连接';$('connection').textContent=offlineMode?'离线备份':demoMode?'示例账户':connected?'OKX · 只读已连接':'未连接账户';$('connection').classList.toggle('online',connected);
 for(const id of ['refresh','reanalyze'])$(id).disabled=!state.connected||demoMode||state.status==='running'||busy||snapshotInFlight;$('export').disabled=!connected||!a.fills;
 $('snapshot-live').classList.toggle('hidden',!connected||demoMode||offlineMode);$('snapshot-live').textContent=snapshotInFlight?'正在更新快照':snapshotError?'更新失败 · 自动重试':state.status==='running'?'历史同步中 · 快照稍后更新':'快照自动更新 · 30 秒';$('snapshot-live').classList.toggle('refresh-error',snapshotError);
 $('workspace-label').textContent=offlineMode?'离线历史工作台':demoMode?'示例工作台':'分析工作台';$('time-label').textContent=offlineMode?`离线历史 · 备份时间 ${state.backupCreated||'—'} · 不提供当前快照`:connected?`快照 ${s.updated||'—'} · 历史同步 ${local.lastSync||'尚未成功'} · 最新记录 ${state.analysis?.range?.end||'—'}`:'接入后显示账户数据';
 $('progress').classList.toggle('hidden',state.status!=='running'||demoMode);$('progress-text').textContent=state.message||'正在获取账户记录';
 updateMetrics('<h2 class="metric-heading">所选区间表现 <span>'+esc(periodScope?periodScope.start+' 至 '+periodScope.end:'等待历史数据')+'</span></h2><div class="metrics period-metrics">'+metric('USDT 永续净盈亏',fmt(m['合约期间已实现净盈亏USDT']),a.scope?.billsComplete?'所选区间 · 含交易成本及其他调整':a.scope?.billsOngoing?'截至最近同步 · 今日尚未结束':'所选区间 · 已保存记录，覆盖不足',sign(m['合约期间已实现净盈亏USDT']),'USDT')+metric('年化夏普',risk?fmt(m['重建年化夏普']):'—',risk?`${rr?.start||''} 至 ${rr?.end||''} · ${m['重建日收益样本数']} 个日收益样本`:'连续有效日收益不足两个样本')+metric('投资净值最大回撤',risk?pct(m['重建日收盘最大回撤']):'—',risk?`${rr?.start||''} 至 ${rr?.end||''} · 资金调整净值高点（估算）`:'有效区间不足',risk?'negative':'')+metric('完整轮次胜率',roundRisk?pct(m['完整轮次胜率']):'—',roundRisk?`${m['完整闭合交易轮次']} 个完整轮次 · 开平仓均在区间内`:'所选区间轮次覆盖尚未完整')+metric('USDT 永续手续费',fmt(m['合约手续费USDT']),'所选区间 · 负数为支出',sign(m['合约手续费USDT']),'USDT')+'</div><h2 class="metric-heading snapshot-heading">当前账户快照 <span>'+ (offlineMode?'离线不可用':'不随分析日期变化')+'</span></h2><div class="metrics snapshot-metrics">'+metric('交易账户权益',fmt(s.equity),'当前快照 · USD','','USD')+metric('USDT 仓位浮盈',fmt(s.floating),'当前未平仓部分',sign(s.floating),'USDT')+metric('当前有效敞口',s.equity>0?`${fmt(s.exposure/s.equity)}<small>倍</small>`:'—',`持仓名义价值 ${fmt(s.exposure)} USD`)+'</div>');
 // 交互图固定使用全历史原序列；区间筛选只重算上方表现与交易汇总。
 const chartSource=state.analysis||{},chartAnalysis=DashboardPeriod.calculate(chartSource,DashboardPeriod.preset(chartSource,'all')),chartRisk=chartAnalysis.riskAvailable,chartRiskRange=chartAnalysis.riskRange;
 const historyInfo=chartSource.equityHistory||{},range=$('chart-mode').value==='重建权益'?(chartAnalysis.equityRange||{start:chartAnalysis.daily?.[0]?.日期,end:chartAnalysis.daily?.at(-1)?.日期}):chartRiskRange;
 $('equity-history-notice').textContent=(historyInfo.message||'')+(historyInfo.missing?.length?' 全历史待核对日期：'+historyInfo.missing.length+' 天。':'');$('equity-history-notice').classList.toggle('hidden',!historyInfo.message||demoMode);
 const equityKey=$('chart-mode').value,equityValid=(chartAnalysis.daily||[]).filter(row=>Number.isFinite(row[equityKey])),equityLatest=equityValid.at(-1);$('equity-stat-label').textContent=equityKey==='重建权益'?'最近日收盘权益 · USDT':equityKey==='净值指数'?'最近净值指数':'最近日收益率';$('equity-stat-value').textContent=equityLatest?(equityKey==='日收益率'?pct(equityLatest[equityKey]):fmt(equityLatest[equityKey],equityKey==='净值指数'?4:2)):'—';$('equity-stat-count').textContent=equityValid.length?`${equityValid.length} 天`:'—';
 $('equity-caption').textContent=range?.start?`${range.start} — ${range.end}${historyInfo.missing?.length?' · '+historyInfo.missing.length+' 天待核对':''}`:'全历史 · 通过日收盘数据重建';chart(chartAnalysis.daily,$('chart-mode').value,'equity-chart','#316ade',$('chart-mode').value==='日收益率');const adjusted=$('drawdown-mode').value==='adjusted',drawdownRows=adjusted?chartAnalysis.daily:DashboardPeriod.equityDrawdown(chartAnalysis.daily),drawdownKey=adjusted?'回撤':'权益回撤',validDrawdown=(drawdownRows||[]).filter(r=>r[drawdownKey]!=null),ddRange=adjusted?chartRiskRange:chartAnalysis.equityRange;
 chart(drawdownRows,drawdownKey,'drawdown-chart','#d17480',true);$('drawdown-caption').textContent=ddRange?.start?`${ddRange.start} 至 ${ddRange.end} · 全历史`:'全历史 · 尚无有效日值';$('drawdown-current').textContent=validDrawdown.length?pct(validDrawdown.at(-1)[drawdownKey]):'—';const ddStat=drawdownStatistic();$('drawdown-value').textContent=pct(ddStat.value);$('drawdown-stat-caption').textContent=ddStat.scope?`最大回撤：${ddStat.scope.start} — ${ddStat.scope.end}${ddStat.incomplete?' · 覆盖不足':ddStat.scope.end===DashboardPeriod.today()?' · 截至最近收盘':''}`:'暂无可统计的日期区间';$('drawdown-value').title='在所选日期内重新建立高点，起始日前一日收盘仅作首日基准；不继承更早的历史高点。';$('drawdown-range').textContent=ddStat.scope?`统计日期：${ddStat.scope.start} 至 ${ddStat.scope.end} · 有效日值：${ddStat.range?`${ddStat.range.start} 至 ${ddStat.range.end}`:'暂无'}${ddStat.incomplete?' · 覆盖不完整，仅统计有效日值':''}${ddStat.scope.end===DashboardPeriod.today()?' · 今日未结束，日值截至最近收盘':''}。区间内重新建立高点；曲线仍显示全部历史。${adjusted?'资金调整口径仅统计最后一段连续有效净值。':''}`:'暂无可统计的日期区间';$('drawdown-method').textContent=adjusted?'按资金调整后的净值高点计算，重新入金不抹去之前的投资亏损。日收益为估算。':'按每日账户权益相对历史权益高点计算，转入与转出资金也会改变曲线；上方风险卡片使用资金调整净值口径。';
 const symbols=DashboardNumbers.contributions(a.symbols||[]),max=Math.max(...symbols.map(r=>Math.abs(r['期间净盈亏'])),.000001);
 $('symbol-chart').innerHTML=symbols.length?`<div class="symbol-bars">${symbols.map(r=>{const value=r['期间净盈亏'],width=Math.abs(value)/max*50;return `<div class="bar-row"><span>${esc(r.合约.replace('-USDT-SWAP',''))}</span><div class="bar-track signed"><svg width="100%" height="12" aria-hidden="true"><line x1="50%" x2="50%" y1="0" y2="12" stroke="#92a1b5"/><rect class="bar-fill ${value<0?'loss':''}" x="${value<0?50-width:50}%" y="2" width="${width}%" height="8" rx="2"/></svg></div><strong class="bar-number ${sign(value)}">${value>0?'+':''}${fmt(value)}</strong></div>`;}).join('')}</div>`:empty();
 $('symbol-total').textContent=symbols.length?`全部合约净盈亏合计 ${fmt(m['合约期间已实现净盈亏USDT'])} USDT · 左侧亏损 / 右侧盈利`:'';
 renderSymbols();
 $('behavior').innerHTML=[['合约平仓毛盈亏',fmt(m['合约平仓毛盈亏USDT'])+' USDT'],['合约资金费',fmt(m['合约资金费USDT'])+' USDT'],['合约其他调整',fmt(m['合约其他调整USDT'])+' USDT'],['合约成交笔数',m['合约成交笔数']??'—'],['Taker 成交占比',pct(m['Taker成交占比'])],['完整轮次盈利因子',roundRisk?fmt(m['完整轮次盈利因子']):'—'],['当前普通挂单',s.orderCount??'—']].map(([k,v])=>`<div class="fact-row"><span>${k}</span><strong>${esc(v)}</strong></div>`).join('');
 $('months').innerHTML=table(a.months,[{key:'月份'},{key:'平仓毛盈亏',format:fmt,color:true},{key:'手续费',format:fmt,color:true},{key:'资金费',format:fmt,color:true},{key:'其他调整',format:fmt,color:true},{key:'净盈亏',format:fmt,color:true},{key:'成交笔数'}]);
 $('positions').innerHTML=table(s.positions,[{key:'instId',label:'合约'},{key:'posSide',label:'持仓模式',format:v=>({net:'净持仓',long:'多仓',short:'空仓'}[v]||v)},{key:'mgnMode',label:'保证金模式',format:v=>v==='cross'?'全仓':'逐仓'},{key:'pos',label:'数量',format:v=>fmt(v,4)},{key:'avgPx',label:'开仓均价',format:precise},{key:'markPx',label:'标记价格',format:precise},{key:'upl',label:'浮盈（结算币）',format:fmt,color:true},{key:'instType',label:'数量单位',format:v=>['SWAP','FUTURES','OPTION'].includes(v)?'张':'币'},{key:'ccy',label:'结算币种'},{key:'lever',label:'杠杆倍数',format:v=>fmt(v,1)+'×'},{key:'liqPx',label:'预估强平价格',format:v=>v?precise(v):'—'}]);
 $('assets').innerHTML=table(s.assets,[{key:'币种'},{key:'权益',format:v=>fmt(v,8)},{key:'现金余额',format:v=>fmt(v,8)},{key:'美元权益',format:v=>fmt(v,4)}]);$('funding').innerHTML=s.blocks?.funding?.status==='error'?empty(s.blocks.funding.message):table(s.funding,[{key:'币种'},{key:'余额',format:v=>fmt(v,8)},{key:'可用余额',format:v=>fmt(v,8)}]);
 renderTrades();renderRounds();
 $('history-gaps-card').classList.toggle('hidden',!a.equityHistory?.missing?.length);$('history-gaps').innerHTML=table(a.equityHistory?.missing,[{key:'日期'},{key:'原因'}],a.equityHistory?.missing?.length||0);
 $('methodology').innerHTML=connected?`<p>累计记录：${esc(a.range?.start||'—')} 至 ${esc(a.range?.end||'—')}。<br>风险指标有效区间：${esc(rr?.start||'—')} 至 ${esc(rr?.end||'—')}。<br>净值图范围：${esc(a.equityRange?.start||a.daily?.[0]?.日期||'—')} 至 ${esc(a.equityRange?.end||a.daily?.at(-1)?.日期||'—')}。<br>轮次胜率区间：${esc(a.roundRange?.start||a.range?.start||'—')} 至 ${esc(a.roundRange?.end||a.range?.end||'—')}。</p><strong>${risk?(a.equityHistory?.state==='partial'?'有效区间日净值已通过复核，其他日期见待核对清单':'日净值重建已通过复核'):esc(a.riskReason||(state.status==='running'?'历史数据尚在采集':'尚未完成历史分析'))}</strong><ul>${(a.caveats||['当前先展示账户快照；历史分析完成后自动更新指标。']).map(c=>c.replace('日简单收益 / 样本标准差 × √365','平均日收益率 / 日收益样本标准差 × √365').replace('日内划转按收盘估值。','USDT 划转按流水金额，非 USDT 划转按日收盘价格估值；收益分母按资金流发生时间加权。')).map(c=>`<li>${esc(c)}</li>`).join('')}</ul>${Object.keys(a.errors||{}).length?'<p>部分数据获取失败：'+esc(Object.keys(a.errors).join('、'))+'。可重新分析重试。</p>':''}`:empty('接入账户后，显示历史覆盖情况与计算口径。');$('coverage').innerHTML=table(a.coverage,[{key:'数据集'},{key:'条目数'},{key:'完整',label:'获取状态',format:v=>v?'已完整获取':'缺失 / 未完整'}]);
 if(offlineMode)notice('正在离线查看账户备份，仅展示保存时的历史记录。当前资产、持仓与 API 同步不可用。');else if(demoMode)notice('正在查看生成的示例数据，与任何真实账户无关。点击“接入我的账户”填写自己的只读 API。',true);else if(state.status==='error')notice(state.message||'分析未完成');
}
function renderSymbols(){
 const rows=[...(displayAnalysis.symbols||[])],sort=$('symbol-sort').value;
 rows.sort((a,b)=>sort==='profit'?b.期间净盈亏-a.期间净盈亏:sort==='loss'?a.期间净盈亏-b.期间净盈亏:Math.abs(b.期间净盈亏)-Math.abs(a.期间净盈亏));
 $('all-symbols').innerHTML=table(rows,[{key:'合约'},{key:'平仓毛盈亏',format:fmt,color:true},{key:'手续费',format:fmt,color:true},{key:'资金费',format:fmt,color:true},{key:'其他调整',format:fmt,color:true},{key:'期间净盈亏',format:fmt,color:true},{key:'成交笔数'}],rows.length);
}
$('symbol-sort').onchange=renderSymbols;
function filteredTrades(){
 const search=$('trade-search').value.trim().toUpperCase(),product=$('trade-product').value,side=$('trade-side').value,source=$('trade-source').value,sort=$('trade-sort').value;
 const rows=(displayAnalysis.fills||[]).filter(r=>(!search||`${r.合约} ${r.产品}`.toUpperCase().includes(search))&&(!product||r.产品===product)&&(!side||r.买卖===side)&&(!source||r.来源===source));
 rows.sort((a,b)=>sort==='old'?String(a.时间).localeCompare(String(b.时间)):sort==='pnl'?(b.平仓盈亏??-Infinity)-(a.平仓盈亏??-Infinity):sort==='fee'?Math.abs(b.手续费||0)-Math.abs(a.手续费||0):String(b.时间).localeCompare(String(a.时间)));
 return rows;
}
function renderTrades(){
 const rows=filteredTrades();
 const pages=Math.max(1,Math.ceil(rows.length/15));page=Math.min(page,pages);$('trade-filter-summary').textContent=`所选日期内 ${displayAnalysis.fills?.length||0} 笔 · 当前筛选 ${rows.length} 笔 · 买卖方向不等于开平仓方向`;
 $('fills').innerHTML=table(rows.slice((page-1)*15,page*15),[{key:'时间'},{key:'产品'},{key:'合约'},{key:'买卖'},{key:'数量',format:precise,rawKey:'原始数量'},{key:'成交价',format:precise,rawKey:'原始成交价'},{key:'平仓盈亏',format:fmt,color:true},{key:'手续费',format:precise,rawKey:'原始手续费',color:true},{key:'费用币种'},{key:'来源'}]);
 $('pagination').innerHTML=`<span>共 ${rows.length} 笔 · ${page} / ${pages} 页</span><button id="prev-page" class="secondary" ${page<=1?'disabled':''}>上一页</button><button id="next-page" class="secondary" ${page>=pages?'disabled':''}>下一页</button>`;$('prev-page').onclick=()=>{page--;renderTrades();};$('next-page').onclick=()=>{page++;renderTrades();};
}
function renderRounds(){
 const a=displayAnalysis,direction=$('round-direction').value,rows=(a.rounds||[]).filter(r=>!direction||r.方向===direction).slice().sort((a,b)=>String(b.平仓时间).localeCompare(String(a.平仓时间))),stats=DashboardNumbers.roundStats(rows);
 $('round-insights').innerHTML=a.roundAvailable?[['完整轮次数',stats.count],['胜率',pct(stats.winRate)],['平均盈利',fmt(stats.averageWin)+' USDT'],['平均亏损',fmt(stats.averageLoss)+' USDT'],['平均持仓',fmt(stats.averageHours)+' 小时'],['最大连续亏损',stats.maxLossStreak==null?'—':stats.maxLossStreak+' 次']].map(([label,value])=>`<div><span>${label}</span><strong>${esc(value)}</strong></div>`).join(''):'<p>轮次覆盖尚未完整，以下记录仅供查看，汇总暂不计算。</p>';
 const key=r=>[r.合约,r.方向,r.开仓时间,r.平仓时间].join('|'),selected=$('round-select').value;$('round-select').innerHTML=rows.map(r=>`<option value="${esc(key(r))}">${esc(r.合约+' · '+r.方向+' · '+r.平仓时间)}</option>`).join('');if(rows.some(r=>key(r)===selected))$('round-select').value=selected;
 const chosen=rows.find(r=>key(r)===$('round-select').value);$('round-detail').innerHTML=chosen?table([chosen],[{key:'合约'},{key:'方向'},{key:'开仓时间'},{key:'平仓时间'},{key:'毛盈亏',format:fmt,color:true},{key:'手续费',format:fmt,color:true},{key:'资金费',format:fmt,color:true},{key:'其他调整',format:fmt,color:true},{key:'净盈亏',format:fmt,color:true},{key:'跨期',format:v=>v?'跨期 · 不计汇总':'完整轮次'}]):empty('所选范围没有交易轮次');
 $('rounds').innerHTML=table(rows.slice(0,30),[{key:'合约'},{key:'方向'},{key:'开仓时间'},{key:'平仓时间'},{key:'持仓小时',format:fmt},{key:'净盈亏',format:fmt,color:true},{key:'跨期',format:v=>v?'跨期 · 不计胜率':'完整轮次'}]);
}
for(const id of ['trade-product','trade-side','trade-source','trade-sort'])$(id).onchange=()=>{page=1;renderTrades();};$('round-direction').onchange=renderRounds;$('round-select').onchange=renderRounds;

function selectView(value){view=value;for(const name of ['overview','positions','trades','quality'])$('view-'+name).classList.toggle('hidden',name!==view);document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-current',b.dataset.view===view?'page':'false');});$('page-title').textContent={overview:'账户概览',positions:'资产与持仓',trades:'交易明细',quality:'数据与计算口径'}[view];}
for(const button of document.querySelectorAll('[data-view]'))button.onclick=()=>selectView(button.dataset.view);
$('chart-mode').onchange=()=>render();$('drawdown-mode').onchange=()=>render();$('drawdown-scope').onchange=()=>{drawdownScopeMode=$('drawdown-scope').value;if(!demoMode&&!offlineMode)try{localStorage.setItem(DRAWDOWN_STORAGE_KEY,drawdownScopeMode);}catch{}render();};
$('trade-search').oninput=()=>{page=1;renderTrades();};
$('connect-form').onsubmit=async event=>{event.preventDefault();if(busy)return;busy=true;$('connect-submit').disabled=true;$('connect-submit').textContent='正在验证只读权限…';notice('');const fields=new FormData(event.target),payload=Object.fromEntries(fields);payload.remember=$('remember').checked;autoRestoreAttempted=true;openedSyncAttempted=true;try{await request('/api/connect',payload);event.target.reset();await sync();selectView('overview');}catch(e){notice(e.message);}finally{for(const k of ['key','secret','passphrase'])payload[k]='';busy=false;$('connect-submit').disabled=false;$('connect-submit').textContent='验证并开始分析';}};
$('disconnect').onclick=async()=>{autoRestoreAttempted=true;clearTimeout(timer);if(!demoMode&&!offlineMode){try{await request('/api/disconnect',{});}catch(e){notice(e.message);return;}}demoMode=false;offlineMode=false;preparedImport=preparedBackup=null;for(const id of ['import-preview-table','backup-preview-table']){if($(id))$(id).innerHTML='';}for(const id of ['history-files','backup-file'])$(id).value='';$('import-history').classList.add('hidden');$('backup-restore').classList.add('hidden');periodDraft=false;periodPreset=savedPeriod?.preset||'all';periodToday=savedPeriod?.endMode==='today';periodScope=savedPeriod?DashboardPeriod.resolvePreference(savedPeriod,{}):null;state={connected:false};page=1;$('trade-search').value='';notice('');$('connect-form').reset();await sync();selectView('overview');};
for(const [id,path] of [['refresh','/api/refresh'],['reanalyze','/api/analyze']])$(id).onclick=async()=>{busy=true;render();notice('');try{await request(path,{});await sync();}catch(e){notice(e.message);}finally{busy=false;render();}};
$('export').onclick=()=>{const blob=new Blob([JSON.stringify({mode:offlineMode?'离线账户备份':demoMode?'示例数据':'只读账户分析',scope:periodScope,snapshot:{...state.snapshot,scope:offlineMode?'离线不可用':'当前快照，不受分析日期筛选'},tradeView:{filters:Object.fromEntries(['trade-search','trade-product','trade-side','trade-source','trade-sort'].map(id=>[id,$(id).value])),fills:filteredTrades()},datePreference:periodPreference(),analysis:displayAnalysis,chartHistory:{drawdownBasis:$('drawdown-mode').value,equityDrawdown:DashboardPeriod.equityDrawdown(state.analysis?.daily||[]),scope:'全部已保存历史，不受分析日期筛选',daily:state.analysis?.daily||[],equityRange:state.analysis?.equityRange,riskRange:state.analysis?.riskRange,maximumDrawdown:drawdownStatistic(),fullHistoryAdjustedMaximumDrawdown:state.analysis?.summary?.['重建日收盘最大回撤']}},null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`衡量-${demoMode?'示例':'账户分析'}-${periodScope?.start||'全部'}-${periodScope?.end||'历史'}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
function demo(){const daily=[];let index=1,peak=1;for(let i=0;i<90;i++){const ret=i?Math.sin(i*1.73)*.016+Math.cos(i*.28)*.003+.0019:0;index*=1+ret;peak=Math.max(peak,index);const date=new Date(Date.UTC(2026,6,5+i)).toISOString().slice(0,10);daily.push({'日期':date,'外部划转估值':0,'重建权益':10000*index,'净值指数':index,'日收益率':i?ret:null,'回撤':index/peak-1});}const symbols=[['ETH',986.41,-72.34,-19.51,86],['BTC',432.64,-46.83,-11.3,42],['SOL',-154.91,-23.52,-6.26,28],['DOGE',-42.91,-11.5,1.6,19]].map(([name,gross,fees,funding,count])=>({'合约':name+'-USDT-SWAP','平仓毛盈亏':gross,'手续费':fees,'资金费':funding,'期间净盈亏':gross+fees+funding,'成交笔数':count}));const net=symbols.reduce((n,r)=>n+r.期间净盈亏,0),fees=symbols.reduce((n,r)=>n+r.手续费,0),gross=symbols.reduce((n,r)=>n+r.平仓毛盈亏,0),funding=symbols.reduce((n,r)=>n+r.资金费,0);const returns=daily.slice(1).map(r=>r.日收益率),mean=returns.reduce((n,v)=>n+v,0)/returns.length,sd=Math.sqrt(returns.reduce((n,v)=>n+(v-mean)**2,0)/(returns.length-1));const fills=Array.from({length:35},(_,i)=>({'时间':`2026-09-${String(30-Math.floor(i/2)).padStart(2,'0')} 14:30:00`,'产品':'SWAP','合约':symbols[i%4].合约,'买卖':i%2?'卖出':'买入','数量':(i%5+1)*.1,'成交价':[2640,63400,149,.11][i%4],'平仓盈亏':i%2?(i%4===1?13.2:-4.3):0,'手续费':-.28,'费用币种':'USDT'}));return {connected:false,status:'ready',csrf:state.csrf,snapshot:{equity:12368.52,floating:42.68,exposure:24890,orderCount:0,updated:'2026-10-04 14:30:00（示例）',assets:[{'币种':'USDT','权益':12150.42,'现金余额':12150.42,'美元权益':12150.42},{'币种':'ETH','权益':.08,'现金余额':.08,'美元权益':218.1}],funding:[{'币种':'USDT','余额':2000,'可用余额':2000}],positions:[{instId:'ETH-USDT-SWAP',instType:'SWAP',posSide:'net',mgnMode:'cross',pos:'2',avgPx:'2640',markPx:'2694',upl:'10.8',lever:'5',liqPx:'1980'}]},analysis:{ledger:fills.map(r=>({'时间':r.时间,'合约':r.合约,'平仓毛盈亏':r.平仓盈亏,'手续费':r.手续费,'资金费':0,'其他调整':0})),periodCoverage:{bills:[{start:'2026-07-05 00:00:00',end:'2026-10-04 00:00:00'}],fills_SWAP:[{start:'2026-07-05 00:00:00',end:'2026-10-04 00:00:00'}]},roundAvailable:true,roundRange:{start:'2026-07-05',end:'2026-10-03'},riskAvailable:true,range:{start:'2026-07-04 14:30:00',end:'2026-10-04 14:30:00'},daily,symbols,fills,rounds:[],groups:[],months:[{'月份':'2026-07','平仓毛盈亏':438,'手续费':-45,'资金费':-11,'净盈亏':382,'成交笔数':52},{'月份':'2026-08','平仓毛盈亏':523,'手续费':-53,'资金费':-12,'净盈亏':458,'成交笔数':61},{'月份':'2026-09','平仓毛盈亏':gross-961,'手续费':fees+98,'资金费':funding+23,'净盈亏':net-840,'成交笔数':62}],summary:{'合约期间已实现净盈亏USDT':net,'合约平仓毛盈亏USDT':gross,'合约手续费USDT':fees,'合约资金费USDT':funding,'合约成交笔数':175,'完整轮次胜率':.5833,'完整闭合交易轮次':48,'完整轮次盈利因子':1.83,'Taker成交占比':.94,'重建年化夏普':mean/sd*Math.sqrt(365),'重建日收益样本数':89,'重建日收盘最大回撤':Math.min(...daily.map(r=>r.回撤))},coverage:[{'数据集':'示例成交','条目数':35,'完整':true},{'数据集':'示例日净值','条目数':90,'完整':true}],errors:{},caveats:['所有数据由程序生成，用于体验页面功能。卡片、资产、持仓和明细是独立的演示场景，不能当作真实账本核对。','真实接入只接受读取权限；认证信息不写入浏览器存储。','历史净值需通过流水与当前持仓核对后才展示风险指标。','夏普按日收益样本标准差计算，365 天年化；回撤按净值指数计算。']}};}
$('demo').onclick=()=>{clearTimeout(timer);demoMode=true;periodScope=null;periodPreset='all';periodDraft=false;state=demo();page=1;render();selectView('overview');};
async function restoreSaved(){if(busy)return;openedSyncAttempted=true;busy=true;$('restore').disabled=true;$('restore').textContent='正在读取系统保存的账户…';notice('');try{await request('/api/restore',{});await sync();}catch(e){notice(e.message);}finally{busy=false;$('restore').disabled=false;$('restore').textContent='连接已保存账户';render();}}
$('restore').onclick=()=>{autoRestoreAttempted=true;restoreSaved();};
$('forget').onclick=async()=>{autoRestoreAttempted=true;clearTimeout(timer);notice('');try{await request('/api/forget',{});state={connected:false};$('connect-form').reset();await sync();notice('已从本机系统凭据存储删除保存的账户。');}catch(e){notice(e.message);}};
function downloadJSON(value,name){const blob=new Blob([JSON.stringify(value,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function importFeedback(files){return table(files,[{key:'类型'},{key:'读取记录'},{key:'有效新增'},{key:'重复记录'},{key:'API覆盖'},{key:'覆盖开始'},{key:'覆盖结束'},{key:'重复文件',format:v=>v?'是':'否'}]);}
$('history-files').onchange=()=>{preparedImport=null;$('import-history').classList.add('hidden');$('import-preview-table').innerHTML='';render();};
$('import-preview').onclick=async()=>{
 const files=[...$('history-files').files];if(!files.length){$('import-message').textContent='请先选择 ZIP 或 CSV 文件。';return;}
 busy=true;render();$('import-message').textContent='正在验证文件归属与覆盖，尚未写入历史…';
 try{
  if(files.length>12||files.some(f=>f.size>8*1024*1024)||files.reduce((n,f)=>n+f.size,0)>22*1024*1024)throw Error('最多 12 个文件，单个不超过 8 MB，总计不超过 22 MB。');
  const entries=await Promise.all(files.map(file=>new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve({name:file.name,data:String(reader.result).split(',')[1]});reader.onerror=()=>reject(Error('文件读取失败'));reader.readAsDataURL(file);})));
  const result=await request('/api/import-preview',{files:entries});preparedImport=entries;$('import-preview-table').innerHTML=importFeedback(result.files);$('import-message').textContent='账户与文件校验通过。有效新增指预计参与分析的新记录；API 覆盖记录只保存作辅助。确认后导入并重新复核净值。';$('import-history').classList.remove('hidden');
 }catch(e){preparedImport=null;$('import-message').textContent=e.message;}finally{busy=false;render();}
};
$('import-history').onclick=async()=>{if(!preparedImport)return;busy=true;render();try{const result=await request('/api/import',{files:preparedImport});preparedImport=null;$('import-preview-table').innerHTML=importFeedback(result.files);$('import-history').classList.add('hidden');$('history-files').value='';$('import-message').textContent='导入已完成，下表为实际结果；历史净值正在重新复核。';await sync();}catch(e){$('import-message').textContent=e.message;}finally{busy=false;render();}};
$('backup-export').onclick=async()=>{busy=true;render();try{const backup=await request('/api/backup',{});if(new Blob([JSON.stringify(backup)]).size>22*1024*1024)throw Error('账户历史超过当前 22 MB 备份恢复上限，暂不能导出可恢复备份');downloadJSON(backup,`衡量-账户历史备份-${DashboardPeriod.today()}.json`);$('backup-message').textContent='备份已下载，包含当前账户原始历史与覆盖元数据，不含 API 凭据。';}catch(e){$('backup-message').textContent=e.message;}finally{busy=false;render();}};
$('backup-file').onchange=()=>{preparedBackup=null;$('backup-restore').classList.add('hidden');$('backup-preview-table').innerHTML='';render();};
$('backup-preview').onclick=async()=>{const file=$('backup-file').files[0];if(!file){$('backup-message').textContent='请先选择账户历史备份 JSON。';return;}busy=true;render();try{if(file.size>22*1024*1024)throw Error('备份文件不得超过 22 MB');const backup=JSON.parse(await file.text()),result=await request('/api/backup-preview',{backup});preparedBackup=backup;$('backup-preview-table').innerHTML=table([result.preview],[{key:'校验'},{key:'记录数'},{key:'可新增'},{key:'已存在'},{key:'上次同步'},{key:'覆盖窗口'}]);$('backup-message').textContent='校验通过，尚未写入。恢复会合并记录，不覆盖当前已有记录；确认后重新复核历史。';$('backup-restore').classList.remove('hidden');}catch(e){preparedBackup=null;$('backup-message').textContent=e instanceof SyntaxError?'JSON 文件格式无效':e.message;}finally{busy=false;render();}};
$('backup-restore').onclick=async()=>{if(!preparedBackup)return;busy=true;render();try{await request('/api/backup-restore',{backup:preparedBackup});preparedBackup=null;$('backup-restore').classList.add('hidden');$('backup-file').value='';$('backup-message').textContent='备份已合并，正在重新复核净值；重复恢复不会重复计数。';await sync();}catch(e){$('backup-message').textContent=e.message;}finally{busy=false;render();}};
function canonicalBackup(value){if(Array.isArray(value))return '['+value.map(canonicalBackup).join(',')+']';if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonicalBackup(value[k])).join(',')+'}';return JSON.stringify(value);}
$('offline-open').onclick=async()=>{
 if(busy)return;const file=$('offline-file').files[0];if(!file){$('offline-message').textContent='请先选择账户历史备份 JSON。';return;}
 try{
  if(file.size>22*1024*1024)throw Error('文件不得超过 22 MB');const backup=JSON.parse(await file.text());if(backup.format!=='hengliang-account-backup'||backup.version!==1)throw Error('请选择本看板下载的账户历史备份');
  const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(canonicalBackup(backup.payload))),checksum=[...new Uint8Array(digest)].map(v=>v.toString(16).padStart(2,'0')).join('');if(checksum!==backup.sha256)throw Error('备份校验失败，文件可能损坏');
  const analysis=JSON.parse(backup.payload.meta[3]||'null');if(!analysis?.daily||!analysis?.summary)throw Error('备份没有可显示的分析缓存，请连接对应账户恢复后复核');
  clearTimeout(timer);clearTimeout(snapshotTimer);offlineMode=true;demoMode=false;autoRestoreAttempted=true;periodScope=null;periodPreset='all';periodToday=false;periodDraft=false;metricsMarkup='';state={connected:false,csrf:state.csrf,status:'ready',analysis,snapshot:{},backupCreated:backup.created};page=1;render();selectView('overview');
 }catch(e){$('offline-message').textContent=e instanceof SyntaxError?'备份 JSON 无效':e.message;}
};
render();sync();

// 仅提供页面导航工具，不暴露认证信息，也不触发联网、交易或导出。
if (document.modelContext?.registerTool) {
 const lifecycle=new AbortController();
 Promise.resolve(document.modelContext.registerTool({
  name:'navigate_account_dashboard',title:'切换账户分析视图',
  description:'切换当前页面中的概览、资产持仓、交易明细或数据口径视图，不连接或查询新的账户。',
  inputSchema:{type:'object',properties:{view:{type:'string',enum:['overview','positions','trades','quality']}},required:['view'],additionalProperties:false},
  annotations:{readOnlyHint:false,untrustedContentHint:false},
  execute(input){if(!input||Object.keys(input).length!==1||!['overview','positions','trades','quality'].includes(input.view))throw Error('视图参数无效');selectView(input.view);return {view,mode:demoMode?'示例':state.connected?'已接入':'未接入'};}
 },{signal:lifecycle.signal})).catch(()=>{});
 window.addEventListener('pagehide',()=>lifecycle.abort(),{once:true});
}
