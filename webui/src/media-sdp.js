// Keep browser RTP off proxy adapters that expose Clash/Mihomo's benchmark Fake-IP range.
//
// Chrome gathers one host candidate per local adapter and uses the highest-priority candidate
// for the SDP m=/c= defaults.  Mihomo's 198.18.0.1 adapter can win that election even though an
// Asterisk container cannot route media to it.  The control plane publishes the real host media
// address, which lets us choose the corresponding browser socket without hard-coding an adapter.

const CANDIDATE = /^a=candidate:(\S+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(\S+)\s+(\d+)\s+typ\s+(\S+)(?:\s|$)/i

function bareAddress(value) {
  return String(value || '').trim().replace(/^\[|\]$/g, '').toLowerCase()
}

function ipv4Parts(value) {
  const parts = bareAddress(value).split('.')
  if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part))) return null
  const numbers = parts.map(Number)
  return numbers.every((part) => part >= 0 && part <= 255) ? numbers : null
}

function isNumericAddress(value) {
  return Boolean(ipv4Parts(value)) || bareAddress(value).includes(':')
}

export function isProxyFakeIp(value) {
  const parts = ipv4Parts(value)
  return Boolean(parts && parts[0] === 198 && (parts[1] === 18 || parts[1] === 19))
}

function parseCandidate(line, index) {
  const match = CANDIDATE.exec(line)
  if (!match) return null
  return {
    index,
    component: Number(match[2]),
    transport: match[3].toLowerCase(),
    priority: Number(match[4]) || 0,
    address: bareAddress(match[5]),
    port: Number(match[6]),
    type: match[7].toLowerCase(),
  }
}

function isPrivateV4(value) {
  const p = ipv4Parts(value)
  if (!p) return false
  return p[0] === 10 || (p[0] === 172 && p[1] >= 16 && p[1] <= 31) ||
    (p[0] === 192 && p[1] === 168) || (p[0] === 169 && p[1] === 254)
}

function sameV4Lan(left, right) {
  const a = ipv4Parts(left)
  const b = ipv4Parts(right)
  return Boolean(a && b && a[0] === b[0] && a[1] === b[1] && a[2] === b[2])
}

function candidateRank(candidate, mediaHost) {
  if (candidate.component !== 1 || candidate.transport !== 'udp' ||
      !candidate.port || !isNumericAddress(candidate.address) || isProxyFakeIp(candidate.address)) {
    return [-1, -1, -1]
  }
  const host = bareAddress(mediaHost)
  const addressRank = host && candidate.address === host ? 4
    : host && sameV4Lan(candidate.address, host) ? 3
      : isPrivateV4(candidate.address) ? 2
        : ipv4Parts(candidate.address) ? 1 : 0
  const typeRank = candidate.type === 'host' ? 3
    : candidate.type === 'srflx' ? 2
      : candidate.type === 'relay' ? 1 : 0
  return [addressRank, typeRank, candidate.priority]
}

function betterCandidate(left, right, mediaHost) {
  const a = candidateRank(left, mediaHost)
  const b = candidateRank(right, mediaHost)
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) return a[i] > b[i]
  }
  return false
}

function rewriteAudioSection(section, mediaHost) {
  const candidates = section.map(parseCandidate).filter(Boolean)
  const connectionIndex = section.findIndex((line) => /^c=IN\s+IP(?:4|6)\s+/i.test(line))
  const connectionAddress = connectionIndex >= 0
    ? bareAddress(section[connectionIndex].trim().split(/\s+/).at(-1)) : ''
  const hasFakeRoute = isProxyFakeIp(connectionAddress) ||
    candidates.some((candidate) => isProxyFakeIp(candidate.address))

  // Leave ordinary browser SDP byte-for-byte unchanged.  This workaround is intentionally
  // scoped to a section contaminated by the known proxy-only range.
  if (!hasFakeRoute) return section

  const usable = candidates.filter((candidate) => candidateRank(candidate, mediaHost)[0] >= 0)
  if (!usable.length) return section
  let selected = usable[0]
  for (const candidate of usable.slice(1)) {
    if (betterCandidate(candidate, selected, mediaHost)) selected = candidate
  }

  let rewritten = section.filter((line) => {
    const candidate = parseCandidate(line, 0)
    return !candidate || !isProxyFakeIp(candidate.address)
  })
  const media = rewritten[0].trim().split(/\s+/)
  if (media.length >= 2) {
    media[1] = String(selected.port)
    rewritten[0] = media.join(' ')
  }
  const family = selected.address.includes(':') ? 'IP6' : 'IP4'
  const connection = `c=IN ${family} ${selected.address}`
  const rewrittenConnectionIndex = rewritten.findIndex((line) => /^c=IN\s+IP(?:4|6)\s+/i.test(line))
  if (rewrittenConnectionIndex >= 0) rewritten[rewrittenConnectionIndex] = connection
  else rewritten.splice(1, 0, connection)
  return rewritten
}

export function rewriteLocalSdpForMediaHost(sdp, mediaHost) {
  if (!sdp) return sdp
  const source = String(sdp)
  const eol = source.includes('\r\n') ? '\r\n' : '\n'
  const lines = source.split(/\r\n|\n|\r/)
  const output = []
  let index = 0
  while (index < lines.length) {
    if (!/^m=audio(?:\s|$)/i.test(lines[index])) {
      output.push(lines[index])
      index += 1
      continue
    }
    let end = index + 1
    while (end < lines.length && !/^m=/i.test(lines[end])) end += 1
    output.push(...rewriteAudioSection(lines.slice(index, end), mediaHost))
    index = end
  }
  return output.join(eol)
}
