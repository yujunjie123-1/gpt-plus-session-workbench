const admin=location.hash.slice(1)||sessionStorage.getItem('workbench-mobile-admin')||'';
if(admin){sessionStorage.setItem('workbench-mobile-admin',admin);history.replaceState(null,'',location.pathname);}
const message=document.querySelector('#message');
async function request(path,body){
  const response=await fetch(path,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+admin,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});
  const result=await response.json();if(!response.ok)throw Error(result.error||'操作未完成');return result;
}
request('/mobile/admin-state').then(result=>{
  const link=document.querySelector('#address');link.href=result.origin;link.textContent=result.origin;
  document.querySelector('#backend').textContent=result.workbench_running?'电脑工作台已找到。':'请先启动电脑上的充值工作台。';
}).catch(error=>message.textContent=error.message);
document.querySelector('#copy').onclick=async()=>{try{await navigator.clipboard.writeText(document.querySelector('#address').textContent);message.textContent='手机地址已复制。';}catch{message.textContent='请选择上方地址复制。';}};
document.querySelector('#generate').onclick=async()=>{try{const result=await request('/mobile/code',{});document.querySelector('#code').textContent=result.code;message.textContent='请在手机上输入此码，10 分钟内有效。';}catch(error){message.textContent=error.message;}};
document.querySelector('#revoke').onclick=async()=>{try{await request('/mobile/revoke',{});document.querySelector('#code').textContent='';message.textContent='所有手机已退出。可重新生成配对码。';}catch(error){message.textContent=error.message;}};
