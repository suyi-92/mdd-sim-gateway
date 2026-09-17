// Browser acceptance against local fixture APIs only; never contacts the installed gateway.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const { chromium } = require('playwright')

const root = path.resolve(__dirname, '..')
const dist = path.join(root, 'webui/dist')
const output = process.env.MDD_UI_TEST_OUTPUT || '/tmp/mdd-communication-line-details'
fs.mkdirSync(output, { recursive: true })

const instances = [
  { id: '1', name: 'Desk line', mcc: '234', mnc: '33', msisdn: '+447700900357', enabled: true,
    status: { state: 'OK', label: 'Working', presentation: { label: 'Working' } } },
  { id: '2', name: 'Travel line', mcc: '310', mnc: '280', msisdn: '+1555**7654#', enabled: true,
    status: { state: 'OK', label: 'Working', presentation: { label: 'Working' } } },
]
const cards = instances.map((line, index) => ({
  name: `Fixture reader ${index + 1}`, index, present: true, matched: line.id,
}))
const devices = [
  { id: 'reader-1', instance_id: '1', device_type: 'reader', present: true,
    name: '3T Electronics SCR Prime reader', sim: { present: true, number: '+447700900357',
      carrier: { name: 'Fixture Mobile', home_network: 'EE', current_network: 'Visited Network', plmn: '234-33' } },
    egress: { country: 'gb', detected_country: 'gb', node: 'GB Fixture Node', mode: 'manual', ready: true },
    capabilities: { vowifi: { desired: true, actual: 'on' } } },
  { id: 'modem-2', instance_id: '2', device_type: 'modem', present: true,
    name: 'Travel modem', sim: { present: true, number: '+1555**7654#',
      carrier: { name: 'Fixture Wireless', home_network: 'Fixture Host', plmn: '310-280' } },
    cellular: { registration: 'roaming', access_technology: 'lte', operator: 'Visited Fixture',
      operator_code: '46001', signal: 78, packet_service: 'attached', data_active: false },
    cellular_network: { mode: 'automatic', operator_id: '' },
    egress: { country: 'us', detected_country: 'us', node: '', mode: 'direct', ready: true },
    capabilities: { cellular: { desired: false, actual: 'off',
      reason: 'Mobile data is disconnected; the modem radio can remain registered to the cellular network.' },
      flight: { desired: false, actual: 'off' }, vowifi: { desired: true, actual: 'on' } } },
]

