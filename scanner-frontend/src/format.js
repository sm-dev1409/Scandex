// Pure presentation helpers, extracted from Dashboard.jsx so they can be unit
// tested without rendering a component or running a backend. Nothing in here
// reads the clock or the network: every time-dependent function takes `now` as
// an argument, which is what keeps rendering a pure function of state.

/** Human "3m ago" for an ISO string or epoch-ms number. */
export function relativeTime(value, now) {
  const then = typeof value === 'number' ? value : new Date(value).getTime()
  if (Number.isNaN(then)) return value
  const secs = Math.max(0, Math.round((now - then) / 1000))
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return new Date(then).toLocaleString()
}

export function fmt(n) {
  return typeof n === 'number' ? n.toLocaleString() : n
}

export function partyLabel(p) {
  return p.display_name || p.party_id
}

// Canton party ids are "<hint>::<fingerprint>". The fingerprint is noise in a
// table row, so show the hint (or the known display_name) and keep the full id
// on the title attribute.
export function shortParty(partyId, partyMap) {
  if (!partyId) return '—'
  const known = partyMap?.get(partyId)
  if (known?.display_name) return known.display_name
  const hint = partyId.split('::')[0]
  return hint || partyId
}

// Amounts are per-instrument, so they are summed per-instrument and never
// added across instruments — "1,200 Amulet + 40 c8ETH" is not a number.
export function summarise(transfers, party) {
  const byInstrument = new Map()
  let inCount = 0
  let outCount = 0

  for (const t of transfers) {
    const amt = Number(t.amount) || 0
    const outgoing = t.sender === party
    const entry = byInstrument.get(t.instrument) ?? { received: 0, sent: 0 }
    if (outgoing) {
      entry.sent += amt
      outCount += 1
    } else {
      entry.received += amt
      inCount += 1
    }
    byInstrument.set(t.instrument, entry)
  }

  const instruments = [...byInstrument.keys()].sort()
  return {
    byInstrument,
    instruments,
    inCount,
    outCount,
    // With exactly one instrument in view the stat cards can show a real
    // amount; with several they fall back to counts plus a per-instrument
    // breakdown underneath.
    single: instruments.length === 1 ? instruments[0] : null,
  }
}

export function breakdown(stats, pick) {
  if (stats.instruments.length === 0) return 'No transfers'
  return stats.instruments
    .map((i) => {
      const e = stats.byInstrument.get(i)
      const v = pick(e)
      return `${v > 0 ? '+' : ''}${fmt(Number(v.toFixed(4)))} ${i}`
    })
    .join(' · ')
}

/**
 * Is this transfer a pending offer that has aged past the stale threshold?
 *
 * The server owns the authoritative answer (GET /tokens/transfers/stale, which
 * applies the same rule in SQL). This mirrors it locally only so an individual
 * row in the history list can be badged without a second round trip per row —
 * `staleIds` is the set the server returned, and the age check is the fallback
 * when the row was not in that response.
 */
export function isStalePending(t, now, staleSeconds = 300, staleIds = null) {
  if (t?.status !== 'pending') return false
  if (staleIds && staleIds.has(t.update_id)) return true
  const recorded = new Date(t?.recorded_at).getTime()
  if (Number.isNaN(recorded)) return false
  return (now - recorded) / 1000 >= staleSeconds
}
