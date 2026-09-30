// Local fixture APIs only. Never talks to production, a real SIM, or an eSIM server.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const { chromium } = require('playwright')
const dist = path.resolve(__dirname, '../webui/dist')
const output = process.env.MDD_UI_TEST_OUTPUT || '/tmp/mdd-esim-maintenance-browser'
fs.mkdirSync(output, { recursive: true })
const reader = 'Fixture reader'
const profile = { iccid: 'fixture-card', profileNickname: 'Card nickname', profileState: 'enabled', local_label: '' }
const se = { id: 'default', eid: 'fixture-euicc', profiles: [profile], notifications: [] }
let operation = null, operationReads = 0, flight = true, importFailure = false, running = true
const writes = [], errors = [], dialogs = []
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://localhost')
  const json = (value, code = 200) => {
    response.writeHead(code, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify(value))
  }
  if (url.pathname.startsWith('/api/')) {
    let raw = ''
    for await (const chunk of request) raw += chunk
    if (request.method !== 'GET') {
      writes.push(url.pathname)
      const body = JSON.parse(raw || '{}')
      if (url.pathname === '/api/instances/7/stop') return json({ ok: true })
      if (url.pathname === '/api/esim/profiles/fixture-next/enable') {
        assert.equal(body.reader, reader)
        assert.equal(body.se_id, 'default')
        se.profiles.forEach(item => { item.profileState = item.iccid === 'fixture-next' ? 'enabled' : 'disabled' })
        return json({ ok: true, card: { identity_state: 'confirmed', iccid: 'fixture-next' } })
      }
      if (url.pathname.endsWith('/label')) {
        assert.equal(body.eid, 'fixture-euicc'); assert.equal(body.se_id, 'default')
        profile.local_label = body.label
        return json({ ok: true, local_label: body.label })
      }
      if (url.pathname === '/api/esim/chip/read' || url.pathname === '/api/esim/download') {
        assert.equal(body.resume_line_id, '7')
        assert.equal(body.expected_iccid, 'fixture-card')
        assert.equal(body.expected_generation, 4)
        if (url.pathname.endsWith('/read')) return json({ ok: true, ses: [se], generation: 4, line_recovery: 'started' })
        operation = { operation_id: 'fixture-operation', state: 'success', step: 'completed', generation: 4, line_recovery: 'failed' }
        operationReads = 0
        return json({ ok: true, operation: { ...operation, state: 'running', step: 'started', line_recovery: 'recovering' } })
      }
      if (url.pathname.endsWith('/reimport-cellular')) {
        return importFailure
          ? json({ detail: { code: 'cellular_unavailable', message: 'ModemManager is unavailable.' } }, 409)
          : json({ ok: true, imported: 2, retained: 2 })
      }
      assert.fail(`Unexpected mutation ${url.pathname}`)
    }
    const values = {
      '/api/auth/status': { configured: true, authenticated: true, csrf: 'fixture-only' },
      '/api/cards': { cards: [{ name: reader, index: 0, present: true, iccid: 'fixture-card', generation: 4, matched: '7', hardware_id: 'modem-fixture' }] },
      '/api/instances': { instances: [{ id: '7', name: 'Fixture line', iccid: 'fixture-card', enabled: true, status: { state: running ? 'OK' : 'STOPPED' } }] },
      '/api/devices': { devices: [{ id: 'modem-fixture', device_type: 'modem', present: true,
        instance_id: '7', name: 'Fixture modem', sim: { present: true },
        cellular: flight ? null : { registration: 'searching', data_active: false },
        shared: { modemmanager_active: !flight },
        capabilities: { flight: { desired: flight, actual: flight ? 'on' : 'off' },
          cellular: { desired: false, actual: 'off' }, vowifi: { desired: true, actual: 'on' } } }], discovering: false },
      '/api/esim/status': { available: true },
      '/api/esim/chip/cached': { cached: true, generation: 4, ts: 1788825600, ses: [se] },
      '/api/esim/download/operation': { operation: operation && url.pathname === '/api/esim/download/operation' && operationReads++ === 0
        ? { ...operation, generation: 3, line_recovery: 'recovering' } : operation },
      '/api/system/status': { version: 'fixture' },
    }
    if (url.pathname.endsWith('/messages/threads')) return json({ threads: [] })
    if (url.pathname.endsWith('/messages/binary')) return json({ payloads: [] })
    return json(values[url.pathname] || {})
  }
  const file = path.resolve(dist, '.' + (url.pathname === '/' ? '/index.html' : url.pathname))
  if (!file.startsWith(dist + path.sep) || !fs.existsSync(file)) { response.writeHead(404); response.end(); return }
  response.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' })[path.extname(file)] || 'application/octet-stream' })
  fs.createReadStream(file).pipe(response)
})
server.on('upgrade', (_request, socket) => socket.destroy())

