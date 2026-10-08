// Fictional local API fixtures only: nickname completion must survive SIM channel recovery.
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
import {I18nProvider} from '/src/i18n.jsx';
import '/src/index.css';
function App() {
 const [view,setView]=React.useState('esim');
 const [cards,setCards]=React.useState([{name:'fixture-reader',index:0,present:true,iccid:'fixture-active',generation:1,matched:'7',hardware_id:'modem-1'}]);
 const [devices,setDevices]=React.useState([{id:'modem-1',present:true,hardware_generation:'usb-1'}]);
 const [running,setRunning]=React.useState(true);
 window.navigate=setView;
 window.changeCard=(iccid='replacement-card',generation=2,identity_state='confirmed')=>setCards([{name:'fixture-reader',index:0,present:true,iccid,generation,identity_state,matched:'7',hardware_id:'modem-1'}]);
 window.changeCards=setCards;
 window.changeDevice=generation=>setDevices([{id:'modem-1',present:true,hardware_generation:generation}]);
 window.setLineRunning=setRunning;
 window.refreshes=window.refreshes||0;window.toasts=window.toasts||[];
 return <I18nProvider>{view==='esim'?<Esim cards={cards} devices={devices}
  instances={[{id:'7',iccid:'fixture-active',status:{state:running?'OK':'STOPPED'}}]}
  refresh={async()=>{window.refreshes++}} showToast={text=>window.toasts.push(text)}/>
  :<p>Another page</p>}</I18nProvider>
}
createRoot(document.getElementById('root')).render(<App/>);`
const server = await createServer({ root, configFile: false, plugins: [{
  name: 'nickname-generation-fixture',
  configureServer(server) {
    server.middlewares.use('/fixture', async (_req, res) => {
      res.setHeader('Content-Type', 'text/html')
      res.end(await server.transformIndexHtml('/fixture', '<html><body><div id="root"></div><script type="module" src="/nickname-generation-fixture.jsx"></script></body></html>'))
    })
  },
  resolveId(id) { if (id === '/nickname-generation-fixture.jsx') return path.join(root, 'nickname-generation-fixture.jsx') },
  load(id) { if (id === path.join(root, 'nickname-generation-fixture.jsx')) return fixture },
}], server: { host: '127.0.0.1', port: 0 } })
await server.listen()
let browser
try {
  browser = await chromium.launch({ headless: true, ...(process.env.MDD_BROWSER_EXECUTABLE ? { executablePath: process.env.MDD_BROWSER_EXECUTABLE } : {}) })
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
  const rename = async (isRunning = true) => {
    await page.getByRole('button', { name: 'Rename', exact: true }).nth(1).click()
    const dialog = page.getByRole('dialog', { name: 'Rename', exact: true })
    await dialog.getByRole('textbox').fill('Renamed fixture')
    await dialog.getByRole('button', { name: isRunning ? 'Stop line and rename' : 'Update', exact: true }).click()
  }
  // A bridge rebuild changes reader generation without replacing the physical SIM.
  // Keep this transaction busy, complete its feedback and release the controls exactly once.
  for (const width of [1440, 900, 390]) {
    await page.setViewportSize({ width, height: 1000 })
    for (const isRunning of [true, false]) {
      await open(); deferred = '/nickname'
      if (!isRunning) await page.evaluate(() => window.setLineRunning(false))
      for (const generation of [2, 3]) {
        writes = []
        await rename(isRunning)
        await waitFor(() => pending, 'nickname during bridge rebuild')
        await page.evaluate(generation => window.changeCard('fixture-active', generation), generation)
        const dialog = page.getByRole('dialog', { name: 'Rename', exact: true })
        assert.equal(await dialog.isVisible(), true, 'same-card channel recovery must keep the submitted rename visible')
        assert.equal(await page.getByRole('button', { name: 'Load', exact: true }).isDisabled(), true)
        const route = pending; pending = null
        await route.fulfill({ json: { ok: true, nickname: 'Renamed fixture', reader_ready: true } })
        await dialog.waitFor({ state: 'hidden' })
        await page.locator('[role="status"]').filter({ hasText: 'Profile renamed.' }).waitFor()
        await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent === 'Rename' && !button.disabled))
        assert.deepEqual(writes, [
          ...(isRunning ? ['/api/instances/7/stop'] : []), '/api/esim/profiles/fixture-inactive/nickname',
          ...(isRunning ? ['/api/instances/7/start'] : []),
        ])
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow at ${width}`)
      }
      assert.equal(liveReads, 0, 'rename recovery must only reconcile the cached list')
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 })
  // Same-card recovery also keeps a rejected write retryable and a bridge failure stopped.
  for (const outcome of ['nickname', 'bridge']) {
    await open(); deferred = '/nickname'
    await rename(); await waitFor(() => pending, 'nickname before recovery error')
    await page.evaluate(() => window.changeCard('fixture-active', 2))
    const route = pending; pending = null
    if (outcome === 'nickname') await route.fulfill({ status: 400, json: { detail: 'fixture rename rejected' } })
    else await route.fulfill({ json: { ok: true, nickname: 'Renamed fixture', reader_ready: false } })
    if (outcome === 'nickname') {
      await page.getByRole('dialog', { name: 'Rename', exact: true }).getByRole('alert').waitFor()
      await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    } else {
      await page.locator('[role="status"]').filter({ hasText: 'The nickname was saved, but the SIM channels could not recover.' }).waitFor()
    }
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent === 'Rename' && !button.disabled))
    assert.deepEqual(writes, ['/api/instances/7/stop', '/api/esim/profiles/fixture-inactive/nickname',
      ...(outcome === 'nickname' ? ['/api/instances/7/start'] : [])])
  }
  // Completion after leaving the page still restores the accepted original line.
  await open(); deferred = '/nickname'
  await rename(); await waitFor(() => pending, 'nickname before navigation')
  await page.evaluate(() => window.changeCard('fixture-active', 2))
  await leaveAndRelease({ ok: true, nickname: 'Renamed fixture', reader_ready: true })
  await waitFor(() => statusReads > 0, 'resume after unmount during channel recovery')
  assert.deepEqual(writes, ['/api/instances/7/stop', '/api/esim/profiles/fixture-inactive/nickname', '/api/instances/7/start'])
  assert.deepEqual(await page.evaluate(() => window.toasts), [])
  // A still-pending SIM identity cannot authorize the original line restart.
  await open(); deferred = '/nickname'
  await rename(); await waitFor(() => pending, 'nickname before pending identity')
  await page.evaluate(() => window.changeCard('fixture-active', 2, 'pending'))
  await pending.fulfill({ json: { ok: true, nickname: 'Renamed fixture', reader_ready: true } }); pending = null
  await page.locator('[role="status"]').filter({ hasText: 'The original SIM identity is not confirmed.' }).waitFor()
  assert.deepEqual(writes, ['/api/instances/7/stop', '/api/esim/profiles/fixture-inactive/nickname'])
  await page.evaluate(() => window.changeCard('fixture-active', 2))
  await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent === 'Rename' && !button.disabled))
  // Temporary reader loss is also part of the same USB/card transaction.
  await open(); deferred = '/nickname'
  await rename(); await waitFor(() => pending, 'nickname before temporary reader loss')
  await page.evaluate(() => window.changeCards([{ name: 'other-reader', index: 8, present: true, iccid: 'unrelated' }]))
  assert.equal(await page.locator('select').first().inputValue(), 'fixture-reader')
  assert.equal(await page.getByRole('dialog', { name: 'Rename', exact: true }).isVisible(), true)
  await page.evaluate(() => window.changeCard('fixture-active', 2))
  await pending.fulfill({ json: { ok: true, nickname: 'Renamed fixture', reader_ready: true } }); pending = null
  await page.getByRole('dialog', { name: 'Rename', exact: true }).waitFor({ state: 'hidden' })
  await waitFor(() => statusReads > 0, 'resume after temporary reader loss')
  assert.deepEqual(writes, ['/api/instances/7/stop', '/api/esim/profiles/fixture-inactive/nickname', '/api/instances/7/start'])
  // Known profiles on the same eUICC and a new USB generation are still real fences.
  for (const replacement of ['profile', 'usb']) {
    await open(); deferred = '/nickname'
    await rename(); await waitFor(() => pending, 'nickname before identity replacement')
    await page.evaluate(replacement => {
      if (replacement === 'usb') window.changeDevice('usb-2')
      window.changeCard(replacement === 'profile' ? 'fixture-inactive' : 'fixture-active', 2)
    }, replacement)
    await pending.fulfill({ json: { ok: true, nickname: 'Old-card result', reader_ready: true } }); pending = null
    await page.waitForTimeout(100)
    assert.deepEqual(writes, ['/api/instances/7/stop', '/api/esim/profiles/fixture-inactive/nickname'])
    assert.deepEqual(await page.evaluate(() => window.toasts), [], 'replacement must not receive old-card completion feedback')
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some(button => button.textContent === 'Rename' && !button.disabled))
  }
  // A real identity change is still a fence, unlike navigation.
  await open()
  await rename()
  await waitFor(() => pending, 'stop before card replacement')
  await page.evaluate(() => window.changeCard())
  await page.waitForTimeout(50)
  deferred = 'never-match'; await pending.fulfill({ json: { ok: true } }); pending = null
  await page.waitForTimeout(100)
  assert.deepEqual(writes, ['/api/instances/7/stop'])
  assert.deepEqual(errors, [])
  console.log('PASS: nickname recovery across generations, navigation and temporary reader loss; repeat/rejected writes; bridge/pending/profile/USB fences; stopped-line preservation; 1440/900/390px')
} finally {
  await browser?.close()
  await server.close()
}
