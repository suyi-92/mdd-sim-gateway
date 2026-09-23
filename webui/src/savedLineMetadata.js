const fields = ['id', 'iccid', 'name', 'msisdn', 'msisdn_source', 'number_country']

export function savedLineMetadata(value) {
  if (!value?.id || !Object.hasOwn(value, 'msisdn')) return null
  return Object.fromEntries(fields.filter(key => Object.hasOwn(value, key)).map(key => [key, value[key]]))
}

export function mergeSavedLine(instances, saved) {
  return instances.map(line => String(line.id) === String(saved.id)
    && (!line.iccid || !saved.iccid || line.iccid === saved.iccid)
    ? { ...line, ...saved } : line)
}

export function mergeSavedDevice(devices, saved) {
  return devices.map(device => String(device.instance_id || '') === String(saved.id)
    ? { ...device, sim: { ...device.sim, number: saved.msisdn,
      number_country: saved.number_country || '', name: saved.name ?? device.sim?.name } }
    : device)
}
