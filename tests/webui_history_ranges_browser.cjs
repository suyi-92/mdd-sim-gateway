// Uses local build assets and fictional history; never accesses a running gateway.
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
const { chromium } = require('playwright')
const dist = path.resolve(__dirname, '../webui/dist')
const output = process.env.MDD_HISTORY_UI_OUTPUT || '/tmp/mdd-history-ranges/browser'
fs.mkdirSync(output, { recursive: true })
const spans = [900, 1800, 3600, 10800, 21600, 43200, 86400, 172800, 259200, 604800, 1209600, 2592000]
const devices = ['7', '8'].map(id => ({ id: `reader-${id}`, name: `Fixture reader ${id}`, device_type: 'reader', present: true,
  instance_id: id, sim: { present: true }, status: { state: 'OK' },
  capabilities: { vowifi: { desired: true, actual: 'on' } }, vowifi: { ims: 'Registered' } }))
const requests = [], writes = [], held = []
let hold = '', mode = 'normal'
const ratio = (id, span) => id === '8' ? 0.9 : span <= 3600 ? 0.8 : span <= 43200 ? 0.6 : 0.25
function history(id, span) {
  const end = Math.floor(Date.now() / 1000), start = end - span
  const up = mode === 'unknown' ? 0 : Math.round(span * (mode === 'edges' ? 0.94 : ratio(id, span)))
  const segments = mode === 'unknown' ? [{ start, end, state: 'unknown' }]
    : [{ start, end: start + up, state: 'up' }, { start: start + up, end, state: 'down', reason: 'reg_unanswered' }]
  if (mode === 'edges') {
    const edge = Math.floor((span - up) / 2)
    segments.splice(0, segments.length,
      { start, end: start + edge, state: 'down', reason: 'reg_unanswered' },
      { start: start + edge, end: end - edge, state: 'up' },
      { start: end - edge, end, state: 'down', reason: 'reg_reauth_failed',
        detail: JSON.stringify({ code: 'server_pcscf_401_missing_security_server', peer: 'pcscf.' + 'fixture-'.repeat(24) + 'example.test' }) })
  }
  return { instance: id, start, end, span_seconds: span, max_span_seconds: 2592000,
    recorded_since: mode === 'unknown' ? null : start,
    segments, summary: { up, down: mode === 'unknown' ? 0 : span - up, unknown: mode === 'unknown' ? span : 0,
      off: 0, observed_seconds: mode === 'unknown' ? 0 : span, uptime_ratio: mode === 'unknown' ? null : up / span,
      outages: mode === 'unknown' ? 0 : mode === 'edges' ? 2 : 1,
      longest_outage_seconds: mode === 'unknown' ? 0 : (span - up) / (mode === 'edges' ? 2 : 1) } }
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost')
  const json = (status, value) => { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(value)) }
  if (url.pathname.startsWith('/api/')) {
    if (req.method !== 'GET' && url.pathname !== '/api/diagnostics/client-events') writes.push(url.pathname)
    const match = url.pathname.match(/^\/api\/instances\/(\d+)\/availability$/)
    if (match) {
      const id = match[1], span = Number(url.searchParams.get('span_seconds'))
      requests.push({ id, span })
      const answer = history(id, span)
      if (hold === `${id}:${span}`) { held.push(() => json(200, answer)); return }
      const failed = mode === 'error'
      setTimeout(() => json(failed ? 503 : 200, failed ? { detail: 'fixture history unavailable' } : answer), 80)
      return
    }
    const values = {
      '/api/auth/status': { configured: true, authenticated: true, csrf: 'fixture-only' },
      '/api/devices': { devices, discovering: false }, '/api/instances': { instances: [] },
      '/api/cards': { cards: [] }, '/api/system/status': { version: 'fixture' },
    }
    return json(200, values[url.pathname] || {})
  }
  const file = path.resolve(dist, '.' + (url.pathname === '/' ? '/index.html' : url.pathname))
  if (!file.startsWith(dist + path.sep) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return }
  res.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(file)] || 'application/octet-stream' })
  fs.createReadStream(file).pipe(res)
})
server.on('upgrade', (_request, socket) => socket.destroy())

