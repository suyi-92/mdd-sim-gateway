// Real App with alternating device GETs and WebSocket status events; no hardware access.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { createServer } from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
const { chromium } = createRequire(import.meta.url)('playwright')
const dist = path.resolve('webui/dist')
const devices = ['modem', 'reader'].map((kind, index) => ({
  id:`${kind}-fixture`, name:`Fixture ${kind}`, device_type:kind, present:true, instance_id:String(index+1),
  sim:{present:true,name:`Fixture ${kind}`}, provisioning:{state:'ready'},
  capabilities:{vowifi:{desired:!!index,actual:index?'on':'off',reason:''},
    cellular:{desired:false,actual:kind==='reader'?'unsupported':'off'},
    flight:{desired:false,actual:kind==='reader'?'unsupported':'off'}},
  cellular:kind==='modem'?{registration:'roaming',operator_code:'00101',signal:88}:null,
}))
let reads=0
const server=createServer((request,response)=>{
  const url=new URL(request.url,'http://fixture')
  if(url.pathname.startsWith('/api/')) {
    const values={
      '/api/auth/status':{configured:true,authenticated:true,csrf:'fixture'},
      '/api/devices':{devices,discovering:false}, '/api/cards':{cards:[]},
      '/api/instances':{instances:devices.map((d,index)=>({id:String(index+1),name:d.name,
        status:{state:index?'OK':'STOPPED'}}))}, '/api/system/status':{version:'fixture'},
      '/api/instances/2/softphone':{enabled:false},
    }
    if(url.pathname==='/api/devices')reads++
    response.writeHead(200,{'Content-Type':'application/json'});response.end(JSON.stringify(values[url.pathname]||{}));return
  }
  const file=path.resolve(dist,'.'+(url.pathname==='/'?'/index.html':url.pathname))
  if(!file.startsWith(dist+path.sep)||!fs.existsSync(file)){response.writeHead(404);response.end();return}
  response.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html')
  response.end(fs.readFileSync(file))
})
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
let browser
try {
  browser=await chromium.launch({headless:true})
  const page=await browser.newPage({viewport:{width:1440,height:1000}})
  const errors=[];page.on('pageerror',error=>errors.push(error.message))
  await page.addInitScript(()=>{
    localStorage.setItem('mdd-language','en')
    window.WebSocket=class {constructor(){window.fixtureSocket=this}close(){}send(){}}
  })
  await page.goto(`http://127.0.0.1:${server.address().port}/#/overview`)
  const details=page.locator('.u-capability:visible').filter({has:page.getByRole('switch',
    {name:'VoWiFi / WiFi Calling',exact:true})}).locator('.u-cap-detail')
  await details.first().waitFor()
  const expected=['VoWiFi is disabled.','Working — connected to the carrier over Wi-Fi.']
  const output='/tmp/mdd-capability-status-browser';fs.mkdirSync(output,{recursive:true})
  for(const width of [1440,900,390]) {
    await page.setViewportSize({width,height:1000})
    for(let cycle=0;cycle<3;cycle++) {
      await page.evaluate(()=>{
        window.fixtureSocket.onmessage({data:JSON.stringify({type:'status',instance:'1',state:'STOPPED',label:'Stopped',reason:'Stopped.'})})
        window.fixtureSocket.onmessage({data:JSON.stringify({type:'status',instance:'2',state:'OK',label:'Working',reason:'IMS registered.'})})
      })
      assert.deepEqual(await details.allTextContents(),expected,'engine events keep capability wording stable')
      const before=reads
      await page.evaluate(()=>window.dispatchEvent(new Event('online')))
      for(let attempt=0;reads===before&&attempt<100;attempt++)await new Promise(resolve=>setTimeout(resolve,10))
      assert.ok(reads>before,'a real device snapshot was received')
      await page.waitForTimeout(30)
      assert.deepEqual(await details.allTextContents(),expected,'device snapshots use the same wording')
    }
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
    await page.screenshot({path:path.join(output,`capability-stable-${width}.png`),fullPage:true})
  }
  // The device detail page uses the same component, and real errors are not frozen.
  await page.evaluate(()=>location.hash='#/devices')
  await page.locator('.u-cap-detail:visible').filter({hasText:/^VoWiFi is disabled\.$/}).waitFor()
  await page.evaluate(()=>window.fixtureSocket.onmessage({data:JSON.stringify({type:'status',instance:'1',
    state:'STOPPED',reason:'Stopped.'})}))
  await page.locator('.u-cap-detail:visible').filter({hasText:/^VoWiFi is disabled\.$/}).waitFor()
  await page.evaluate(()=>location.hash='#/overview')
  await page.evaluate(()=>window.fixtureSocket.onmessage({data:JSON.stringify({type:'status',instance:'2',
    state:'ERROR',reason:'Fixture registration failure'})}))
  await page.locator('.u-cap-detail:visible').filter({hasText:'Fixture registration failure'}).waitFor()
  assert.deepEqual(errors,[])
  console.log('PASS: alternating HTTP/WS, modem/reader, overview/device detail, genuine fault, 1440/900/390px')
} finally {await browser?.close();await new Promise(resolve=>server.close(resolve))}
