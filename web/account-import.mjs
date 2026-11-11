const mailbox = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function mailLink(value) {
  if (!value) return '';
  let url;
  try {url = new URL(value);} catch {throw Error('邮箱取码链接格式不正确');}
  if (url.protocol !== 'https:' || !url.hostname || url.username || url.password || url.hash) {
    throw Error('邮箱取码链接须为不含嵌入凭据或片段的 HTTPS 地址');
  }
  return value;
}

export function parseAccountLine(source) {
  const line = String(source).trim().replace(/^\ufeff/, '');
  // Stop at the domain suffix, so -- within the mailbox local part is safe.
  const boundary = line.match(/^([^\s@]+@[^@\s/]+?\.[a-z\u00a1-\uffff]{2,63})(?=\s*(?:----|--|\/\/|$))/i);
  if (!boundary || !mailbox.test(boundary[1])) throw Error('应为邮箱，或使用 --、----、// 分隔的账号资料');
  const email = boundary[1];
  let rest = line.slice(boundary[0].length).trim();
  if (!rest) return {email};
  const delimiter = rest.match(/^(----|--|\/\/)/)[1];
  rest = rest.slice(delimiter.length);
  const first = rest.indexOf(delimiter);
  let password = (first < 0 ? rest : rest.slice(0,first)).trim(), totp='', mail_url='';
  if (first >= 0) {
    const tail = rest.slice(first + delimiter.length).trim();
    // Never split https:// or delimiters in a URL query.
    if (/^https?:\/\//i.test(tail)) mail_url=tail;
    else {
      const next = tail.indexOf(delimiter);
      totp=(next < 0 ? tail : tail.slice(0,next)).trim();
      if (next >= 0) mail_url=tail.slice(next + delimiter.length).trim();
    }
  }
  if (mail_url && !/^https:\/\//i.test(mail_url)) throw Error('请核对字段数量；邮箱取码链接可省略，分隔符需在同一行保持一致');
  return {email,password,totp,mail_url:mailLink(mail_url)};
}

function csvRows(text) {
  const lines=[];let row=[],cell='',quoted=false;
  for(let index=0;index<text.length;index++) {
    const char=text[index];
    if(char==='"'){if(quoted&&text[index+1]==='"'){cell+='"';index++;}else quoted=!quoted;}
    else if(char===','&&!quoted){row.push(cell);cell='';}
    else if((char==='\n'||char==='\r')&&!quoted){if(char==='\r'&&text[index+1]==='\n')index++;row.push(cell);if(row.some(value=>value.trim()))lines.push(row);row=[];cell='';}
    else cell+=char;
  }
  if(quoted)throw Error('CSV 引号未闭合');
  row.push(cell);if(row.some(value=>value.trim()))lines.push(row);return lines;
}

export function parseAccounts(source) {
  const text=String(source).trim().replace(/^\ufeff/,'');
  if(!text)throw Error('请先粘贴或选择账号文件');
  if(text.startsWith('[')) {
    let items;
    try{items=JSON.parse(text);}catch{throw Error('JSON 格式不正确，请核对账号数组');}
    if(!Array.isArray(items))throw Error('JSON 需为账号数组');return items;
  }
  if(/^email\s*(?:,|$)/i.test(text.split(/\r?\n/)[0])) {
    const lines=csvRows(text),headers=lines.shift().map(value=>value.trim().toLowerCase());
    return lines.map(row=>Object.fromEntries(headers.map((header,index)=>[header,row[index]||''])));
  }
  return text.split(/\r?\n/).map((line,index)=>({line,index})).filter(item=>item.line.trim()).map(({line,index})=>{
    try{return parseAccountLine(line);}catch(error){throw Error(`第 ${index+1} 行：${error.message}`);}
  });
}
