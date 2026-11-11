// A readiness signal only. No page credential is copied into a message.
void chrome.runtime.sendMessage({type: 'workbench_ready'}).catch(() => {});
