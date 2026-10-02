// Local source/browser fixtures only: no SIP endpoint, microphone or production API.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '..')
const output = process.env.MDD_UI_TEST_OUTPUT || '/tmp/mdd-softphone-merge-ui'
fs.mkdirSync(output, { recursive: true })
const mockPhone = `
export const MEDIA_FAIL_CAUSE = 'User Denied Media Access'
export const RELAY_UNAVAILABLE = 'Relay unavailable'
export const RELAY_UNREACHABLE = 'Relay unreachable'
export const audioInputPresence = async () => 'none'
export const microphoneMessage = () => 'No microphone was found. Calls can still be placed and you will hear the other side, but they will not hear you.'
window.fixturePhones = []
export class Softphone {
  constructor(onEvent) { this.onEvent = onEvent; this.tones = []; this.stopped = false; window.fixturePhones.push(this) }
  start(prov) { this.id = prov.id; queueMicrotask(() => this.emit('registered', true)); return true }
  emit(type, data) { this.onEvent(type, data) }
  setAudioEl() {}
  unlockAudio() {}
  call(number) { this.emit('calling', { to: number }); this.emit('mediafallback', 'NotFoundError'); this.emit('active') }
  answer() { this.emit('mediafallback', 'NotFoundError'); this.emit('active') }
  stop() { this.stopped = true }
  hangup() { this.emit('ended', { cause: 'Normal' }) }
  reject() { this.emit('ended', { cause: 'Rejected' }) }
  rejectBusy() { this.busy = true }
  sendDTMF(tone) { this.tones.push(tone) }
  setMuted() {}
  stopLiveTranslation() { this.translating = false }
  async startLiveTranslation(secret, onEvent) { this.translating = true; onEvent({type: 'status', status: 'active'}); onEvent({type: 'source', delta: 'Fixture original'}); onEvent({type: 'target', delta: 'Fixture translated'}) }
}
`

