'use strict';

function workflowNotice(message, error = false) {
  const box = $('#workflow-notice');
  box.textContent = message;
  box.hidden = !message;
  box.classList.toggle('is-error', error);
}

function blockedReason(action, rows) {
  if (!rows.length) return '请先选择账号';
  if (rows.some(row => row.busy) && !['stop'].includes(action)) return '所选账号仍在处理，请等待完成或先停止';
  if (['redeem_auto', 'prepare', 'auto_batch', 'preflight', 'confirm', 'refresh', 'retry'].includes(action) && rows.some(row => row.probe_only)) return '所选账号尚未绑定卡密，先点击“补全资料 / 绑定卡密”';
  if (action === 'confirm' && rows.some(row => row.status !== 'confirm')) return '先点击“登录并预检”或“预检”，核对结果后才能确认兑换';
  if (['session_only', 'redeem_auto', 'prepare', 'auto_batch', 'preflight', 'edge_login'].includes(action) && rows.some(row => row.submitted || row.status === 'completed')) return '所选账号已提交或完成兑换，请查询原卡密状态';
  if (action === 'retry' && rows.some(row => !row.can_retry)) return '服务端尚未确认可重试，请先查询结果';
  if (action === 'edge_login' && rows.length !== 1) return '单独获取 Session 时一次选择一个账号';
  return '';
}

actions = function(row) {
  if (row.busy) return `<button data-row-action="stop" data-id="${row.id}">停止</button>`;
  const button = (action, title, cls = '', disabled = false) => `<button class="${cls}" data-row-action="${action}" data-id="${row.id}" ${disabled ? 'disabled title="请先绑定卡密"' : ''}>${title}</button>`;
  const controls = [];
  if (!row.submitted && row.status !== 'completed') {
    controls.push(button('edit', row.probe_only ? '补全资料 / 绑定卡密' : '修改资料 / 卡密'));
    controls.push(button('session_only', '仅获取 Session'));
    controls.push(button('redeem_auto', '获取 Session 并兑换', 'accent', row.probe_only));
    if (!row.probe_only) controls.push(button('prepare', '仅预检'));
    if (row.status === 'confirm') controls.push(button('confirm', '确认兑换', 'accent'));
    if (row.can_retry) controls.push(button('retry', '重试'));
  }
  if (row.session_valid) controls.push(button('session', '查看 / 复制 Session'));
  controls.push(button('precheck_view', '查看预检'));
  if (!row.probe_only) controls.push(button('refresh', '查询'));
  return controls.join('');
};

const originalRender = render;
render = function() {
  originalRender();
  const rows = [...selected].map(id => state.records.find(row => row.id === id)).filter(Boolean);
  for (const button of document.querySelectorAll('[data-action]')) {
    const reason = blockedReason(button.dataset.action, rows);
    button.disabled = Boolean(reason);
    button.title = reason;
  }
  $('#edit-selected').disabled = rows.length !== 1 || rows[0].busy || rows[0].submitted || rows[0].status === 'completed';
  $('#view-selected-session').disabled = rows.length !== 1 || !rows[0].session_valid;
  $('#workflow-reason').textContent = rows.length ? rows.some(row => row.probe_only)
    ? '已获取的 Session 可以查看和复制；绑定卡密后即可直接预检。'
    : rows.every(row => row.status === 'completed') ? '所选账号已经兑换完成，可以查询结果或查看详情。'
    : rows.some(row => row.submitted) ? '所选卡密已经提交，请查询原卡密状态，不要重复兑换。'
    : rows.every(row => row.status === 'confirm') ? '所选账号已通过预检，可以核对账号与套餐后确认兑换。'
    : rows.some(row => row.busy) ? '任务处理中，进度与错误会显示在账号行及详情中。'
    : '“仅获取 Session”不操作卡密；“获取 Session 并兑换”在确认后自动完成预检和兑换。下方可查看实时结果。'
    : '可只保存账号，或同时保存卡密；选择账号后分别获取 Session 或执行自动兑换。';
  const helper = state.helper || {};
  $('#helper-state').textContent = helper.connected
    ? helper.version.startsWith('0.1') ? '扩展为旧版：在 Edge 扩展页点击“重新加载”，以启用自动填写和验证码功能。现有 Session 可继续使用。'
      : helper.incognito === false ? '扩展已连接，请在扩展详情中勾选“允许在 InPrivate 中使用”。'
      : `扩展已连接 · ${helper.version} · 自动填写和验证码监听可用`
    : '正在等待扩展连接；更新扩展后，打开此工作台标签页即可自动连接。';
};

command = async function(action, ids, reviewed = false) {
  const rows = ids.map(id => state.records.find(row => row.id === id)).filter(Boolean);
  const reason = blockedReason(action, rows);
  if (reason) {workflowNotice(reason, true); return;}
  if (['redeem_auto', 'confirm', 'delete', 'stop'].includes(action) && !reviewed) {review(action, ids); return;}
  try {
    await api('/api/command', {action, ids});
    workflowNotice({session_only: '仅获取 Session 已入队，本次不进行卡密预检或兑换。', redeem_auto: '已确认自动兑换：获取 Session、预检通过后提交并查询结果，请查看下方面板。', edge_login: '已开始获取 Session，已配置的账号将自动填写登录资料和验证码。', prepare: '正在获取 Session 并预检，本次不自动提交兑换。结果显示在下方面板。', auto_batch: '批量登录及预检已入队，每个账号单独显示结果。', confirm: '兑换请求已入队，请查看账号进度。', refresh: '正在查询原卡密状态。', stop: '本地任务已停止，已提交的远端兑换仍需查询。', delete: '本地记录已删除。', retry: '正在核实卡密释放状态，然后重试。'}[action] || '操作已提交');
    await refresh();
  } catch (error) {workflowNotice(error.message, true);}
};

