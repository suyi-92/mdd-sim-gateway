// Release one remote-audio sink without breaking ownership boundaries.
//
// React owns the persistent <audio> element rendered by Softphone.jsx. Removing that node from
// inside the imperative JsSIP wrapper leaves the ref pointing at a detached element when the
// user switches lines, so a later call can receive a MediaStream without producing page audio.
// Only the fallback element created by softphone.js itself may be removed here.
export function releaseAudioSink(element, ownedByPhone = false) {
  if (!element) return
  try { element.pause() } catch {}
  try { element.srcObject = null } catch {}
  if (ownedByPhone) {
    try { element.remove() } catch {}
  }
}
