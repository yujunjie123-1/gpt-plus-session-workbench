let widget=null,orderId='DEMO-1024-0';
const $=id=>document.getElementById(id);
async function refreshBooks(){const s=await(await fetch('/api/state',{cache:'no-store'})).json();$('balance').textContent='¥'+s.balance.toFixed(2);$('debits').textContent=s.debits+' 次';$('attempts').textContent=s.attempts+' 次';}
async function openOrder(){
  const state=await(await fetch('/api/state',{cache:'no-store'})).json();orderId=state.orderId;
  if(widget)widget.destroy();
  $('buy').hidden=true;$('paid').hidden=false;
  widget=CDKRedemption.mount({container:'#redemption',orderId,endpoint:'http://127.0.0.1:18766/',development:true,
    async getDelivery(){const response=await fetch('/api/orders/'+encodeURIComponent(orderId)+'/redemption-card',{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw new Error('not ready');return response.json();},
    onStatusChange({status}){$('message').textContent='组件状态：'+status;refreshBooks();},
    onError({message}){$('message').textContent=message;}
  });
}
$('checkout').addEventListener('click',async()=>{const response=await fetch('/api/checkout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scenario:$('scenario').value})});if(response.ok){openOrder();await refreshBooks();}});
$('reopen').addEventListener('click',openOrder);
$('reset').addEventListener('click',async()=>{if(widget)widget.destroy();widget=null;await fetch('/api/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});$('buy').hidden=false;$('paid').hidden=true;$('message').textContent='';await refreshBooks();});
refreshBooks();
