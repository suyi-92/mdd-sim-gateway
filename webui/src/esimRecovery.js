import { boundedRead } from './pollRequest.js'

// Starting a container does not prove its PIN reader or IMS registration recovered.
export async function waitForEsimLine(api, id, timeoutMs = 90000) {
  const deadline = performance.now() + timeoutMs
  while (performance.now() < deadline) {
    try {
      const status = await boundedRead(signal => api.status(id, signal),
        Math.min(5000, deadline - performance.now()))
      if (status?.state === 'OK') return true
    } catch { /* Transient status requests may fail while the line is starting. */ }
    const remaining = deadline - performance.now()
    if (remaining <= 0) break
    await new Promise(resolve => setTimeout(resolve, Math.min(1000, remaining)))
  }
  return false
}
