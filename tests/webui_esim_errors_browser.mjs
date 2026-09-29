// Local fixture only: no real card writes, accounts or production API traffic.
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
function App() {
 const [generation,setGeneration] = React.useState(1);
 window.bumpGeneration=()=>setGeneration(value=>value+1);
 return <I18nProvider><Esim cards={[{name:'fixture-reader',index:0,present:true,
  iccid:'card-active',identity_state:'confirmed',generation}]} instances={[]}/></I18nProvider>
}
createRoot(document.getElementById('root')).render(<App/>);`
const server = await createServer({ root, configFile: false, plugins: [{
  name: 'card-errors-fixture',
  configureServer(server) {
    server.middlewares.use('/fixture', async (_req, res) => {
      res.setHeader('Content-Type', 'text/html')
      res.end(await server.transformIndexHtml('/fixture', '<html><body><div id="root"></div><script type="module" src="/errors-fixture.jsx"></script></body></html>'))
    })
  },
  resolveId(id) { if (id === '/errors-fixture.jsx') return path.join(root, 'errors-fixture.jsx') },
  load(id) { if (id === path.join(root, 'errors-fixture.jsx')) return fixture },
}], server: { host: '127.0.0.1', port: 0 } })
await server.listen()
let browser
const output = '/tmp/mdd-card-errors-browser'
fs.mkdirSync(output, { recursive: true })
try {
  browser = await chromium.launch({ headless: true })
  for (const width of [1440, 900, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 1000 } })
    await page.addInitScript(() => localStorage.setItem('mdd-language', 'zh'))
    const errors = []; page.on('pageerror', e => errors.push(e.message))
    let operation = null, method = '', attempts = 0
    const payload = () => ({ ok: true, cached: true, ts: 100, ses: [{
      id: 'default', eid: 'fixture-euicc', profiles: [
        { iccid: 'card-active', profileNickname: 'Active profile', profileState: 'enabled' },
        { iccid: 'card-target', profileNickname: 'Target profile', profileState: 'disabled', operation_status: operation },
      ],
    }] })
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url())
      if (url.pathname === '/api/esim/status') return route.fulfill({ json: { available: true } })
      if (url.pathname === '/api/esim/download/operation') return route.fulfill({ json: { operation: null } })
      if (url.pathname === '/api/esim/chip/cached' || url.pathname === '/api/esim/chip') return route.fulfill({ json: payload() })
      if (url.pathname.startsWith('/api/esim/profiles/')) {
        attempts++; method = route.request().method()
        const detail = { code: 'esim_operation_failed',
          message: "The card's profile policy does not allow this operation.",
          diagnostic: { operation: method === 'DELETE' ? 'profile delete' : 'profile enable',
            step: method === 'DELETE' ? 'es10c_delete_profile' : 'es10c_enable_profile', lpac_code: -1, card_result: 3, failure_stage: "response_status", status_word: "6985" } }
        operation = { state: 'failed', error: detail, updated_at: attempts + 100 }
        return route.fulfill({ status: 400, json: { detail } })
      }
      throw new Error('Unexpected API ' + url.pathname)
    })
    await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/fixture`)
    await page.getByText('Target profile', { exact: true }).waitFor()
    await page.getByRole('button', { name: '读取', exact: true }).click()
    const row = page.getByText('Target profile', { exact: true }).locator('..').locator('..').locator('..')
    const enable = row.getByRole('button', { name: '启用', exact: true })
    await enable.waitFor()
    const before = await enable.boundingBox()
    const beforeRow = await row.boundingBox()
    await enable.click()
    await row.getByRole('status').filter({ hasText: '卡端结果：3' }).waitFor()
    await row.getByRole('status').filter({ hasText: '卡返回异常状态' }).waitFor()
    await row.getByRole('status').filter({ hasText: 'SW 6985' }).waitFor()
    const after = await enable.boundingBox()
    const afterRow = await row.boundingBox()
    // The reserved feedback line must not move the row's controls.
    assert.ok(Math.abs((after.y - afterRow.y) - (before.y - beforeRow.y)) < 1)
    assert.ok(Math.abs((after.x - afterRow.x) - (before.x - beforeRow.x)) < 1)
    assert.equal(method, 'POST')
    await page.evaluate(() => window.bumpGeneration())
    await row.getByRole('status').filter({ hasText: 'es10c_enable_profile' }).waitFor()
    await page.reload()
    await row.getByRole('status').filter({ hasText: '上次操作失败' }).waitFor()
    await page.getByRole('button', { name: '读取', exact: true }).click()
    page.once('dialog', dialog => dialog.accept())
    await row.getByRole('button', { name: '删除', exact: true }).click()
    await row.getByRole('status').filter({ hasText: 'es10c_delete_profile' }).waitFor()
    assert.equal(method, 'DELETE')
    assert.equal(attempts, 2, 'a failed operation must never replay automatically')
    const feedback = await row.getByRole('status').getAttribute('title')
    assert.match(feedback, /工具码：-1/)
    assert.match(feedback, /卡端结果：3/)
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    assert.deepEqual(errors, [])
    await page.screenshot({ path: path.join(output, `${width}.png`), fullPage: true })
    await page.close()
  }
  console.log('eSIM error persistence and fixed row feedback passed at 1440/900/390px')
} finally {
  await browser?.close()
  await server.close()
}