function openAccountEdit(id) {
  const row = state.records.find(item => item.id === id);
  if (!row) return;
  const form = $('#account-edit-form');
  form.reset();
  form.dataset.id = id;
  $('#edit-account-email').textContent = row.email;
  $('#edit-account-status').textContent = `GPT 密码：${row.password_configured ? '已配置' : '未配置'} · 2FA：${row.totp_configured ? '已配置' : '未配置'} · 邮箱取码：${row.mail_configured ? '已配置' : '未配置'} · Session：${row.session_valid ? '将保留当前 Session' : '尚未获取'}`;
  $('#edit-account-months').value = String(row.eligibility_months || '');
  $('#edit-account-card-hint').textContent = row.probe_only ? '当前没有卡密，粘贴卡密后即可继续充值。' : '当前卡密：' + row.card + '；留空保留当前卡密。';
  $('#account-edit-error').textContent = '';
  $('#account-edit-dialog').showModal();
}

$('#account-edit-form').onsubmit = async event => {
  event.preventDefault();
  const form = event.target;
  const row = state.records.find(item => item.id === form.dataset.id);
  const button = event.submitter;
  const nextAction = button.dataset.mode || 'save';
  button.disabled = true;
  try {
    const source = $('#edit-account-source').value.trim();
    const card = $('#edit-account-card').value.trim();
    if (nextAction === 'redeem_auto' && row.probe_only && !card) throw Error('自动兑换需要卡密，请先粘贴卡密；也可以选择只保存或仅获取 Session。');
    const data = {id: row.id, eligibility_months: $('#edit-account-months').value};
    if (source) Object.assign(data, ImportParser.account(source));
    else for (const name of ['password', 'totp', 'mail_url']) {
      const value = form.elements[name].value.trim();
      if (value) data[name] = value;
    }
    if (data.email && data.email.toLowerCase() !== row.email.toLowerCase()) throw Error('粘贴资料的邮箱与当前账号不一致，请检查。');
    if (card) data.code = ImportParser.card(card).code;
    const result = await api('/api/account/update', data);
    form.reset();
    $('#account-edit-dialog').close();
    workflowNotice(result.session_valid ? '资料已保存，当前 Session 已保留。' : '资料已保存，可以开始自动登录。');
    await refresh();
    if (nextAction !== 'save') await command(nextAction, [row.id]);
  } catch (error) {$('#account-edit-error').textContent = error.message;}
  finally {button.disabled = false;}
};

async function openSession(id) {
  try {
    const result = await api('/api/session', {id});
    $('#session-email').textContent = result.email;
    $('#session-value').value = JSON.stringify(result.session);
    $('#session-dialog').showModal();
  } catch (error) {workflowNotice(error.message, true); await refresh();}
}
$('#copy-session').onclick = async () => {
  const field = $('#session-value');
  if (!field.value) return;
  try {
    await navigator.clipboard.writeText(field.value);
    $('#session-copy-status').textContent = '已复制转换后的单行 Session JSON';
  } catch {
    field.focus(); field.select();
    $('#session-copy-status').textContent = document.execCommand('copy') ? '已复制转换后的单行 Session JSON' : '请选择文本并按 Ctrl+C 复制';
  }
};
$('#session-dialog').addEventListener('close', () => {$('#session-value').value = ''; $('#session-copy-status').textContent = '';});

$('#rows').onclick = event => {
  const checkbox = event.target.closest('[data-select]');
  if (checkbox) {checkbox.checked ? selected.add(checkbox.dataset.select) : selected.delete(checkbox.dataset.select); if (checkbox.checked) showPrecheck(checkbox.dataset.select); render(); return;}
  const button = event.target.closest('[data-row-action]');
  if (!button) return;
  const {rowAction: action, id} = button.dataset;
  if (action === 'edit') openAccountEdit(id);
  else if (action === 'session') void openSession(id);
  else if (action === 'precheck_view') showPrecheck(id, true);
  else if (action === 'detail') showDetails(id);
  else if (action === 'eligibility') editEligibility([id]);
  else void command(action, [id]);
};
$('#edit-selected').onclick = () => openAccountEdit([...selected][0]);
$('#view-selected-session').onclick = () => openSession([...selected][0]);

$('#add-form').onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter;
  const nextAction = button.dataset.mode || 'save';
  if (!parseSource('account', false) || !parseSource('card', false)) return;
  button.disabled = true;
  $('#add-account-error').textContent = '';
  try {
    const item = Object.fromEntries(new FormData(event.target));
    if (nextAction === 'redeem_auto' && !item.code.trim()) throw Error('自动兑换需要卡密，请先填写；也可以选择只保存或仅获取 Session。');
    const result = await api('/api/import', {items: [item]});
    event.target.reset();
    $('#add-dialog').close();
    await refresh();
    showPrecheck(result.ids[0]);
    workflowNotice('账号已保存，可单独获取 Session 或绑定卡密后兑换。');
    if (nextAction !== 'save') await command(nextAction, result.ids);
  } catch (error) {$('#add-account-error').textContent = error.message;}
  finally {button.disabled = false;}
};

for (const button of document.querySelectorAll('[data-close]')) button.onclick = () => $('#' + button.dataset.close).close();
render();
