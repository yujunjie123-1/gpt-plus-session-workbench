import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import lighthouse from 'lighthouse';
import * as chromeLauncher from 'chrome-launcher';

const root=fileURLToPath(new URL('..',import.meta.url));
const fixture=spawn('C:\\Users\\86180\\AppData\\Local\\Programs\\Python\\Python312\\python.exe',
  [fileURLToPath(new URL('../tests/serve_inventory_audit.py',import.meta.url))],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});
let browser,fixtureUrl;
try {
  const lines=createInterface({input:fixture.stdout});
  const {url}=await new Promise((resolve,reject)=>{
    const timeout=setTimeout(()=>reject(Error('Fixture did not start')),20000);
    lines.once('line',line=>{clearTimeout(timeout);resolve(JSON.parse(line));});
    fixture.once('error',reject);
  });
  fixtureUrl=new URL(url);
  browser=await chromeLauncher.launch({chromePath:'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    chromeFlags:['--headless=new','--disable-gpu','--no-first-run'],logLevel:'silent'});
  const result=await lighthouse(url,{port:browser.port,logLevel:'error',output:'json',onlyCategories:['performance','accessibility','best-practices'],
    formFactor:'desktop',screenEmulation:{mobile:false,width:1530,height:1000,deviceScaleFactor:1,disabled:false},
    throttlingMethod:'simulate',throttling:{rttMs:40,throughputKbps:10240,cpuSlowdownMultiplier:1}});
  const report={generatedAt:new Date().toISOString(),fixture:'24 synthetic accounts, independent four-category stock',
    categories:Object.fromEntries(Object.entries(result.lhr.categories).map(([key,value])=>[key,Math.round(value.score*100)])),
    metrics:Object.fromEntries(['first-contentful-paint','largest-contentful-paint','total-blocking-time','cumulative-layout-shift','speed-index']
      .map(key=>[key,{value:result.lhr.audits[key].numericValue,display:result.lhr.audits[key].displayValue}])),
    failedAudits:Object.values(result.lhr.audits).filter(a=>a.score!==null&&a.score<.9).map(a=>({id:a.id,title:a.title,score:a.score}))};
  await writeFile(fileURLToPath(new URL('../output/playwright/v2-lighthouse.json',import.meta.url)),JSON.stringify(report,null,2),'utf8');
  console.log(JSON.stringify(report));
} finally {
  if(browser)await browser.kill();
  if(fixtureUrl){
    try{await fetch(fixtureUrl.origin+'/api/shutdown',{method:'POST',headers:{Authorization:'Bearer '+fixtureUrl.hash.slice(1),Origin:fixtureUrl.origin,'Content-Type':'application/json'},body:'{}'});}
    catch{fixture.kill();}
  } else fixture.kill();
}
