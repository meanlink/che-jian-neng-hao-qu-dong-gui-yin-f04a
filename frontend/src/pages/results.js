import {jobs} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {chart} from '../components/common/chart.js';
import {table} from '../components/common/table.js';
import {status} from '../components/common/status.js';
import {esc,fmt,date,notify} from '../utils/dom.js';

const VERDICTS={passed:'复算通过',failed:'复算未通过',unverifiable:'无法复算'};
const RECHECK_STATES={pending:'等待复核',running:'复核进行中',completed:'复核完成'};

function verification(j){
 const recheck=j.recheck,seal=j.seal;
 const verdict=recheck?.status==='completed'?(VERDICTS[recheck.verdict]||recheck.verdict):(RECHECK_STATES[recheck?.status]||'尚未复核');
 const days=(recheck?.days||[]).filter(d=>!d.ok);
 const sealLine=seal?'算法版本 '+esc(seal.algorithm_version)+' · 封存 '+seal.sample_days+' 天样本 · 留出 '+seal.expected_days+' 天 · 样本指纹 '+esc(seal.sample_checksum.slice(0,18))+'…':'该试验未封存样本、参数与算法版本';
 const differenceRows=days.map(d=>'<tr><td>'+esc(d.day)+'</td><td>'+esc(fmt(d.archived))+'</td><td>'+esc(fmt(d.replayed))+'</td><td>'+esc(d.delta==null?'—':fmt(d.delta))+'</td><td>'+esc(d.note||'超出千分之一度容差')+'</td></tr>').join('');
 const missingReason=recheck?.status==='completed'&&recheck.verdict==='unverifiable'?'<p class="warning">缺证原因：'+esc(recheck.reason)+'</p>':(recheck?.status==='completed'&&!recheck.material_ok&&recheck.verdict==='failed'?'<p class="warning">'+esc(recheck.reason)+'</p>':'');
 const progress=recheck&&recheck.status!=='pending'?'<span>已核对 '+recheck.checked_days+' / '+recheck.expected_days+' 天</span>':'';
 const retryable=!recheck||(recheck.status==='completed'&&recheck.verdict==='unverifiable');
 const action=session.user.role==='admin'&&retryable?'<button class="button small" id="recheck-job">'+(recheck?'补齐证据后重试复算':'启动复算复核')+' ↻</button>':'';
 return '<section class="panel verification"><div class="section-title"><h2>复算证据与逐日核对</h2>'+action+'</div><div class="verification-head"><strong class="verdict '+(recheck?.verdict||'')+'">'+esc(verdict)+'</strong><span>'+sealLine+'</span>'+progress+'</div>'+(recheck?.reason&&recheck.status==='completed'?'<p class="hint">'+esc(recheck.reason)+'</p>':'')+missingReason+(differenceRows?'<div class="table-wrap"><table><thead><tr><th>差异日期</th><th>原留出预测</th><th>复算预测</th><th>偏差 kWh</th><th>说明</th></tr></thead><tbody>'+differenceRows+'</tbody></table></div>':'')+'<p class="hint">千分之一度（'+'0.001 kWh'+'）为单日预测最大允许偏差；材料校验失败、缺日期或超差均判为未通过。复核中断后从已核对日期继续，已有结论不重复登记。</p></section>';
}

export async function results(id){
 workspace.jobs=await jobs.list();const j=id?await jobs.get(id):(workspace.jobs.find(j=>j.id===workspace.selectedJob)||workspace.jobs[0]);
 if(j&&!workspace.jobs.some(x=>x.id===j.id))workspace.jobs.push(j);
 if(!j)return heading('04 / REVIEW','结果复核','尚无分析结果。')+'<div class="empty">先到分析工作台，选择批次并提交一次计算。<a href="#/workbench">前往工作台 ↗</a></div>';
 workspace.selectedJob=j.id;const r=j.result||{};
 return heading('04 / REVIEW','结果复核与业务解释',j.name,'<button class="button" id="refresh-results">刷新结果 ↻</button>')+'<div class="result-selector"><label>分析记录<select id="select-job">'+workspace.jobs.map(x=>'<option value="'+x.id+'" '+(x.id===j.id?'selected':'')+'>'+esc(x.name)+' · '+esc(x.status)+'</option>').join('')+'</select></label>'+status(j.status)+'<span>'+date(j.created_at)+'</span><span class="approval">'+(j.approved?'已获业务批准':'待负责人复核')+'</span></div>'+(j.status==='completed'?'<section class="result-lead"><p class="eyebrow">COMPUTED FINDING</p><h2>'+esc(r.summary)+'</h2><div class="metrics">'+(r.metrics||[]).map(m=>'<div><span>'+esc(m.label)+'</span><strong>'+esc(fmt(m.value))+'<small>'+esc(m.unit||'')+'</small></strong></div>').join('')+'</div></section><section class="panel">'+chart(r.chart)+'</section>'+verification(j)+'<div class="two-columns"><section class="panel"><div class="section-title"><h2>业务解释</h2><span>由计算结果生成</span></div><ol class="questions">'+(r.findings||[]).map(v=>'<li>'+esc(v)+'</li>').join('')+'</ol>'+(r.warnings||[]).map(v=>'<p class="warning">'+esc(v)+'</p>').join('')+'</section><section class="method-sheet compact"><p class="eyebrow">REPRODUCIBILITY</p><h3>计算口径与参数</h3><p>'+esc(r.method)+'</p><pre>'+esc(JSON.stringify(j.params,null,2))+'</pre>'+(session.user.role==='admin'&&!j.approved?'<button class="primary" id="approve-job">批准此业务结论</button>':'')+'<p class="hint">仅批准结果可进入定时分发档案。</p></section></div><section class="panel"><div class="section-title"><h2>结果明细</h2><span>'+(r.table?.rows?.length||0)+' 行</span></div>'+table(r.table?.columns||[],r.table?.rows||[])+'</section>':'<section class="panel empty">'+(j.status==='failed'?'计算失败：'+esc(j.error)+'。请返回工作台调整参数或数据。':'后台正在执行计算。点击刷新结果查看最新进展。')+'<a href="#/workbench">返回工作台 ↗</a></section>');
}
export function bindResults(refresh){
 document.querySelector('#refresh-results')?.addEventListener('click',refresh);
 document.querySelector('#select-job')?.addEventListener('change',e=>{workspace.selectedJob=Number(e.target.value);location.hash='#/results/'+e.target.value;});
 document.querySelector('#approve-job')?.addEventListener('click',async()=>{try{await jobs.approve(workspace.selectedJob);notify('已批准，可创建分发计划');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelector('#recheck-job')?.addEventListener('click',async e=>{const b=e.target;b.disabled=true;try{await jobs.recheck(workspace.selectedJob);notify('复核已登记，后台将逐日核对；中断可从已核日期续跑');await refresh();}catch(error){notify(error.message,true);}finally{b.disabled=false;}});
}
