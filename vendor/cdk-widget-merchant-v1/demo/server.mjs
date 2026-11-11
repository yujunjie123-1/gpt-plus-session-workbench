// Local-only, synthetic end-to-end demo. Never imports supply credentials or production endpoints.
import http from 'node:http';
import { readFile } from 'node:fs/promises';
const SHOP='http://127.0.0.1:18765', FRAME='http://127.0.0.1:18766';
const CARD='DEMO-5X-ONE-CARD';
let generation=0, ORDER='DEMO-1024-0';
let paid=false, debits=0, attempts=0, queries=0, status='unused', scenario='success';
function json(res,statusCode,data) { res.writeHead(statusCode,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(JSON.stringify(data)); }
async function body(req) { let raw='';for await(const chunk of req){raw+=chunk;if(raw.length>65000)throw new Error('oversize');}return JSON.parse(raw||'{}'); }
async function file(res,path,type,frame=false) {
  try { const data=await readFile(new URL(path,import.meta.url));res.writeHead(200,{'Content-Type':type,'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':frame?"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors http://127.0.0.1:18765; base-uri 'none'; form-action 'none'":"default-src 'self'; script-src 'self'; style-src 'self'; frame-src http://127.0.0.1:18766; connect-src 'self'; base-uri 'none'"});res.end(data); } catch { json(res,404,{error:'NOT_FOUND'}); }
}
function row() {
  if (status==='processing' && scenario!=='review' && ++queries>=2) status='used';
  return {cdk_code:CARD,status,used:status==='used',can_resubmit:status==='unused'||status==='failed',plan:'pro_5x',account_email:attempts?'demo@example.test':'',message:status==='used'?'卡密使用成功':status==='processing'?'卡密兑换处理中':status==='failed'?'使用失败，卡密已重新激活，可以重新提交':'卡密未使用',...(status==='used'?{plus_refund:{state:'recorded',remaining_full_days:10,refund_amount_fen:3000,daily_rate_fen:300}}:{})};
}
const shop=http.createServer(async(req,res)=>{
  const path=new URL(req.url,SHOP).pathname;
  try {
    if(req.method==='GET'&&path==='/')return file(res,'index.html','text/html; charset=utf-8');
    if(req.method==='GET'&&path==='/demo.mjs')return file(res,'demo.mjs','text/javascript; charset=utf-8');
    if(req.method==='GET'&&path==='/demo.css')return file(res,'demo.css','text/css; charset=utf-8');
    if(req.method==='GET'&&path==='/sdk/cdk-widget.js')return file(res,'../sdk/cdk-widget.js','text/javascript; charset=utf-8');
    if(req.method==='GET'&&path==='/api/state')return json(res,200,{orderId:ORDER,paid,debits,balance:300-debits*100,attempts,status,scenario});
    if(req.method==='GET'&&path==='/api/orders/'+ORDER+'/redemption-card')return paid?json(res,200,{orderId:ORDER,code:CARD}):json(res,409,{error:'ORDER_NOT_READY'});
    if(req.method==='POST'&&req.headers.origin!==SHOP)return json(res,403,{error:'ORIGIN_DENIED'});
    if(req.method==='POST'&&path==='/api/checkout') { const data=await body(req);if(!paid){paid=true;debits++;scenario=['success','released','review','network'].includes(data.scenario)?data.scenario:'success';}return json(res,200,{orderId:ORDER}); }
    if(req.method==='POST'&&path==='/api/reset'){paid=false;debits=0;attempts=0;queries=0;status='unused';scenario='success';ORDER='DEMO-1024-'+(++generation);return json(res,200,{ok:true});}
    return json(res,404,{error:'NOT_FOUND'});
  }catch{return json(res,400,{error:'INVALID_REQUEST'});}
});
const frame=http.createServer(async(req,res)=>{
  const path=new URL(req.url,FRAME).pathname;
  try {
    const files={'/': ['provider/index.html','text/html; charset=utf-8'],'/widget.mjs':['provider/widget.mjs','text/javascript; charset=utf-8'],'/core.mjs':['provider/core.mjs','text/javascript; charset=utf-8'],'/widget.css':['provider/widget.css','text/css; charset=utf-8']};
    if(req.method==='GET'&&files[path])return file(res,...files[path],true);
    if(req.method!=='POST'||req.headers.origin!==FRAME)return json(res,403,{error:'ORIGIN_DENIED'});
    const data=await body(req);if(!paid)return json(res,409,{error:'UNPAID'});
    if(path==='/api/lookup'){if(data.codes?.length!==1||data.codes[0]!==CARD)return json(res,400,{error:'INVALID_CDK'});return json(res,200,{results:[row()]});}
    if(path==='/api/preview'){if(data.code!==CARD||!['unused','failed'].includes(status))return json(res,409,{error:'CARD_UNAVAILABLE'});return json(res,200,{code:0,data:{redemption_token:'DEMO-REDEMPTION',plan:'pro_5x'}});}
    if(path==='/api/preflight'){
      let session;try{session=JSON.parse(data.credential?.session);}catch{return json(res,400,{error:'演示只接受提供的测试 Session'});}
      if(session?.sessionToken!=='DEMO-ONLY-NOT-A-REAL-TOKEN')return json(res,400,{error:'演示只接受提供的测试 Session，请勿填写真实凭证'});
      return json(res,200,{code:0,data:{preflight_token:'DEMO-PREFLIGHT-'+attempts,email:'demo@example.test',currentPlan:'plus',subscription_has_active:true,subscription_active_until:new Date(Date.now()+263*3600000).toISOString(),subscription_will_renew:false}});
    }
    if(path==='/api/redeem'){
      if(data.preflight_token!=='DEMO-PREFLIGHT-'+attempts||!['unused','failed'].includes(status))return json(res,409,{error:'INVALID_PREFLIGHT'});
      attempts++;queries=0;status=scenario==='released'&&attempts===1?'failed':'processing';
      if(scenario==='network'){req.socket.destroy();return;}
      return json(res,202,{code:0,data:{order:{status:scenario==='review'?'review':'queued'}}});
    }
    return json(res,404,{error:'NOT_FOUND'});
  }catch{return json(res,400,{error:'INVALID_REQUEST'});}
});
shop.listen(18765,'127.0.0.1',()=>console.log('模拟店铺：'+SHOP));
frame.listen(18766,'127.0.0.1',()=>console.log('模拟兑换组件：'+FRAME));
function stop(){shop.close();frame.close();}
process.on('SIGINT',stop);process.on('SIGTERM',stop);
