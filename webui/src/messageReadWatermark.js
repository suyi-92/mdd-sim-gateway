// Only rendered rows intersecting the actual message viewport are evidence of reading.
export function displayedMessageWatermark(list) {
  if (!list || !list.getClientRects().length) return 0
  const bounds = list.getBoundingClientRect()
  if (bounds.width <= 0 || bounds.height <= 0) return 0
  let watermark = 0
  for (const row of list.querySelectorAll('[data-message-id]')) {
    const rect = row.getBoundingClientRect()
    const id = Number(row.dataset.messageId)
    if (Number.isSafeInteger(id) && id > watermark && rect.width > 0 && rect.height > 0
        && rect.bottom > bounds.top && rect.top < bounds.bottom) watermark = id
  }
  return watermark
}
