// Retained hardware records restore settings on reconnect; presence controls live views.
export const physicallyPresentDevices = (devices = []) => devices.filter(
  (device) => device?.present !== false,
)

export function deviceSelection(devices = [], selectedDeviceId = null, showDisconnected = false) {
  const connectedDevices = physicallyPresentDevices(devices)
  const visibleDevices = showDisconnected ? devices : connectedDevices
  const activeDeviceId = visibleDevices.some(device => device.id === selectedDeviceId)
    ? selectedDeviceId : (visibleDevices[0]?.id ?? null)
  return {
    visibleDevices,
    disconnectedCount: devices.length - connectedDevices.length,
    activeDeviceId,
    device: visibleDevices.find(device => device.id === activeDeviceId),
  }
}
