// Full App regression with fictional API data; no production service or SIM is touched.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { createServer } from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
const { chromium } = createRequire(import.meta.url)('playwright')
const dist = path.resolve('webui/dist')
let line = { id:'1', name:'Fixture SIM', iccid:'fixture-card', imsi:'001010123456789', msisdn:'', mcc:'001', mnc:'01',
  reader:'fixture-reader', reader_index:0, provisioning_state:'ready', status:{state:'STOPPED'}, enabled:true }
const device = () => ({ id:'modem-fixture',name:'Fixture modem',device_type:'modem',present:true,
  instance_id:'1',reader:'fixture-reader',sim:{present:true,name:line.name,number:line.msisdn,number_country:'us'},
  capabilities:{vowifi:{desired:false,actual:'off'},cellular:{desired:false,actual:'off'}},
  cellular:{registration:'roaming',operator_code:'00101'},provisioning:{state:'ready'} })
let holdReads=false, failReads=false, pending=[], posts=0
const server = createServer((request,response) => {
  const url = new URL(request.url,'http://fixture')
  const json = (value,status=200) => { response.writeHead(status,{'Content-Type':'application/json'});response.end(JSON.stringify(value)) }
  if(url.pathname.startsWith('/api/')) {
    if(url.pathname==='/api/instances' && request.method==='POST') {
      let body='';request.on('data',chunk=>body+=chunk);request.on('end',()=>{
        const saved=JSON.parse(body);assert.equal(saved.msisdn_source,'manual');
        line={...line,...saved};posts++;json({...line,applied:false,number_country:'us'})
      });return
    }
    const values = {
      '/api/auth/status':{configured:true,authenticated:true,csrf:'fixture'},
      '/api/instances':{instances:[line]}, '/api/devices':{devices:[device()],discovering:false},
      '/api/cards':{cards:[{name:'fixture-reader',index:0,present:true,iccid:'fixture-card',matched:'1',hardware_id:'modem-fixture'}]},
      '/api/readers':{readers:['fixture-reader']}, '/api/settings':{},
      '/api/system/status':{version:'fixture'}, '/api/instances/1/softphone':{enabled:false},
      '/api/instances/1/messages/threads':{threads:[]}, '/api/instances/1/messages/binary':{payloads:[]},
    }
    if(['/api/instances','/api/devices'].includes(url.pathname)) {
      if(failReads) return json({detail:'fixture network unavailable'},503)
      if(holdReads) { const old=structuredClone(values[url.pathname]);pending.push(()=>json(old));return }
    }
    return json(values[url.pathname]||{})
  }
  const file = path.resolve(dist,'.'+(url.pathname==='/'?'/index.html':url.pathname))
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
    localStorage.setItem('mdd-language','en');sessionStorage.setItem('mdd-device-tab','sim')
    window.WebSocket=class {constructor(){window.fixtureSocket=this}close(){}send(){}}
  })
  const base=`http://127.0.0.1:${server.address().port}`
  await page.goto(base+'/#/devices')
  const input=page.getByText('Phone number (MSISDN)',{exact:true}).locator('..').locator('input')
  await input.waitFor();await input.fill('+15555550100')
  holdReads=true
  await page.evaluate(()=>window.dispatchEvent(new Event('online')))
  await page.waitForTimeout(100)
  assert.equal(pending.length,2,'both old snapshots are in flight before save')
  await page.getByRole('button',{name:'Save',exact:true}).click()
  await page.getByText('Saved.',{exact:true}).waitFor()
  assert.equal(posts,1)
  // Follow-up GETs fail, while the pre-save responses complete out of order.
  holdReads=false;failReads=true;pending.reverse().forEach(done=>done());pending=[]
  await page.waitForTimeout(100)
  const output='/tmp/mdd-saved-number-browser';fs.mkdirSync(output,{recursive:true})
  for(const width of [1440,900,390]) {
    await page.setViewportSize({width,height:1000})
    for(const view of ['overview','messages','calls']) {
      await page.evaluate(view=>location.hash='#/'+view,view)
      const displayed=page.locator('.u-copyable-text:visible').filter({hasText:'+1 5555550100'}).first()
      await displayed.waitFor()
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
    }
    await page.screenshot({path:path.join(output,`saved-number-${width}.png`),fullPage:true})
  }
  // A save in another browser reaches this one even while GET polling fails.
  await page.evaluate(()=>window.fixtureSocket.onmessage({data:JSON.stringify({type:'instance_updated',line:{
    id:'1',iccid:'fixture-card',name:'Fixture SIM',msisdn:'+15555550102',msisdn_source:'manual',number_country:'us'}})}))
  await page.locator('.u-copyable-text:visible').filter({hasText:'+1 5555550102'}).first().waitFor()
  await page.evaluate(()=>location.hash='#/devices')
  await input.waitFor();assert.equal(await input.inputValue(),'+15555550102')
  await input.fill('+15555550103')
  await page.evaluate(()=>window.fixtureSocket.onmessage({data:JSON.stringify({type:'instance_updated',line:{
    id:'1',iccid:'fixture-card',msisdn:'+15555550104',msisdn_source:'manual',number_country:'us'}})}))
  assert.equal(await input.inputValue(),'+15555550103','background updates preserve an unsaved draft')
  assert.deepEqual(errors,[])
  console.log('PASS: saved number, stale GETs, failed refresh, cross-session event, draft preservation, 1440/900/390px')
} finally {await browser?.close();await new Promise(resolve=>server.close(resolve))}
