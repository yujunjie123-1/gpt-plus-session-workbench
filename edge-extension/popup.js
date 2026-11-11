'use strict';
async function action(type) {
  const buttons = [...document.querySelectorAll('button')];
  buttons.forEach(button => button.disabled = true);
  try {
    const result = await chrome.runtime.sendMessage({type});
    document.querySelector('#status').textContent = result?.message || '请重新打开扩展';
    document.querySelector('#private-note').hidden = Boolean(result?.incognito);
  } catch {
    document.querySelector('#status').textContent = '扩展尚未就绪，请关闭后重新打开';
  } finally { buttons.forEach(button => button.disabled = false); }
}
for (const id of ['connect', 'scan', 'disconnect']) document.querySelector('#' + id).addEventListener('click', () => action(id));
void action('status');
