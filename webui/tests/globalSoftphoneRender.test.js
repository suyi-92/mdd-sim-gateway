import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToString } from 'react-dom/server'
import { createServer } from 'vite'

const WEBUI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('authenticated shell can execute the global softphone first render', async t => {
  const server = await createServer({
    root: WEBUI_ROOT,
    appType: 'custom',
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
  })
  t.after(() => server.close())
  const module = await server.ssrLoadModule('/src/GlobalSoftphone.jsx')
  const output = renderToString(React.createElement(module.default, { instances: [] }))
  assert.equal(output, '')
})
