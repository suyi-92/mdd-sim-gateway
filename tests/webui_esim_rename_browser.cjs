// Local assets and fictional data only; no connection to a real gateway or smart card.
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
const { chromium } = require('playwright')
const dist = path.resolve(__dirname, '../webui/dist')
const output = process.env.MDD_RENAME_UI_OUTPUT || '/tmp/mdd-esim-rename-browser'
fs.mkdirSync(output, { recursive: true })
const reader = 'VoWiFi Modem fixture 00 00'
const profiles = [
  { iccid: 'profile-active', profileNickname: 'Active fixture', profileState: 'enabled' },
  { iccid: 'profile-inactive', profileNickname: 'Inactive fixture', profileState: 'disabled' },
]
let running = true, failure = '', liveReads = 0
const writes = []
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://localhost')
  const json = (status, value) => {
    response.writeHead(status, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify(value))
  }
  if (url.pathname.startsWith('/api/')) {
    let body = ''
    for await (const chunk of request) body += chunk.toString()
    if (request.method !== 'GET') {
      writes.push(url.pathname)
      assert.equal(request.headers['x-mdd-csrf-token'], 'fixture-only')
      await new Promise(resolve => setTimeout(resolve, 250))
      if (url.pathname.endsWith('/stop')) {
        if (failure === 'stop') return json(409, { detail: 'fixture stop failed' })
        running = false
      } else if (url.pathname.endsWith('/start')) {
        if (failure === 'start') return json(409, { detail: 'fixture restart failed' })
        running = true
      } else if (url.pathname.endsWith('/nickname')) {
        assert.equal(running, false, 'nickname must wait for exclusive reader access')
        const data = JSON.parse(body)
        assert.equal(data.reader, reader)
        assert.equal(data.se_id, 'default')
        assert.equal(data.aid, 'fixture-aid')
        if (failure === 'nickname') return json(400, { detail: 'fixture nickname rejected' })
        const p = profiles.find(p => url.pathname.includes(`/${p.iccid}/`))
        p.profileNickname = data.nickname
        return json(200, { ok: true, nickname: data.nickname })
      } else {
        assert.fail(`Unexpected write ${url.pathname}`)
      }
      return json(200, { ok: true })
    }
    if (url.pathname === '/api/esim/chip') liveReads++
    const values = {
      '/api/auth/status': { configured: true, authenticated: true, csrf: 'fixture-only' },
      '/api/devices': { devices: [], discovering: false },
      '/api/cards': { cards: [{ name: reader, index: 0, present: true, iccid: 'profile-active', matched: '7' }] },
      '/api/instances': { instances: [{ id: '7', name: 'Fixture line', iccid: 'profile-active', status: { state: running ? 'OK' : 'STOPPED' } },
        { id: '8', name: 'Unrelated line', status: { state: 'OK' } }] },
      '/api/esim/status': { available: true },
      '/api/esim/chip/cached': { cached: true, ts: 1788825600, ses: [{ id: 'default', aid: 'fixture-aid', eid: 'fixture-euicc', profiles }] },
      '/api/system/status': { version: 'fixture' },
    }
    return json(200, values[url.pathname] || {})
  }
  const file = path.resolve(dist, '.' + (url.pathname === '/' ? '/index.html' : url.pathname))
  if (!file.startsWith(dist + path.sep) || !fs.existsSync(file)) {
    response.writeHead(404); response.end(); return
  }
  response.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(file)] || 'application/octet-stream' })
  fs.createReadStream(file).pipe(response)
})
server.on('upgrade', (_request, socket) => socket.destroy())

;(async () => {
  let browser
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
    browser = await chromium.launch({ headless: true, ...(process.env.MDD_BROWSER_EXECUTABLE ? { executablePath: process.env.MDD_BROWSER_EXECUTABLE } : {}) })
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    const open = async (isRunning = true) => {
      running = isRunning
      const url = `http://127.0.0.1:${server.address().port}/#/esim`
      if (page.url() === url) await page.reload()
      else await page.goto(url)
      await page.getByRole('button', { name: '重命名', exact: true }).first().waitFor()
      writes.length = 0
    }
    const dialog = page.getByRole('dialog', { name: '重命名', exact: true })
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      await open()
      const rename = page.getByRole('button', { name: '重命名', exact: true }).nth(1)
      assert.equal(await rename.isEnabled(), true, 'cached inactive profile can be renamed while a line runs')
      assert.equal(await page.getByRole('button', { name: '删除', exact: true }).first().isDisabled(), true)
      await rename.scrollIntoViewIfNeeded()
      const before = await rename.boundingBox()
      await rename.click()
      await dialog.getByRole('button', { name: '取消', exact: true }).click()
      assert.deepEqual(writes, [], 'cancel must not stop a line or write the eSIM')
      await rename.click()
      await dialog.getByRole('textbox').fill(`Renamed ${width} ${'long fixture '.repeat(12)}`)
      await page.screenshot({ path: path.join(output, `rename-dialog-${width}.png`), fullPage: true })
      await dialog.getByRole('button', { name: '停止线路并重命名', exact: true }).click()
      await page.keyboard.press('Enter')
      await page.keyboard.press('Escape')
      assert.equal(await dialog.isVisible(), true, 'busy dialog stays open')
      assert.equal(await page.locator('.u-esim-reader-field select').isDisabled(), true)
      await dialog.waitFor({ state: 'hidden' })
      await page.locator('.u-esim-page [role="status"]').filter({ hasText: '配置文件已重命名。' }).waitFor()
      await page.waitForFunction(() => [...document.querySelectorAll('.u-esim-page button')]
        .some(button => button.textContent === '重命名' && !button.disabled))
      assert.deepEqual(await rename.boundingBox(), before, 'feedback must not move profile actions')
      assert.deepEqual(writes, ['/api/instances/7/stop', '/api/esim/profiles/profile-inactive/nickname', '/api/instances/7/start'])
      assert.equal(running, true)
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow at ${width}`)
      await page.screenshot({ path: path.join(output, `renamed-${width}.png`), fullPage: true })
      await page.reload()
      await page.getByText(`Renamed ${width} ${'long fixture '.repeat(12)}`.trim(), { exact: true }).waitFor()
    }
    for (failure of ['stop', 'nickname', 'start', '']) {
      await open(failure !== '')
      await page.getByRole('button', { name: '重命名', exact: true }).first().click()
      await dialog.getByRole('textbox').fill('New active fixture')
      await dialog.getByRole('button', { name: failure ? '停止线路并重命名' : '更新', exact: true }).click()
      if (failure === 'stop' || failure === 'nickname') {
        await dialog.getByRole('alert').waitFor()
        assert.equal(running, true, 'original line survives a rejected rename')
      } else {
        await dialog.waitFor({ state: 'hidden' })
      }
      if (failure === 'start') await page.locator('.u-esim-page [role="status"]').filter({ hasText: '线路 7 恢复失败' }).waitFor()
      const expected = failure === 'stop' ? ['/api/instances/7/stop']
        : failure ? ['/api/instances/7/stop', '/api/esim/profiles/profile-active/nickname', '/api/instances/7/start']
          : ['/api/esim/profiles/profile-active/nickname']
      assert.deepEqual(writes, expected)
    }
    assert.equal(liveReads, 0, 'rename must not force a live card read after restarting')
    assert.deepEqual(errors, [])
    console.log('PASS: cached active/inactive rename; stop/write/resume order; cancel; duplicate keys; all failure paths; stopped-line preservation; persisted cache; 1440/900/390px')
  } finally {
    if (browser) await browser.close()
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
