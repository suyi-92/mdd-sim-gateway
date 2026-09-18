// Each read publishes its own result immediately. Requests made during a batch
// share one trailing batch, so a WebSocket event is not lost. Each caller waits
// only for the batch covering its request, even while more events keep arriving.
export function createRefreshCoordinator(sources, onSettled, onComplete) {
  let active = null
  let trailing = null
  const completion = () => {
    let resolve, reject
    const promise = new Promise((done, fail) => { resolve = done; reject = fail })
    return { promise, resolve, reject }
  }

  async function run(batch) {
    active = batch
    let failure
    try {
      const results = {}
      const observers = await Promise.allSettled(Object.entries(sources).map(async ([key, read]) => {
        const result = await Promise.resolve().then(read).then(
          value => ({ status: 'fulfilled', value }),
          reason => ({ status: 'rejected', reason }),
        )
        results[key] = result
        onSettled(key, result, results)
      }))
      const failedObserver = observers.find(result => result.status === 'rejected')
      if (failedObserver) throw failedObserver.reason
      await onComplete?.(results)
    } catch (error) {
      failure = error
    }
    active = null
    const next = trailing
    trailing = null
    if (next) void run(next)
    if (failure) batch.reject(failure)
    else batch.resolve()
  }

  return function refresh() {
    if (active) {
      if (!trailing) trailing = completion()
      return trailing.promise
    }
    const batch = completion()
    void run(batch)
    return batch.promise
  }
}
