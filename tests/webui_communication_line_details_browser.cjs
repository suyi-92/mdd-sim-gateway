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
    cellular: { registration: 'roaming', access_technology: 'lte', operator: 'China Unicom', operator_zh: '中国联通',
      operator_code: '46001', observed_at: 100, signal: 78, packet_service: 'attached', data_active: false },
    cellular_network: { mode: 'automatic', operator_id: '' },
    egress: { country: 'us', detected_country: 'us', node: '', mode: 'direct', ready: true },
    capabilities: { cellular: { desired: false, actual: 'off',
      reason: 'Mobile data is disconnected; the modem radio can remain registered to the cellular network.' },
      flight: { desired: false, actual: 'off' }, vowifi: { desired: true, actual: 'on' } } },
]

const writes = []
let operation = null
let networks = []
let operationNumber = 0
let applying = false
let registrationFailure = false
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
    if (/\/cellular\/network-operation$/.test(url.pathname)) {
      return json(response, { context: 'fixture-context', networks, operation })
    }
    if (/\/cellular\/networks\/scan$/.test(url.pathname)) {
      operation = { id: `scan-${++operationNumber}`, action: 'scan', state: 'running', selection: {} }
      return json(response, { context: 'fixture-context', networks, operation })
    }
    if (/\/cellular\/network$/.test(url.pathname)) {
      operation = { id: `apply-${++operationNumber}`, action: 'apply', state: applying ? 'running' : registrationFailure ? 'failed' : 'success',
        selection: { mode: 'manual', operator_id: '46000' },
        ...(registrationFailure ? { error: { code: 'network_timeout', recovery: { state: 'restored' } } }
          : { result: { ok: true, mode: 'manual', operator_id: '46000', registration: { operator_id: '46000', state: 'roaming' } } }) }
      return json(response, { context: 'fixture-context', networks, operation })
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
      for (const expected of ['运营商', '线路名称', '号码', '号码地区', 'SIM 归属网', '网络线路',
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
    assert.equal(detailBoxes.length, 6)
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
      '漫游已注册 · LTE · 中国联通 · 信号 78% · 无数据承载', { exact: true })
    await registered.waitFor()
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        `device status overflow at ${width}px`)
      await page.screenshot({ path: path.join(output, `device-status-${width}.png`), fullPage: true, animations: 'disabled' })
    }

    await page.locator('.u-tabs').getByRole('button', { name: '蜂窝数据（4G）' }).click()
    for (const width of [2560, 1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      const controls = page.locator('.u-cellular-network-controls')
      const modeBox = await controls.getByRole('group').boundingBox()
      const scanBox = await controls.getByRole('button', { name: '扫描网络' }).boundingBox()
      assert.equal(await controls.getByRole('button', { name: '扫描网络' }).isEnabled(), false)
      assert.ok(scanBox.x - (modeBox.x + modeBox.width) < 24, `automatic action disconnected from mode at ${width}`)
      await page.locator('.u-cellular-network').screenshot({ path: path.join(output, `automatic-panel-${width}.png`), animations: 'disabled' })
    }
    page.once('dialog', dialog => dialog.accept())
    await page.getByRole('button', { name: '手动', exact: true }).click()
    await page.getByRole('button', { name: '扫描网络' }).click()
    await page.getByText('正在扫描附近网络，模块可能需要几分钟才能完成。', { exact: true }).waitFor()
    assert.equal(await page.getByRole('button', { name: '正在扫描…' }).isEnabled(), false)
    assert.equal(await page.getByRole('button', { name: '应用网络' }).isEnabled(), false)
    assert.equal(operation.state, 'running')
    networks = [
      { operator_id: '46001', name: 'China Unicom', name_zh: '中国联通', access_technology: 'lte', status: 'current' },
      { operator_id: '46000', name: 'China Mobile', name_zh: '中国移动', access_technology: 'lte', status: 'available' },
      { operator_id: '46011', name: 'China Telecom', name_zh: '中国电信', access_technology: 'lte', status: 'forbidden' },
    ]
    operation = { ...operation, state: 'success', result: { networks } }
    await page.getByText('发现 3 个蜂窝网络。', { exact: true }).waitFor()
    assert.equal(await page.getByRole('radio', { name: /中国电信/ }).isEnabled(), false)
    assert.equal(await page.getByRole('radio', { name: /China Mobile/ }).locator('b').innerText(), '中国移动')
    assert.equal(await page.getByRole('radio', { name: /China Mobile/ }).locator('small').innerText(), 'China Mobile')
    await page.getByRole('radio', { name: /China Mobile/ }).click()
    applying = true
    page.once('dialog', dialog => dialog.accept())
    await page.getByRole('button', { name: '应用网络' }).click()
    await page.getByText('正在等待模块确认驻网，失败后将恢复之前的选择…', { exact: true }).waitFor()
    await page.locator('.u-tabs').getByRole('button', { name: '硬件', exact: true }).click()
    await page.locator('.u-tabs').getByRole('button', { name: '蜂窝数据（4G）' }).click()
    assert.equal(await page.getByRole('button', { name: '正在应用…' }).isEnabled(), false)
    assert.equal(await page.getByRole('button', { name: '扫描网络' }).isEnabled(), false)
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.locator('.u-sidebar nav').getByRole('button', { name: /概览/ }).click()
    await page.locator('.u-sidebar nav').getByRole('button', { name: /设备/ }).click()
    await page.getByRole('radio', { name: /China Mobile/ }).waitFor()
    assert.equal(await page.getByRole('radio', { name: /China Mobile/ }).getAttribute('aria-checked'), 'true')
    assert.equal(await page.getByRole('button', { name: '扫描网络' }).isEnabled(), false)
    await page.reload()
    await page.locator('.u-tabs').getByRole('button', { name: '蜂窝数据（4G）' }).click()
    await page.getByRole('button', { name: '正在应用…' }).waitFor()
    assert.equal(await page.getByRole('button', { name: '扫描网络' }).isEnabled(), false)
    assert.equal(await page.getByRole('radio', { name: /China Mobile/ }).getAttribute('aria-checked'), 'true')
    operation = { ...operation, phase: 'confirming', remaining_seconds: 60 }
    await page.getByText('正在确认驻网状态… 最多还需 60 秒。', { exact: true }).waitFor()
    const waitingButton = await page.getByRole('button', { name: '正在应用…' }).boundingBox()
    operation = { ...operation, phase: 'restoring', remaining_seconds: 30 }
    await page.getByText('驻网未确认，正在恢复原选网方式… 最多还需 30 秒。', { exact: true }).waitFor()
    const restoringButton = await page.getByRole('button', { name: '正在应用…' }).boundingBox()
    assert.equal(restoringButton.x, waitingButton.x)
    assert.equal(restoringButton.y, waitingButton.y)
    await page.locator('.u-cellular-network').screenshot({ path: path.join(output, 'restoring-stage.png'), animations: 'disabled' })
    applying = false
    operation = { ...operation, state: 'success' }
    // Model the Control normalization of the modem's stale name/new PLMN pair.
    devices[1].cellular = { ...devices[1].cellular, operator: 'China Mobile', operator_zh: '中国移动',
      operator_code: '46000', operator_reported: 'CHN-UNICOM', operator_name_conflict: true, observed_at: 200 }
    devices[1].cellular_network = { mode: 'manual', operator_id: '46000',
      operator_name: 'China Mobile', operator_name_zh: '中国移动', access_technology: 'lte' }
    await page.getByText('当前已驻网：中国移动 (46000)。', { exact: true }).waitFor()
    assert.equal(await page.locator('.u-cellular-operator-detail strong').innerText(), '中国移动 (46000)')
    assert.equal(await page.locator('.u-cellular-operator-detail small').innerText(), 'China Mobile')
    assert.equal((await page.locator('.u-cellular-operator-detail').innerText()).includes('CHN-UNICOM'), false)
    assert.equal(await page.getByRole('radio', { name: /China Mobile/ }).locator('.u-network-availability').innerText(), '当前')
    assert.equal(await page.getByRole('radio', { name: /China Unicom/ }).locator('.u-network-availability').innerText(), '已检测')
    for (const width of [2560, 1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        `cellular network selection overflow at ${width}px`)
      assert.equal(await page.locator('.u-content').evaluate(element => element.scrollWidth > element.clientWidth), false,
        `cellular network selection is clipped at ${width}px`)
      await page.screenshot({ path: path.join(output, `cellular-network-${width}.png`), fullPage: true, animations: 'disabled' })
      await page.locator('.u-cellular-network').screenshot({
        path: path.join(output, `cellular-network-panel-${width}.png`), animations: 'disabled' })
    }

    registrationFailure = true
    page.once('dialog', dialog => dialog.accept())
    await page.getByRole('button', { name: '应用网络' }).click()
    const feedback = page.locator('.u-cellular-network-feedback')
    await feedback.getByText(/所选网络未能完成注册/).waitFor()
    assert.ok((await feedback.innerText()).includes('已恢复之前的选网并确认驻网。'))
    assert.equal((await feedback.innerText()).includes('GDBus'), false)
    for (const width of [2560, 1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.locator('.u-content').evaluate(element => element.scrollWidth > element.clientWidth), false)
      await page.locator('.u-cellular-network').screenshot({ path: path.join(output, `registration-error-${width}.png`), animations: 'disabled' })
    }
    await page.getByRole('button', { name: '自动', exact: true }).click()
    await feedback.getByText('由 SIM 自动选择可接入的运营商。', { exact: true }).waitFor()
    assert.equal(await page.locator('.u-cellular-network-feedback.is-error').count(), 0,
      'changing selection must clear the previous attempt’s error')
    assert.equal(await page.getByRole('button', { name: '扫描网络' }).isEnabled(), false)
    await page.getByRole('button', { name: '手动', exact: true }).click()
    page.once('dialog', dialog => dialog.accept())
    await page.getByRole('button', { name: '扫描网络' }).click()
    await page.getByRole('button', { name: '正在扫描…' }).waitFor()
    devices[1].cellular = { ...devices[1].cellular, registration: 'searching', observed_at: 300 }
    operation = { ...operation, state: 'partial', finished_at: 250,
      error: { code: 'scan_recovery', recovery: { state: 'failed', mode: 'manual', operator_id: '46000' } } }
    await page.locator('.u-cellular-network-feedback.is-warning').getByText(/扫描后未能恢复驻网/).waitFor()
    await page.locator('.u-cellular-current').getByText('未连接', { exact: true }).waitFor()
    assert.equal(await page.getByRole('radio', { name: /China Mobile/ }).count(), 1)
    assert.equal(await page.getByRole('button', { name: '扫描网络' }).isEnabled(), true)
    await page.locator('.u-cellular-network').screenshot({ path: path.join(output, 'scan-recovery-failed.png'), animations: 'disabled' })
    devices[1].cellular = { ...devices[1].cellular, registration: 'roaming', observed_at: 400 }
    await page.reload()
    await page.getByText('扫描完成，发现 3 个运营商网络。当前已恢复驻网：中国移动 (46000)。', { exact: true }).waitFor()
    assert.equal(await page.locator('.u-cellular-network-feedback.is-warning').count(), 0)
    await page.locator('.u-cellular-network').screenshot({ path: path.join(output, 'scan-recovered.png'), animations: 'disabled' })

    // eSIM REFRESH may publish devices before the cards websocket catches up. The
    // old card must disappear, and selection follows the same physical modem.
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto(origin + '/#/messages')
    await page.locator('.u-line-selector:visible select').selectOption('2')
    cards[1].hardware_id = 'modem-2'
    instances.push({ id: '3', name: 'New eSIM profile', mcc: '454', mnc: '00', enabled: false, msisdn: '+12025550123',
      status: { state: 'STOPPED', label: 'Stopped' } })
    devices[1] = { ...devices[1], instance_id: '3', cellular: null,
      capabilities: { ...devices[1].capabilities, vowifi: { desired: false, actual: 'off' } },
      egress: { country: 'hk', detected_country: 'hk', mode: 'manual', ready: false },
      sim: { present: true, number: '+12025550123', number_country: 'us', home_country: 'hk',
        carrier: { name: 'Saily', brand_source: 'esim_profile', plmn: '454-00' } } }
    await page.waitForFunction(() => [...document.querySelectorAll('.u-line-selector select')]
      .some(select => select.offsetParent && select.value === '3'))
    assert.deepEqual(await page.locator('.u-line-selector:visible select option').evaluateAll(
      options => options.map(option => option.value)), ['1', '3'])
    assert.ok((await page.locator('.u-line-selector:visible select option:checked').innerText()).includes('VoWiFi 已关闭'))
    const sailyDetails = page.locator('.u-line-selector-meta:visible')
    await sailyDetails.getByText('Saily', { exact: true }).waitFor()
    await sailyDetails.getByText('美国 (US)', { exact: true }).waitFor()
    await sailyDetails.getByText('香港 (HK) · 454-00', { exact: true }).waitFor()
    await sailyDetails.getByRole('button', { name: '复制号码' }).click()
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), '+12025550123')
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 900 })
      assert.equal(await page.locator('.u-content').evaluate(element => element.scrollWidth > element.clientWidth), false)
      await page.locator('.u-line-selector:visible').screenshot({ path: path.join(output, `esim-switch-${width}.png`), animations: 'disabled' })
    }
    assert.deepEqual(errors, [])
    assert.deepEqual(writes, [
      ['POST', '/api/instances/2/messages/reimport-cellular'],
      ['POST', '/api/devices/modem-2/cellular/networks/scan'],
      ['PUT', '/api/devices/modem-2/cellular/network'],
      ['PUT', '/api/devices/modem-2/cellular/network'],
      ['POST', '/api/devices/modem-2/cellular/networks/scan'],
    ])
    console.log('PASS: line details, retained-SMS confirmation, cellular network selection, registered-without-bearer status, and wide/narrow layouts; fixture API only')
  } finally {
    if (browser) await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
