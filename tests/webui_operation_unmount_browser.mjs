// Deferred local API fixtures only: accepted hardware workflows must outlive their view.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { createServer } from '../webui/node_modules/vite/dist/node/index.js'
import path from 'node:path'
const require = createRequire(import.meta.url)
const { chromium } = require('playwright')
const root = path.resolve('webui')
const fixture = `import React from 'react';
import {createRoot} from 'react-dom/client';
import Esim from '/src/views/Esim.jsx';
import {BackupImport} from '/src/views/BackupTransfer.jsx';
import {I18nProvider} from '/src/i18n.jsx';
import '/src/index.css';
function App() {
 const [view,setView]=React.useState('esim');
 const [cards,setCards]=React.useState([{name:'fixture-reader',index:0,present:true,iccid:'fixture-active',generation:1,matched:'7'}]);
 window.navigate=setView;
 window.changeCard=()=>setCards([{name:'fixture-reader',index:0,present:true,iccid:'replacement-card',generation:2}]);
 window.refreshes=window.refreshes||0;window.toasts=window.toasts||[];window.imported=window.imported||0;
 return <I18nProvider>{view==='esim'?<Esim cards={cards}
  instances={[{id:'7',iccid:'fixture-active',status:{state:'OK'}}]}
  refresh={async()=>{window.refreshes++}} showToast={text=>window.toasts.push(text)}/>
  :view==='backup'?<BackupImport onImported={async()=>{window.imported++}}/>
  :<p>Another page</p>}</I18nProvider>
}
createRoot(document.getElementById('root')).render(<App/>);`
const server = await createServer({ root, configFile: false, plugins: [{
  name: 'operation-unmount-fixture',
  configureServer(server) {
    server.middlewares.use('/fixture', async (_req, res) => {
      res.setHeader('Content-Type', 'text/html')
      res.end(await server.transformIndexHtml('/fixture', '<html><body><div id="root"></div><script type="module" src="/operation-unmount-fixture.jsx"></script></body></html>'))
    })
  },
  resolveId(id) { if (id === '/operation-unmount-fixture.jsx') return path.join(root, 'operation-unmount-fixture.jsx') },
  load(id) { if (id === path.join(root, 'operation-unmount-fixture.jsx')) return fixture },
}], server: { host: '127.0.0.1', port: 0 } })
await server.listen()
let browser
try {
  browser = await chromium.launch({ headless: true })
  const page = await browser.newPage()
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('dialog', dialog => dialog.accept())
  await page.addInitScript(() => localStorage.setItem('mdd-language', 'en'))
  const profiles = [{ iccid: 'fixture-active', profileNickname: 'Active fixture', profileState: 'enabled' },
    { iccid: 'fixture-inactive', profileNickname: 'Inactive fixture', profileState: 'disabled' }]
  const chip = { cached: true, ts: 100, ses: [{ id: 'default', aid: 'fixture-aid', eid: 'fixture-euicc', profiles }] }
  let writes = [], liveReads = 0, statusReads = 0, pending = null, deferred = '', failure = ''
  const waitFor = async (condition, label) => {
    const deadline = Date.now() + 10000
    while (!condition()) {
      if (Date.now() >= deadline) throw new Error(`Timed out: ${label}`)
      await new Promise(resolve => setTimeout(resolve, 10))
    }
  }
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const pathname = url.pathname
    if (route.request().method() !== 'GET') {
      writes.push(pathname)
      if (pathname.endsWith(deferred)) { pending = route; return }
      if (pathname.endsWith('/nickname')) {
        assert.equal(route.request().postDataJSON().reader, 'fixture-reader')
        if (failure === 'nickname') return route.fulfill({ status: 400, json: { detail: 'fixture rename rejected' } })
        if (failure === 'bridge') return route.fulfill({ json: { ok: true, nickname: 'Renamed fixture', reader_ready: false } })
        return route.fulfill({ json: { ok: true, nickname: 'Renamed fixture', reader_ready: true } })
      }
      return route.fulfill({ json: { ok: true } })
    }
    if (pathname === '/api/esim/status') return route.fulfill({ json: { available: true } })
    if (pathname === '/api/esim/download/operation') return route.fulfill({ json: { operation: null } })
    if (pathname === '/api/esim/chip/cached') return route.fulfill({ json: chip })
    if (pathname === '/api/esim/chip') { liveReads++; return route.fulfill({ json: chip }) }
    if (pathname === '/api/instances/7/status') { statusReads++; return route.fulfill({ json: { state: 'OK' } }) }
    throw new Error(`Unexpected API ${pathname}`)
  })
  const address = `http://127.0.0.1:${server.httpServer.address().port}/fixture`
  const open = async () => {
    pending = null; writes = []; liveReads = 0; statusReads = 0; deferred = '/stop'; failure = ''
    await page.goto(address)
    await page.getByRole('button', { name: 'Rename', exact: true }).first().waitFor()
  }
  const leaveAndRelease = async (json = { ok: true }) => {
    await waitFor(() => pending, 'deferred request')
    await page.evaluate(() => window.navigate('away'))
    await page.getByText('Another page', { exact: true }).waitFor()
    const route = pending; pending = null; deferred = 'never-match'
    await route.fulfill({ json })
  }
  const rename = async () => {
    await page.getByRole('button', { name: 'Rename', exact: true }).nth(1).click()
    const dialog = page.getByRole('dialog', { name: 'Rename', exact: true })
    await dialog.getByRole('textbox').fill('Renamed fixture')
    await dialog.getByRole('button', { name: 'Stop line and rename', exact: true }).click()
  }
  // A page switch after stop admission must still enable the chosen profile exactly once.
  await open()
  await page.getByRole('button', { name: 'Enable', exact: true }).click()
  await leaveAndRelease()
  await waitFor(() => writes.length === 2, 'profile enable after unmount')
  await page.waitForFunction(() => window.refreshes === 1)
  assert.deepEqual(writes, ['/api/instances/7/stop', '/api/esim/profiles/fixture-inactive/enable'])
  assert.deepEqual(await page.evaluate(() => window.toasts), [])
  // Both success and a rejected rename restore the stopped line after navigation.
  for (const outcome of ['', 'nickname', 'bridge']) {
    await open(); failure = outcome
    await rename(); await leaveAndRelease()
    await waitFor(() => writes.length >= 2, 'rename after unmount')
    if (outcome !== 'bridge') await waitFor(() => statusReads > 0, 'line resume health after unmount')
    else await page.waitForTimeout(100)
    assert.deepEqual(writes, ['/api/instances/7/stop', '/api/esim/profiles/fixture-inactive/nickname',
      ...(outcome === 'bridge' ? [] : ['/api/instances/7/start'])])
    assert.deepEqual(await page.evaluate(() => window.toasts), [])
  }
  // Leaving during the nickname request, after stop, also preserves the resume step.
  await open(); deferred = '/nickname'
  await rename(); await leaveAndRelease({ ok: true, nickname: 'Renamed fixture', reader_ready: true })
  await waitFor(() => statusReads > 0, 'resume after in-flight rename')
  assert.deepEqual(writes, ['/api/instances/7/stop', '/api/esim/profiles/fixture-inactive/nickname', '/api/instances/7/start'])
  // Stop-before-load does the accepted read even though its original component is gone.
  await open()
  await page.getByRole('button', { name: 'Load', exact: true }).click()
  await leaveAndRelease()
  await waitFor(() => liveReads === 1, 'live read after unmount')
  assert.deepEqual(writes, ['/api/instances/7/stop'])
  // A real identity change is still a fence, unlike navigation.
  await open()
  await rename()
  await waitFor(() => pending, 'stop before card replacement')
  await page.evaluate(() => window.changeCard())
  await page.waitForTimeout(50)
  deferred = 'never-match'; await pending.fulfill({ json: { ok: true } }); pending = null
  await page.waitForTimeout(100)
  assert.deepEqual(writes, ['/api/instances/7/stop'])
  // An import completing after its file input was removed must still notify its owner.
  await open(); deferred = '/import'
  await page.evaluate(() => window.navigate('backup'))
  await page.locator('input[type="file"]').setInputFiles({ name: 'fixture.mddbackup', mimeType: 'application/octet-stream', buffer: Buffer.from('fixture') })
  await page.getByRole('button', { name: 'Import backup', exact: true }).click()
  await leaveAndRelease({ ok: true, backup_name: 'fixture.tar.gz' })
  await page.waitForFunction(() => window.imported === 1)
  assert.deepEqual(writes, ['/api/system/backups/import'])
  assert.deepEqual(errors, [])
  console.log('PASS: eSIM switch/read/rename and policy-aware resume survive unmount; card replacement fence; import completion without file input')
} finally {
  await browser?.close()
  await server.close()
}