;(async () => {
  let browser
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
    const origin = `http://127.0.0.1:${server.address().port}`
    browser = await chromium.launch({ headless: true, ...(process.env.MDD_BROWSER_EXECUTABLE ? { executablePath: process.env.MDD_BROWSER_EXECUTABLE } : {}) })
    const page = await browser.newPage()
    page.on('pageerror', error => errors.push(error.message))
    page.on('dialog', dialog => {
      if (page.url().endsWith('/esim')) {
        dialogs.push(dialog.message())
        return dialog.dismiss()
      }
      return dialog.accept()
    })
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      operation = null
      await page.goto(`${origin}/#/esim`)
      await page.reload()
      await page.getByRole('button', { name: '本地备注', exact: true }).waitFor()
      writes.length = 0
      await page.getByRole('button', { name: '本地备注', exact: true }).click()
      const note = page.getByRole('dialog', { name: '本地备注', exact: true })
      await note.getByRole('textbox').fill(`Desk ${width} ${'long note '.repeat(7)}`)
      await note.getByRole('button', { name: '更新', exact: true }).click()
      await note.waitFor({ state: 'hidden' })
      assert.deepEqual(writes, ['/api/esim/profiles/fixture-card/label'])
      await page.reload()
      await page.getByText(profile.local_label, { exact: true }).waitFor()
      writes.length = 0
      await page.getByRole('button', { name: '下载 eSIM 卡', exact: true }).click()
      await page.locator('.u-esim-dialog').getByRole('button', { name: '取消', exact: true }).click()
      assert.deepEqual(writes, [], 'Opening and cancelling download must not stop a line')
      await page.getByRole('button', { name: '读取', exact: true }).click()
      await page.getByText('已请求启动原线路，请在“设备”查看注册结果。', { exact: true }).waitFor()
      assert.deepEqual(writes, ['/api/esim/chip/read'], 'Server owns stop and recovery')
      assert.deepEqual(dialogs, [], 'Reading must proceed without a browser confirmation')
      await page.screenshot({ path: path.join(output, `esim-${width}.png`), fullPage: true })
      await page.getByRole('button', { name: '本地备注', exact: true }).scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(output, `esim-profile-${width}.png`), fullPage: true })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
      flight = true
      await page.goto(`${origin}/#/messages`)
      await page.reload()
      const reimport = page.getByRole('button', { name: '重新导入模块保留短信', exact: true })
      await reimport.waitFor()
      assert.equal(await reimport.isDisabled(), true)
      await page.getByText(/飞行模式已停用蜂窝服务/).waitFor()
      await page.screenshot({ path: path.join(output, `sms-${width}.png`), fullPage: true })
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
      flight = false
      await page.reload()
      await reimport.waitFor()
      await page.waitForFunction(() => [...document.querySelectorAll('button')]
        .some(button => button.textContent === '重新导入模块保留短信' && !button.disabled))
      assert.equal(await reimport.isEnabled(), true, 'Mobile data off and searching still permit stored SMS import')
      importFailure = true
      await reimport.click()
      await page.getByText('重新导入失败: 蜂窝服务不可用，请前往“设备”检查模块状态后重试。', { exact: true }).waitFor()
      assert.equal(await page.getByText('ModemManager is unavailable.', { exact: false }).count(), 0)
      importFailure = false
      await reimport.click()
      await page.getByText('已重新导入 2 条模块保留短信。', { exact: true }).waitFor()
    }
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.goto(`${origin}/#/esim`)
    await page.reload()
    await page.getByRole('button', { name: '下载 eSIM 卡', exact: true }).click()
    await page.locator('.u-esim-dialog textarea').fill('LPA:1$fixture.example.invalid$fixture-code')
    writes.length = 0
    await page.locator('.u-esim-dialog').getByRole('button', { name: '下载', exact: true }).click()
    await page.getByText('原线路恢复失败，请在“设备”检查 SIM 和注册状态。', { exact: true }).waitFor()
    assert.ok(operationReads >= 2, 'A temporarily mismatched generation must not stop recovery polling')
    assert.deepEqual(writes, ['/api/esim/download'])
    assert.deepEqual(dialogs, [], 'Submitting download must not ask for repeated confirmation')
    await page.reload()
    await page.getByText('原线路恢复失败，请在“设备”检查 SIM 和注册状态。', { exact: true }).waitFor()
    const complete = page.getByText('下载完成', { exact: true })
    const closeResult = page.getByRole('button', { name: '关闭', exact: true })
    await page.waitForTimeout(5500)
    assert.equal(await complete.isVisible(), true, 'Recovery failure must not disappear automatically')
    await closeResult.click()
    await page.reload()
    await page.getByRole('button', { name: '下载 eSIM 卡', exact: true }).waitFor()
    await page.waitForTimeout(1200)
    assert.equal(await complete.count(), 0, 'Manual dismissal survives reload')
    operation = { ...operation, operation_id: 'fixture-success', line_recovery: 'recovering' }
    await page.reload()
    await complete.waitFor()
    await page.waitForTimeout(5500)
    assert.equal(await complete.isVisible(), true, 'Do not hide success while restoring the line')
    assert.equal(await closeResult.count(), 0, 'Recovery must finish before a result can be dismissed')
    operation.line_recovery = 'started'
    await page.getByText('已请求启动原线路，请在“设备”查看注册结果。', { exact: true }).waitFor()
    await complete.waitFor({ state: 'hidden', timeout: 8000 })
    await page.reload()
    await page.getByRole('button', { name: '下载 eSIM 卡', exact: true }).waitFor()
    await page.waitForTimeout(1200)
    assert.equal(await complete.count(), 0, 'Automatically dismissed success stays closed after reload')
    operation = { ...operation, operation_id: 'fixture-failed', state: 'failed', error_code: 'remote_rejected', line_recovery: undefined }
    await page.reload()
    await page.getByText('下载失败', { exact: true }).waitFor()
    await page.waitForTimeout(5500)
    assert.equal(await page.getByText('下载失败', { exact: true }).isVisible(), true, 'Download failures remain actionable')
    await closeResult.click()
    operation = { ...operation, operation_id: 'fixture-new-download', state: 'success', line_recovery: 'not_needed' }
    await page.reload()
    await complete.waitFor()
    await page.screenshot({ path: path.join(output, 'download-complete.png'), fullPage: true })
    await closeResult.click()
    await page.goto(`${origin}/#/messages`)
    await page.goto(`${origin}/#/esim`)
    await page.waitForTimeout(1200)
    assert.equal(await complete.count(), 0, 'Dismissal survives navigation and does not hide another job')
    se.profiles.push({ iccid: 'fixture-next', profileNickname: 'Next fixture', profileState: 'disabled' })
    await page.reload()
    writes.length = 0
    await page.getByRole('button', { name: '启用', exact: true }).click()
    await page.getByText('配置文件切换完成，VoWiFi 线路已启动。', { exact: true }).waitFor()
    assert.deepEqual(writes, ['/api/instances/7/stop', '/api/esim/profiles/fixture-next/enable'],
      'One click must retain stop-before-switch ordering')
    assert.deepEqual(dialogs, [], 'Switching must proceed without a browser confirmation')
    running = false
    await page.reload()
    writes.length = 0
    await page.getByRole('button', { name: '删除', exact: true }).first().click()
    assert.equal(dialogs.length, 1, 'Deleting a profile must still require confirmation')
    assert.deepEqual(writes, [], 'Cancelling deletion must not mutate the card')
    assert.deepEqual(errors, [])
    console.log('PASS: download auto-dismiss, recovery/failure retention, persistent dismissal, independent new jobs; no repeated read/download/switch dialogs; deletion confirmation; stop/recovery; local notes; SMS availability; 1440/900/390 layouts')
  } finally {
    if (browser) await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
