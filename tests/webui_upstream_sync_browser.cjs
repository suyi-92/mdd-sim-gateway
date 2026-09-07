// Browser acceptance against local fixture APIs. Never connects to the installed gateway.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '..')
const dist = path.join(root, 'webui/dist')
const output = process.env.MDD_UI_TEST_OUTPUT || '/tmp/mdd-upstream-ui-check'
fs.mkdirSync(output, { recursive: true })
const privateName = 'PRIVATE_BOT_NAME_' + 'long_name_'.repeat(8)
const privateHook = 'https://open.feishu.cn/open-apis/bot/v2/hook/fixture-private-key'
let settings = { feishu: { enabled: false, channels: [
  { id: 'alpha', name: privateName, enabled: true, url: privateHook, secret: 'fixture-signature', instances: [], events: {}, message_templates: {} },
  { id: 'beta', name: 'PRIVATE_BOT_BETA', enabled: true, url: privateHook, secret: 'fixture-signature', instances: ['2'], events: {}, message_templates: {} },
] }, webhook: {}, telegram: {}, pushplus: {}, proxy: { enabled: false, profiles: {}, exits: {} } }
const instances = [1, 2].map(id => ({ id: String(id), name: `Fixture line ${id}`, enabled: false, status: { state: 'STOPPED' } }))
const writes = [], testIds = [], pending = new Map()
const reply = (response, value, status = 200) => { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)) }
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://localhost')
  if (url.pathname.startsWith('/api/')) {
    let body = ''
    for await (const chunk of request) body += chunk
    if (request.method !== 'GET') writes.push([request.method, url.pathname])
    if (url.pathname === '/api/settings' && request.method === 'PUT') {
      settings = JSON.parse(body); reply(response, settings); return
    }
    if (url.pathname === '/api/notifications/feishu/test') {
      const value = JSON.parse(body); testIds.push(value.id)
      if (value.id === 'alpha') pending.set('alpha', response)
      else reply(response, { ok: true })
      return
    }
    const values = {
      '/api/auth/status': { configured: true, authenticated: true, csrf: 'fixture-only' },
      '/api/devices': { devices: [], discovering: false },
      '/api/instances': { instances }, '/api/cards': { cards: [] },
      '/api/settings': settings,
      '/api/notifications/deliveries': { deliveries: [] },
      '/api/egress/status': { exits: {}, updated_at: 1 },
      '/api/system/status': { version: 'fixture', repository_url: 'https://example.invalid/repo' },
    }
    reply(response, /\/softphone$/.test(url.pathname) ? { enabled: false } : values[url.pathname] || {})
    return
  }
  const file = path.resolve(dist, '.' + (url.pathname === '/' ? '/index.html' : url.pathname))
  if (!file.startsWith(dist + path.sep) || !fs.existsSync(file)) { response.writeHead(404); response.end(); return }
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
    await context.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.continue() : route.abort())
    await context.addInitScript(() => {
      localStorage.setItem('mdd-language', 'en')
      window.fixtureSockets = []
      window.WebSocket = class extends EventTarget {
        static OPEN = 1
        constructor(url) { super(); this.url = url; this.readyState = 1; window.fixtureSockets.push(this); queueMicrotask(() => this.onopen?.(new Event('open'))) }
        send() {}
        close() { this.readyState = 3 }
        emit(value) { const event = new MessageEvent('message', { data: JSON.stringify(value) }); this.onmessage?.(event); this.dispatchEvent(event) }
      }
    })
    const page = await context.newPage(), errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(origin + '/#/notifications')
    const panel = page.locator('.u-feishu-panel')
    await panel.getByRole('tab', { name: 'Bot 1', exact: true }).waitFor()
    const test = panel.locator('.u-notification-test button')
    const status = panel.locator('.u-notification-test [role=status]')
    const relativeBox = () => test.evaluate(element => {
      const button = element.getBoundingClientRect(), panel = element.closest('.u-feishu-panel').getBoundingClientRect()
      return { x: button.x - panel.x, y: button.y - panel.y }
    })
    const originalBox = await relativeBox()
    await test.click()
    await page.waitForFunction(() => document.querySelector('.u-feishu-panel .u-notification-test button')?.disabled)
    await panel.getByRole('tab', { name: 'Bot 2', exact: true }).click()
    assert.equal(await test.isEnabled(), true, 'a pending bot must not lock another bot')
    await test.click()
    await status.getByText('Test succeeded', { exact: true }).waitFor()
    assert.deepEqual(testIds, ['alpha', 'beta'])
    await panel.getByRole('tab', { name: 'Bot 1', exact: true }).click()
    assert.equal(await test.isDisabled(), true)
    reply(pending.get('alpha'), { detail: `fixture delivery failed: ${privateHook}` }, 502)
    pending.delete('alpha')
    await status.getByText('Test failed', { exact: true }).waitFor()
    const resultBox = await relativeBox()
    assert.ok(Math.abs(originalBox.x - resultBox.x) <= 1 && Math.abs(originalBox.y - resultBox.y) <= 1, 'test feedback moved its button')
    const hidden = await page.locator('body').innerText()
    assert.equal(hidden.includes(privateName), false)
    assert.equal(hidden.includes('fixture-private-key'), false)
    const annotations = await page.locator('[title], [aria-label]').evaluateAll(nodes => nodes.map(node => [node.title, node.getAttribute('aria-label')]).flat().join(' '))
    assert.equal(annotations.includes('fixture-private-key'), false)
    await page.getByRole('button', { name: 'Show sensitive information', exact: true }).click()
    assert.equal((await status.innerText()).includes('fixture-private-key'), true)
    await page.getByRole('button', { name: 'Hide sensitive information', exact: true }).click()
    assert.equal((await status.innerText()).includes('fixture-private-key'), false)

    await panel.getByRole('button', { name: 'Add bot', exact: true }).click()
    const nameInput = panel.getByText('Channel name', { exact: true }).locator('..').locator('input')
    await nameInput.fill('Fixture new bot')
    await panel.getByLabel('Line 2', { exact: true }).check()
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await page.locator('.u-notification-save [role=status]').getByText('Saved', { exact: true }).waitFor()
    assert.equal(settings.feishu.channels.length, 3)
    const added = settings.feishu.channels[2]
    assert.ok(added.id && added.id !== 'alpha' && added.id !== 'beta')
    assert.equal(added.enabled, false)
    assert.deepEqual(added.instances, ['2'])
    assert.equal(settings.feishu.channels[0].id, 'alpha')
    await panel.getByRole('button', { name: 'Delete bot', exact: true }).click()
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await page.waitForFunction(() => document.querySelectorAll('.u-feishu-tabs [role=tab]').length === 2)

    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `notification overflow at ${width}px`)
      await panel.screenshot({ path: path.join(output, `notifications-${width}.png`), fullPage: true, animations: 'disabled' })
    }
    await page.goto(origin + '/#/egress')
    const country = page.locator('.u-country-picker input')
    await country.fill('Germany')
    await country.press('ArrowDown'); await country.press('Enter')
    await page.getByRole('button', { name: '+ Add', exact: true }).click()
    await page.locator('.u-exit-row').first().waitFor()
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      await country.scrollIntoViewIfNeeded()
      await country.fill('United')
      await page.locator('.u-country-picker-list').waitFor()
      await page.waitForFunction(() => {
        const rect = document.querySelector('.u-country-picker-list')?.getBoundingClientRect()
        return rect && rect.top >= 0 && rect.bottom <= innerHeight - 50
      })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `country picker overflow at ${width}px`)
      await page.screenshot({ path: path.join(output, `country-search-${width}.png`), fullPage: true, animations: 'disabled' })
      await country.press('Escape')
    }
    await country.fill('')
    const beforeNavigation = await country.boundingBox()
    await country.press('End')
    const afterNavigation = await country.boundingBox()
    assert.ok(Math.abs(beforeNavigation.y - afterNavigation.y) <= 1, 'option navigation scrolled the page')
    await country.press('Escape')
    assert.equal(await page.locator('.u-toast').count(), 0)
    const message = { type: 'sms', instance: '1', message: { id: 12, direction: 'in', peer: '+15555550100', body: 'fixture complete' } }
    await page.evaluate(value => window.fixtureSockets.forEach(socket => socket.emit({ ...value, updated: true })), message)
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    assert.equal(await page.locator('.u-toast').count(), 0, 'late completion must not toast as a new message')
    await page.evaluate(value => window.fixtureSockets.forEach(socket => socket.emit(value)), message)
    await page.locator('.u-toast').waitFor()
    assert.deepEqual(errors, [])
    assert.equal(writes.every(([method, url]) => (method === 'PUT' && url === '/api/settings') || (method === 'POST' && url === '/api/notifications/feishu/test')), true)
    console.log('PASS: multi-bot CRUD/routing, independent pending/results, privacy, fixed feedback, country keyboard search, SMS updates, 1440/900/390px; fixture API only')
  } finally {
    for (const response of pending.values()) response.end()
    if (browser) await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
