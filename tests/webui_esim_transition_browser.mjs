// Isolated React fixture, fictional identities and deferred HTTP replies only.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { createServer } from '../webui/node_modules/vite/dist/node/index.js'
import path from 'node:path'
import fs from 'node:fs'
const require = createRequire(import.meta.url)
const { chromium } = require('playwright')
const root = path.resolve('webui')
const fixture = `import React from 'react';
import {createRoot} from 'react-dom/client';
import Esim from '/src/views/Esim.jsx';
import {I18nProvider} from '/src/i18n.jsx';
import '/src/index.css';
let eventHandler;
function App() {
 const [cards, setCards] = React.useState([{name:'fixture-reader',index:0,present:true,iccid:'card-a',generation:1,hardware_id:'modem-1'}]);
 const [devices, setDevices] = React.useState([{id:'modem-1',present:true,hardware_generation:'usb-1'}]);
 window.changeCards = setCards;
 window.changeCard = (iccid, generation) => setCards([{name:'fixture-reader',index:0,present:true,iccid,generation,hardware_id:'modem-1'}]);
 window.changeDevices = setDevices;
 window.changeIdentityState = state => setCards(cards => cards.map(card => ({...card,identity_state:state})));
 window.deliver = msg => eventHandler?.(msg);
 return <I18nProvider><Esim cards={cards} devices={devices} instances={[]} subscribe={fn => {eventHandler=fn;return ()=>{eventHandler=null}}}/></I18nProvider>
}
const root=createRoot(document.getElementById('root')); window.unmount=()=>root.unmount(); root.render(<App/>);`
const server = await createServer({ root, configFile: false, plugins: [{
  name: 'recovery-fixture',
  configureServer(server) {
server.middlewares.use('/fixture', async (_req, res) => {
 res.setHeader('Content-Type','text/html');
 res.end(await server.transformIndexHtml('/fixture','<html><body><div id="root"></div><script type="module" src="/recovery-fixture.jsx"></script></body></html>'))
})
  },
  resolveId(id) { if (id === '/recovery-fixture.jsx') return path.join(root, 'recovery-fixture.jsx') },
  load(id) { if (id === path.join(root, 'recovery-fixture.jsx')) return fixture },
}], server: { host:'127.0.0.1', port:0 } })
await server.listen()
let browser
try {
 browser = await chromium.launch({headless:true, ...(process.env.MDD_BROWSER_EXECUTABLE ? {executablePath:process.env.MDD_BROWSER_EXECUTABLE} : {})})
 const page=await browser.newPage(), errors=[]
 page.on('pageerror', e=>errors.push(e.message))
 await page.addInitScript(()=>localStorage.setItem('mdd-language','en'))
 let current='card-a', generation=1, cacheAvailable=true, cacheReads=0, pending, operation=null, downloads=0
 const profiles=[{iccid:'card-a',profileNickname:'Current fixture'},{iccid:'card-b',profileNickname:'Club fixture'}]
 const snapshot=()=>({cached:true,ts:100,generation,ses:[{id:'default',eid:'fixture-euicc',profiles:profiles.map(p=>({...p,profileState:p.iccid===current?'enabled':'disabled'}))}]})
 await page.route('**/api/**', async route=>{
  const url=new URL(route.request().url())
  if(url.pathname==='/api/esim/status') return route.fulfill({json:{available:true}})
  if(url.pathname==='/api/esim/chip/cached') {cacheReads++;return route.fulfill({json:cacheAvailable?snapshot():{cached:false}})}
  if(url.pathname==='/api/esim/download/operation') return route.fulfill({json:{operation}})
  if(url.pathname==='/api/esim/download') {
   operation={operation_id:'fixture-download-'+(++downloads),state:'running',generation,step:'started'}
   return route.fulfill({json:{ok:true,operation}})
  }
  if(url.pathname==='/api/esim/profiles/card-b/enable') {pending=route;return}
  throw new Error('Unexpected API '+url.pathname)
 })
 const url='http://127.0.0.1:'+server.httpServer.address().port+'/fixture'
 for(const width of [1440,900,390]) {
  current='card-a';generation=1;cacheAvailable=true;operation=null;pending=null
  await page.setViewportSize({width,height:1000});await page.goto(url)
  await page.getByText('Club fixture',{exact:true}).waitFor()
  await page.getByRole('button',{name:'Download eSIM',exact:true}).click()
  await page.locator('textarea').fill('LPA:1$fixture.example.invalid$fixture-code')
  await page.locator('.u-download-action').click()
  await page.getByText('Downloading…',{exact:true}).waitFor()
  await page.evaluate(id=>window.deliver({type:'esim_download',reader:'fixture-reader',generation:1,operation_id:id,event:'preview',step:'es8p_metadata_parse',metadata:{profileName:{unexpected:true}}}),operation.operation_id)
  await page.waitForTimeout(100)
  assert.deepEqual(errors,[], 'Unexpected preview fields must not blank the page')
  operation={...operation,state:'success',step:'completed',line_recovery:'not_needed',generation:2};generation=2
  cacheAvailable=false
  await page.evaluate(()=>window.changeCard('card-a',2))
  await page.getByText('Current fixture',{exact:true}).waitFor()
  assert.equal(await page.getByRole('button',{name:'Enable',exact:true}).isDisabled(),true)
  const reads=cacheReads
  await page.waitForTimeout(2200);assert.ok(cacheReads>reads,'Cache miss must retry without a manual Load')
  cacheAvailable=true
  await page.getByText('Download complete',{exact:true}).waitFor()
  await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent==='Enable'&&!b.disabled))
  await page.getByRole('button',{name:'Enable',exact:true}).click()
  await page.locator('[data-switch-target=true]').waitFor()
  assert.ok((await page.locator('[data-switch-target=true]').innerText()).includes('Club fixture'))
  assert.ok((await page.locator('[data-switch-target=true]').innerText()).includes('Switching'))
  fs.mkdirSync('/tmp/mdd-dji-browser',{recursive:true})
  await page.screenshot({path:'/tmp/mdd-dji-browser/target-'+width+'.png',fullPage:true})
  while(!pending) await new Promise(r=>setTimeout(r,10))
  cacheAvailable=false
  await page.evaluate(()=>window.changeCards([{name:'other-reader',index:8,present:true,iccid:'unrelated',generation:1}]))
  await page.waitForTimeout(100)
  assert.equal(await page.locator('select').first().inputValue(),'fixture-reader','Temporary reader loss must not select another card')
  assert.equal(await page.getByText('Club fixture',{exact:true}).count(),1)
  assert.equal(await page.getByRole('button',{name:'Load',exact:true}).isDisabled(),true)
  assert.equal(await page.locator('[data-switch-target=true]').count(),1)
  current='card-b';generation=3
  await page.evaluate(()=>window.changeCard('card-b',3))
  await pending.fulfill({json:{ok:true,card:{identity_state:'confirmed',iccid:'card-b'},recovery_skipped:'vowifi_disabled'}});pending=null
  await page.waitForTimeout(100);cacheAvailable=true
  await page.getByText('Profile enabled. VoWiFi is off for this device; enable it from Devices when needed.',{exact:true}).waitFor()
  await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent==='Enable'&&!b.disabled))
  assert.equal(await page.getByText('Club fixture',{exact:true}).count(),1)
  assert.equal(await page.getByText('Current fixture',{exact:true}).count(),1)
  assert.equal(await page.getByRole('button',{name:'Disable',exact:true}).count(),1)
  fs.mkdirSync('/tmp/mdd-dji-browser',{recursive:true})
  await page.screenshot({path:'/tmp/mdd-dji-browser/switch-'+width+'.png',fullPage:true})
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,
    JSON.stringify(await page.evaluate(()=>[...document.querySelectorAll('*')].filter(el=>el.getBoundingClientRect().right>innerWidth).map(el=>[el.tagName,el.className,el.getBoundingClientRect().width]).slice(0,12))))
  cacheAvailable=false
  await page.evaluate(()=>{window.changeDevices([{id:'modem-1',present:true,hardware_generation:'usb-2'}]);window.changeCard('replacement',4)})
  await page.getByText('Club fixture',{exact:true}).waitFor({state:'hidden'})
 }
 assert.deepEqual(errors,[])
 cacheAvailable=true;current='card-a';generation=1
 profiles[0].local_label={unexpected:true}
 await page.reload()
 await page.getByRole('button',{name:'Reload view',exact:true}).waitFor()
 delete profiles[0].local_label
 const previousDownloads=downloads
 await page.getByRole('button',{name:'Reload view',exact:true}).click()
 await page.getByText('Club fixture',{exact:true}).waitFor()
 assert.equal(downloads,previousDownloads,'Reloading a failed view must not replay writes')
 assert.equal(await page.getByRole('button',{name:'Reload view',exact:true}).count(),0)
 console.log('PASS: download generation/preview, cache miss retry, target highlight, temporary reader loss, profile switch recovery, physical replacement fence, 1440/900/390 widths')
} finally {await browser?.close();await server.close()}
