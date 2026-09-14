import assert from 'node:assert/strict'
import test from 'node:test'

import { defaultDeviceName, deviceTitle } from '../webui/src/deviceNames.js'

const t = (value) => ({
  '3T Electronics SCR Prime reader': '三体电子 SCR Prime 读卡器',
  'Smart-card reader': '智能读卡器',
  'Cellular modem': '蜂窝通信模块',
  Device: '设备',
}[value] || value)

test('all SCR Prime reader variants share one default display name', () => {
  const first = { name: 'SCR Prime CCID Reader (fixture-a) 00 00' }
  const second = { name: 'SCR Prime CCID Reader (fixture-b) 01 00' }
  assert.equal(deviceTitle(first, 0, t), '三体电子 SCR Prime 读卡器')
  assert.equal(deviceTitle(second, 1, t), '三体电子 SCR Prime 读卡器')
})

test('a user display name overrides the default in every consumer', () => {
  const device = {
    name: '3T Electronics SCR Prime reader',
    default_name: '3T Electronics SCR Prime reader',
    display_name: '办公桌 eSIM',
  }
  assert.equal(defaultDeviceName(device, 0, t), '三体电子 SCR Prime 读卡器')
  assert.equal(deviceTitle(device, 0, t), '办公桌 eSIM')
})

test('modem profile names remain stable defaults', () => {
  assert.equal(deviceTitle({ default_name: 'DJI/Quectel EC25' }, 0, t),
    'DJI/Quectel EC25')
  assert.equal(deviceTitle({ name: 'VoWiFi Modem fixture 00 00' }, 0, t),
    '蜂窝通信模块')
})