;(async () => {
  const { createServer } = await import(path.join(root, 'webui/node_modules/vite/dist/node/index.js'))
  const server = await createServer({ root: path.join(root, 'webui'), optimizeDeps: { include: ['react', 'react-dom/client', 'jssip', 'jsqr'] }, server: { port: 0, host: '127.0.0.1' } })
  let browser
  try {
    await server.listen()
    const origin = server.resolvedUrls.local[0].replace(/\/$/, '')
    browser = await chromium.launch({ headless: true, executablePath: process.env.MDD_BROWSER_EXECUTABLE || undefined })
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.addInitScript(() => {
      localStorage.setItem('mdd-language', 'en')
      window.WebSocket = class extends EventTarget { static OPEN = 1; constructor() { super(); this.readyState = 1 }; close() {}; send() {} }
    })
    const lines = ['1','2'].map(id => ({ id, name: `Fixture line ${id}`, enabled: true, status: { state: 'OK' } }))
    const devices = lines.map(line => ({ id: `reader-${line.id}`, instance_id: line.id, name: line.name,
      device_type: 'reader', present: true, sim: { present: true }, capabilities: { vowifi: { desired: true, actual: 'on' } } }))
    let contactName = 'Fixture Contact'
    await context.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (!url.href.startsWith(origin + '/')) return route.abort()
      if (url.pathname === '/src/softphone.js') return route.fulfill({ contentType: 'text/javascript', body: mockPhone })
      if (!url.pathname.startsWith('/api/')) return route.continue()
      let result = {}
      const body = route.request().postDataJSON()
      if (url.pathname === '/api/auth/status') result = { configured: true, authenticated: true, csrf: 'fixture-only' }
      else if (url.pathname === '/api/instances') result = { instances: lines }
      else if (url.pathname === '/api/devices') result = { devices, discovering: false }
      else if (url.pathname === '/api/cards') result = { cards: lines.map((line,i) => ({name:`Fixture reader ${i}`,matched:line.id,present:true})) }
      else if (/\/softphone$/.test(url.pathname)) result = { enabled: true, id: url.pathname.split('/')[3], host:'fixture.invalid' }
      else if (/\/calls$/.test(url.pathname)) result = { calls: [] }
      else if (/\/voicemails$/.test(url.pathname)) result = { voicemails: [] }
      else if (url.pathname === '/api/contacts/resolve') result = { contacts: Object.fromEntries((body.numbers || []).map(n => [n, {name:contactName}])) }
      else if (url.pathname === '/api/contacts' || url.pathname === '/api/contacts/fixture') {
        if (route.request().method() === 'PUT') contactName = body.name
        const contact = { id:'fixture',name:contactName,numbers:[{number:'+12025550123',label:'work'}] }
        result = { total:1,contacts:[contact],contact }
      }
      else if (/live-translation/.test(url.pathname)) result = url.pathname.endsWith('/session') ? {value:'fixture'} : { enabled:true,configured:true }
      await route.fulfill({ contentType:'application/json',body:JSON.stringify(result) })
    })
    const page = await context.newPage(), errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(origin + '/#/overview')
    await page.waitForFunction(() => window.fixturePhones?.some(p => p.id === '2' && !p.stopped)).catch(async error => { console.error({ errors, text: await page.locator('body').innerText(), phones: await page.evaluate(() => window.fixturePhones?.map(p => ({id:p.id,stopped:p.stopped}))) }); throw error })
    await page.evaluate(() => window.fixturePhones.find(p => p.id === '2' && !p.stopped).emit('incoming', {from:'+12025550123'}))
    const surface = page.locator('.u-call-surface:visible')
    await surface.getByText('Fixture Contact', {exact:true}).waitFor()
    assert.equal(await surface.getAttribute('class').then(value => value.includes('is-embedded')), true)
    assert.equal(await page.locator('[aria-modal=true]').count(), 0, 'incoming controls must not block navigation')
    await surface.getByRole('button', {name:'Answer',exact:true}).click()
    await surface.getByText('Listen only · the other side cannot hear you', {exact:true}).waitFor()
    assert.equal(await surface.getByRole('button', {name:'No mic',exact:true}).isDisabled(), true)
    await surface.getByRole('button', {name:'1',exact:true}).click()
    assert.deepEqual(await page.evaluate(() => window.fixturePhones.find(p => p.id === '2' && !p.stopped).tones), ['1'])
    await surface.getByRole('button', {name:'Keypad',exact:true}).click()
    const navigate = async view => { await page.evaluate(value => { location.hash = '#/' + value }, view) }
    await navigate('contacts')
    await page.getByRole('heading', {name:'Contacts',exact:true}).first().waitFor()
    await page.getByRole('button', {name:'Edit',exact:true}).click()
    await page.locator('form.u-panel input').first().fill('Updated Fixture Contact')
    await page.locator('form.u-panel').getByRole('button', {name:'Save',exact:true}).click()
    await surface.getByText('Updated Fixture Contact', {exact:true}).waitFor()
    assert.equal(await surface.getAttribute('class').then(value => value.includes('is-floating')), true)
    await surface.getByRole('button', {name:'Subtitles',exact:true}).click()
    await surface.getByText('Fixture translated',{exact:true}).waitFor()
    await surface.getByRole('button', {name:'Stop subtitles',exact:true}).click()
    for (const width of [1440,900,390]) {
      await page.setViewportSize({width,height:1000})
      await surface.getByText('Listen only · the other side cannot hear you',{exact:true}).waitFor()
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `page overflow at ${width}`)
      const rect = await surface.boundingBox()
      assert.ok(rect.x >= 0 && rect.x + rect.width <= width + 1, `call dock outside viewport at ${width}`)
      await surface.screenshot({path:path.join(output,`call-dock-${width}.png`),animations:'disabled'})
    }
    await surface.getByRole('button', {name:'Hangup',exact:true}).click()
    await surface.getByText('Call ended',{exact:true}).first().waitFor()
    assert.deepEqual(errors, [])
    console.log('PASS: global incoming ownership, persistent cross-page call, contact refresh after edit, silent-call warning, DTMF, subtitle start/stop, 1440/900/390px; fixture APIs/phone only')
  } finally {
    if (browser) await browser.close()
    await server.close()
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
