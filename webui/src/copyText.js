export async function copyExactText(value, navigatorRef = globalThis.navigator, documentRef = globalThis.document) {
  const text = String(value ?? '')
  if (!text) return false

  try {
    if (navigatorRef?.clipboard?.writeText) {
      await navigatorRef.clipboard.writeText(text)
      return true
    }
  } catch {
    // Some browsers expose Clipboard but deny it outside a trusted gesture. Fall back to
    // a temporary selection without changing or normalising the subscriber number.
  }

  if (!documentRef?.body || typeof documentRef.execCommand !== 'function') return false
  const field = documentRef.createElement('textarea')
  field.value = text
  field.setAttribute('readonly', '')
  field.style.position = 'fixed'
  field.style.opacity = '0'
  documentRef.body.appendChild(field)
  field.select()
  try {
    return Boolean(documentRef.execCommand('copy'))
  } catch {
    return false
  } finally {
    field.remove()
  }
}
