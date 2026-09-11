export const HISTORY_SPANS = [900, 1800, 3600, 10800, 21600, 43200, 86400, 172800,
  259200, 604800, 1209600, 2592000]
export const DEFAULT_HISTORY_SPAN = 86400
const STORAGE_KEY = 'mdd.connection-history.span-seconds'

export function savedHistorySpan() {
  try {
    const value = Number(localStorage.getItem(STORAGE_KEY))
    return HISTORY_SPANS.includes(value) ? value : DEFAULT_HISTORY_SPAN
  } catch { return DEFAULT_HISTORY_SPAN }
}

export function saveHistorySpan(value) {
  if (!HISTORY_SPANS.includes(value)) return
  try { localStorage.setItem(STORAGE_KEY, String(value)) } catch { /* Selection still works. */ }
}
