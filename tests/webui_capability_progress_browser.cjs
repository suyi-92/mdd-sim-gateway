// Local-only browser acceptance for capability progress across navigation and reload.
const assert = require('node:assert/strict')
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')
const { chromium } = require('playwright')

const root = path.resolve(__dirname, '..')
const dist = path.join(root, 'webui/dist')
let posts = 0
let operation = {}
let flightDesired = true
let flightActual = 'on'

const device = () => ({
  id: 'modem-fixture', device_type: 'modem', present: true, instance_id: '7',
  name: 'Fixture modem', sim: { present: true }, capability_operation: operation,
  capabilities: {
    cellular: { available: true, desired: false, actual: 'off' },
    flight: { available: true, desired: flightDesired, actual: flightActual },
    vowifi: { available: true, desired: true, actual: 'starting' },
  },
})
const json = (response, value, status = 200) => {
  response.writeHead(status, { 'Content-Type': 'application/json' })
  response.end(JSON.stringify(value))
}
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost')
  if (url.pathname.startsWith('/api/')) {
    if (request.method === 'PATCH' && url.pathname === '/api/devices/modem-fixture/capabilities') {
      posts += 1
      flightDesired = false; flightActual = 'stopping'
      operation = { operation_id: '0123456789abcdef01234567', state: 'running',
        phase: 'reconciling', target: { flight_mode: false }, updated_at: Date.now() / 1000 }
      return json(response, { accepted: true, operation })
    }
    if (url.pathname === '/api/devices/modem-fixture/capability-operation') {
      return json(response, { operation })
    }
    const values = {
      '/api/auth/status': { configured: true, authenticated: true, csrf: 'fixture' },
      '/api/devices': { devices: [device()], discovering: false },
      '/api/instances': { instances: [{ id: '7', status: { state: 'REGISTERING' } }] },
      '/api/cards': { cards: [] },
      '/api/devices/modem-fixture/cellular/network-operation': {
        context: 'fixture', networks: [], operation: null,
      },
      '/api/devices/rescan/progress': { state: 'idle' },
      '/api/system/status': { version: 'fixture' },
    }
    return json(response, values[url.pathname] || {})
  }
  const file = path.resolve(dist, '.' + (url.pathname === '/' ? '/index.html' : url.pathname))
  if (!file.startsWith(dist + path.sep) || !fs.existsSync(file)) {
    response.writeHead(404); response.end(); return
  }
  const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.ttf': 'font/ttf' }
  response.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' })
  fs.createReadStream(file).pipe(response)
})
server.on('upgrade', (_request, socket) => socket.destroy())

;(async () => {
  let browser
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
    browser = await chromium.launch({ headless: true,
      ...(process.env.MDD_BROWSER_EXECUTABLE ? { executablePath: process.env.MDD_BROWSER_EXECUTABLE } : {}) })
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    await page.goto(`http://127.0.0.1:${server.address().port}/#/devices`)
    await page.getByRole('heading', { name: 'Fixture modem', exact: true }).waitFor()
    const flight = page.getByRole('switch', { name: '飞行模式', exact: true })
    const vowifi = page.getByRole('switch', { name: 'VoWiFi / WiFi Calling', exact: true })
    // Carrier registration can remain "starting" for minutes, but it must not lock OFF.
    assert.equal(await vowifi.isEnabled(), true)

    page.once('dialog', dialog => dialog.accept())
    await flight.click()
    await page.getByText('正在等待硬件确认目标状态。', { exact: true }).waitFor()
    assert.equal(posts, 1)

    await page.getByRole('button', { name: /概览/ }).click()
    await page.getByRole('button', { name: /设备/ }).click()
    await page.getByText('正在等待硬件确认目标状态。', { exact: true }).waitFor()
    assert.equal(posts, 1)

    await page.reload()
    await page.getByText('正在等待硬件确认目标状态。', { exact: true }).waitFor()
    assert.equal(posts, 1)

    operation = { ...operation, state: 'success', phase: 'complete', updated_at: Date.now() / 1000 }
    flightActual = 'off'
    await page.waitForTimeout(1200)
    await page.reload()
    const restored = page.getByRole('switch', { name: '飞行模式', exact: true })
    await restored.waitFor()
    assert.equal(await restored.getAttribute('aria-checked'), 'false')
    assert.equal(posts, 1)

    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
    }
    console.log('PASS: capability request returns once, survives page navigation/reload, and starting service remains switchable')
  } finally {
    if (browser) await browser.close()
    server.close()
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
