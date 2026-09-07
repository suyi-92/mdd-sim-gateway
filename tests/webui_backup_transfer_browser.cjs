// Uses local assets and fictional API data; never contacts a running gateway.
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
const { chromium } = require('playwright')
const dist = path.resolve(__dirname, '../webui/dist')
const output = process.env.MDD_BACKUP_UI_OUTPUT || '/tmp/mdd-recovery-browser/results'
fs.mkdirSync(output, { recursive: true })
const backups = [
  { name: `mdd-data-${'fixture-'.repeat(15)}.tar.gz`, size_bytes: 1048576, created_at: 1788825600, kind: 'manual' },
  { name: 'pre-update-fixture.tar.gz', size_bytes: 2048, created_at: 1788825600, kind: 'pre-update' },
]
let exportFails = false
let importCount = 0
const restores = []
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://localhost')
  if (url.pathname.startsWith('/api/')) {
    let body = ''
    for await (const chunk of request) body += chunk.toString()
    const json = (status, value) => {
      response.writeHead(status, { 'Content-Type': 'application/json' })
      response.end(JSON.stringify(value))
    }
    if (url.pathname.endsWith('/export')) {
      await new Promise(resolve => setTimeout(resolve, 250))
      if (exportFails) return json(400, { detail: 'backup.transfer.invalid' })
      response.writeHead(200, { 'Content-Type': 'application/octet-stream' })
      response.end('fictional migration package'); return
    }
    if (url.pathname === '/api/system/backups/import') {
      assert.equal(request.headers['x-mdd-csrf-token'], 'fixture-only')
      assert.equal(request.headers['content-type'], 'application/octet-stream')
      await new Promise(resolve => setTimeout(resolve, 250))
      importCount++
      return json(200, { ok: true, backup_name: 'imported-fixture.tar.gz' })
    }
    if (url.pathname.endsWith('/restore')) {
      restores.push(JSON.parse(body))
      return json(200, { state: 'success', action: 'restore', operation_id: 'fixture' })
    }
    const values = {
      '/api/auth/status': { configured: true, authenticated: true, csrf: 'fixture-only' },
      '/api/devices': { devices: [], discovering: false },
      '/api/instances': { instances: [] }, '/api/cards': { cards: [] },
      '/api/system/status': { version: 'fixture', repository_url: 'https://example.invalid/repo' },
      '/api/system/backups': { backups, operation: { state: 'idle' } },
    }
    return json(200, values[url.pathname] || {})
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
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(`http://127.0.0.1:${server.address().port}/#/settings`)
    await page.getByRole('button', { name: '备份与更新', exact: true }).click()
    await page.locator('.u-backup-row').first().waitFor()
    const bounds = async locator => {
      const rect = await locator.boundingBox()
      return [rect.x, rect.y, rect.width, rect.height].map(Math.round)
    }
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 1100 })
      const rows = page.locator('.u-backup-row')
      const first = rows.nth(0)
      const second = rows.nth(1)
      const firstExport = first.getByRole('button', { name: '导出', exact: true })
      const firstRestore = first.getByRole('button', { name: '恢复', exact: true })
      await firstExport.scrollIntoViewIfNeeded()
      const beforeExport = await bounds(firstExport)
      const beforeRestore = await bounds(firstRestore)
      exportFails = false
      const download = page.waitForEvent('download')
      await firstExport.click()
      await first.getByText('正在准备下载…', { exact: true }).waitFor()
      assert.equal(await firstExport.isDisabled(), true)
      assert.equal(await second.getByRole('button', { name: '导出', exact: true }).isEnabled(), true)
      assert.deepEqual(await bounds(firstRestore), beforeRestore)
      assert.ok((await download).suggestedFilename().endsWith('.mddbackup'))
      await first.getByText('已开始下载', { exact: true }).waitFor()
      assert.deepEqual(await bounds(firstExport), beforeExport)
      assert.deepEqual(await bounds(firstRestore), beforeRestore)
      exportFails = true
      await second.getByRole('button', { name: '导出', exact: true }).click()
      await second.getByText('备份包无效、损坏、权限不安全或超出校验限制。', { exact: true }).waitFor()
      const input = page.locator('.u-backup-import input')
      await input.setInputFiles({ name: 'fixture.mddbackup', mimeType: 'application/octet-stream', buffer: Buffer.from('fixture') })
      const upload = page.getByRole('button', { name: '导入备份', exact: true })
      await upload.scrollIntoViewIfNeeded()
      const beforeUpload = await bounds(upload)
      await upload.click()
      await page.getByText('正在上传并校验…', { exact: true }).waitFor()
      assert.deepEqual(await bounds(upload), beforeUpload)
      await page.getByText('已导入，点击下方“恢复”后才会生效。', { exact: true }).waitFor()
      assert.deepEqual(await bounds(upload), beforeUpload)
      assert.equal(restores.length, 0, 'import must not submit a restore')
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        `horizontal overflow at ${width}px`)
      await page.screenshot({ path: path.join(output, `backup-transfer-${width}.png`), fullPage: true })
    }
    assert.equal(importCount, 3)
    const dialog = item => item.accept(item.type() === 'prompt' ? 'RESTORE' : undefined)
    page.on('dialog', dialog)
    await page.locator('.u-backup-row').first().getByRole('button', { name: '恢复', exact: true }).click()
    await page.waitForTimeout(200)
    assert.deepEqual(restores, [{ confirm: 'RESTORE' }])
    assert.deepEqual(errors, [])
    console.log('PASS: 1440/900/390px, per-record export feedback, download, import, fixed controls and explicit restore')
  } finally {
    if (browser) await browser.close()
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