;(async () => {
  let browser
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
    browser = await chromium.launch({ headless: true, ...(process.env.MDD_BROWSER_EXECUTABLE ? { executablePath: process.env.MDD_BROWSER_EXECUTABLE } : {}) })
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 }, timezoneId: 'Asia/Shanghai' })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    const base = `http://127.0.0.1:${server.address().port}`
    await page.goto(base + '/#/devices')
    await page.getByRole('button', { name: 'VoWiFi', exact: true }).click()
    const widget = page.locator('.u-uptime')
    const select = widget.getByRole('combobox', { name: '连接历史时间范围' })
    const figure = widget.locator('.u-uptime-figure strong')
    const ready = async () => page.waitForFunction(() => document.querySelector('.u-uptime-body')?.getAttribute('aria-busy') === 'false')
    await ready()
    assert.equal(await select.inputValue(), '86400')
    assert.equal(await select.locator('option').count(), 12)
    assert.equal(await figure.innerText(), '25.0%')

    hold = '7:3600'
    await select.selectOption('3600')
    while (!held.length) await new Promise(resolve => setTimeout(resolve, 10))
    assert.equal(await figure.innerText(), '—', 'old range metrics must disappear immediately')
    assert.equal(await widget.locator('.u-uptime-body').getAttribute('aria-busy'), 'true')
    await select.selectOption('21600')
    await ready()
    assert.equal(await figure.innerText(), '60.0%')
    hold = ''; held.splice(0).forEach(release => release())
    await page.waitForTimeout(150)
    assert.equal(await figure.innerText(), '60.0%', 'late 1-hour response must not replace the 6-hour result')

    hold = '7:43200'
    await select.selectOption('43200')
    while (!held.length) await new Promise(resolve => setTimeout(resolve, 10))
    await page.locator('.u-device-option').nth(1).click()
    await ready()
    assert.equal(await figure.innerText(), '90.0%')
    hold = ''; held.splice(0).forEach(release => release())
    await page.waitForTimeout(150)
    assert.equal(await figure.innerText(), '90.0%', 'late response from the prior line must be ignored')
    await page.locator('.u-device-option').first().click()
    await ready()

    for (const span of spans) {
      if (await select.inputValue() !== String(span)) {
        const response = page.waitForResponse(r => r.url().includes('/instances/7/availability?span_seconds=' + span))
        await select.selectOption(String(span))
        await response
      }
      await ready()
      assert.equal(await figure.innerText(), (ratio('7', span) * 100).toFixed(1) + '%')
      assert.ok(requests.some(r => r.id === '7' && r.span === span))
    }
    await page.reload()
    await page.getByRole('button', { name: 'VoWiFi', exact: true }).click()
    await ready()
    assert.equal(await select.inputValue(), '2592000', 'selection survives a reload')
    assert.ok(await widget.locator('.u-uptime-axis span').count() <= 8, 'month view needs readable day ticks')
    await widget.locator('details summary').click()
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 1100 })
      await widget.scrollIntoViewIfNeeded()
      await page.mouse.move(0, 0)
      await page.keyboard.press('Escape')
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `full view overflow at ${width}`)
      const a = await select.boundingBox(), b = await widget.locator('.u-uptime-figure').boundingBox()
      assert.ok(a.x + a.width <= b.x + 1 || a.y + a.height <= b.y + 1, 'range control and metric must not overlap')
      const labels = await widget.locator('.u-uptime-axis span').evaluateAll(nodes => nodes.map(node => {
        const r = node.getBoundingClientRect(); return { x: r.x, right: r.right, height: r.height }
      }))
      assert.ok(labels.every(label => label.height < 20), 'date ticks must stay on one line')
      assert.ok(labels.every((label, i) => i === 0 || labels[i - 1].right <= label.x), 'date ticks must not overlap')
      await widget.screenshot({ path: path.join(output, `history-${width}.png`), animations: 'disabled' })
    }

    mode = 'unknown'
    await select.selectOption('604800'); await ready()
    assert.equal(await figure.innerText(), '—')
    assert.equal(await widget.locator('.u-uptime-seg.is-unknown').count(), 1)
    mode = 'error'
    await select.selectOption('21600'); await ready()
    await widget.getByRole('alert').waitFor()
    assert.equal(await figure.innerText(), '—')
    assert.equal(await select.isEnabled(), true)
    mode = 'normal'
    await widget.getByRole('button', { name: '重试', exact: true }).click(); await ready()
    assert.equal(await figure.innerText(), '60.0%')
    await page.clock.install()
    const before = requests.length
    mode = 'error'
    await page.clock.fastForward(31000)
    await widget.getByText('刷新失败，当前显示上次成功读取的数据。', { exact: true }).waitFor()
    assert.ok(requests.length > before)
    assert.equal(requests.at(-1).span, 21600)
    assert.equal(await figure.innerText(), '60.0%', 'failed refresh keeps labeled same-range data')
    mode = 'normal'
    await page.goto(base + '/#/overview')
    await page.waitForFunction(() => document.querySelectorAll('.u-uptime.compact .u-uptime-body[aria-busy="false"]').length === 2)
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 1100 })
      await page.mouse.move(0, 0)
      await page.keyboard.press('Escape')
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `compact overflow at ${width}`)
      await page.locator('.u-uptime.compact').first().screenshot({ path: path.join(output, `compact-history-${width}.png`), animations: 'disabled' })
    }
    // Real clipped card ancestors, two neighboring timelines, long diagnostics and viewport
    // edges. Checking hit targets proves the tooltip paints above the neighboring card.
    mode = 'edges'
    const edgePage = await browser.newPage({ viewport: { width: 1440, height: 1100 }, timezoneId: 'Asia/Shanghai' })
    edgePage.on('pageerror', error => errors.push(error.message))
    await edgePage.goto(base + '/#/overview')
    await edgePage.waitForFunction(() => document.querySelectorAll('.u-uptime.compact .u-uptime-body[aria-busy="false"]').length === 2)
    const tip = edgePage.getByRole('tooltip')
    const assertTip = async () => {
      await tip.waitFor({ state: 'visible' })
      const layout = await tip.evaluate(node => {
        const r = node.getBoundingClientRect()
        const points = [[r.left + 12, r.top + 12], [r.right - 12, r.top + 12], [r.right - 12, r.bottom - 12]]
        return { left: r.left, right: r.right, top: r.top, bottom: r.bottom,
          width: innerWidth, height: innerHeight, portal: node.parentElement === document.body,
          overflow: node.scrollWidth > node.clientWidth,
          visible: points.every(([x, y]) => node.contains(document.elementFromPoint(x, y))) }
      })
      assert.ok(layout.portal && layout.visible, 'tooltip must escape card clipping and paint over adjacent cards')
      assert.ok(layout.left >= 11 && layout.right <= layout.width - 11 && layout.top >= 11 && layout.bottom <= layout.height - 11,
        'tooltip must fit inside the viewport')
      assert.equal(layout.overflow, false, 'long tooltip diagnostics must wrap')
      return layout
    }
    for (const width of [1440, 900, 390]) {
      await edgePage.setViewportSize({ width, height: 1100 })
      const cards = edgePage.locator('.u-uptime.compact')
      const firstCard = cards.first()
      for (const [card, side] of [[firstCard, 'first'], [firstCard, 'last'], [cards.last(), 'last']]) {
        await edgePage.keyboard.press('Escape')
        await card.scrollIntoViewIfNeeded()
        const segment = card.locator('.u-uptime-seg')[side]()
        await segment.hover()
        const layout = await assertTip()
        if (width === 1440 && card === firstCard && side === 'last') {
          const boundary = await card.locator('xpath=ancestor::*[contains(@class,"u-device-card")]').boundingBox()
          assert.ok(layout.right > boundary.x + boundary.width, 'fixture must exercise painting outside the first card')
          await edgePage.screenshot({ path: path.join(output, 'tooltip-between-cards.png'), animations: 'disabled' })
        }
        await tip.hover()
        await edgePage.waitForTimeout(200)
        await assertTip()
      }
      await edgePage.screenshot({ path: path.join(output, `tooltip-edge-${width}.png`), animations: 'disabled' })
      await edgePage.keyboard.press('Escape')
      await tip.waitFor({ state: 'hidden' })
      assert.equal(await edgePage.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
    }
    await edgePage.setViewportSize({ width: 1440, height: 1100 })
    const first = edgePage.locator('.u-uptime.compact').first()
    const lastSegment = first.locator('.u-uptime-seg').last()
    await first.scrollIntoViewIfNeeded()
    await lastSegment.focus()
    await assertTip()
    assert.ok(await lastSegment.getAttribute('aria-describedby'))
    await edgePage.keyboard.press('Escape')
    await tip.waitFor({ state: 'hidden' })
    await lastSegment.press('Enter')
    await assertTip()
    await edgePage.locator('.u-uptime-title h4').first().click()
    await tip.waitFor({ state: 'hidden' })
    await lastSegment.hover()
    await assertTip()
    await first.getByRole('combobox').selectOption('3600')
    await tip.waitFor({ state: 'hidden' })
    await edgePage.waitForFunction(() => document.querySelector('.u-uptime-body')?.getAttribute('aria-busy') === 'false')
    await lastSegment.hover()
    await assertTip()
    await edgePage.setViewportSize({ width: 900, height: 700 })
    await tip.waitFor({ state: 'hidden' })
    await first.scrollIntoViewIfNeeded()
    await lastSegment.hover()
    await assertTip()
    const moved = await edgePage.locator('.u-content').evaluate(node => {
      const before = node.scrollTop
      node.scrollTop += before > 25 ? -25 : 25
      return node.scrollTop !== before
    })
    assert.ok(moved, 'scroll dismissal must exercise an actual scroll')
    await tip.waitFor({ state: 'hidden' })

    // A top-edge anchor must place the detail below it; themes also apply to the portal.
    await edgePage.setViewportSize({ width: 390, height: 700 })
    await first.locator('.u-uptime-track').evaluate(node => {
      const scroller = node.closest('.u-content')
      scroller.scrollTop += node.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 12
    })
    await edgePage.mouse.move(0, 0)
    await lastSegment.hover()
    const flipped = await assertTip(), anchor = await lastSegment.boundingBox()
    assert.ok(flipped.top >= anchor.y + anchor.height, 'tooltip flips below a track near the top of the screen')
    await edgePage.evaluate(() => document.documentElement.dataset.theme = 'dark')
    assert.equal(await tip.evaluate(node => getComputedStyle(node).backgroundColor), 'rgb(19, 26, 43)')
    await edgePage.screenshot({ path: path.join(output, 'tooltip-dark-flipped.png'), animations: 'disabled' })
    await edgePage.close()
    const touchPage = await browser.newPage({ viewport: { width: 390, height: 700 }, hasTouch: true })
    touchPage.on('pageerror', error => errors.push(error.message))
    await touchPage.goto(base + '/#/overview')
    const touchCard = touchPage.locator('.u-uptime.compact').first()
    await touchCard.locator('.u-uptime-seg').last().tap()
    await touchPage.getByRole('tooltip').waitFor({ state: 'visible' })
    await touchCard.locator('h4').tap()
    await touchPage.getByRole('tooltip').waitFor({ state: 'hidden' })
    await touchPage.close()
    assert.deepEqual(writes, [], 'range selection must not change device settings or restart lines')
    assert.deepEqual(errors, [])
    console.log('PASS: 12 presets, statistics, stale replies, persistence, loading/retry, periodic refresh, 1440/900/390px, unclipped edge tooltips, long diagnostics, hover/keyboard/touch, escape/outside/scroll/resize dismissal, top-edge flip, dark theme, no configuration writes')
  } finally {
    held.splice(0).forEach(release => release())
    if (browser) await browser.close()
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
