/* Independent, read-only research-map browser. JSON lines on stdin; DOM evidence on stdout. */
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const readline = require('node:readline');
const ROOT=path.resolve(__dirname,'..');
const origin=process.argv[2] || 'http://127.0.0.1:8612';
if(!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(origin))throw Error('Local origin only');
const out=path.resolve(process.argv[3] || 'pilot/reports/research_map_20261107/stage2_manual');
fs.mkdirSync(out,{recursive:true});
const profile=fs.mkdtempSync(path.join(ROOT,'.cache','manual-research-map-'));
const browser=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',[
 '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
 '--disable-background-networking','--disable-component-update','--disable-breakpad','--disable-crash-reporter',
 '--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'
],{windowsHide:true,stdio:'ignore'});
const wait=ms=>new Promise(r=>setTimeout(r,ms));
let socket,tab,seq=0,transcript=[],events=[];
const pending=new Map();
function call(method,params={},sessionId=tab){return new Promise((resolve,reject)=>{
 const id=++seq;const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout '+method));},20000);
 pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));
});}
async function evaluate(expression){const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);return r.result.value;}
async function snapshot(){return evaluate(`({url:location.href,title:document.title,text:document.body.innerText,buttons:[...document.querySelectorAll('button')].filter(e=>e.getBoundingClientRect().width&&e.getBoundingClientRect().height).map((e,i)=>({index:i,text:e.innerText,attrs:[...e.attributes].filter(a=>a.name.startsWith('data-')||a.name==='id'||a.name.startsWith('aria-')).reduce((a,c)=>(a[c.name]=c.value,a),{})})),selects:[...document.querySelectorAll('select')].map((e,i)=>({index:i,value:e.value,attrs:[...e.attributes].reduce((a,c)=>(a[c.name]=c.value,a),{}),options:[...e.options].map(o=>({value:o.value,text:o.text,disabled:o.disabled}))})),links:[...document.querySelectorAll('a')].filter(e=>e.getBoundingClientRect().width&&e.getBoundingClientRect().height).map((e,i)=>({index:i,text:e.innerText,href:e.href,target:e.target})),images:[...document.images].filter(e=>e.getBoundingClientRect().width&&e.getBoundingClientRect().height).map(e=>({src:e.src,loaded:e.complete&&e.naturalWidth>0}))})`);}
async function capture(name){const r=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});const f=path.join(out,name+'.png');fs.writeFileSync(f,Buffer.from(r.data,'base64'));return f;}
(async()=>{
 const portfile=path.join(profile,'DevToolsActivePort');
 for(let i=0;!fs.existsSync(portfile)&&i<250;i++)await wait(100);
 if(!fs.existsSync(portfile))throw Error('Isolated Chrome did not expose debugging endpoint');
 const [port,url]=fs.readFileSync(portfile,'utf8').trim().split(/\r?\n/);
 socket=new WebSocket('ws://127.0.0.1:'+port+url);
 await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
 socket.onmessage=e=>{const m=JSON.parse(e.data);const cb=pending.get(m.id);if(cb){pending.delete(m.id);clearTimeout(cb.timer);return m.error?cb.reject(Error(m.error.message)):cb.resolve(m.result);}if(m.method==='Network.requestWillBeSent'&&m.params.request.method==='POST')events.push({kind:'post',url:m.params.request.url});if(m.method==='Runtime.exceptionThrown')events.push({kind:'js_error',details:m.params.exceptionDetails});};
 const t=await call('Target.createTarget',{url:'about:blank'},null);tab=(await call('Target.attachToTarget',{targetId:t.targetId,flatten:true},null)).sessionId;
 await call('Page.enable');await call('Runtime.enable');await call('Network.enable');
 await call('Emulation.setDeviceMetricsOverride',{width:1600,height:1100,deviceScaleFactor:1,mobile:false});
 await call('Page.navigate',{url:origin+'/#/graph'});await wait(1200);
 console.log(JSON.stringify({ready:true,profile,snapshot:await snapshot()}));
 const rl=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
 for await(const line of rl){if(!line.trim())continue;let command,result;
  try{command=JSON.parse(line);if(command.action==='close')break;
   if(command.action==='snapshot')result=await snapshot();
   else if(command.action==='evaluate')result=await evaluate(command.expression);
   else if(command.action==='capture')result=await capture(command.name);
   else if(command.action==='navigate'){if(!String(command.url).startsWith(origin))throw Error('Origin mismatch');result=await call('Page.navigate',{url:command.url});}
   else if(command.action==='targets')result=await call('Target.getTargets',{},null);
   else if(command.action==='attach'){tab=(await call('Target.attachToTarget',{targetId:command.targetId,flatten:true},null)).sessionId;await call('Page.enable');await call('Runtime.enable');result={attached:command.targetId};}
   else throw Error('Unknown action');
   if(command.wait_ms)await wait(Math.min(command.wait_ms,3000));
   if(command.observe)result={action_result:result,snapshot:await snapshot()};
   transcript.push({at:new Date().toISOString(),command,result});
   fs.writeFileSync(path.join(out,'browser_transcript.json'),JSON.stringify({origin,transcript,events},null,2));
   console.log(JSON.stringify({ok:true,result}));
  }catch(e){console.log(JSON.stringify({ok:false,error:e.message}));}
 }
 await call('Browser.close',{},null);
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>{socket?.close();browser.kill();});
