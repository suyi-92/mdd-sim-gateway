import test from 'node:test'
import assert from 'node:assert/strict'
import { formatPhoneNumberDisplay } from '../src/phoneNumberDisplay.js'

test('international phone displays separate the country calling code with one space', () => {
  assert.equal(formatPhoneNumberDisplay('+385000000'), '+385 000000')
  assert.equal(formatPhoneNumberDisplay('+447700900357'), '+44 7700900357')
  assert.equal(formatPhoneNumberDisplay('+15550000000'), '+1 5550000000')
  assert.equal(formatPhoneNumberDisplay('+860000000000'), '+86 0000000000')
})

test('existing whitespace is normalised only for valid international phone numbers', () => {
  assert.equal(formatPhoneNumberDisplay('+44 7700 900357'), '+44 7700900357')
  assert.equal(formatPhoneNumberDisplay('+385\u00a0000 000'), '+385 000000')
})

test('local numbers, short codes, USSD and non-phone values remain exact', () => {
  for (const value of ['10086', '*#21#', '+1555**7654#', '+999000000', 'Unknown', '']) {
    assert.equal(formatPhoneNumberDisplay(value), value)
  }
})
