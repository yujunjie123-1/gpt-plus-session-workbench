import React, {useEffect, useMemo, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Badge, Button, Checkbox, Dialog, DialogActions, DialogBody, DialogContent,
  DialogSurface, DialogTitle, Field, FluentProvider, Input, Select, Skeleton,
  SkeletonItem, ToggleButton, Textarea, Switch, createLightTheme, createDarkTheme, createDOMRenderer, RendererProvider, useRestoreFocusTarget} from '@fluentui/react-components';
import {UsersThree, Ticket, CreditCard, Stack, ClockCounterClockwise, UploadSimple,
  MagnifyingGlass, ArrowRight, ArrowClockwise, Sun, Moon, Desktop, Copy, Trash,
  DownloadSimple, WarningCircle, Pause, Play, X, CaretLeft, CaretRight, FileText} from '@phosphor-icons/react';
import {flexRender, getCoreRowModel, getPaginationRowModel, getSortedRowModel, useReactTable} from '@tanstack/react-table';
import {parseAccounts} from '../../web/account-import.mjs';
import './style.css';

const auth = location.hash.slice(1) || sessionStorage.getItem('workbench-access') || '';
const renderer = createDOMRenderer(document, {styleElementAttributes:{nonce:document.querySelector('meta[name="style-nonce"]')?.content || ''}});
if (auth) {sessionStorage.setItem('workbench-access', auth); history.replaceState(null, '', location.pathname + location.search);}
const navigation = [
  ['accounts','账号池',UsersThree], ['risk','风控账号池',WarningCircle], ['extract','Session 提取',FileText], ['cards','卡密管理',Ticket], ['single','单个充值',CreditCard],
  ['batch','批量充值',Stack], ['records','充值记录',ClockCounterClockwise]
];
const titles = Object.fromEntries(navigation.map(([key,label])=>[key,label]));
const copy = {
  accounts:'先把账号存好，需要时再获取 Session 或充值。',
  risk:'暂缓充值的账号单独放在这里，养号后手动移回。',
  extract:'从本地 DOCX 或 TXT 文件提取单个 Session，并按账号池格式复制。',
  cards:'卡密独立入库，按规格分配给充值任务。',
  single:'选择账号和套餐，系统自动分配一张可用卡密。',
  batch:'选择一批账号，为它们分配相同规格的卡密。',
  records:'每次充值的账号、卡密和处理结果都在这里。'
};
const statusLabels = {idle:'待登录',ready:'Session 就绪',queued:'排队中',running:'处理中',login:'正在登录',
  preflight:'正在预检',confirm:'待确认',pending:'处理中',completed:'充值成功',error:'处理异常',failed:'充值失败',
  blocked:'不可充值',stopped:'已停止',unknown:'待核实',unused:'未使用',reserved:'占用中',used:'已使用',exception:'异常'};
const cardCheckErrorLabels = {
  PARTNER_CONFIGURATION_MISSING:'未配置 CDK_PARTNER_PASSWORD。请设置 Windows 用户环境变量并完全重启工作台后重试。',
  AUTHENTICATION_FAILED:'Partner API 授权失败，请检查本机授权配置',
  RATE_LIMITED:'兑换服务限流，稍后再核对',
  UPSTREAM_TIMEOUT:'兑换服务响应超时，未改变本地卡密状态',
  UPSTREAM_UNAVAILABLE:'兑换服务暂不可用，未改变本地卡密状态',
  NETWORK_ERROR:'兑换服务网络暂不可用，未改变本地卡密状态',
  INVALID_RESPONSE:'兑换服务返回格式异常，未改变本地卡密状态',
  REQUEST_REJECTED:'兑换服务拒绝了核对请求，未改变本地卡密状态',
  CARD_NOT_FOUND:'上游查无此卡密或未识别卡密，请核对卡密和服务配置',
  INVALID_LOOKUP_REQUEST:'上游拒绝核对请求，未改变本地卡密状态',
  NOT_RESUBMITTABLE:'上游未明确允许重新兑换，卡密保留为异常状态',
  STATE_CONFLICT:'此前已确认使用，本次结果不一致，仍保持已使用',
  CHECK_FAILED:'核对暂未成功，未改变本地卡密状态，请稍后重试',
};
const productNames = {plus_half:'半价 Plus',plus:'Plus',pro100:'Pro 100 (5×)',pro200:'Pro 200 (10×)',pro500:'Pro 500 (25×)'};
const taskProductLabel = task => task.provider_product && task.provider_plan
  ? `${task.provider_product} / ${task.provider_plan}`
  : productNames[task.sku] || task.sku || '上游未提供';
const activeStatuses = new Set(['queued','running','login','preflight','confirm','pending']);
const empty = {accounts:[],cards:[],tasks:[],products:{},helper:{},paused:false,limit:1,session_limit:5,session_active:0,session_queued:0};
const brand={10:'#061710',20:'#0c2318',30:'#11301f',40:'#153d27',50:'#194a30',60:'#1d573a',70:'#216043',80:'#27664f',90:'#36785c',100:'#4c8c71',110:'#609d83',120:'#71b296',130:'#93cfb2',140:'#b1dfc4',150:'#d1ecdc',160:'#e8f6ee'};
const green = {colorBrandBackground:'#27664f',colorBrandBackgroundHover:'#205640',colorBrandBackgroundPressed:'#194631',
  colorBrandForeground1:'#205e48',colorBrandForeground2:'#205e48',colorBrandStroke1:'#27664f',colorBrandBackground2:'#e8f2ec'};
const darkGreen = {colorBrandBackground:'#71b296',colorBrandBackgroundHover:'#86c4a9',colorBrandBackgroundPressed:'#609d83',
  colorBrandForeground1:'#93cfb2',colorBrandForeground2:'#a0d8bd',colorBrandStroke1:'#71b296',colorBrandBackground2:'#203d31',colorNeutralForegroundOnBrand:'#10251b'};
const time = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '暂无记录';
const beijing = value => value ? new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '未记录';
const labelPlan = value => ({free:'Free',plus:'Plus',pro:'Pro',team:'Team',business:'Business'})[String(value).toLowerCase()] || '未核实';
const charged = row => Boolean(row.has_recharged || row.last_successful_recharge || row.last_recharge?.status==='completed');
const canRecharge = row => row.recharge_eligible===true && !row.busy;
const heldFor = value => Math.floor(Math.max(0,Date.now()-(Date.parse(value)||Date.now()))/86400000);

class AppErrorBoundary extends React.Component {
  state={failed:false};
  static getDerivedStateFromError(){return {failed:true};}
  render(){return this.state.failed?<main className="recovery-screen" role="alert"><h1>页面出现异常</h1><p>账号、卡密和充值记录保存在本机后端。请重新打开页面继续。</p><button onClick={()=>location.assign(location.pathname)}>重新打开工作台</button></main>:this.props.children;}
}

async function api(path, body) {
  const response = await fetch(path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+auth,
    ...(body===undefined?{}:{'Content-Type':'application/json'})},...(body===undefined?{}:{body:JSON.stringify(body)})});
  let payload; try {payload=await response.json();} catch {throw Error('本地服务暂不可用，请检查工作台是否仍在运行');}
  if (!response.ok) throw Error(payload.error || '操作未完成，请稍后重试');
  return payload;
}

function legacyCopy(text) {
  const area=document.createElement('textarea');
  area.value=text; area.readOnly=true;
  area.style.position='fixed'; area.style.opacity='0'; area.style.pointerEvents='none';
  document.body.appendChild(area); area.focus(); area.select();
  let copied=false;
  try {copied=document.execCommand('copy');}
  catch {copied=false;}
  area.remove();
  return copied;
}

