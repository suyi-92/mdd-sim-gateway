// A retained list is presentation only. Its write controls stay blocked until a
// fresh same-card cache/read is available. Physical replacement clears the view.
export function canRetainEsimView(owner, reader, card, devices) {
  if (!owner || owner.reader !== reader || !owner.hardwareId || !owner.hardwareGeneration) return false
  if (card?.hardware_id && card.hardware_id !== owner.hardwareId) return false
  const device = devices.find(item => item.id === owner.hardwareId)
  if (!device || device.present === false || device.hardware_generation !== owner.hardwareGeneration) return false
  return !card?.iccid || owner.profileIds.includes(card.iccid)
}

export function esimText(value, fallback = '') {
  return typeof value === 'string' ? value : fallback
}
