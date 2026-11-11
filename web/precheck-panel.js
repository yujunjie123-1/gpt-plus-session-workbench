'use strict';

let precheckId = '';

function showPrecheck(id, scroll = false) {
  precheckId = id;
  renderPrecheck();
  if (scroll) $('#precheck-panel').scrollIntoView({behavior: 'smooth', block: 'start'});
}

function subscriptionTime(value) {
  const stamp = Date.parse(value);
  if (!Number.isFinite(stamp)) return {expires: '上游未提供', remaining: '上游未提供'};
  const difference = stamp - Date.now();
  const hours = Math.max(0, Math.floor(difference / 3600000));
  return {expires: date(value), remaining: difference <= 0 ? '已到期' : `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`};
}

function renderPrecheck() {
  const rows = state.records;
  if (!rows.some(row => row.id === precheckId)) precheckId = [...selected][0] || rows[0]?.id || '';
  const chooser = $('#precheck-account');
  const options = rows.map(row => `<option value="${row.id}">${esc(row.email)}</option>`).join('');
  if (chooser.innerHTML !== options) chooser.innerHTML = options;
  chooser.value = precheckId;
  chooser.disabled = !rows.length;
  const row = rows.find(item => item.id === precheckId);
  $('#precheck-empty').hidden = Boolean(row);
  $('#precheck-content').hidden = !row;
  if (!row) return;
  const result = row.precheck || {};
  const hasResult = Boolean(result.checked_at);
  const time = subscriptionTime(result.active_until);
  const facts = [
    ['任务邮箱', row.email], ['预检账号', result.email || '等待账号预检'],
    ['兑换卡密', row.probe_only ? '未绑定卡密' : row.card],
    ['卡密状态', ({unused:'未使用', failed:'上次失败', pending:'处理中', completed:'已使用', blocked:'禁用或过期', unknown:'未知'})[result.card_status] || result.card_status || '尚未查询'],
    ['目标套餐', result.target_plan || '等待卡密资格查询'], ['预检时套餐', result.current_plan || '等待账号预检'],
    ['订阅状态', result.subscription || '上游未提供'], ['套餐到期', time.expires],
    ['当前剩余', time.remaining], ['自动续费', result.will_renew === true ? '到期后自动续费' : result.will_renew === false ? '到期后停用' : '上游未提供'],
    ['Session', row.session_valid ? '已就绪，可查看 / 复制' : row.status === 'completed' ? '兑换完成后已清除' : '尚未获取'],
    ['预检时间', hasResult ? date(result.checked_at) : '尚未预检']
  ];
  $('#precheck-facts').innerHTML = facts.map(([name,value]) => `<div><dt>${esc(name)}</dt><dd>${esc(value)}</dd></div>`).join('');
  const badge = $('#precheck-status');
  badge.className = 'pill ' + row.status;
  badge.textContent = labels[row.status] || row.status;
  $('#precheck-message').textContent = row.message || (row.probe_only ? '可以单独获取 Session；绑定卡密后才能兑换。' : '请选择仅获取 Session、仅预检或自动兑换。');
  $('#precheck-message').classList.toggle('precheck-error', ['error','failed','blocked'].includes(row.status));
  $('#precheck-note').textContent = hasResult && !['preflight','confirm','pending','completed'].includes(row.status)
    ? '以下为最近一次预检结果，实际可否兑换以后续重新预检为准。'
    : row.probe_only ? '账号已保存，尚未绑定卡密。仅获取 Session 不会查询或消耗卡密。'
    : '预检返回的信息会逐步显示；“获取 Session 并兑换”确认后会自动提交，无需再次确认。';
  const progress = [row.session_valid || hasResult, Boolean(result.target_plan), Boolean(result.email), row.submitted || row.status === 'completed'];
  const current = row.status === 'login' ? 0 : result.stage === 'card' || result.stage === 'qualification' ? 1 : result.stage === 'account' ? 2 : row.status === 'pending' ? 3 : -1;
  $('#precheck-steps').innerHTML = ['获取 Session','卡密资格','账号预检','提交及查询'].map((title,index) => `<li class="${progress[index]?'done':row.busy&&current===index?'active':''}"><span>${progress[index]?'✓':index+1}</span>${title}</li>`).join('');
  for (const button of $('#precheck-actions').querySelectorAll('[data-precheck-action]')) {
    const action = button.dataset.precheckAction;
    const reason = action === 'session' ? row.session_valid ? '' : '尚未获取 Session'
      : action === 'edit' ? row.busy || row.submitted ? '当前任务不可编辑' : ''
      : blockedReason(action, [row]);
    button.disabled = Boolean(reason);
    button.title = reason;
  }
  $('#precheck-actions [data-precheck-action=confirm]').hidden = row.status !== 'confirm';
  $('#precheck-log').innerHTML = row.history.slice(-6).reverse().map(item => `<div><time>${esc(date(item.time))}</time><span>${esc(item.message)}</span></div>`).join('') || '<p>尚无处理记录</p>';
}

const renderBeforePrecheck = render;
render = function() {renderBeforePrecheck(); renderPrecheck();};
const commandBeforePrecheck = command;
command = async function(action, ids, reviewed = false) {
  if (ids.length) showPrecheck(ids[0]);
  return commandBeforePrecheck(action, ids, reviewed);
};
const reviewBeforePrecheck = review;
review = function(action, ids) {
  if (action !== 'redeem_auto') return reviewBeforePrecheck(action, ids);
  const rows = ids.map(id => state.records.find(row => row.id === id)).filter(Boolean);
  pendingConfirm = {action, ids};
  $('#confirm-title').textContent = '确认开始自动兑换';
  $('#confirm-content').innerHTML = '<p>确认后自动获取或复用 Session、核验卡密和账号，并在预检通过后直接提交 Plus 兑换、查询结果。提交会消耗下面的卡密，过程中不再弹第二次兑换确认；预检失败的账号会停止并显示原因。</p>'
    + rows.map(row => `<div class="confirm-row">${esc(row.email)}<small>卡密：${esc(row.card)} · 目标：Plus</small></div>`).join('');
  $('#confirm-execute').textContent = `确认开始，自动兑换 ${rows.length} 个账号`;
  $('#confirm-dialog').showModal();
};
$('#precheck-account').onchange = event => showPrecheck(event.target.value);
$('#precheck-actions').onclick = event => {
  const button = event.target.closest('[data-precheck-action]');
  if (!button || !precheckId) return;
  const action = button.dataset.precheckAction;
  if (action === 'edit') openAccountEdit(precheckId);
  else if (action === 'session') void openSession(precheckId);
  else void command(action, [precheckId]);
};
renderPrecheck();
