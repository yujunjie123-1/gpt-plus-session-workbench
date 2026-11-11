(function(root){
  'use strict';
  function account(text){
    const parts=String(text).trim().split('----');
    if(parts.length!==4)throw Error('账号资料应为：邮箱----GPT密码----2FA密钥----邮箱取码链接');
    const [email,password,totp,mail_url]=parts.map(s=>s.trim());
    if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))throw Error('账号资料中的邮箱格式不正确');
    if(!password||!totp)throw Error('账号资料中的 GPT 密码和 2FA 信息不能为空');
    let url;try{url=new URL(mail_url)}catch{throw Error('邮箱取码链接格式不正确')}
    if(url.protocol!=='https:'||!url.hostname||url.username||url.password||url.hash)throw Error('邮箱取码链接须为不含嵌入用户名、密码或片段的 HTTPS 地址');
    return {email,password,totp,mail_url};
  }
  function card(text){
    const lines=String(text).trim().split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
    let code='',address='';
    if(!lines.length)throw Error('请粘贴卡密资料');
    for(const line of lines){
      const c=line.match(/^(?:(?:卡密|兑换卡密|code)\s*[:：]\s*)?([A-Za-z0-9][A-Za-z0-9_-]{3,255})$/i);
      const a=line.match(/^(?:兑换地址|兑换链接)\s*[:：]\s*(https:\/\/\S+)$/);
      if(c&&!code){code=c[1];continue}
      if(a&&!address){address=a[1];continue}
      throw Error('卡密资料包含重复卡密或无法识别的内容，请每次只粘贴一张卡密');
    }
    if(!code)throw Error('未识别到完整卡密');
    if(address){let url;try{url=new URL(address)}catch{throw Error('兑换地址格式不正确')}
      if(url.origin!=='https://cdk.bar'||url.pathname!=='/'||url.search||url.hash||url.username||url.password)throw Error('兑换地址必须是 https://cdk.bar/');}
    return {code,address:'https://cdk.bar/'};
  }
  const api={account,card};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.ImportParser=Object.freeze(api);
})(globalThis);