async function copyText(text) {
  try {
    if(typeof navigator.clipboard?.writeText==='function') {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch {}
  if(!legacyCopy(text)) throw Error('clipboard unavailable');
}

async function copyFromRequest(request, getText) {
  if(typeof navigator.clipboard?.write==='function'&&typeof ClipboardItem==='function'&&typeof Blob==='function') {
    let write;
    try {
      write=navigator.clipboard.write([new ClipboardItem({
        'text/plain':request.then(result=>new Blob([getText(result)],{type:'text/plain'}))
      })]);
    } catch(error) {
      write=Promise.reject(error);
    }
    const [response,clipboard]=await Promise.allSettled([request,write]);
    if(response.status==='rejected') throw response.reason;
    return {result:response.value,copied:clipboard.status==='fulfilled'};
  }
  const result=await request;
  try {await copyText(getText(result)); return {result,copied:true};}
  catch {return {result,copied:false};}
}

async function extractSessionFile(file, type) {
  const response=await fetch('/api/session/extract',{method:'POST',headers:{Authorization:'Bearer '+auth,
    'Content-Type':'application/octet-stream','X-Session-File-Type':type},body:file});
  let payload;try{payload=await response.json();}catch{throw Error('本地解析服务暂不可用，请检查工作台是否仍在运行');}
  if(!response.ok)throw Error(payload.error||'未能提取 Session');
  return payload;
}

function SessionExtractPage() {
  const picker=useRef(null);
  const [session,setSession]=useState(null),[fileName,setFileName]=useState(''),[error,setError]=useState(''),[chargeError,setChargeError]=useState(''),[status,setStatus]=useState(''),[chargeStatus,setChargeStatus]=useState(''),[busy,setBusy]=useState(false),[dragging,setDragging]=useState(false);
  const [cardCode,setCardCode]=useState(''),[quote,setQuote]=useState(null),[chargeTask,setChargeTask]=useState(null),[chargeBusy,setChargeBusy]=useState(false);
  async function processFile(file) {
    setSession(null);setError('');setStatus('');
    if(!file)return;
    const suffix=file.name.split('.').pop().toLowerCase();
    if(!['docx','txt'].includes(suffix)){setFileName('');setError('仅支持 .docx 和 .txt 文件');return;}
    if(file.size>10*1024*1024){setFileName('');setError('文件超过 10 MiB 上限');return;}
    setFileName(file.name);setBusy(true);
    try{setSession(await extractSessionFile(file,suffix));}
    catch(problem){setError(problem.message);}
    finally{setBusy(false);}
  }
  function clear(){setSession(null);setFileName('');setError('');setChargeError('');setStatus('');setChargeStatus('');setCardCode('');setQuote(null);setChargeTask(null);if(picker.current)picker.current.value='';}
  async function copy(){
    if(!session)return;
    try{await navigator.clipboard.writeText(session.value);setStatus('Session 已复制');}
    catch{setStatus('请选中文本后按 Ctrl+C 复制');document.querySelector('[aria-label="提取的单行 Session"]')?.focus();}
  }
  async function prepareCharge(){
    if(!session||!cardCode.trim())return;
    setChargeBusy(true);setChargeError('');setQuote(null);setChargeStatus('正在核对卡密状态、上游产品和 Session 邮箱，请稍候…');
    try{const result=await api('/api/session/direct-preview',{session:session.value,code:cardCode.trim()});setQuote(result);setChargeStatus('预检通过，请核对上游返回的产品、套餐和当前账号信息后确认直充。');}
    catch(problem){setChargeError(problem.message);setChargeStatus('直充预检未完成，未提交兑换。');}
    finally{setChargeBusy(false);}
  }
  async function startCharge(){
    if(!quote?.quote)return;
    setChargeBusy(true);setChargeError('');setChargeStatus('正在提交兑换，提交结果未确认前不会重复发送。');
    try{const result=await api('/api/session/direct-start',{quote:quote.quote});setChargeTask(result.task);setQuote(null);setChargeStatus(result.task?.message||'兑换已提交，正在查询上游结果。');}
    catch(problem){setChargeError(problem.message);setChargeStatus('直充未提交，请根据错误提示处理后重试。');}
    finally{setChargeBusy(false);}
  }
  async function refreshCharge(){
    if(!chargeTask?.id)return;
    try{const result=await api('/api/session/direct-task',{id:chargeTask.id,action:'refresh'});if(result.task){setChargeTask(result.task);setChargeStatus(result.task.message||'正在查询上游结果。');}}
    catch(problem){setChargeError(problem.message);setChargeStatus('查询原交易失败，后台会继续按原交易重试查询。');}
  }
  useEffect(()=>{if(!chargeTask||['completed','failed','blocked'].includes(chargeTask.status))return;const timer=setInterval(refreshCharge,3000);return()=>clearInterval(timer);},[chargeTask]);
  return <section className="session-extract" aria-label="Session 提取内容">
    <div className="extract-panel">
      <div className="extract-intro"><span className="extract-icon"><FileText size={25}/></span><div><h2>从文档提取 Session</h2><p>选择或拖入一个 DOCX / TXT 文件，提取后可复制为账号池使用的紧凑单行 JSON。</p></div></div>
      <div className={'session-dropzone '+(dragging?'dragging':'')} onDragEnter={event=>{event.preventDefault();setDragging(true);}} onDragOver={event=>event.preventDefault()} onDragLeave={event=>{if(!event.currentTarget.contains(event.relatedTarget))setDragging(false);}} onDrop={event=>{event.preventDefault();setDragging(false);processFile(event.dataTransfer.files?.[0]);}}>
        <FileText size={32}/><strong>将 DOCX 或 TXT 文件拖到这里</strong><span>单个文件 · 最大 10 MiB</span>
        <div className="extract-actions"><Button appearance="primary" icon={<UploadSimple/>} disabled={busy} onClick={()=>picker.current?.click()}>{busy?'正在解析':'选择文件'}</Button>{fileName&&<Button appearance="subtle" disabled={busy} onClick={clear}>清除</Button>}</div>
        <input ref={picker} className="visually-hidden" type="file" accept=".docx,.txt" aria-label="选择 DOCX 或 TXT 文件" onChange={event=>{const file=event.target.files?.[0];event.target.value='';processFile(file);}}/>
      </div>
      {fileName&&<p className="extract-file" role="status">当前文件：{fileName}</p>}
      {error&&<p className="inline-error" role="alert">{error}</p>}
      {session&&<div className="extract-result"><div className="extract-result-heading"><div><span className="eyebrow">已提取</span><h3>{session.email}</h3></div><span className="extract-copy-status" role="status">{status}</span></div><Textarea aria-label="提取的单行 Session" className="session-value" value={session.value} readOnly rows={8} spellCheck={false} onFocus={event=>event.target.select()}/><div className="extract-actions"><Button appearance="primary" icon={<Copy/>} onClick={copy}>复制 Session</Button><Button appearance="subtle" onClick={clear}>清除结果</Button></div><p className="field-note">此处只完成格式提取，不代表 Session 已通过服务端验证；关闭或清除页面后结果即被丢弃。</p>
        <div className="direct-charge"><h3>使用提取的 Session 直接充值</h3><p className="field-note">直充只保留充值历史，不会自动加入账号池；Session 仅在本次任务内存中使用。</p><Field label="手动输入卡密"><Input aria-label="直充卡密" value={cardCode} onChange={(_,value)=>setCardCode(value.value)} placeholder="输入完整卡密" autoComplete="off"/></Field><Button appearance="primary" disabled={chargeBusy||!cardCode.trim()} onClick={prepareCharge}>{chargeBusy?'正在预检':'预检卡密并充值'}</Button>{chargeStatus&&<p className="direct-feedback" role="status">{chargeStatus}</p>}{chargeError&&<p className="inline-error" role="alert">{chargeError}</p>}{quote&&<div className="direct-quote"><p>预检通过：{quote.summary.email} · {quote.summary.product} / {quote.summary.plan} · 当前套餐 {quote.summary.current_plan}</p><Button appearance="primary" disabled={chargeBusy} onClick={startCharge}>{chargeBusy?'正在提交':'确认直充'}</Button><Button disabled={chargeBusy} onClick={()=>{setQuote(null);setChargeStatus('已取消本次预检，未提交兑换。');}}>取消</Button></div>}{chargeTask&&<div className="direct-task"><Status value={chargeTask.status}/><span>{chargeTask.message}</span><Button appearance="subtle" onClick={refreshCharge}>查询结果</Button></div>}</div>
      </div>}
    </div>
  </section>;
}

function Status({value, text}) {
  const tone = ['completed','used','ready'].includes(value)?'success':['error','failed','blocked','exception'].includes(value)?'danger':
    ['pending','login','running','preflight','queued','reserved'].includes(value)?'warning':'informative';
  return <Badge appearance="tint" color={tone} shape="rounded" className="status-badge">{text || statusLabels[value] || value}</Badge>;
}

function SessionStatus({account}) {
  const info=account.session_info||{},state=info.state||'missing';
  const labels={missing:'未获取',usable:'可用（本地检查）',expiring:'即将过期',expired:'已过期',invalid:'已失效'};
  return <div className="session-facts">
    <Status value={state==='usable'?'ready':state==='expiring'?'pending':['expired','invalid'].includes(state)?'error':'idle'} text={labels[state]}/>
    {account.busy&&<small className="cell-small">{statusLabels[account.status]||'处理中'}</small>}
    <small>获取：{beijing(info.acquired_at)}</small>
    <small>标注到期：{beijing(info.session_expires_at)}</small>
    <small>令牌到期：{info.access_token_expires_at?beijing(info.access_token_expires_at):'未知'}</small>
    <small>{info.verification==='provider_accepted'?`兑换预检通过：${beijing(info.verified_at)}`:info.verification==='provider_rejected'?`服务端已拒绝：${beijing(info.verified_at)}`:'服务端：尚未验证'}</small>
    {info.next_retry_at&&<small>下次重试：{beijing(info.next_retry_at)}</small>}
    {info.manual_required&&<small className="error-text">{info.refresh_message||'需要人工处理'}</small>}
  </div>;
}

function Table({rows, columns, selection, onSelection, emptyTitle, emptyText, pageSize=15}) {
  const [sorting,setSorting]=useState([]);
  const table=useReactTable({data:rows,columns,getRowId:row=>row.id,
    state:{sorting,...(selection?{rowSelection:selection}:{})},onSortingChange:setSorting,
    onRowSelectionChange:onSelection,enableRowSelection:Boolean(onSelection),
    getCoreRowModel:getCoreRowModel(),getSortedRowModel:getSortedRowModel(),getPaginationRowModel:getPaginationRowModel(),
    initialState:{pagination:{pageSize}},autoResetPageIndex:false});
  useEffect(()=>{if(table.getState().pagination.pageIndex>=table.getPageCount()) table.setPageIndex(0);},[rows.length]);
  return <div className="data-table">
    <div className="table-scroll"><table>
      <thead>{table.getHeaderGroups().map(group=><tr key={group.id}>
        {onSelection&&<th className="selection-cell"><Checkbox aria-label="选择本页全部" checked={table.getIsAllPageRowsSelected()?true:table.getIsSomePageRowsSelected()?'mixed':false} onChange={(_,data)=>table.toggleAllPageRowsSelected(data.checked===true)}/></th>}
        {group.headers.map(header=><th key={header.id} style={{width:header.column.columnDef.size}}>
          {header.column.getCanSort()?<button className="sort-button" onClick={header.column.getToggleSortingHandler()}>{flexRender(header.column.columnDef.header,header.getContext())}<span>{header.column.getIsSorted()==='asc'?' ↑':header.column.getIsSorted()==='desc'?' ↓':''}</span></button>:flexRender(header.column.columnDef.header,header.getContext())}
        </th>)}
      </tr>)}</thead>
      <tbody>{table.getRowModel().rows.map(row=><tr key={row.id} data-selected={row.getIsSelected()}>
        {onSelection&&<td className="selection-cell"><Checkbox aria-label={'选择 '+(row.original.email||row.original.masked||row.original.sequence)} checked={row.getIsSelected()} onChange={(_,data)=>row.toggleSelected(data.checked===true)}/></td>}
        {row.getVisibleCells().map(cell=><td key={cell.id} data-label={cell.column.columnDef.header} className={cell.column.id==='actions'?'action-cell':''}>{flexRender(cell.column.columnDef.cell,cell.getContext())}</td>)}
      </tr>)}</tbody>
    </table></div>
    {!rows.length&&<div className="empty-state"><div className="empty-mark"><Stack size={28}/></div><h3>{emptyTitle||'还没有内容'}</h3><p>{emptyText||'导入资料后会自动排列在这里。'}</p></div>}
    <div className="table-footer"><span>共 {rows.length} 条{selection&&Object.values(selection).filter(Boolean).length?`，已选 ${Object.values(selection).filter(Boolean).length} 条`:''}</span><div className="pagination"><Button appearance="subtle" size="small" icon={<CaretLeft/>} aria-label="上一页" disabled={!table.getCanPreviousPage()} onClick={()=>table.previousPage()}/><span>{rows.length?table.getState().pagination.pageIndex+1:0} / {table.getPageCount()}</span><Button appearance="subtle" size="small" icon={<CaretRight/>} aria-label="下一页" disabled={!table.getCanNextPage()} onClick={()=>table.nextPage()}/></div></div>
  </div>;
}

function Modal({title,children,onClose,actions,wide=false}) {
  const returnFocus=useRef(document.activeElement);
  useEffect(()=>()=>{requestAnimationFrame(()=>{
    if(document.querySelector('[role="dialog"]'))return;
    const target=returnFocus.current?.isConnected?returnFocus.current:document.querySelector('nav [aria-current="page"]');
    target?.focus();
  });},[]);
  return <Dialog open modalType="modal" onOpenChange={(_,data)=>{if(!data.open)onClose();}}><DialogSurface className={wide?'modal-wide':''}>
    <DialogBody><DialogTitle action={<Button appearance="subtle" icon={<X size={22}/>} aria-label="关闭" onClick={onClose}/>}>{title}</DialogTitle>
      <DialogContent>{children}</DialogContent>{actions&&<DialogActions>{actions}</DialogActions>}</DialogBody>
  </DialogSurface></Dialog>;
}

// These controls filter data rather than switch ARIA tab panels. Toggle buttons
// keep native Tab navigation without a second keyboard-navigation focus group.
function FilterTabs({selectedValue,onTabSelect,children,className=''}) {
  return <div role="group" aria-label="筛选条件" className={'filter-tabs '+className}>{React.Children.map(children,child=>React.cloneElement(child,{selected:child.props.value===selectedValue,onSelect:()=>onTabSelect(null,{value:child.props.value})}))}</div>;
}
function FilterTab({selected,onSelect,disabled,children}) {
  return <ToggleButton className="filter-tab" appearance="transparent" checked={selected} onClick={onSelect} disabled={disabled}>{children}</ToggleButton>;
}

function parseCards(source) {
  const text=source.trim();if(!text)throw Error('请先粘贴或选择卡密文件');
  if(text.startsWith('[')){const items=JSON.parse(text);return items.map(item=>typeof item==='string'?item:item.code);}
  return text.split(/\r?\n/).map(value=>value.trim()).filter(Boolean).filter(value=>{
    if(/^(兑换地址|兑换链接)\s*[:：]/.test(value)){const url=value.replace(/^[^:：]+[:：]\s*/,'');if(url!=='https://cdk.bar/'&&url!=='https://cdk.bar')throw Error('兑换地址需为 https://cdk.bar/');return false;}return value.toLowerCase()!=='code';
  }).map(value=>value.replace(/^(卡密|兑换卡密|code)\s*[:：]\s*/i,''));
}

function ImportDialog({kind,initialSku,onClose,onDone,products}) {
  const [source,setSource]=useState(''),[sku,setSku]=useState(initialSku||'plus'),[group,setGroup]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const file=useRef();
  const isAccount=kind!=='cards', isRisk=kind==='risk-accounts';
  async function submit(){setBusy(true);setError('');try {
    const body=isAccount?{items:parseAccounts(source),group}:{codes:parseCards(source),sku};
    const result=await api('/api/inventory/'+kind,body);await onDone(`已导入 ${result.imported} 条${result.moved?`，已移入风控池 ${result.moved} 条`:''}${result.skipped?`，跳过重复 ${result.skipped} 条`:''}`);onClose();
  }catch(error){setError(error.message);}finally{setBusy(false);}}
  return <Modal title={isRisk?'添加风控账号':isAccount?'导入账号':'导入卡密'} onClose={onClose} wide actions={<><Button onClick={onClose}>取消</Button><Button appearance="primary" disabled={busy||!source.trim()} onClick={submit}>{busy?'正在保存':isRisk?'添加到风控池':'确认导入'}</Button></>}>
    <p className="dialog-description">{isRisk?'每行一个邮箱或账号资料。已有普通账号会移入风控池，原有密码和密钥保持不变；不会发起充值。':isAccount?'每行一个账号，导入后自动编号，同邮箱会跳过。':'选择卡密规格，每行粘贴一张卡密，重复卡密会跳过。'}</p>
    <div className="form-row"><Field label={isAccount?'资格标签（可选）':'卡密规格'}>
      <Select size="large" value={isAccount?group:sku} onChange={(_,data)=>isAccount?setGroup(data.value):setSku(data.value)}>
        {isAccount?<><option value="">未分类</option><option value="3">3个月资格</option><option value="1">1个月资格</option></>:Object.entries(products).map(([key,value])=><option key={key} value={key}>{value.label}</option>)}
      </Select></Field><div className="file-action"><Button icon={<UploadSimple/>} onClick={()=>file.current.click()}>选择文件</Button><input ref={file} type="file" accept=".txt,.csv,.json" hidden onChange={async event=>{const picked=event.target.files[0];if(picked){if(picked.size>2000000){setError('文件过大，请拆分为每批 500 条');return;}setSource(await picked.text());}}}/></div></div>
    <Field label={isAccount?'账号资料':'卡密资料'} hint={isAccount?'可只填邮箱，或邮箱＋密码＋2FA；2FA 和接码链接均可省略。支持 --、----、// 分隔（同一行保持一致），也支持 CSV / JSON。密码含分隔符时请用 CSV / JSON。':'支持纯卡密，或含“卡密：”“兑换地址：”的文本。'}>
      <Textarea resize="vertical" rows={9} value={source} onChange={(_,data)=>setSource(data.value)} className="import-source" autoComplete="off" spellCheck={false} placeholder={isAccount?'邮箱--GPT密码--2FA密钥（接码链接可不填）':'每行一张卡密'}/>
    </Field>{error&&<p className="inline-error" role="alert">{error}</p>}
  </Modal>;
}

function CardSpecDialog({card,onClose,onSaved}) {
  const [sku,setSku]=useState(card.sku),[pending,setPending]=useState(false),[error,setError]=useState('');
  const originalSku=useRef(card.sku);
  async function save(){setPending(true);setError('');try{await api('/api/inventory/edit-card-spec',{id:card.id,sku,expected_sku:originalSku.current});onSaved(sku);}catch(error){setError(error.message);}finally{setPending(false);}}
  return <Modal title="修改卡密规格" onClose={()=>{if(!pending)onClose();}} actions={<><Button disabled={pending} onClick={onClose}>取消</Button><Button appearance="primary" disabled={pending||!card.spec_editable||sku===originalSku.current} onClick={save}>{pending?'正在保存':'保存规格'}</Button></>}>
    <p className="dialog-description">卡密 {card.masked}，原规格：{productNames[originalSku.current]}。仅更正未使用卡密的库存分类，不验证兑换资格，不开始充值，已有充值记录保留原规格。</p>
    <Field label="新的卡密规格"><Select size="large" value={sku} onChange={(_,value)=>setSku(value.value)}>{Object.entries(productNames).map(([key,label])=><option key={key} value={key}>{label}</option>)}</Select></Field>
    <p className="field-note">半价 Plus 与普通 Plus 分开管理；充值时仍须与上游返回的套餐精确匹配。</p>
    {!card.spec_editable&&<p className="inline-error" role="alert">{card.spec_edit_block_reason}</p>}{error&&<p className="inline-error" role="alert">{error}</p>}
  </Modal>;
}

function Progress({task}) {
  if(!task)return <div className="progress-empty"><CreditCard size={34}/><h3>准备好后，一键开始充值</h3><p>开始后会在这里显示登录、预检和兑换结果。</p></div>;
  const result=task.precheck||{},done=task.status==='completed';
  const steps=[['获取 Session',Boolean(result.checked_at)||done],['核验账号和卡密',result.stage==='passed'||task.submitted||done],['提交兑换',task.submitted||done],['查询结果',done]];
  return <div className="task-progress" data-testid="task-progress"><div className="progress-title"><div><h3>{task.email}</h3><p>{taskProductLabel(task)} <span className="separator">/</span> 卡密 {task.card}</p></div><Status value={task.status}/></div>
    <ol className="task-steps">{steps.map(([label,passed])=><li key={label} className={passed?'passed':''}><span>{passed?'✓':''}</span>{label}</li>)}</ol>
    <div className={'task-message '+(['error','failed','blocked'].includes(task.status)?'error':'')} role="status">{task.message}</div>
    {done&&<dl className="result-facts"><div><dt>自动归属邮箱</dt><dd>{task.result_email||task.email}</dd></div><div><dt>上游返回邮箱</dt><dd>{task.provider_result_email||'上游未提供'}</dd></div></dl>}
    {task.risk&&<div className="risk-explanation"><Status value="blocked" text="已移入风控池"/><span>{task.risk.reason} · {time(task.risk.since)}</span></div>}
    {result.checked_at&&<dl className="result-facts">{[['预检邮箱',result.email||'等待返回'],['目标套餐',result.target_plan||'等待返回'],['预检时套餐',labelPlan(result.current_plan)],['订阅状态',result.subscription||'等待返回'],['到期时间',result.active_until&&result.active_until!=='上游未提供'?time(result.active_until):'上游未提供'],['自动续费',result.will_renew===true?'已开启':result.will_renew===false?'未开启':'上游未提供']].map(([key,value])=><div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>}
    <details className="logs"><summary>查看操作记录</summary><div>{(task.history||[]).slice().reverse().map((item,index)=><div className="log-line" key={index}><time>{time(item.time)}</time><span>{item.message}</span></div>)}</div></details>
  </div>;
}

function AccountDetails({account,onClose,onChange,onSession,onViewSession,onRisk}) {
  const [editing,setEditing]=useState(false),[password,setPassword]=useState(''),[totp,setTotp]=useState(''),[mail,setMail]=useState(''),[group,setGroup]=useState(String(account.eligibility_months||'')),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  async function save(){setBusy(true);setError('');try{await api('/api/inventory/edit-account',{id:account.id,password,totp,mail_url:mail,eligibility_months:group});await onChange('账号资料已更新');setEditing(false);setPassword('');setTotp('');setMail('');}catch(error){setError(error.message);}finally{setBusy(false);}}
  return <Modal title="账号详情" onClose={onClose} actions={<Button onClick={onClose}>关闭</Button>}>
    <h3 className="account-detail-email">{account.email}</h3><dl className="detail-facts">{[['编号',String(account.display_sequence||account.sequence||'')],['当前套餐',labelPlan(account.current_plan)],['GPT密码',account.password_configured?'已配置':'未配置'],['2FA密钥',account.totp_configured?'已配置':'未配置'],['邮箱取码',account.mail_configured?'已配置':'未配置'],['资格标签',account.eligibility_months?account.eligibility_months+'个月资格':'未分类']].map(([key,value])=><div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
    <div className="detail-actions"><Button appearance="primary" disabled={account.busy} onClick={()=>onSession([account.id])}>获取 Session</Button><Button disabled={!account.session_valid} onClick={()=>onViewSession(account.id)}>查看 / 复制 Session</Button><Button disabled={account.busy} onClick={()=>setEditing(!editing)}>修改资料</Button></div>
    {account.risk?.active&&<div className="risk-detail"><Status value="blocked" text="风控暂缓"/><p>{account.risk.reason}</p><p>移入时间：{time(account.risk.since)} · 已养号 {heldFor(account.risk.since)} 天</p></div>}
    <div className="detail-actions risk-detail-actions"><Button disabled={account.busy} onClick={()=>onRisk([account.id],!account.risk?.active)}>{account.risk?.active?'移回普通账号池':'移入风控池'}</Button></div>
    {account.risk?.history?.length>0&&<details className="logs"><summary>查看风控记录</summary>{account.risk.history.slice().reverse().map((item,index)=><p key={index}>{time(item.time)} · {item.action==='restore'?'手动移回普通池':item.reason}</p>)}</details>}
    {account.message&&<p className="account-message">{account.message}</p>}
    {editing&&<div className="edit-fields"><Field label="GPT 登录密码" hint="留空保留原有配置"><Input type="password" size="large" value={password} onChange={(_,data)=>setPassword(data.value)} autoComplete="new-password"/></Field><Field label="2FA 密钥"><Input type="password" size="large" value={totp} onChange={(_,data)=>setTotp(data.value)} autoComplete="off"/></Field><Field label="邮箱取码链接"><Input type="password" size="large" value={mail} onChange={(_,data)=>setMail(data.value)} autoComplete="off"/></Field><Field label="资格标签"><Select size="large" value={group} onChange={(_,data)=>setGroup(data.value)}><option value="">未分类</option><option value="3">3个月资格</option><option value="1">1个月资格</option></Select></Field>{error&&<p className="inline-error" role="alert">{error}</p>}<Button appearance="primary" disabled={busy} onClick={save}>保存资料</Button></div>}
  </Modal>;
}

function App() {
  const focusTarget=useRestoreFocusTarget();
  const [page,setPage]=useState(()=>new URLSearchParams(location.search).get('page') in titles?new URLSearchParams(location.search).get('page'):'accounts');
  const [data,setData]=useState(empty),[loading,setLoading]=useState(true),[connection,setConnection]=useState('connecting'),[notice,setNotice]=useState(null);
  const [search,setSearch]=useState(''),[accountFilter,setAccountFilter]=useState('all'),[groupFilter,setGroupFilter]=useState(''),[cardSku,setCardSku]=useState('plus_half'),[cardFilter,setCardFilter]=useState('all'),[recordFilter,setRecordFilter]=useState('all');
  const [accountSelection,setAccountSelection]=useState({}),[cardSelection,setCardSelection]=useState({}),[batchSelection,setBatchSelection]=useState({});
  const [riskSelection,setRiskSelection]=useState({}),[riskRequest,setRiskRequest]=useState(null);
  const [importKind,setImportKind]=useState(null),[detailAccount,setDetailAccount]=useState(null),[detailTask,setDetailTask]=useState(null),[detailCard,setDetailCard]=useState(null),[cardDetailBusy,setCardDetailBusy]=useState(false),[cardCopyStatus,setCardCopyStatus]=useState(''),[session,setSession]=useState(null),[sessionCopy,setSessionCopy]=useState('');
  const [quote,setQuote]=useState(null),[confirmBusy,setConfirmBusy]=useState(false),[busy,setBusy]=useState(false),[deleteRequest,setDeleteRequest]=useState(null);
  const [editCardSpec,setEditCardSpec]=useState(null);
  const [storageOpen,setStorageOpen]=useState(false);
  const [sessionAutoPending,setSessionAutoPending]=useState(null);
  const [copyingAccount,setCopyingAccount]=useState(''),[copyingSession,setCopyingSession]=useState(''),[accountCopy,setAccountCopy]=useState(null),[accountCopyStatus,setAccountCopyStatus]=useState('');
  const [targetId,setTargetId]=useState(''),[accountQuery,setAccountQuery]=useState(''),[targetSku,setTargetSku]=useState('plus'),[singleTaskId,setSingleTaskId]=useState(null),[batchTasks,setBatchTasks]=useState([]),[batchMode,setBatchMode]=useState('select');
  const [theme,setTheme]=useState(()=>localStorage.getItem('workbench-theme')||'system'),[systemDark,setSystemDark]=useState(()=>matchMedia('(prefers-color-scheme: dark)').matches);
  const snapshot=useRef(''),mounted=useRef(true),clipboardBusy=useRef(false);
  const dark=theme==='dark'||theme==='system'&&systemDark;
  const fluentTheme=useMemo(()=>({...dark?createDarkTheme(brand):createLightTheme(brand),...dark?darkGreen:green,fontFamilyBase:'"Segoe UI", "Microsoft YaHei", sans-serif',fontSizeBase300:'16px',lineHeightBase300:'24px',borderRadiusMedium:'8px'}),[dark]);
  useEffect(()=>{document.documentElement.dataset.theme=dark?'dark':'light';},[dark]);
  useEffect(()=>{const media=matchMedia('(prefers-color-scheme: dark)');const listener=event=>setSystemDark(event.matches);media.addEventListener('change',listener);return()=>media.removeEventListener('change',listener);},[]);
  async function refresh(message){try{const state=await api('/api/inventory/state');if(!mounted.current)return;const key=JSON.stringify(state);if(key!==snapshot.current){snapshot.current=key;setData(state);}setConnection('online');if(typeof message==='string')setNotice({text:message,error:false});}catch(error){if(mounted.current){setConnection('offline');setNotice({text:auth?error.message:'请双击“启动工作台”，使用自动打开的页面访问。',error:true});}}finally{if(mounted.current)setLoading(false);}}
  useEffect(()=>{mounted.current=true;refresh();const timer=setInterval(refresh,2000);return()=>{mounted.current=false;clearInterval(timer);};},[]);
  function go(next){setPage(next);setSearch('');setNotice(null);history.pushState(null,'','?page='+next);}
  useEffect(()=>{const listener=()=>{const next=new URLSearchParams(location.search).get('page');setPage(next in titles?next:'accounts');setSearch('');};window.addEventListener('popstate',listener);return()=>window.removeEventListener('popstate',listener);},[]);
  async function perform(path,body,message){setBusy(true);try{const result=await api(path,body);await refresh(message);return result;}catch(error){setNotice({text:error.message,error:true});return null;}finally{setBusy(false);}}
  async function getSession(ids){await perform('/api/inventory/session',{ids},`已提交 ${ids.length} 个账号，同时获取最多 ${data.session_limit||5} 个 Session；可在账号行查看进度。`);}
  function selectMissingSessions(){
    const limit=Math.max(1,Number(data.session_limit)||5);
    const candidates=regularAccounts.filter(row=>{
      const info=row.session_info||{};
      return info.state==='missing'&&!info.acquired_at&&!info.restored_at&&!info.invalidated_at&&
        row.session_refresh_eligible===true&&!row.busy;
    }).slice(0,limit);
    setAccountSelection(Object.fromEntries(candidates.map(row=>[row.id,true])));
    setNotice({text:candidates.length?`已选择 ${candidates.length} 个未获取 Session 的账号，可点击“获取 Session”开始。`:'没有符合条件的未获取 Session 账号。',error:false});
  }
  async function refreshEligible(){const result=await perform('/api/inventory/refresh-sessions',{});if(result)setNotice({text:`已排队 ${result.queued} 个账号；排除 ${result.excluded} 个，需补全资料 ${result.manual} 个。`,error:false});}
  async function setSessionAuto(enabled){setSessionAutoPending(enabled);try{await perform('/api/inventory/session-auto',{enabled},enabled?'自动更新已开启，仅维护符合下方范围的账号。':'自动更新已关闭。');}finally{setSessionAutoPending(null);}}
  async function viewSession(id){try{const result=await api('/api/session',{id});setDetailAccount(null);setSession({email:result.email,value:JSON.stringify(result.session)});setSessionCopy('');}catch(error){setNotice({text:error.message,error:true});await refresh();}}
  async function copySession(id){
    if(clipboardBusy.current){setNotice({text:'另一项复制正在处理，请稍后重试。',error:false});return;}
    clipboardBusy.current=true;
    setCopyingSession(id);
    try{
      const {result,copied}=await copyFromRequest(api('/api/session',{id}),value=>JSON.stringify(value.session));
      const value=JSON.stringify(result.session);
      if(copied)setNotice({text:'Session 已复制',error:false});
      else {setSession({email:result.email,value});setSessionCopy('浏览器未允许直接复制，请点击“复制 Session”或选中文本复制。');}
    }catch(error){setNotice({text:error.message,error:true});await refresh();}
    finally{clipboardBusy.current=false;setCopyingSession('');}
  }
  function closeSession(){setSession(null);setSessionCopy('');}
  function closeAccountCopy(){setAccountCopy(null);setAccountCopyStatus('');}
  async function copyAccount(id){
    if(clipboardBusy.current){setNotice({text:'另一项复制正在处理，请稍后重试。',error:false});return;}
    clipboardBusy.current=true;
    setCopyingAccount(id);
    closeSession();
    closeAccountCopy();
    try{
      const {result,copied}=await copyFromRequest(api('/api/inventory/copy-account',{id}),value=>value.text);
      if(copied)setNotice({text:'账号资料已复制（邮箱、GPT密码、2FA及验证网站）',error:false});
      else {setAccountCopy({value:result.text});setAccountCopyStatus('浏览器未允许直接复制，请点击复制或选中文本复制。');}
    }catch(error){setNotice({text:error.message,error:true});}
    finally{clipboardBusy.current=false;setCopyingAccount('');}
  }
  async function openCardDetail(id){
    if(cardDetailBusy)return;
    setCardDetailBusy(true);setCardCopyStatus('');
    try{setDetailCard(await api('/api/inventory/card-detail',{id}));}
    catch(error){setNotice({text:error.message,error:true});}
    finally{setCardDetailBusy(false);}
  }
  async function copyCardCode(){
    if(!detailCard?.code)return;
    try{await navigator.clipboard.writeText(detailCard.code);setCardCopyStatus('已复制');}
    catch{setCardCopyStatus('浏览器未允许直接复制，请选中文本后按 Ctrl+C。');}
  }
  async function prepare(ids){setBusy(true);try{const result=await api('/api/inventory/quote',{ids,sku:targetSku});setQuote({...result,kind:page==='batch'?'batch':'single'});}catch(error){setNotice({text:error.message,error:true});}finally{setBusy(false);}}
  async function start(){if(!quote)return;setConfirmBusy(true);try{const result=await api('/api/inventory/start',{quote:quote.quote});if(quote.kind==='batch'){setBatchTasks(result.tasks);setBatchMode('progress');}else setSingleTaskId(result.tasks[0]);setQuote(null);await refresh('任务已开始，预检通过后会自动提交兑换。');}catch(error){setQuote(null);setNotice({text:error.message,error:true});}finally{setConfirmBusy(false);}}
  async function stopBatch(){setBusy(true);let stopped=0;try{for(const task of data.tasks.filter(task=>batchTasks.includes(task.id)&&activeStatuses.has(task.status))){try{await api('/api/inventory/task',{id:task.id,action:'stop'});stopped++;}catch{}}await refresh(`已停止 ${stopped} 个本地任务；已经提交的兑换仍需查询结果。`);}finally{setBusy(false);}}
  function chooseRecharge(id){setTargetId(id);setAccountQuery('');setSingleTaskId(null);go('single');}
  function requestRisk(ids,active){setDetailAccount(null);setRiskRequest({ids,active});}
  const regularAccounts=data.accounts.filter(row=>!row.risk?.active);
  const riskAccounts=data.accounts.filter(row=>row.risk?.active);
  const rechargeAccounts=data.accounts.filter(canRecharge);
  useEffect(()=>{
    if(targetId&&!rechargeAccounts.some(row=>row.id===targetId)){setTargetId('');setAccountQuery('');}
    const prune=(selection,rows,setter)=>{const next=Object.fromEntries(Object.entries(selection).filter(([id,selected])=>selected&&rows.some(row=>row.id===id)));if(Object.keys(next).length!==Object.keys(selection).length)setter(next);};
    prune(batchSelection,rechargeAccounts,setBatchSelection);prune(accountSelection,regularAccounts,setAccountSelection);prune(riskSelection,riskAccounts,setRiskSelection);prune(cardSelection,data.cards,setCardSelection);
  },[data]);
  const accountIds=Object.keys(accountSelection).filter(key=>accountSelection[key]&&regularAccounts.some(row=>row.id===key));
  const riskIds=Object.keys(riskSelection).filter(key=>riskSelection[key]&&riskAccounts.some(row=>row.id===key));
  const cardIds=Object.keys(cardSelection).filter(key=>cardSelection[key]&&data.cards.some(row=>row.id===key));
  const batchIds=Object.keys(batchSelection).filter(key=>batchSelection[key]&&rechargeAccounts.some(row=>row.id===key));
  const q=search.toLowerCase();
  const accounts=regularAccounts.filter(row=>row.email.toLowerCase().includes(q)&&(!groupFilter||String(row.eligibility_months||'')===groupFilter)&&
    (accountFilter==='all'||accountFilter==='charged'&&charged(row)||accountFilter==='uncharged'&&!charged(row)||accountFilter==='ready'&&row.session_valid));
  const stock=data.cards.filter(row=>row.sku===cardSku&&(cardFilter==='all'||row.status===cardFilter)&&row.masked.toLowerCase().includes(q));
  const tasks=data.tasks.filter(task=>(task.email.toLowerCase().includes(q)||task.card.toLowerCase().includes(q))&&(recordFilter==='all'||(recordFilter==='active'?activeStatuses.has(task.status):recordFilter==='error'?['error','failed','blocked','unknown'].includes(task.status):task.status===recordFilter)));
  const selectedAccount=rechargeAccounts.find(row=>row.id===targetId);
  const available=data.cards.filter(card=>card.sku===targetSku&&card.status==='unused');
  const enabled=data.products[targetSku]?.redeem_enabled===true;
  const singleTask=data.tasks.find(task=>task.id===singleTaskId)||data.tasks.find(task=>task.account_id===targetId);
  const latestDetailAccount=data.accounts.find(row=>row.id===detailAccount);
  const latestDetailTask=data.tasks.find(task=>task.id===detailTask);
  const latestDetailCard=detailCard;
  const latestSpecCard=data.cards.find(card=>card.id===editCardSpec);
  const accountColumns=[
    {accessorKey:'display_sequence',header:'序号',size:80,cell:info=><span className="tabular">{String(info.getValue()||info.row.original.sequence||'').padStart(3,'0')}</span>},
    {accessorKey:'email',header:'账号邮箱',cell:info=><div className="email-cell"><span>{info.getValue()}</span>{info.row.original.eligibility_months&&<small>{info.row.original.eligibility_months}个月资格</small>}{info.row.original.busy&&<small>{info.row.original.status==='queued'?'等待空闲处理名额':info.row.original.message}</small>}</div>},
    {accessorKey:'current_plan',header:'当前套餐',size:130,cell:info=><span className="plan-text">{labelPlan(info.getValue())}</span>},
    {id:'recharge',header:'最近充值',size:190,enableSorting:false,cell:({row})=>row.original.last_recharge?<div><Status value={row.original.last_recharge.status}/><small className="cell-small">{productNames[row.original.last_recharge.sku]}</small></div>:<span className="muted">未充值</span>},
    {id:'session',header:'Session（北京时间）',size:245,enableSorting:false,cell:({row})=><SessionStatus account={row.original}/>},
    {id:'actions',header:'操作',size:390,enableSorting:false,cell:({row})=><div className="row-actions"><Button appearance="subtle" onClick={()=>setDetailAccount(row.original.id)}>详情</Button><Button icon={<Copy/>} disabled={copyingAccount===row.original.id} aria-busy={copyingAccount===row.original.id} onClick={event=>{event.stopPropagation();copyAccount(row.original.id);}}>{copyingAccount===row.original.id?'正在复制':'一键复制'}</Button><Button disabled={!canRecharge(row.original)} title={row.original.recharge_block_reason||undefined} onClick={()=>chooseRecharge(row.original.id)}>充值</Button><Button icon={<Copy/>} disabled={!row.original.session_valid||copyingSession===row.original.id} title={!row.original.session_valid?'Session 当前不可用':undefined} onClick={event=>{event.stopPropagation();copySession(row.original.id);}}>{copyingSession===row.original.id?'正在复制':'复制 Session'}</Button></div>}
  ];
  const riskColumns=[...accountColumns.slice(0,2),
    {id:'reason',header:'移入原因',size:200,enableSorting:false,cell:({row})=><div><Status value="blocked" text="风控暂缓"/><small className="cell-small">{row.original.risk.reason}</small></div>},
    {id:'since',header:'移入时间 / 养号',size:220,enableSorting:false,cell:({row})=><div><span className="time-text">{time(row.original.risk.since)}</span><small className="cell-small">已养号 {heldFor(row.original.risk.since)} 天</small></div>},
    {id:'actions',header:'操作',size:220,enableSorting:false,cell:({row})=><div className="row-actions"><Button appearance="subtle" onClick={()=>setDetailAccount(row.original.id)}>详情</Button><Button disabled={busy||row.original.busy} onClick={()=>requestRisk([row.original.id],false)}>移回普通池</Button></div>}
  ];
  const cardColumns=[{accessorKey:'display_sequence',header:'序号',size:90,cell:info=><span className="tabular">{String(info.getValue()||info.row.original.sequence||'').padStart(3,'0')}</span>},
    {accessorKey:'masked',header:'卡密',cell:info=><span className="code-text">{info.getValue()}</span>},
    {accessorKey:'sku',header:'规格',size:180,cell:info=>productNames[info.getValue()]},
    {accessorKey:'status',header:'使用状态',size:250,cell:info=><div><Status value={info.getValue()}/>{info.row.original.check_error&&<small className="cell-small error-text">{cardCheckErrorLabels[info.row.original.check_error_code]||info.row.original.check_error}</small>}</div>},
    {accessorKey:'created',header:'入库时间',size:200,cell:info=><span className="time-text">{time(info.getValue())}</span>},
    {id:'actions',header:'操作',size:350,enableSorting:false,cell:({row})=><div className="row-actions"><Button appearance="subtle" disabled={cardDetailBusy} onClick={()=>openCardDetail(row.original.id)}>详情</Button><Button disabled={busy||!row.original.spec_editable} title={row.original.spec_edit_block_reason||undefined} onClick={()=>setEditCardSpec(row.original.id)}>修改规格</Button><Button disabled={busy||row.original.checking} onClick={()=>perform('/api/inventory/check-cards',{ids:[row.original.id]},'正在核对卡密状态。')}>{row.original.checking?'核对中':'核对'}</Button><Button appearance="subtle" disabled={busy||!row.original.deletable} title={row.original.delete_block_reason||undefined} onClick={()=>setDeleteRequest({kind:'cards',ids:[row.original.id]})}>删除</Button></div>}
  ];
  const taskColumns=[{accessorKey:'sequence',header:'序号',size:80}, {accessorKey:'email',header:'账号邮箱',cell:info=><span className="email-cell">{info.getValue()}</span>},
    {id:'provider-product',header:'产品 / 套餐',size:180,cell:info=>taskProductLabel(info.row.original)},
    {accessorKey:'card',header:'卡密',size:185,cell:info=><span className="code-text">{info.getValue()}</span>},
    {accessorKey:'status',header:'结果',size:145,cell:info=><Status value={info.getValue()}/>},
    {accessorKey:'updated',header:'更新时间',size:200,cell:info=><span className="time-text">{time(info.getValue())}</span>},
    {id:'actions',header:'操作',size:95,enableSorting:false,cell:({row})=><Button appearance="subtle" onClick={()=>setDetailTask(row.original.id)}>详情</Button>}
  ];
  function exportTasks(){const quote=value=>{let text=String(value??'');if(/^[\s]*[=+@-]/.test(text))text="'"+text;return '"'+text.replaceAll('"','""')+'"';};const lines=[['序号','账号邮箱','产品 / 套餐','卡密','结果','更新时间','结果说明','风控原因'],...tasks.map(task=>[task.sequence,task.email,taskProductLabel(task),task.card,statusLabels[task.status],time(task.updated),task.message,task.risk?.reason||''])];const url=URL.createObjectURL(new Blob(['\ufeff'+lines.map(row=>row.map(quote).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download='充值记录.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  const sessionConcurrency=<div className="session-concurrency"><label htmlFor="session-concurrency">Session 并发</label><Select id="session-concurrency" aria-label="Session 并发数量" value={String(data.session_limit||5)} onChange={(_,value)=>perform('/api/command',{action:'session-limit',limit:Number(value.value)},'Session 并发已保存；超过上限的登录任务会排队。')}>{Array.from({length:5},(_,index)=>index+1).map(value=><option key={value} value={value}>同时 {value} 个</option>)}</Select>{page==='accounts'&&<Button icon={<UsersThree/>} disabled={busy} onClick={selectMissingSessions}>一键选择未获取 Session</Button>}<span>最多同时登录 5 个 · 处理中 {data.session_active||0} · 排队 {data.session_queued||0}{data.paused?' · 队列已暂停':''}</span>{data.paused&&<Button onClick={()=>perform('/api/command',{action:'resume'},'队列已继续。')}>继续队列</Button>}</div>;
  const sessionMaintenance=<div className="session-maintenance"><div><Switch label="自动更新 Session" checked={sessionAutoPending??Boolean(data.session_auto_enabled)} disabled={busy||sessionAutoPending!==null} onChange={(_,value)=>setSessionAuto(value.checked)}/><Button icon={<ArrowClockwise/>} disabled={busy} onClick={refreshEligible}>更新符合条件的 Session</Button></div><p>仅处理普通池中从未充值成功、空闲且无待核实兑换的账号；没有独立交付标记，复制资料不算交付。每 30 秒检查，已知截止时间不足 10 分钟时提前更新。缺少登录资料或遇到人工验证会暂停，最多尝试 3 次。</p><p>所有时间均为北京时间。可用仅表示本地凭证和时间检查通过；服务端验证结果为所标时间的兑换预检结果。令牌到期未知时，不保证其持续有效；可解析的令牌声明也仅作保守更新依据。</p>{data.session_maintenance_error&&<p className="error-text" role="alert">{data.session_maintenance_error}</p>}</div>;
  const productPicker=<div className="product-options" role="group" aria-label="充值规格">{Object.entries(productNames).map(([key,label])=><button key={key} type="button" className={'product-option '+(targetSku===key?'selected':'')} aria-pressed={targetSku===key} onClick={()=>setTargetSku(key)}><strong>{label}</strong><span>{data.cards.filter(card=>card.sku===key&&card.status==='unused').length} 张可用</span></button>)}</div>;
  return <FluentProvider theme={fluentTheme} className="app-root" data-theme={dark?'dark':'light'}>
    <aside className="sidebar"><a className="brand" href="?page=accounts" onClick={event=>{event.preventDefault();go('accounts');}}><span className="brand-mark">G+</span><span>充值工作台<small>账号与卡密管理</small></span></a>
      <nav aria-label="主导航">{navigation.map(([key,label,Icon])=><button {...focusTarget} key={key} className={page===key?'active':''} aria-label={label} aria-current={page===key?'page':undefined} onClick={()=>go(key)}><Icon size={22} weight={page===key?'fill':'regular'}/><span>{label}</span>{(key==='accounts'||key==='risk')&&<em aria-hidden="true">{key==='risk'?riskAccounts.length:regularAccounts.length}</em>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="helper-status"><span className={'connection-dot '+(connection==='online'?'online':'')}/>{connection==='online'?'本地后端已连接':connection==='connecting'?'正在连接':'本地后端已断开'}</div><p>{data.isolation?.enabled?`独立无痕登录已启用${data.isolation.active_instances?` · ${data.isolation.connected_instances}/${data.isolation.active_instances} 个窗口已连接`:''}`:data.helper.connected&&data.helper.incognito?'无痕登录助手已连接':'等待无痕登录助手连接'}</p><Select aria-label="外观主题" value={theme} icon={dark?<Moon/>:<Sun/>} onChange={(_,value)=>{setTheme(value.value);localStorage.setItem('workbench-theme',value.value);}}><option value="system">跟随系统</option><option value="light">浅色外观</option><option value="dark">深色外观</option></Select><Button appearance="subtle" className="storage-link" onClick={()=>setStorageOpen(true)}>查看数据保存位置</Button></div>
    </aside>
    <main className="main-content"><header className="page-header"><div><h1>{titles[page]}</h1><p>{copy[page]}</p></div>{page==='accounts'&&<Button size="large" appearance="primary" icon={<UploadSimple/>} onClick={()=>setImportKind('accounts')}>导入账号</Button>}{page==='risk'&&<Button size="large" appearance="primary" icon={<UploadSimple/>} onClick={()=>setImportKind('risk-accounts')}>添加风控账号</Button>}{page==='cards'&&<Button size="large" appearance="primary" icon={<UploadSimple/>} onClick={()=>setImportKind('cards')}>导入卡密</Button>}{page==='records'&&<Button size="large" icon={<DownloadSimple/>} disabled={!tasks.length} onClick={exportTasks}>导出记录</Button>}</header>
      {notice&&<div className={'notice '+(notice.error?'notice-error':'')} role={notice.error?'alert':'status'}><span>{notice.error&&<WarningCircle size={20}/>} {notice.text}</span><Button appearance="subtle" icon={<X/>} aria-label="关闭提示" onClick={()=>setNotice(null)}/></div>}
      {loading?<div className="loading-skeleton"><Skeleton><SkeletonItem size={32}/>{[1,2,3,4].map(item=><SkeletonItem key={item} size={64}/>)}</Skeleton></div>:connection==='offline'&&!snapshot.current?<div className="backend-unavailable" role="alert"><h2>尚未读取到已保存的数据</h2><p>请双击“启动工作台”，使用启动器打开的页面访问。连接后会重新读取账号、卡密和充值记录。</p><Button appearance="primary" onClick={()=>refresh()}>重新连接后端</Button></div>:<>
      {page==='accounts'&&<section aria-label="账号池内容">{sessionConcurrency}{sessionMaintenance}<div className="summary-line"><strong>{regularAccounts.length}<span>个账号</span></strong><span>已充值 {regularAccounts.filter(charged).length}</span><span>可充值 {rechargeAccounts.length}</span><span>Session 本地未过期 {regularAccounts.filter(row=>row.session_valid).length}</span></div>
        <div className="filter-bar"><FilterTabs selectedValue={accountFilter} onTabSelect={(_,value)=>setAccountFilter(value.value)}><FilterTab value="all">全部账号</FilterTab><FilterTab value="uncharged">未充值</FilterTab><FilterTab value="charged">已充值</FilterTab><FilterTab value="ready">Session 就绪</FilterTab></FilterTabs><div className="filter-controls"><Input aria-label="搜索账号" contentBefore={<MagnifyingGlass/>} placeholder="搜索邮箱" value={search} onChange={(_,value)=>setSearch(value.value)}/><Select aria-label="资格筛选" value={groupFilter} onChange={(_,value)=>setGroupFilter(value.value)}><option value="">全部资格</option><option value="3">3个月资格</option><option value="1">1个月资格</option></Select></div></div>
        {accountIds.length>0&&<div className="selection-bar"><span>已选 {accountIds.length} 个账号</span><Button disabled={busy} onClick={()=>getSession(accountIds)}>获取 Session</Button><Button disabled={!accountIds.some(id=>rechargeAccounts.some(row=>row.id===id))} onClick={()=>{setBatchSelection(Object.fromEntries(accountIds.filter(id=>rechargeAccounts.some(row=>row.id===id)).map(id=>[id,true])));go('batch');}}>批量充值</Button><Button disabled={busy} onClick={()=>requestRisk(accountIds,true)}>移入风控池</Button><Button appearance="subtle" icon={<Trash/>} onClick={()=>setDeleteRequest({kind:'accounts',ids:accountIds})}>删除</Button></div>}
        <Table key="accounts" rows={accounts} columns={accountColumns} selection={accountSelection} onSelection={setAccountSelection} emptyTitle={regularAccounts.length?'没有匹配的账号':'普通账号池还是空的'} emptyText={regularAccounts.length?'调整搜索或筛选条件再试。':'导入账号，或将养号后的账号从风控池手动移回。'}/>
      </section>}
      {page==='risk'&&<section aria-label="风控账号池内容">{sessionConcurrency}<div className="summary-line"><strong>{riskAccounts.length}<span>个暂缓账号</span></strong><span>本池账号不会出现在充值选项中</span></div><div className="risk-pool-note">银行卡拒付或 approve 未允许的账号会自动移入。养号天数仅供参考，需要你手动移回后才能再次选择充值。</div>
        <div className="filter-bar"><Input aria-label="搜索风控账号" contentBefore={<MagnifyingGlass/>} placeholder="搜索邮箱" value={search} onChange={(_,value)=>setSearch(value.value)}/></div>
        {riskIds.length>0&&<div className="selection-bar"><span>已选 {riskIds.length} 个风控账号</span><Button disabled={busy} onClick={()=>getSession(riskIds)}>获取 Session</Button><Button appearance="primary" disabled={busy} onClick={()=>requestRisk(riskIds,false)}>移回普通池</Button></div>}
        <Table key="risk" rows={riskAccounts.filter(row=>row.email.toLowerCase().includes(q))} columns={riskColumns} selection={riskSelection} onSelection={setRiskSelection} emptyTitle={riskAccounts.length?'没有匹配的风控账号':'暂时没有风控账号'} emptyText={riskAccounts.length?'调整搜索条件再试。':'可点击右上方添加风控账号，或在普通账号行移入。明确拒付时也会自动移入。'}/>
      </section>}
      {page==='extract'&&<SessionExtractPage/>}
      {page==='cards'&&<section aria-label="卡密管理内容"><div className="summary-line"><strong>{data.cards.filter(card=>card.sku===cardSku&&card.status==='unused').length}<span>张可用卡密</span></strong><span>已使用 {data.cards.filter(card=>card.sku===cardSku&&card.status==='used').length}</span><span>占用中 {data.cards.filter(card=>card.sku===cardSku&&card.status==='reserved').length}</span></div>
        <FilterTabs className="stock-tabs" selectedValue={cardSku} onTabSelect={(_,value)=>{setCardSku(value.value);setCardSelection({});}}>{Object.entries(productNames).map(([key,label])=><FilterTab key={key} value={key}>{label}</FilterTab>)}</FilterTabs>
        <div className="filter-bar"><div className="filter-controls"><Input aria-label="搜索卡密" contentBefore={<MagnifyingGlass/>} placeholder="搜索卡密尾号" value={search} onChange={(_,value)=>setSearch(value.value)}/><Select aria-label="卡密状态筛选" value={cardFilter} onChange={(_,value)=>setCardFilter(value.value)}><option value="all">全部状态</option><option value="unused">未使用</option><option value="reserved">占用中</option><option value="used">已使用</option><option value="exception">异常</option></Select></div><span className="muted">卡密默认脱敏显示</span></div>
        {cardIds.length>0&&<div className="selection-bar"><span>已选 {cardIds.length} 张卡密</span><Button disabled={busy} onClick={()=>perform('/api/inventory/check-cards',{ids:cardIds},'已开始核对所选卡密状态。')}>核对状态</Button><Button appearance="subtle" icon={<Trash/>} disabled={busy||cardIds.some(id=>!data.cards.find(card=>card.id===id)?.deletable)} onClick={()=>setDeleteRequest({kind:'cards',ids:cardIds})}>删除卡密</Button></div>}
        <Table key={'cards-'+cardSku} rows={stock} columns={cardColumns} selection={cardSelection} onSelection={setCardSelection} emptyTitle={data.cards.some(card=>card.sku===cardSku)?'没有匹配的卡密':'还没有 '+productNames[cardSku]+' 卡密'} emptyText="点击右上方导入卡密，账号可以稍后再选择。"/>
      </section>}
      {page==='single'&&<section className="recharge-layout" aria-label="单个充值内容"><div className="recharge-form"><h2>创建充值</h2><Field label="充值账号"><div className="account-picker"><Input size="large" aria-label="搜索充值账号" placeholder="输入邮箱筛选" contentBefore={<MagnifyingGlass/>} value={accountQuery} onChange={(_,value)=>setAccountQuery(value.value)}/><Select size="large" aria-label="选择充值账号" value={selectedAccount?.id||''} onChange={(_,value)=>{setTargetId(value.value);setSingleTaskId(null);}}><option value="">请选择邮箱</option>{rechargeAccounts.filter(row=>row.id===targetId||row.email.toLowerCase().includes(accountQuery.trim().toLowerCase())).map(row=><option key={row.id} value={row.id}>{row.email}</option>)}</Select>{!rechargeAccounts.some(row=>row.email.toLowerCase().includes(accountQuery.trim().toLowerCase()))&&<p className="field-note">{rechargeAccounts.length?'没有匹配的邮箱，请调整搜索。':'暂无可充值账号，请导入未充值账号或手动移回已恢复的风控账号。'}</p>}<p className="field-note">仅显示从未充值成功、未风控且当前空闲的账号。</p></div></Field>
        {selectedAccount&&<div className="account-summary"><span>当前套餐 <b>{labelPlan(selectedAccount.current_plan)}</b></span><span>Session <b>{selectedAccount.session_valid?'已就绪':'自动获取'}</b></span></div>}
        <Field label="充值规格">{productPicker}</Field>{!enabled&&<p className="field-note">该规格支持库存管理，实际充值接口待确认。目前可使用 Plus 充值。</p>}
        <div className="assignment"><div><span>自动分配卡密</span><strong>{available[0]?.masked||'暂无可用卡密'}</strong></div><Badge appearance="tint" color="informative">{available.length} 张可用</Badge></div>
        <Button className="start-button" size="large" appearance="primary" icon={<ArrowRight/>} iconPosition="after" disabled={busy||!selectedAccount||selectedAccount.busy||!available.length||!enabled} onClick={()=>prepare([targetId])}>{busy?'正在准备':'一键充值'}</Button><p className="form-footnote">开始前核对一次，之后自动获取 Session、预检和兑换。</p>
      </div><div className="progress-panel"><Progress task={singleTask}/>{singleTask&&['pending','error','failed','blocked'].includes(singleTask.status)&&singleTask.result_state!=='completed'&&<div className="progress-actions"><Button disabled={busy} icon={<ArrowClockwise/>} onClick={()=>perform('/api/inventory/task',{id:singleTask.id,action:'refresh'},'正在查询原任务。')}>查询结果</Button>{activeStatuses.has(singleTask.status)&&<Button onClick={()=>perform('/api/inventory/task',{id:singleTask.id,action:'stop'},'已停止本地任务。')}>停止任务</Button>}</div>}</div></section>}
      {page==='batch'&&<section aria-label="批量充值内容"><div className="batch-heading"><FilterTabs selectedValue={batchMode} onTabSelect={(_,value)=>setBatchMode(value.value)}><FilterTab value="select">选择账号</FilterTab><FilterTab value="progress" disabled={!batchTasks.length}>本次进度</FilterTab></FilterTabs>{batchMode==='progress'&&<div className="row-actions"><Button disabled={busy||!data.tasks.some(task=>batchTasks.includes(task.id)&&activeStatuses.has(task.status))} onClick={stopBatch}>停止本批</Button><Button icon={data.paused?<Play/>:<Pause/>} onClick={()=>perform('/api/command',{action:data.paused?'resume':'pause'},data.paused?'队列已继续':'队列已暂停，当前步骤会继续完成。')}>{data.paused?'继续队列':'暂停队列'}</Button></div>}</div>
        {batchMode==='select'?<><div className="batch-products"><Field label="本批充值规格">{productPicker}</Field><div className="batch-availability"><strong>{batchIds.length}<small>个账号待充值</small></strong><span>{productNames[targetSku]} 库存 {available.length} 张</span></div></div>{!enabled&&<p className="field-note">Pro 库存可独立管理，实际充值接口待确认。</p>}
          <div className="filter-bar"><Input aria-label="搜索批量账号" contentBefore={<MagnifyingGlass/>} placeholder="搜索邮箱" value={search} onChange={(_,value)=>setSearch(value.value)}/><span className="muted">仅显示从未充值成功、未风控且当前空闲的账号</span></div>
          <Table key="batch-select" rows={rechargeAccounts.filter(row=>row.email.toLowerCase().includes(q))} columns={accountColumns.slice(0,4)} selection={batchSelection} onSelection={setBatchSelection} emptyTitle="暂无可选账号" emptyText="请导入未充值账号，或在风控池手动移回已恢复的账号。"/>
          <div className="batch-start"><span>{batchIds.length>available.length?'卡密不足，请补充库存或减少所选账号。':`将分配 ${batchIds.length} 张 ${productNames[targetSku]} 卡密`}</span><div className="batch-start-actions"><Select aria-label="并发数量" value={String(data.limit)} onChange={(_,value)=>perform('/api/command',{action:'limit',limit:Number(value.value)},'并发数量已更新。')}>{Array.from({length:10},(_,index)=>index+1).map(value=><option key={value} value={value}>并发 {value}</option>)}</Select><Button appearance="primary" size="large" disabled={busy||!batchIds.length||batchIds.length>available.length||!enabled} onClick={()=>prepare(batchIds)}>批量充值</Button></div></div><p className="form-footnote">每个账号使用独立无痕窗口；获取 Session 后自动关闭该窗口，再继续充值。首次启动会先准备登录助手。</p>
        </>:<><div className="summary-line"><strong>{batchTasks.length}<span>个充值任务</span></strong><span>成功 {data.tasks.filter(task=>batchTasks.includes(task.id)&&task.status==='completed').length}</span><span>需关注 {data.tasks.filter(task=>batchTasks.includes(task.id)&&['failed','error','blocked'].includes(task.status)).length}</span></div><Table key="batch-progress" rows={data.tasks.filter(task=>batchTasks.includes(task.id))} columns={taskColumns}/></>}
      </section>}
      {page==='records'&&<section aria-label="充值记录内容"><div className="summary-line"><strong>{data.tasks.length}<span>次充值</span></strong><span>成功 {data.tasks.filter(task=>task.status==='completed').length}</span><span>处理中 {data.tasks.filter(task=>activeStatuses.has(task.status)).length}</span></div><div className="filter-bar"><FilterTabs selectedValue={recordFilter} onTabSelect={(_,value)=>setRecordFilter(value.value)}><FilterTab value="all">全部记录</FilterTab><FilterTab value="completed">成功</FilterTab><FilterTab value="active">处理中</FilterTab><FilterTab value="error">异常</FilterTab></FilterTabs><Input aria-label="搜索充值记录" contentBefore={<MagnifyingGlass/>} placeholder="搜索邮箱或卡密尾号" value={search} onChange={(_,value)=>setSearch(value.value)}/></div><Table key="records" rows={tasks} columns={taskColumns} emptyTitle="还没有充值记录" emptyText="创建充值任务后，每次分配和处理结果都会保留在这里。"/></section>}
      </>}
    </main>
    {importKind&&<ImportDialog kind={importKind} initialSku={cardSku} products={data.products} onClose={()=>setImportKind(null)} onDone={refresh}/>}
    {storageOpen&&<Modal title="数据保存在本机" onClose={()=>setStorageOpen(false)} actions={<Button onClick={()=>setStorageOpen(false)}>关闭</Button>}><p className="dialog-description">账号、卡密、充值记录和 Session 由本地后端写入数据库，Session 凭证使用当前 Windows 用户 DPAPI 加密。关闭页面或重启后会尝试恢复未过期且邮箱匹配的 Session；账号删除、充值成功或服务端明确拒绝时会删除对应 Session。不同版本共用同一保存位置。</p><dl className="detail-facts"><div><dt>后端连接</dt><dd>{connection==='online'?'已连接':'尚未连接'}</dd></div><div><dt>数据库</dt><dd>{data.storage?.backend||'等待连接'}</dd></div><div><dt>当前数据</dt><dd>{data.accounts.length} 个账号 / {data.cards.length} 张卡密 / {data.tasks.length} 条记录</dd></div></dl><p className="storage-path">{data.storage?.database||'连接后显示保存位置'}</p></Modal>}
    {accountCopy&&<Modal title="复制账号资料" onClose={closeAccountCopy} wide actions={<><span className="copy-status" role="status">{accountCopyStatus}</span><Button onClick={closeAccountCopy}>关闭</Button><Button icon={<Copy/>} appearance="primary" onClick={async()=>{try{await copyText(accountCopy.value);closeAccountCopy();setNotice({text:'账号资料已复制',error:false});}catch{setAccountCopyStatus('请选中下面的资料复制；电脑可按 Ctrl+C。');}}}>复制资料</Button></>}><Textarea aria-label="待复制的账号资料" value={accountCopy.value} readOnly rows={6} spellCheck={false} autoComplete="off" onFocus={event=>event.target.select()}/></Modal>}
    {latestDetailAccount&&<AccountDetails account={latestDetailAccount} onClose={()=>setDetailAccount(null)} onChange={refresh} onSession={getSession} onViewSession={viewSession} onRisk={requestRisk}/>}
    {riskRequest&&<Modal title={riskRequest.active?'移入风控账号池':'移回普通账号池'} onClose={()=>setRiskRequest(null)} actions={<><Button disabled={busy} onClick={()=>setRiskRequest(null)}>取消</Button><Button appearance="primary" disabled={busy} onClick={async()=>{const result=await perform('/api/inventory/risk-pool',riskRequest,riskRequest.active?'账号已移入风控池，充值列表已排除。':'账号已手动移回；满足未充值和空闲条件后可再次选择。');if(result){setRiskRequest(null);setAccountSelection({});setRiskSelection({});}}}>{riskRequest.active?'确认移入':'确认移回'}</Button></>}><p className="dialog-description">{riskRequest.active?`将 ${riskRequest.ids.length} 个账号暂缓充值，单个及批量充值不再显示这些账号。`:`将 ${riskRequest.ids.length} 个账号移回普通池。已有充值成功记录或仍有未核实任务的账号，仍不能创建新充值。此操作不会自动重试或兑换。`}</p></Modal>}
    {session&&<Modal title="Session 已转换为单行格式" onClose={closeSession} wide actions={<><span className="copy-status" role="status">{sessionCopy}</span><Button onClick={closeSession}>关闭</Button><Button icon={<Copy/>} appearance="primary" onClick={async()=>{try{await copyText(session.value);setSessionCopy('已复制');}catch{setSessionCopy('请选中内容后按 Ctrl+C 复制');}}}>复制 Session</Button></>}><p className="dialog-description">{session.email}</p><Textarea aria-label="转换后的单行 Session" value={session.value} readOnly rows={8} className="session-value" spellCheck={false}/></Modal>}
    {quote&&<Modal title="确认账号和卡密分配" onClose={()=>{if(!confirmBusy)setQuote(null);}} wide actions={<><Button disabled={confirmBusy} onClick={()=>setQuote(null)}>取消</Button><Button appearance="primary" disabled={confirmBusy} onClick={start}>{confirmBusy?'正在开始':'确认充值'}</Button></>}><p className="dialog-description">共 {quote.assignments.length} 个账号，使用 {productNames[quote.assignments[0]?.sku]} 卡密。确认后预检通过会直接提交兑换并消耗卡密。</p><div className="assignment-list">{quote.assignments.map(item=><div key={item.account_id}><span>{item.email}</span><code>{item.card}</code></div>)}</div></Modal>}
    {latestDetailTask&&<Modal title={'充值记录 '+latestDetailTask.sequence} onClose={()=>setDetailTask(null)} wide actions={<><Button onClick={()=>setDetailTask(null)}>关闭</Button>{latestDetailTask.source!=='session_extract'&&activeStatuses.has(latestDetailTask.status)&&<Button onClick={()=>perform('/api/inventory/task',{id:latestDetailTask.id,action:'stop'},'本地任务已停止。')}>停止任务</Button>}{latestDetailTask.status!=='completed'&&latestDetailTask.result_state!=='completed'&&<Button appearance="primary" disabled={busy} onClick={()=>latestDetailTask.source==='session_extract'?perform('/api/session/direct-task',{id:latestDetailTask.id,action:'refresh'},'正在查询原交易。'):perform('/api/inventory/task',{id:latestDetailTask.id,action:'refresh'},'正在查询原任务。')}>查询结果</Button>}</>}><Progress task={latestDetailTask}/>{latestDetailTask.source==='session_extract'&&<p className="field-note">Session 提取直充记录仅保留独立充值历史，不会创建账号池账号。</p>}</Modal>}
    {latestDetailCard&&<Modal title="卡密详情" onClose={()=>{setDetailCard(null);setCardCopyStatus('');}} actions={<><Button icon={<Copy/>} appearance="primary" onClick={copyCardCode}>复制完整卡密</Button><Button disabled={busy||!latestDetailCard.spec_editable} title={latestDetailCard.spec_edit_block_reason||undefined} onClick={()=>{setEditCardSpec(latestDetailCard.id);setDetailCard(null);}}>修改规格</Button><Button onClick={()=>{setDetailCard(null);setCardCopyStatus('');}}>关闭</Button></>}><dl className="detail-facts">{[['卡密',latestDetailCard.code],['库存规格',productNames[latestDetailCard.sku]],['状态',statusLabels[latestDetailCard.status]],['入库时间',time(latestDetailCard.created)],['最近核对',latestDetailCard.checked_at?time(latestDetailCard.checked_at):'尚未核对'],['本地使用时间',latestDetailCard.used_at?time(latestDetailCard.used_at):'尚未记录'],['上游充值邮箱',latestDetailCard.provider_account_email||'上游未返回'],['上游产品',latestDetailCard.provider_product||'上游未返回'],['上游套餐',latestDetailCard.provider_plan||'上游未返回'],['上游使用时间',latestDetailCard.provider_used_at?beijing(latestDetailCard.provider_used_at):'上游未返回']].map(([key,value])=><div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>{cardCopyStatus&&<p className="copy-status" role="status">{cardCopyStatus}</p>}{latestDetailCard.task_id&&<Button onClick={()=>{setDetailTask(latestDetailCard.task_id);setDetailCard(null);}}>查看对应充值记录</Button>}{latestDetailCard.check_error&&<p className="inline-error">{cardCheckErrorLabels[latestDetailCard.check_error_code]||latestDetailCard.check_error}</p>}<p className="field-note">上游邮箱仅显示脱敏结果。完整卡密只在你主动打开此详情时从本机解密返回，不进入列表、导出或普通日志。</p></Modal>}
    {latestSpecCard&&<CardSpecDialog key={latestSpecCard.id} card={latestSpecCard} onClose={()=>setEditCardSpec(null)} onSaved={sku=>{setEditCardSpec(null);setCardSelection({});setCardSku(sku);refresh('已修改卡密规格，原充值记录保留，未开始充值。');}}/>}
    {deleteRequest&&<Modal title="删除所选资料" onClose={()=>setDeleteRequest(null)} actions={<><Button onClick={()=>setDeleteRequest(null)}>取消</Button><Button appearance="primary" disabled={busy} onClick={async()=>{const result=await perform('/api/inventory/delete',deleteRequest,'已删除所选资料，充值历史仍保留。');if(result){setDeleteRequest(null);setAccountSelection({});setCardSelection({});}}}>确认删除</Button></>}><p className="dialog-description">将删除 {deleteRequest.ids.length} 条{deleteRequest.kind==='accounts'?'账号':'卡密'}资料。已使用卡密会从库存移除，使用标记和充值历史继续保留，重复导入也不会变成可用卡密。登录或充值处理中、核对中的条目不可删除；待核实任务可删除账号并保留充值记录，已使用待核实卡密可手动删除。</p></Modal>}
  </FluentProvider>;
}

createRoot(document.getElementById('root')).render(<AppErrorBoundary><RendererProvider renderer={renderer}><App/></RendererProvider></AppErrorBoundary>);
