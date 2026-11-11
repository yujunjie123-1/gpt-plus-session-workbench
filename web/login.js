document.querySelector('form').addEventListener('submit',async event=>{
  event.preventDefault();const button=document.querySelector('button'),message=document.querySelector('#message');
  button.disabled=true;message.textContent='正在连接…';
  try{
    const response=await fetch('/mobile/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:document.querySelector('#code').value.trim()}),redirect:'error'});
    const result=await response.json();if(!response.ok)throw Error(result.error||'连接未完成');
    // Mobile uses an HttpOnly cookie; desktop bearer credentials are never stored here.
    sessionStorage.removeItem('workbench-access');location.replace('/');
  }catch(error){message.textContent=error.message;button.disabled=false;}
});
