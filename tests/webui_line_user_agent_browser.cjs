// Browser layout regression for the per-line SIP User-Agent. Local fixture APIs only.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const { chromium } = require('playwright')

const root = path.resolve(__dirname, '..')
const dist = path.join(root, 'webui/dist')
const output = process.env.MDD_USER_AGENT_UI_OUTPUT || '/tmp/mdd-user-agent-ui-check'
fs.mkdirSync(output, { recursive: true })

const readerName = 'Fixture smart-card reader 00 00'
const line = {
  id: '1', name: 'Fixture line', enabled: false, imsi: '001010000000001',
  mcc: '001', mnc: '01', iccid: 'test-card', reader: readerName, reader_index: 0,
  reader_port: 'fixture-port', apn: 'ims', sip: {
    user_agent: 'Fixture-Terminal/1.0 ' + 'A'.repeat(40), webrtc: { enable: false },
  }, status: { state: 'STOPPED', presentation: { label: 'Stopped' } },
}
const device = {
  id: 'reader-fixture', name: 'Fixture reader', default_name: 'Fixture reader',
  device_type: 'reader', reader: readerName, present: true, instance_id: '1',
  sim: { present: true }, capabilities: { vowifi: { desired: false, actual: 'off' } },
}
const card = {
  name: readerName, index: 0, reader_port: 'fixture-port', present: true,
  iccid: 'test-card', imsi: line.imsi,
}
const writes = []

const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost')
  if (url.pathname.startsWith('/api/')) {
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method)) {
      writes.push([request.method, url.pathname])
    }
    const values = {
      '/api/auth/status': { configured: true, authenticated: true, csrf: 'fixture-only' },
      '/api/devices': { devices: [device], discovering: false },
      '/api/instances': { instances: [line] },
      '/api/cards': { cards: [card] },
      '/api/readers': { readers: [readerName] },
      '/api/settings': { vm_enabled: false },
      '/api/system/status': { version: 'fixture', repository_url: 'https://example.invalid/repo' },
    }
    const value = /\/softphone$/.test(url.pathname) ? { enabled: false } : values[url.pathname] || {}
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify(value))
    return
  }
  const file = path.resolve(dist, '.' + (url.pathname === '/' ? '/index.html' : url.pathname))
  if (!file.startsWith(dist + path.sep) || !fs.existsSync(file)) {
    response.writeHead(404); response.end(); return
  }
  const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }
  response.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' })
  fs.createReadStream(file).pipe(response)
})
server.on('upgrade', (_request, socket) => socket.destroy())

;(async () => {
  let browser
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
    const origin = `http://127.0.0.1:${server.address().port}`
    browser = await chromium.launch({ headless: true })
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.route('**/*', route => route.request().url().startsWith(origin + '/')
      ? route.continue() : route.abort())
    await context.addInitScript(() => {
      localStorage.setItem('mdd-language', 'en')
      window.WebSocket = class extends EventTarget {
        static OPEN = 1
        constructor() { super(); this.readyState = 1; queueMicrotask(() => this.onopen?.(new Event('open'))) }
        send() {}
        close() { this.readyState = 3 }
      }
    })
    const page = await context.newPage()
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(origin + '/#/devices')
    await page.getByRole('heading', { name: 'Fixture reader', exact: true }).waitFor()
    await page.getByRole('button', { name: 'SIM', exact: true }).click()
    await page.getByText('Advanced IMS identity', { exact: true }).click()
    const field = page.getByText(
      'Device User-Agent (how the line identifies to the carrier)', { exact: true }).locator('..')
    const input = field.locator('input')
    await input.waitFor()
    assert.equal(await input.inputValue(), line.sip.user_agent)
    assert.equal(await input.getAttribute('maxlength'), '64')
    assert.equal(await input.getAttribute('placeholder'), 'MDD-Sim-Gateway')
    await input.fill('B'.repeat(64))
    assert.equal((await input.inputValue()).length, 64)

    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      await input.scrollIntoViewIfNeeded()
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false, `User-Agent form overflow at ${width}px`)
      const bounds = await field.evaluate(node => {
        const outer = node.getBoundingClientRect()
        const inner = node.querySelector('input').getBoundingClientRect()
        return { contained: inner.left >= outer.left - 1 && inner.right <= outer.right + 1,
          wrapped: node.scrollWidth <= node.clientWidth + 1 }
      })
      assert.equal(bounds.contained && bounds.wrapped, true,
        `User-Agent value/help must stay inside its field at ${width}px`)
      await field.screenshot({ path: path.join(output, `line-user-agent-${width}.png`),
        animations: 'disabled' })
    }
    assert.deepEqual(writes, [], 'viewing and editing the unsaved field must not write configuration')
    assert.deepEqual(errors, [])
    console.log('PASS: per-line User-Agent value/default/limit and 1440/900/390px layout; fixture API only')
  } finally {
    if (browser) await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