const writes = []
let pendingScanResponse = null
const json = (response, value, status = 200) => {
  response.writeHead(status, { 'Content-Type': 'application/json' })
  response.end(JSON.stringify(value))
}
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost')
  if (url.pathname.startsWith('/api/')) {
    if (request.method !== 'GET') writes.push([request.method, url.pathname])
    const values = {
      '/api/auth/status': { configured: true, authenticated: true, csrf: 'fixture-only' },
      '/api/instances': { instances }, '/api/cards': { cards },
      '/api/devices': { devices, discovering: false },
      '/api/system/status': { version: 'fixture', repository_url: 'https://example.invalid/repo' },
    }
    if (/\/softphone$/.test(url.pathname)) return json(response, { enabled: false })
    if (/\/calls$/.test(url.pathname)) return json(response, { calls: [] })
    if (/\/voicemails$/.test(url.pathname)) return json(response, { voicemails: [] })
    if (/\/messages\/threads$/.test(url.pathname)) return json(response, { threads: [] })
    if (/\/messages\/binary$/.test(url.pathname)) return json(response, { payloads: [] })
    if (/\/messages\/reimport-cellular$/.test(url.pathname)) {
      return json(response, { ok: true, imported: 2, retained: 2 })
    }
    if (/\/cellular\/networks\/scan$/.test(url.pathname)) {
      pendingScanResponse = response
      return
    }
    if (/\/cellular\/network$/.test(url.pathname)) {
      return json(response, { ok: true, mode: 'manual', operator_id: '46000' })
    }
    return json(response, values[url.pathname] || {})
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
    browser = await chromium.launch({ headless: true,
      ...(process.env.MDD_BROWSER_EXECUTABLE
        ? { executablePath: process.env.MDD_BROWSER_EXECUTABLE }
        : {}) })
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin })
    await context.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.continue() : route.abort())
    await context.addInitScript(() => {
      localStorage.setItem('mdd-language', 'zh')
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

    const verifyFirstLine = async view => {
      const details = page.locator('.u-line-selector-meta:visible')
      await details.getByText('Fixture Mobile (234-33)', { exact: true }).waitFor()
      const text = await details.innerText()
      for (const expected of ['运营商', '线路名称', '号码', '国家', '网络线路',
        'Desk line', '+447700900357', '英国 (GB)', 'GB Fixture Node']) {
        assert.ok(text.includes(expected), `${view} missing ${expected}`)
      }
      assert.equal(text.includes('承载网络'), false)
      assert.equal(text.includes('Visited Network · EE'), false)
      const number = details.getByRole('button', { name: '复制号码' })
      await number.click()
      assert.equal(await page.evaluate(() => navigator.clipboard.readText()), '+447700900357')
      const selectedOption = await page.locator('.u-line-selector:visible select option:checked').innerText()
      assert.ok(selectedOption.includes('••••0357'), `${view} selector must keep its masked number`)
      assert.equal(selectedOption.includes('+447700900357'), false)
    }

    await page.goto(origin + '/#/calls')
    await verifyFirstLine('calls')
    await page.locator('.u-line-selector:visible select').selectOption('2')
    const callDetails = page.locator('.u-line-selector-meta:visible')
    await callDetails.getByText('Travel line', { exact: true }).waitFor()
    const switched = await callDetails.innerText()
    for (const expected of ['Fixture Wireless (310-280)', '+1555**7654#', '美国 (US)',
      '明确直连']) assert.ok(switched.includes(expected), `switched calls missing ${expected}`)
    await callDetails.getByRole('button', { name: '复制号码' }).click()
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), '+1555**7654#')
    const switchedOption = await page.locator('.u-line-selector:visible select option:checked').innerText()
    assert.ok(switchedOption.includes('••••7654'))
    assert.equal(switchedOption.includes('+1555**7654#'), false)

    await page.setViewportSize({ width: 2048, height: 900 })
    const selectBox = await page.locator('.u-line-selector:visible select').boundingBox()
    const detailsBox = await callDetails.boundingBox()
    assert.ok(selectBox.width >= 679 && selectBox.width <= 681,
      `wide selector must retain the vmware.2 680px width: ${selectBox.width}`)
    assert.ok(detailsBox.x >= selectBox.x + selectBox.width,
      'wide layout must place the added details after the unchanged selector')
    await page.setViewportSize({ width: 3420, height: 900 })
    const detailBoxes = await callDetails.locator(':scope > div').evaluateAll(nodes => nodes.map(node => {
      const box = node.getBoundingClientRect()
      return { left: box.left, right: box.right }
    }))
    assert.equal(detailBoxes.length, 5)
    assert.ok(detailBoxes.at(-1).right - detailBoxes[0].left < 1000,
      'wide details should stay compact instead of distributing across all empty space')
    for (let index = 1; index < detailBoxes.length; index += 1) {
      assert.ok(detailBoxes[index].left - detailBoxes[index - 1].right <= 21,
        'adjacent details should use only the compact configured gap')
    }
    await page.setViewportSize({ width: 1440, height: 900 })
    const mediumSelectBox = await page.locator('.u-line-selector:visible select').boundingBox()
    assert.ok(mediumSelectBox.width >= 679 && mediumSelectBox.width <= 681,
      `medium selector must retain the vmware.2 680px width: ${mediumSelectBox.width}`)
    for (const width of [3420, 2048, 1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        `calls overflow at ${width}px`)
      await page.screenshot({ path: path.join(output, `calls-${width}.png`), fullPage: true, animations: 'disabled' })
    }

    await page.goto(origin + '/#/messages')
    await verifyFirstLine('messages')
    await page.locator('.u-line-selector:visible select').selectOption('2')
    page.once('dialog', dialog => dialog.accept())
    await page.getByRole('button', { name: '重新导入模块保留短信' }).click()
    await page.getByText('已重新导入 2 条模块保留短信。', { exact: true }).waitFor()
    for (const width of [3420, 2048, 1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        `messages overflow at ${width}px`)
      await page.screenshot({ path: path.join(output, `messages-${width}.png`), fullPage: true, animations: 'disabled' })
    }

    await page.goto(origin + '/#/overview')
    const overviewNumber = page.locator('.u-device-card').first().getByRole('button', { name: '复制号码' })
    await overviewNumber.waitFor()
    assert.equal(await overviewNumber.innerText(), '+447700900357')
    await overviewNumber.click()
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), '+447700900357')
    for (const width of [3420, 2048, 1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        `overview overflow at ${width}px`)
      await page.screenshot({ path: path.join(output, `overview-${width}.png`), fullPage: true, animations: 'disabled' })
    }

    await page.goto(origin + '/#/devices')
    await page.getByText('Travel modem', { exact: true }).click()
    const registered = page.getByText(
      '漫游已注册 · LTE · Visited Fixture · 信号 78% · 无数据承载', { exact: true })
    await registered.waitFor()
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        `device status overflow at ${width}px`)
      await page.screenshot({ path: path.join(output, `device-status-${width}.png`), fullPage: true, animations: 'disabled' })
    }

    await page.locator('.u-tabs').getByRole('button', { name: '蜂窝数据（4G）' }).click()
    page.once('dialog', dialog => dialog.accept())
    await page.getByRole('button', { name: '扫描网络' }).click()
    await page.getByText('正在扫描附近网络，模块可能需要几分钟才能完成。', { exact: true }).waitFor()
    assert.equal(await page.getByRole('button', { name: '正在扫描…' }).isEnabled(), false)
    assert.equal(await page.getByRole('button', { name: '应用网络' }).isEnabled(), false)
    assert.ok(pendingScanResponse)
    json(pendingScanResponse, { networks: [
      { operator_id: '46001', name: 'Visited Fixture', access_technology: 'lte', status: 'current' },
      { operator_id: '46000', name: 'China Mobile', access_technology: 'lte', status: 'available' },
    ] })
    pendingScanResponse = null
    await page.getByText('发现 2 个蜂窝网络。', { exact: true }).waitFor()
    await page.getByLabel('选择方式').selectOption('manual')
    await page.getByLabel('可用网络').selectOption('46000')
    page.once('dialog', dialog => dialog.accept())
    await page.getByRole('button', { name: '应用网络' }).click()
    await page.getByText('已应用蜂窝网络选择：China Mobile (46000)。', { exact: true }).waitFor()
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        `cellular network selection overflow at ${width}px`)
      assert.equal(await page.locator('.u-content').evaluate(element => element.scrollWidth > element.clientWidth), false,
        `cellular network selection is clipped at ${width}px`)
      await page.screenshot({ path: path.join(output, `cellular-network-${width}.png`), fullPage: true, animations: 'disabled' })
      await page.locator('.u-cellular-network').screenshot({
        path: path.join(output, `cellular-network-panel-${width}.png`), animations: 'disabled' })
    }

    assert.deepEqual(errors, [])
    assert.deepEqual(writes, [
      ['POST', '/api/instances/2/messages/reimport-cellular'],
      ['POST', '/api/devices/modem-2/cellular/networks/scan'],
      ['PUT', '/api/devices/modem-2/cellular/network'],
    ])
    console.log('PASS: line details, retained-SMS confirmation, cellular network selection, registered-without-bearer status, and wide/narrow layouts; fixture API only')
  } finally {
    if (browser) await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
