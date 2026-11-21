/* Real browser, real read-only localhost research-map API. Generates no model requests. */
const fs = require("node:fs");
const path = require("node:path");
const {spawn} = require("node:child_process");
const assert = require("node:assert/strict");
const ROOT = path.resolve(__dirname, "..");
const address = new URL(process.argv[2] || "http://127.0.0.1:8600");
if (!["127.0.0.1","localhost"].includes(address.hostname) || address.protocol !== "http:") throw Error("Use local HTTP only");
const out = path.resolve(process.argv[3] || path.join(ROOT, ".cache", "research-map-check"));
const wait = ms => new Promise(r => setTimeout(r, ms));
const chrome = [process.env.CHROME_PATH, "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "/usr/bin/chromium"].find(f => f && fs.existsSync(f));
assert(chrome);
fs.mkdirSync(out, {recursive:true});
const profile = fs.mkdtempSync(path.join(ROOT, ".cache", "atlas-chrome-"));
fs.writeFileSync(path.join(profile,".atlas-owned-check"),"atlas-browser-test\n");
const browser = spawn(chrome, ["--headless=new", "--disable-gpu","--no-first-run",
  "--disable-background-networking","--disable-component-update","--disable-breakpad","--disable-crash-reporter",
  "--remote-debugging-port=0","--user-data-dir="+profile, "about:blank"], {windowsHide:true,stdio:"ignore"});
let socket;
(async()=>{
  const portFile=path.join(profile,"DevToolsActivePort");
  for(let i=0;!fs.existsSync(portFile) && i<250;i++)await wait(100);
  assert(fs.existsSync(portFile),"Headless browser did not open its isolated CDP profile");
  const [port,url]=fs.readFileSync(portFile,"utf8").trim().split(/\r?\n/);
  socket=new WebSocket("ws://127.0.0.1:"+port+url);
  await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
  const pending=new Map(), mapRequests=new Map(), mapResponses=[], errors=[],posts=[];let sequence=0,tab,faultedPage=null,recoveredPage=false,simulateBusy=false;
  socket.onmessage=e=>{
    const m=JSON.parse(e.data), cb=pending.get(m.id);
    if(cb){pending.delete(m.id);clearTimeout(cb.timer);return m.error?cb.reject(Error(m.error.message)):cb.resolve(m.result);}
    if (m.method==="Fetch.requestPaused") {
      const url=new URL(m.params.request.url);
      if (simulateBusy && !faultedPage) {
        faultedPage=url.pathname;
        call("Fetch.fulfillRequest",{requestId:m.params.requestId,responseCode:503,responseHeaders:[{name:"Content-Type",value:"application/json"}],
          body:Buffer.from('{"detail":"busy fixture"}').toString("base64")},m.sessionId).catch(e=>errors.push(e.message));
      } else {
        if (url.pathname===faultedPage && url.searchParams.has("retry")) recoveredPage=true;
        call("Fetch.continueRequest",{requestId:m.params.requestId},m.sessionId).catch(e=>errors.push(e.message));
      }
      return;
    }
    if(m.method==="Network.responseReceived") {
      const url=new URL(m.params.response.url);
      if (url.pathname==="/api/research-map") {
        mapResponses.push([url.search,m.params.response.status]);
        if (m.params.response.status===200) mapRequests.set(url.search,m.params.requestId);
      }
    }
    if(m.method==="Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if(m.method==="Network.requestWillBeSent" && m.params.request.method==="POST") posts.push(new URL(m.params.request.url).pathname);
  };
  function call(method,params={},sessionId=tab){
    return new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(Error("CDP timeout "+method));},20000);
      pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));});
  }
  async function evaluate(expression){
    const r=await call("Runtime.evaluate",{expression,awaitPromise:true,returnByValue:true});
    if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
  async function until(expression, timeout=22000){
    const until=Date.now()+timeout;
    while(Date.now()<until){const r=await evaluate(expression);if(r)return r;await wait(100);}
    const detail=await evaluate("({hash:location.hash,busy:document.querySelector('.rm-loading')?.textContent,error:document.querySelector('.rm-error')?.textContent,buttons:[...document.querySelectorAll('.rm-route-group [data-route]')].map(b=>[b.dataset.route,b.disabled,b.getAttribute('aria-pressed')])})");
    throw Error("Did not observe "+expression+"; state: "+JSON.stringify(detail)+"; recent API responses: "+JSON.stringify(mapResponses.slice(-8))+"; runtime errors: "+errors.join(" | ").slice(0,450)+"; visible text: "+(await evaluate("document.body.innerText.slice(-650)")));
  }
  async function capture(name){await wait(180);const s=await call("Page.captureScreenshot",{format:"png",captureBeyondViewport:false}); fs.writeFileSync(path.join(out,name+".png"),Buffer.from(s.data,"base64"));}
  async function viewport(w,h,dark=false){
    await call("Emulation.setDeviceMetricsOverride",{width:w,height:h,deviceScaleFactor:1,mobile:w<600});
    await call("Emulation.setEmulatedMedia",{features:[{name:"prefers-reduced-motion",value:"reduce"},{name:"prefers-color-scheme",value:dark?"dark":"light"}]});
  }
  const target=await call("Target.createTarget",{url:"about:blank"});
  tab=(await call("Target.attachToTarget",{targetId:target.targetId,flatten:true})).sessionId;
  await call("Page.enable");await call("Runtime.enable");await call("Network.enable");await call("Network.setCacheDisabled",{cacheDisabled:true});
  const data=await (await fetch(address.origin+"/api/research-map")).json();
  const count=data.rows.length*data.columns.length;
  const example=data.cells.filter(c=>c.documents>1).sort((a,b)=>b.experiments-a.experiments)[0];
  assert(example && count>0);
  const routes=[],layouts=[];
  for(const [name,w,h,dark] of [["desktop",1536,960,false],["laptop",1280,800,false],["mobile",390,844,false],["dark",1440,900,true]]){
    await viewport(w,h,dark);await call("Page.navigate",{url:address.origin+"/#/graph"});
    await until("document.querySelectorAll('[data-cell-row]').length==="+count);
    await evaluate("document.querySelector('#view').scrollTop=0");
    await capture(name);
    const layout=await evaluate("({width:innerWidth,bodyWidth:document.documentElement.scrollWidth,viewWidth:document.querySelector('#view').clientWidth,viewScrollWidth:document.querySelector('#view').scrollWidth,inspector:document.querySelector('.rm-inspector').getBoundingClientRect().top,firstCandidate:document.querySelector('[data-direction-index]')?.getBoundingClientRect().top})");
    layouts.push({name,...layout});assert(layout.bodyWidth<=w+1,name+" body overflow");assert(layout.viewScrollWidth<=layout.viewWidth+1,name+" view overflow");
  }
  await viewport(1440,900);
  await call("Page.navigate",{url:address.origin+"/#/graph?"+new URLSearchParams({row:example.row,column:example.column})});
  await until("document.querySelector('.rm-compare-button')");
  await evaluate("document.querySelector('.rm-compare-button').click()");
  await until("document.querySelector('.drawer.open .research-compare')");
  assert.equal(await evaluate("new Set([...document.querySelectorAll('.rc-picker [data-pick=paper]')].map(x=>x.value)).size"),2);
  assert.equal(await evaluate("document.querySelectorAll('.rc-picker').length"),2);
  assert(await evaluate("[...document.querySelectorAll('.rc-page-link')].every(a=>a.hash.startsWith('#/papers?doc=')&&a.hash.includes('page='))"));
  await capture("compare");
  await evaluate("document.getElementById('drawer-close').click()");
  await evaluate("document.querySelector('.rm-inspector [data-open-page]').click()");
  await until("document.querySelector('.drawer.open .paper-reader img')?.naturalWidth>0");
  await evaluate("document.getElementById('drawer-close').click()");
  for(const route of ["W2W","D2W","D2D",""]){
    await evaluate("document.querySelector('.rm-route-group [data-route=\""+route+"\"]').click()");
    await until("document.querySelector('.rm-route-group [data-route=\""+route+"\"]').getAttribute('aria-pressed')==='true'");
    routes.push({route,hash:await evaluate("location.hash")});
  }
  const controls = {material:false,contrasts:false,lookup:false,study:false,lineage:false,topic:false,samePaper:false};
  await evaluate("document.querySelector('.rm-materials select').value='cu_only';document.querySelector('.rm-materials select').dispatchEvent(new Event('change',{bubbles:true}))");
  await until("location.hash.includes('system=cu_only') && document.querySelector('.rm-materials select')?.value==='cu_only'");
  const materialResponse=await call("Network.getResponseBody",{requestId:mapRequests.get("?system=cu_only")});
  const pure=JSON.parse(materialResponse.base64Encoded ? Buffer.from(materialResponse.body,"base64").toString("utf8") : materialResponse.body);
  assert(Object.values(pure.record_details).every(c=>c.system==="cu_only"));
  controls.material=true;
  await evaluate("document.querySelector('[data-layer=contrast]').click()");
  await until("location.hash.includes('layer=contrast')");
  const uiCounts=await evaluate("[...document.querySelectorAll('[data-cell-row]')].map(b=>({row:b.dataset.cellRow,col:b.dataset.cellColumn,n:Number(b.querySelector('span').textContent)||0}))");
  const cellKeys=new Map(pure.cells.map(c=>[c.row+"|"+c.column,c]));
  // Compare against the exact HTTP response consumed by this browser, including during atomic review publication.
  const exact=uiCounts.every(c=>c.n===cellKeys.get(c.row+"|"+c.col)?.contrasts);
  assert(exact,"Material-scoped experimental contrasts and rendered counts differ");
  controls.contrasts=true;
  await call("Page.navigate",{url:address.origin+"/#/graph"});
  await until("document.querySelector('.atlas-lookup summary') && document.querySelector('.rm-materials select')?.value==='' && document.querySelector('[data-layer=all]')?.getAttribute('aria-pressed')==='true'");
  await evaluate("document.querySelector('.atlas-lookup summary').click()");
  await until("document.querySelector('.atlas-lookup input')===document.activeElement");
  await evaluate("const i=document.querySelector('.atlas-lookup input');i.value='r037';i.dispatchEvent(new Event('input'))");
  await until("document.querySelector('.atlas-hit .atlas-hit-link')");
  await evaluate("document.querySelector('.atlas-hit .atlas-hit-link').click()");
  await until("document.querySelector('.drawer.open .paper-reader img')?.naturalWidth>0");
  await evaluate("document.getElementById('drawer-close').click()");
  await evaluate("document.querySelector('.atlas-lookup').open=true");
  await until("document.querySelector('.atlas-lookup input')?.value==='r037'");
  await evaluate("const search=document.querySelector('.atlas-lookup input');search.value='j005';search.dispatchEvent(new Event('input'))");
  await until("document.querySelector('[data-hit-claim=j005]')");
  await call("Fetch.enable",{patterns:[{urlPattern:"*/api/papers/*/pages/*.png*",requestStage:"Request"}]});
  simulateBusy=true;
  await evaluate("document.querySelector('[data-hit-claim=j005]').click()");
  await until("document.querySelector('.drawer.open .viewer img')?.naturalWidth>0");
  assert(faultedPage && recoveredPage,"A temporary page-render 503 was retried and the intended physical page loaded");
  simulateBusy=false;await call("Fetch.disable");
  await evaluate("document.getElementById('drawer-close').click()");
  controls.lookup=true;controls.busyPageRecovery=true;
  await call("Page.navigate",{url:address.origin+"/#/graph?row=surface_passivation&column=thermomechanical_reliability"});
  await until("document.querySelector('[data-cell-row=surface_passivation][data-cell-column=thermomechanical_reliability]')?.classList.contains('is-selected') && document.querySelector('[data-text-leads]')");
  await evaluate("document.querySelector('[data-text-leads]').click()");
  await until("document.querySelector('.atlas-passage-list article mark')");
  const tiArticle = "[...document.querySelectorAll('.atlas-passage-list article')].find(el=>el.querySelector('h3').textContent.includes('Ti Passivation'))";
  assert(await evaluate(tiArticle + "?.querySelectorAll('mark').length===2"),
    await evaluate("document.querySelector('.atlas-passages')?.innerText.slice(0,1800)"));
  await evaluate(tiArticle + ".querySelector('summary').click()");
  await until("document.querySelector('.atlas-passage-list details[open] img')?.naturalWidth>0");
  await capture("text-passage");
  await evaluate(tiArticle + ".querySelector('summary').click()");
  await evaluate(tiArticle + ".querySelector('summary').click()");
  assert.equal(await evaluate(tiArticle + ".querySelectorAll('.viewer').length"),1);
  await evaluate("document.getElementById('drawer-close').click()");
  controls.fulltextPassages=true;
  await call("Page.navigate",{url:address.origin+"/#/graph?view=studies&case=C06"});
  await until("document.querySelector('.rn-reading')?.innerText.includes('28.6')");
  await capture("matched-study");controls.study=true;
  await call("Page.navigate",{url:address.origin+"/#/graph?view=studies&case=C01"});
  await until("document.querySelector('.rn-design-review [data-source-page]')");
  await evaluate("document.querySelector('.rn-design-review [data-source-page]').click()");
  await until("document.querySelector('.drawer.open .paper-reader img')?.naturalWidth>0");
  assert.equal(await evaluate("Number(document.querySelector('.drawer.open .paper-reader .viewer input').value)"),2);
  await evaluate("document.getElementById('drawer-close').click()");
  controls.circuitBaseline=true;
  await call("Page.navigate",{url:address.origin+"/#/graph?view=landscape&lens=papers&paper=reliability_gao_d2w_2021"});
  await until("document.querySelectorAll('.research-lineage [data-edge]').length>0");
  await capture("paper-lineage");
  await evaluate("document.querySelector('[data-open-citation]').click()");
  await until("document.querySelector('.drawer.open .paper-reader img')?.naturalWidth>0");
  await evaluate("document.getElementById('drawer-close').click()");
  controls.lineage=true;
  await evaluate("document.querySelector('.atlas-lenses a[href*=topics]').click()");
  await until("document.querySelector('.rl-network-panel')");
  await capture("topic-context");controls.topic=true;
  const sameIds=["doc_eba2ba217b78130d11ad_c001","doc_eba2ba217b78130d11ad_c002"];
  await call("Page.navigate",{url:address.origin+"/#/graph?view=matrix&compare="+sameIds.join(",")});
  await until("document.querySelector('.drawer.open .research-compare')?.innerText.includes('7.33')");
  const sameText=await evaluate("document.querySelector('.drawer.open .research-compare').innerText");
  assert.match(sameText,/同篇|同一篇/);assert.match(sameText,/2.44/);controls.samePaper=true;
  await capture("selected-pair");await evaluate("document.getElementById('drawer-close').click()");
  const jump=await evaluate("!!document.querySelector('[data-direction-index]')");
  if(jump){
    const dir=data.directions[0];
    await call("Page.navigate",{url:address.origin+"/#/graph?"+new URLSearchParams({row:dir.row,column:dir.column})});
    await until("document.querySelector('[data-refine-selected]')");
    await evaluate("document.querySelector('[data-refine-selected]').click()");
    await until("document.querySelector('#ideas-prompt')?.value.length>20");
    const draft=await evaluate("document.querySelector('#ideas-prompt').value");
    assert(draft.includes(dir.question),"Selected question arrived in editable idea draft");
    const original="保留我自行编辑的实验对照记录";
    await evaluate("const f=document.querySelector('#ideas-prompt');f.value="+JSON.stringify(original)+";f.dispatchEvent(new Event('input',{bubbles:true}));");
    const other=data.cells.find(c=>c.documents && !data.directions.some(d=>d.row===c.row && d.column===c.column));
    await evaluate("location.hash="+JSON.stringify("#/graph?"+new URLSearchParams({row:other.row,column:other.column})));
    await until("document.querySelector('[data-cell-draft]')");
    await evaluate("document.querySelector('[data-cell-draft]').click()");
    await until("document.querySelector('.map-draft-choice')");
    assert.equal(await evaluate("document.querySelector('#ideas-prompt').value"),original);
    await evaluate("[...document.querySelectorAll('.map-draft-choice button')].find(b=>b.textContent.includes('保留')).click()");
    assert.equal(await evaluate("document.querySelector('#ideas-prompt').value"),original);
    controls.existingDraftPreserved=true;
  }
  await call("Page.navigate",{url:address.origin+"/#/graph?focus="+encodeURIComponent(example.records[0])});
  await until("document.querySelector('.graph-stage canvas') && document.querySelector('[data-open-claim]')");
  await evaluate("document.querySelector('.graph-top a').click()");
  await until("document.querySelector('.research-map-page')");
  await evaluate("document.querySelector('.rc-coverage').open=true");
  await until("document.querySelector('[data-rc-readings]')");
  await evaluate("document.querySelector('[data-rc-readings]').click()");
  await until("document.querySelector('.rc-selection .rc-reading-finding')");
  await evaluate("document.querySelector('.rc-selection .rc-paper [data-rc-page]').click()");
  await until("document.querySelector('.drawer.open .paper-reader img')?.naturalWidth>0");
  await evaluate("document.getElementById('drawer-close').click()");
  controls.originalReading=true;
  await evaluate("document.querySelector('.atlas-lookup summary').click()");
  await evaluate("(()=>{const form=document.querySelector('.atlas-lookup form');form.querySelector('input').value='plasma activation';form.querySelector('input').dispatchEvent(new Event('input'));form.requestSubmit()})()");
  await until("location.hash.startsWith('#/search?') && document.querySelector('.result[data-i]')");
  assert.equal(await evaluate("document.querySelector('.search-box input[name=q]').value"),"plasma activation");
  controls.lookupStartsFulltext=true;
  assert.deepEqual(errors,[]);assert.deepEqual(posts,[]);
  const result={at:new Date().toISOString(),url:address.origin,totals:data.totals,layouts,routes,controls,comparison:example.row+" × "+example.column,draftHandoff:jump,legacyFocus:true,jsErrors:errors,postRequests:posts};
  fs.writeFileSync(path.join(out,"browser_check.json"),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
  await call("Target.closeTarget",{targetId:target.targetId},null);
  await call("Browser.close",{},null);
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{
  socket?.close();browser.kill();
  for (let i=0;i<30 && browser.exitCode===null && browser.signalCode===null;i++)await wait(80);
  const resolved=fs.realpathSync(profile),parent=fs.realpathSync(path.join(ROOT,".cache"));
  if (path.dirname(resolved)===parent && path.basename(resolved).startsWith("atlas-chrome-") &&
      fs.existsSync(path.join(resolved,".atlas-owned-check"))) {
    try { fs.rmSync(resolved,{recursive:true,force:true,maxRetries:3,retryDelay:120}); } catch (_) { /* Keep a locked test profile. */ }
  }
});

