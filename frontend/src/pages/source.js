import {api, download} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {actions} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc,time} from '../utils/html.js';

export async function overview() {
  const profile = state.profile;
  const history = await api('/audit');
  return `<section class="summary"><h2>${esc(profile.organization)}</h2><p>${esc(profile.context)}</p><ol>${profile.scenario.map(step => `<li>${esc(step)}</li>`).join('')}</ol><a class="button" href="#1">核对数据</a></section><section><h2>最近操作</h2>${table(history.map(row => ({时间:time(row.at),账号:row.actor,操作:actions[row.action] ?? row.action,记录:row.subject})))}</section>`;
}

function mergeSummary(result) {
  const events = result.events || {};
  const labels = [['applied','采用'],['released','解除隔离'],['quarantined','新隔离'],['stale','忽略旧版'],['duplicates','重复跳过'],['held','仍处隔离']];
  const parts = labels.filter(([key]) => events[key]?.length).map(([key,label]) => `${label} ${events[key].length} 件`);
  const detail = parts.length ? parts.join('，') : '没有可应用的更正';
  if (result.run_id) return `合并完成：${detail}。已按上一分析参数排队重算 #${result.run_id}，算完后结果页一次性切换。`;
  if (result.changed) return `合并完成：${detail}。台账已更新为版本 #${result.id}，尚无分析任务可重算，请提交计算。`;
  return `合并完成：${detail}。有效记录未变化，未触发重算。`;
}

export async function source() {
  const profile = state.profile;
  const revisions = await api('/datasets');
  const active = revisions.find(row => row.id === state.datasetId) || revisions[0];
  const detail = active ? await api(`/datasets/${active.id}`) : null;
  const analyst = state.account.role === 'analyst';
  const importer = analyst ? `<form id="upload" class="inline-form"><label>导入数据文件<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">保存数据版本</button>${errorSlot}</form>` : '';
  const merger = analyst && revisions.length ? `<form id="merge" class="inline-form"><label>补传更正文件<input name="correction" type="file" accept=".csv,text/csv" required></label><button type="submit">合并更正到最新台账</button>${errorSlot}</form><p class="hint">${esc(profile.correction.input)}</p>` : '';
  let version = empty('请导入项目样例或业务 CSV。');
  if (detail) {
    const revisionColumn = profile.correction.column;
    const columns = detail.records.some(row => row[revisionColumn.key] != null) ? [...profile.columns, revisionColumn] : profile.columns;
    version = `<section><label>数据版本<select id="revision">${revisions.map(row => `<option value="${row.id}" ${row.id === active.id ? 'selected' : ''}>${esc(row.name)} · #${row.id}</option>`).join('')}</select></label><p class="hint">${detail.count} 行 · 页面预览前 ${detail.records.length} 行 · SHA-256 ${esc(detail.checksum.slice(0,12))}</p>${table(detail.records,columns)}</section>`;
  }
  const held = Object.entries(detail?.quarantined || {});
  const quarantine = held.length ? `<section><h2>同版冲突隔离</h2><p class="hint">以下耗材因同一校点修订号下记录内容不一致被隔离，暂不参与重算；收到更高修订号后自动解除。</p>${table(held.map(([id,item]) => ({耗材编号:id,隔离修订号:item.revision,冲突记录数:item.contents.length,隔离时间:time(item.at)})))}</section>` : '';
  return `<section><p>${esc(profile.input)}</p><p class="hint">CSV 表头顺序可调整，列名需匹配样例。</p>${importer}<button type="button" id="example" class="secondary">下载样例 CSV</button><p id="download-error" role="alert" class="error"></p></section><section><h2>补传耗材更正</h2>${merger || '<p class="hint">分析员可在此合并校点补传的更正记录。</p>'}</section>${version}${quarantine}`;
}

export function bindSource(root, refresh) {
  bindForm(root, '#upload', async form => {
    const file = form.elements.file.files[0];
    const imported = await api('/datasets', {name:file.name,csv:await file.text()});
    state.datasetId = imported.id;
    state.status = imported.created ? `已保存 ${imported.rows} 行。` : '数据与已有版本一致，已选中该版本。';
  }, refresh);
  bindForm(root, '#merge', async form => {
    const file = form.elements.correction.files[0];
    const merged = await api('/datasets/merge', {name:file.name,csv:await file.text()});
    state.datasetId = merged.id;
    state.status = mergeSummary(merged);
  }, refresh);
  root.querySelector('#revision')?.addEventListener('change', event => {
    state.datasetId = Number(event.target.value);
    refresh();
  });
  root.querySelector('#example')?.addEventListener('click', async () => {
    try {await download('/example.csv', state.profile.filename);}
    catch (error) {root.querySelector('#download-error').textContent = error.message;}
  });
}
