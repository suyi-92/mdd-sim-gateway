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
 const [cards, setCards] = React.useState([{name:'fixture-reader',index:0,present:true,iccid:'card-a',generation:1}]);
 window.changeCard = (iccid, generation) => setCards([{name:'fixture-reader',index:0,present:true,iccid,generation}]);
 window.deliver = msg => eventHandler?.(msg);
 return <I18nProvider><Esim cards={cards} instances={[]} subscribe={fn => {eventHandler=fn;return ()=>{eventHandler=null}}}/></I18nProvider>
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
const output = '/tmp/mdd-esim-identity-browser'
fs.mkdirSync(output, {recursive:true})
try {
 browser = await chromium.launch({headless:true})
 const page = await browser.newPage()
 const errors=[]; page.on('pageerror', e=>errors.push(e.message))
 await page.addInitScript(()=>localStorage.setItem('mdd-language','en'))
 let current='card-a', pending, cacheReads=0, notificationStatus=null, downloadOperation=null
 const payload = (card) => ({ok:true,cached:true,ts:100,ses:[{id:'one',eid:'fixture-euicc',profiles:[{iccid:card,profileNickname:card,profileState:'enabled',notification_status:notificationStatus}]}]})
 await page.route('**/api/**', async route => {
   const url=new URL(route.request().url())
   if(url.pathname==='/api/esim/status') return route.fulfill({json:{available:true}})
   if(url.pathname==='/api/esim/download/operation') return route.fulfill({json:{operation:downloadOperation}})
   if(url.pathname==='/api/esim/download' && route.request().method()==='POST') {
     downloadOperation={operation_id:'0123456789abcdef01234567',state:'running',step:'es10b_prepare_download',generation:1,updated_at:100}
     return route.fulfill({json:{ok:true,started:true,operation_id:downloadOperation.operation_id,operation:downloadOperation}})
   }
   if(url.pathname==='/api/esim/chip/cached') { cacheReads++; return route.fulfill({json:payload(current)}) }
   if(url.pathname==='/api/esim/chip') { pending=route; return }
   throw new Error('Unexpected API '+url.pathname)
 })
 const address=server.httpServer.address()
 await page.goto('http://127.0.0.1:'+address.port+'/fixture')
 await page.getByText('card-a',{exact:true}).last().waitFor()
 // Initial operation status was idle. Starting a new download must start polling; no WS
 // completion is delivered, so only the persisted GET can finish this card.
 await page.getByRole('button',{name:'Download eSIM',exact:true}).click()
 await page.getByText('LPA activation code',{exact:true}).locator('..').locator('textarea').fill('LPA:1$smdp.example.invalid$fixture-match')
 await page.locator('.u-download-action').click()
 await page.getByText('Downloading…',{exact:true}).waitFor()
 downloadOperation={...downloadOperation,state:'success',step:'completed',generation:1,updated_at:101,finished_at:101}
 await page.getByText('Download complete',{exact:true}).waitFor()
 // An old live response must not replace a same-name replacement card cache.
 await page.getByRole('button',{name:'Load',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('.u-load-action')?.textContent.includes('Loading'))
 while(!pending) await new Promise(r=>setTimeout(r,10))
 current='card-b'
 await page.evaluate(()=>window.changeCard('card-b',2))
 await page.getByText('card-b',{exact:true}).last().waitFor()
 await pending.fulfill({json:payload('stale-card-a')}); pending=null
 await page.waitForTimeout(100)
 assert.equal(await page.getByText('stale-card-a',{exact:true}).count(),0)
 assert.ok(cacheReads>=2)
 // Delayed old generation event cannot mark a new page profile as active.
 await page.evaluate(()=>window.deliver({type:'esim_profile',reader:'fixture-reader',generation:1,iccid:'card-a',event:'recovery_error'}))
 assert.equal(await page.getByText('The profile is enabled, but automatic line recovery failed. Start the line from Devices.',{exact:true}).count(),0)
 await page.evaluate(()=>window.deliver({type:'esim_profile',reader:'fixture-reader',generation:2,iccid:'card-b',event:'line_disabled',profile_state:'enabled'}))
 await page.getByText('Profile enabled. VoWiFi is off for this device; enable it from Devices when needed.',{exact:true}).waitFor()
 for(const width of [1440,900,390]) {
   await page.setViewportSize({width,height:900})
   const action = page.getByRole('button',{name:'Rename',exact:true})
   const before = await action.boundingBox()
   await page.evaluate(()=>window.deliver({type:'esim_notification_status',reader:'fixture-reader',iccid:'card-b',notification_status:{state:'failed',reason_code:'reader_busy'}}))
   await page.getByText(/The reader remained busy; the eSIM notification is still pending\./).waitFor()
   assert.deepEqual(await action.boundingBox(),before,'notification feedback must not move profile actions')
   await page.evaluate(()=>window.deliver({type:'esim_recovery_status',reader:'fixture-reader',iccid:'card-b',recovery_status:{state:'waiting_flight_mode',phase:'baseband_initialization'}}))
   await page.getByText('Profile enabled; cellular initialization will continue when flight mode is turned off.',{exact:true}).waitFor()
   assert.deepEqual(await action.boundingBox(),before,'cellular recovery feedback must not move profile actions')
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
   await page.screenshot({path:path.join(output,`identity-${width}.png`),fullPage:true})
   await page.evaluate(()=>window.deliver({type:'esim_recovery_status',reader:'fixture-reader',iccid:'card-b',recovery_status:{state:'success'}}))
   await page.evaluate(()=>window.deliver({type:'esim_notification_status',reader:'fixture-reader',iccid:'card-b',notification_status:{state:'processed'}}))
   await page.getByText('card-b',{exact:true}).last().waitFor()
 }
 // Persisted delivery failure survives a page reload and a new card-monitor generation.
 notificationStatus={state:'failed',reason_code:'reader_busy'}
 await page.reload()
 await page.evaluate(()=>window.changeCard('card-b',4))
 await page.getByText(/The reader remained busy; the eSIM notification is still pending\./).waitFor()
 await page.evaluate(()=>window.deliver({type:'esim_notifications',reader:'fixture-reader',se_id:'one'}))
 assert.equal(await page.getByText(/The reader remained busy; the eSIM notification is still pending\./).count(),0)
 notificationStatus=null
 // Late failed response after card replacement must not clear new results or show an error.
 await page.getByRole('button',{name:'Load',exact:true}).click()
 while(!pending) await new Promise(r=>setTimeout(r,10))
 current='card-c'; await page.evaluate(()=>window.changeCard('card-c',3))
 await page.getByText('card-c',{exact:true}).last().waitFor()
 await pending.fulfill({status:500,json:{detail:'obsolete failure'}}); pending=null
 await page.waitForTimeout(100)
 assert.equal(await page.getByText('obsolete failure',{exact:true}).count(),0)
 await page.getByRole('button',{name:'Load',exact:true}).click()
 while(!pending) await new Promise(r=>setTimeout(r,10))
 await page.evaluate(()=>window.unmount())
 await pending.fulfill({json:payload('unmounted')})
 await page.waitForTimeout(100)
 assert.deepEqual(errors,[])
 console.log('PASS: same-name identity replacement, old HTTP success/failure/finally, old WS generation, unmount, 1440/900/390px')
} finally {
 await browser?.close(); await server.close()
}
