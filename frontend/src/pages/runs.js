import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {badge} from '../components/common/status.js';
import {statusLabels} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {submit} from '../hooks/submit.js';
import {date, esc} from '../utils/dom.js';

function maintenancePanel(state) {
  const maintenance = state.maintenance;
  if (!maintenance) return '';
  const reviewer = state.session.role==='reviewer';
  if (!maintenance.stopped) {
    const action = reviewer ? '<form id="stop-form"><button class="secondary" type="submit">停领回放单</button></form>' : '';
    return panel('印务库维护', `<p>当前正常领单。维护前停领后新单照收，已领单办结且凭证入库后报排空。</p>${action}`);
  }
  const progress = maintenance.drained
    ? '<p data-testid="drained">已排空：已领单全部办结，成功凭证均已入库，可由值班员恢复领单。</p>'
    : `<p data-testid="draining">待排空：执行中 ${maintenance.running} 单，待入库凭证 ${maintenance.pending_evidence} 单，排队 ${maintenance.queued} 单。</p>`;
  const action = reviewer ? '<form id="resume-form"><button type="submit">恢复领单</button></form>' : '';
  return panel('印务库维护 · 停领中', `<p>${esc(maintenance.stopped_by)} 于 ${esc(date(maintenance.stopped_at))} 停领，新取号单照常提交。</p>${progress}${action}`);
}

export function runsPage(state, identity) {
  const selected = state.runs.find(row=>row.id===identity) || state.runs[0];
  const cases = new Map(state.cases.map(row=>[row.id, row.title]));
  const rows = state.runs.map(row=>[`<a href="#runs/${row.id}">${esc(cases.get(row.case_id) || row.case_id)}</a>`, badge(row.status), esc(row.owner), esc(date(row.created_at))]);
  const result = state.evidence.find(row=>row.run_id===selected?.id);
  const active = selected && ['queued','running'].includes(selected.status);
  const detail = selected ? `<h2>${esc(cases.get(selected.case_id) || selected.case_id)}</h2>
    <p data-testid="run-status">${badge(selected.status)}</p>
    <dl><dt>启动时间</dt><dd>${esc(date(selected.started_at))}</dd><dt>完成时间</dt><dd>${esc(date(selected.finished_at))}</dd></dl>
    ${selected.error ? `<p class="error">${esc(selected.error)}</p>` : ''}
    ${active?'<p class="note">后台处理中，记录会自动刷新。</p>':''}
    ${result?`<a class="button" href="#evidence/${result.id}">查看业务证据</a>`:''}` : '<h2>执行记录</h2><p>从档案创建一条执行记录后，可在此查看进度。</p>';
  const audit = state.audit.filter(x=>selected && x.reference===String(selected.id) && ['enqueue', 'complete', 'failed'].includes(x.action));
  return `${heading(topic.modules[2], `${state.spec.entity_labels[1]}与后台处理进度`, '<button class="secondary" type="button" id="refresh">刷新记录</button>')}
    ${maintenancePanel(state)}
    <div class="spread"><section>${records(['档案','状态','经办','提交时间'], rows, '暂无执行记录。')}</section><aside class="brief">${detail}</aside></div>
    ${audit.length?panel('操作记录', records(['操作者','操作','时间'], audit.map(x=>[esc(x.actor),esc(statusLabels[x.action]||'提交执行'),esc(date(x.created_at))]))):''}`;
}

export function bindRuns(state, api, update, fail) {
  submit('stop-form', async()=>{ await api('/maintenance/stop', {method:'POST'}); await update(); }, fail);
  submit('resume-form', async()=>{ await api('/maintenance/resume', {method:'POST', data:{epoch:state.maintenance.epoch}}); await update(); }, fail);
}
