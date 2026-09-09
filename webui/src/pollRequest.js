// Only idempotent background reads use this deadline. It never retries mutations.
export async function boundedRead(run, timeoutMs = 15000) {
  const controller = new AbortController()
  let timer
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      const error = Object.assign(new Error('Request timed out'), { code: 'timeout' })
      reject(error)
      controller.abort()
    }, timeoutMs)
  })
  try { return await Promise.race([Promise.resolve().then(() => run(controller.signal)), deadline]) }
  finally { clearTimeout(timer) }
}
