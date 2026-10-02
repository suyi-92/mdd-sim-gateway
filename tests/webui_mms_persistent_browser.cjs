// Merged MMS/read-state regression: local fixture APIs only, no real messages or hardware.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const { chromium } = require('playwright')
const dist = path.resolve(__dirname, '../webui/dist')
const output = process.env.MDD_UI_TEST_OUTPUT || '/tmp/mdd-mms-persistent-ui'
fs.mkdirSync(output, { recursive: true })
const peer = '+12025550123'
const instances = [{ id: '1', name: 'Fixture line', msisdn: '+12025550100', enabled: true,
  status: { state: 'OK', label: 'Working' } }]
const devices = [{ id: 'reader-fixture', name: 'Fixture reader', display_name: 'Fixture reader',
  device_type: 'reader', present: true, instance_id: '1', sim: { present: true },
  capabilities: { vowifi: { desired: true, actual: 'on' } } }]
let unread = 1, reads = 0, attachmentSeq = 0, revoked = false
const fits = [], sends = []
const reply = (res, value) => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(value)) }
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost')
  if (url.pathname.startsWith('/api/')) {
    let body = ''
    for await (const chunk of req) body += chunk
    const p = url.pathname
    if (p === '/api/auth/clients/app-fixture' && req.method === 'DELETE') { revoked = true; return reply(res, { ok: true }) }
    if (p === '/api/auth/clients') return reply(res, { clients: revoked ? [] : [{ id: 'app-fixture', name: 'Fixture mobile app', platform: 'android', app_version: '1.0', last_seen: 1700000000 }] })
    if (p === '/api/contacts/resolve') return reply(res, { contacts: { [peer]: { name: 'Fixture Contact' } } })
    if (p === '/api/messages/unread') return reply(res, { total: unread, lines: unread ? ['1'] : [] })
    if (p.endsWith('/messages/unread')) return reply(res, { total: unread, unread: unread ? { [peer]: unread } : {} })
    if (p.endsWith('/messages/read')) { reads++; unread = 0; return reply(res, { ok: true }) }
    if (p.endsWith('/messages/threads')) return reply(res, { threads: [{ peer, last_kind: 'mms', last_body: 'Fixture MMS' }] })
    if (p.endsWith('/messages/binary')) return reply(res, { payloads: [] })
    if (p === '/api/instances/1/messages/' + encodeURIComponent(peer)) return reply(res, { messages: [{
      id: 1, peer, direction: 'in', body: 'Fixture MMS', ts: 1700000000, status: 'delivered', kind: 'mms',
      mms: { state: 'retrieved', parts: [] },
    }] })
    if (p.endsWith('/mms/settings')) return reply(res, { effective: { enabled: true, configured: true } })
    if (p.endsWith('/mms/attachments/fit')) {
      const value = JSON.parse(body); fits.push(value)
      return reply(res, { size: 20, limit: 300000, fits: true, split: value.split,
        messages: value.ids.map(id => ({ size: 10 })), attachments: value.ids.map(id => ({ id, size: 10, content_type: 'text/plain' })) })
    }
    if (p.endsWith('/mms/attachments') && req.method === 'POST') return reply(res, {
      attachment: { id: `attachment-${++attachmentSeq}`, name: `fixture-${attachmentSeq}.txt`, size: 10, content_type: 'text/plain' } })
    if (p.endsWith('/mms/send')) { sends.push(body); return reply(res, { ok: true, message: { peer } }) }
    if (p.endsWith('/softphone')) return reply(res, { enabled: false })
    const values = {
      '/api/auth/status': { configured: true, authenticated: true, csrf: 'fixture-only' },
      '/api/instances': { instances }, '/api/devices': { devices, discovering: false },
      '/api/cards': { cards: [{ name: 'Fixture reader', hardware_id: 'reader-fixture', present: true, matched: '1' }] },
      '/api/system/status': { version: 'fixture' }, '/api/contacts': { contacts: [], total: 0 },
      '/api/settings': { timezone: 'UTC', max_sim_lines: 13 },
      '/api/media': { mode: 'relay', public_port: 41000, relay: { state: 'ready' } },
    }
    return reply(res, values[p] || {})
  }
  const file = path.resolve(dist, '.' + (url.pathname === '/' ? '/index.html' : url.pathname))
  if (!file.startsWith(dist + path.sep) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return }
  res.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' })[path.extname(file)] || 'application/octet-stream' })
  fs.createReadStream(file).pipe(res)
})
server.on('upgrade', (_req, socket) => socket.destroy())
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
        constructor(url) { super(); this.url = url; this.readyState = 1; window.fixtureSockets.push(this); queueMicrotask(() => this.onopen?.(new Event('open'))) }
        send() {} close() { this.readyState = 3 }
        emit(value) { const event = new MessageEvent('message', { data: JSON.stringify(value) }); this.onmessage?.(event); this.dispatchEvent(event) }
      }
    })
    const page = await context.newPage(), errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(origin + '/#/messages')
    const conversation = page.locator('.u-thread-open')
    await conversation.getByText('Fixture Contact').waitFor()
    await Promise.all([page.waitForResponse(r => r.url().endsWith('/messages/read')), conversation.click()])
    const initialReads = reads
    assert.ok(initialReads > 0)
    await page.evaluate(() => { window.location.hash = '#/contacts' })
    await page.getByRole('heading', { name: 'Contacts', exact: true }).waitFor()
    unread = 1
    await page.evaluate(peer => window.fixtureSockets.forEach(socket => socket.emit({
      type: 'sms', instance: '1', message: { direction: 'in', peer },
    })), peer)
    await page.waitForResponse(r => r.url().endsWith('/messages/unread'))
    await page.waitForTimeout(150)
    assert.equal(reads, initialReads, 'hidden persistent Messages page must leave incoming messages unread')
    const readAgain = page.waitForResponse(r => r.url().endsWith('/messages/read'))
    await page.evaluate(() => { window.location.hash = '#/messages' })
    await readAgain
    assert.ok(reads > initialReads, 'returning to a visible conversation marks its new messages read')
    await page.locator('.u-message-conversation input[type=file]').setInputFiles([
      { name: 'first.txt', mimeType: 'text/plain', buffer: Buffer.from('fixture A') },
      { name: 'second.txt', mimeType: 'text/plain', buffer: Buffer.from('fixture B') },
    ])
    await page.getByRole('radio', { name: 'One MMS per attachment', exact: true }).waitFor()
    await page.waitForResponse(r => r.url().endsWith('/mms/attachments/fit'))
    await Promise.all([page.waitForResponse(r => r.url().endsWith('/mms/attachments/fit') && r.request().postDataJSON().split),
      page.getByRole('radio', { name: 'One MMS per attachment', exact: true }).click()])
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      await page.locator('.u-content').screenshot({ path: path.join(output, `mms-compose-${width}.png`), animations: 'disabled' })
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `MMS compose no overflow at ${width}`)
    }
    await Promise.all([page.waitForResponse(r => r.url().endsWith('/mms/send')), page.getByRole('button', { name: 'Send', exact: true }).click()])
    assert.equal(sends.length, 1)
    assert.ok(sends[0].includes('name="split"\r\n\r\n1'))
    assert.equal((sends[0].match(/name="attachment_ids"/g) || []).length, 2)
    assert.equal(fits.at(-1).split, true)
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      await page.waitForTimeout(100)
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `no overflow at ${width}`)
      await page.locator('.u-content').screenshot({ path: path.join(output, `messages-${width}.png`), animations: 'disabled' })
      if (width === 390) {
        assert.equal(await page.locator('.u-messages-list').isVisible(), false)
        await page.getByRole('button', { name: 'Back to conversations' }).click()
        assert.equal(await page.locator('.u-messages-list').isVisible(), true)
      }
    }
    await page.evaluate(() => { window.location.hash = '#/settings' })
    await page.getByRole('button', { name: 'Calls & VoWiFi', exact: true }).click()
    await page.getByText('Media relay, port 41000').waitFor()
    await page.getByText('Relay ready', { exact: true }).waitFor()
    await page.getByText(/Switch with sudo mddctl media relay/).waitFor()
    await page.getByRole('button', { name: 'Security', exact: true }).click()
    await page.getByText('Fixture mobile app', { exact: true }).waitFor()
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      await page.locator('.u-content').screenshot({ path: path.join(output, `security-${width}.png`), animations: 'disabled' })
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `security no overflow at ${width}`)
      await page.locator('.u-settings-section').filter({ has: page.getByRole('heading', { name: 'Signed-in apps' }) }).screenshot({ path: path.join(output, `apps-${width}.png`), animations: 'disabled' })
    }
    page.once('dialog', dialog => dialog.accept())
    await page.locator('.u-settings-section').filter({ has: page.getByRole('heading', { name: 'Signed-in apps' }) }).getByRole('button', { name: 'Sign out', exact: true }).click()
    await page.getByText('No apps are signed in.', { exact: true }).waitFor()
    assert.equal(revoked, true)
    assert.deepEqual(errors, [])
    console.log('PASS MMS attachments/split-send, contact labels, hidden-page unread, media status, app revocation, and 1440/900/390px layout')
  } finally { await browser?.close(); server.close() }
})().catch(error => { console.error(error); process.exitCode = 1 })
