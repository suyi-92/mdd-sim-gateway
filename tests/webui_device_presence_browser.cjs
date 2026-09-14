// Run after npm ci/build with Playwright available in NODE_PATH.
// Serves only local build assets and fictional API responses; never contacts production.
const assert = require('node:assert/strict')
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '..')
const dist = path.join(root, 'webui/dist')
const output = process.env.MDD_UI_TEST_OUTPUT || '/tmp/mdd-device-ui-check/results'
fs.mkdirSync(output, { recursive: true })
const modem = { id: 'modem-fixture', name: 'DJI/Quectel EC25', default_name: 'DJI/Quectel EC25', device_type: 'modem', present: true,
  sim: { present: false }, capabilities: {
    cellular: { supported: true, available: true, desired: false, actual: 'off' },
    flight: { supported: true, available: true, desired: true, actual: 'on' },
    vowifi: { supported: true, available: false, desired: true, actual: 'off' },
  } }
const reader = { id: 'reader-fixture', name: 'SCR Prime CCID Reader (fixture) 00 00',
  default_name: '3T Electronics SCR Prime reader', device_type: 'reader', present: true,
  sim: { present: false }, capabilities: { vowifi: { desired: false, actual: 'off' } } }
let devices = [modem, reader]
const mutations = []
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost')
  if (url.pathname.startsWith('/api/')) {
    if (request.method !== 'GET') mutations.push([request.method, url.pathname])
    const hardware = url.pathname.match(/^\/api\/devices\/([^/]+)\/hardware$/)
    if (request.method === 'PUT' && hardware) {
      let body = ''
      request.on('data', chunk => { body += chunk })
      request.on('end', () => {
        const patch = JSON.parse(body || '{}')
        const id = decodeURIComponent(hardware[1])
        devices = devices.map(device => device.id === id
          ? { ...device, display_name: String(patch.name || '').trim(),
            name: String(patch.name || '').trim() || device.default_name || device.name }
          : device)
        response.writeHead(200, { 'Content-Type': 'application/json' })
        response.end(JSON.stringify({ ok: true }))
      })
      return
    }
    const values = {
      '/api/auth/status': { configured: true, authenticated: true, csrf: 'fixture-only' },
      '/api/devices': { devices, discovering: false },
      '/api/instances': { instances: [] },
      '/api/cards': { cards: [] },
      '/api/system/status': { version: 'fixture', repository_url: 'https://example.invalid/repo' },
    }
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify(values[url.pathname] || {}))
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
    browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.clock.install()
    await page.goto(`http://127.0.0.1:${server.address().port}/#/devices`)
    const heading = name => page.getByRole('heading', { name, exact: true })
    const options = page.locator('.u-device-option')
    const history = page.getByLabel(/显示已断开的设备/)
    await heading('DJI/Quectel EC25').waitFor()
    assert.equal(await options.count(), 2)
    await page.screenshot({ path: path.join(output, 'connected.png'), fullPage: true, animations: 'disabled' })
    await page.getByRole('button', { name: '蜂窝数据（4G）', exact: true }).click()

    devices = [{ ...modem, present: false }, reader]
    await page.clock.fastForward(11000)
    await heading('三体电子 SCR Prime 读卡器').waitFor()
    assert.equal(await options.count(), 1)
    assert.equal(await page.getByRole('heading', { name: 'DJI/Quectel EC25', exact: true }).count(), 0)
    assert.equal(await page.getByRole('button', { name: '蜂窝数据（4G）', exact: true }).count(), 0)
    assert.equal(await history.isChecked(), false)
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      await page.screenshot({ path: path.join(output, `modem-unplugged-${width}.png`), fullPage: true, animations: 'disabled' })
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
      assert.equal(overflow, false, `horizontal overflow at ${width}px`)
    }

    await history.check()
    await page.getByRole('button', { name: /DJI\/Quectel EC25/ }).click()
    await page.getByRole('button', { name: '硬件', exact: true }).click()
    const deviceNameInput = page.locator('.u-hardware-name input')
    assert.equal(await deviceNameInput.getAttribute('placeholder'), 'DJI/Quectel EC25')
    assert.equal(await deviceNameInput.isEnabled(), true)
    assert.deepEqual(mutations, [], 'navigation and history views must not write configuration')
    await deviceNameInput.fill('Main modem')
    await page.locator('.u-hardware-name button').click()
    await heading('Main modem').waitFor()
    await page.locator('.u-hardware-name input').fill('')
    await page.locator('.u-hardware-name button').click()
    await heading('DJI/Quectel EC25').waitFor()
    assert.equal(await page.getByRole('button', { name: '删除设备', exact: true }).isEnabled(), true)
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      await page.screenshot({ path: path.join(output, `hardware-name-${width}.png`), fullPage: true, animations: 'disabled' })
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
      assert.equal(overflow, false, `hardware-name horizontal overflow at ${width}px`)
    }
    await history.uncheck()
    await heading('三体电子 SCR Prime 读卡器').waitFor()

    devices = [{ ...modem, present: false }, { ...reader, present: false }]
    await page.clock.fastForward(11000)
    await heading('未发现通信设备').waitFor()
    assert.equal(await options.count(), 0)
    await page.screenshot({ path: path.join(output, 'all-unplugged.png'), fullPage: true, animations: 'disabled' })

    devices = [modem, { ...reader, present: false }]
    await page.clock.fastForward(11000)
    await heading('DJI/Quectel EC25').waitFor()
    assert.equal(await options.count(), 1)
    assert.equal(devices[0].capabilities.flight.desired, true)
    assert.equal(devices[0].capabilities.vowifi.desired, true)
    assert.deepEqual(mutations, [
      ['PUT', '/api/devices/modem-fixture/hardware'],
      ['PUT', '/api/devices/modem-fixture/hardware'],
    ], 'only the explicit custom-name save and restore may mutate configuration')
    assert.deepEqual(errors, [])
    console.log('PASS: unified names, custom save/restore, unplug fallback, retained settings, guarded mutations, 1440/900/390px')
  } finally {
    if (browser) await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
