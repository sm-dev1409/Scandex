import { describe, expect, it } from 'vitest'

import {
  breakdown,
  fmt,
  isStalePending,
  partyLabel,
  relativeTime,
  shortParty,
  summarise,
} from './format.js'

const ALICE = 'alice-demo::1220alice'
const BOB = 'bob-demo::1220bob'
const CAROL = 'carol-demo::1220carol'

// `now` is always passed in, never read from the clock, so these are
// deterministic and do not need fake timers.
const NOW = Date.UTC(2026, 7, 29, 12, 0, 0)

describe('relativeTime', () => {
  it('renders seconds under a minute', () => {
    expect(relativeTime(NOW - 5_000, NOW)).toBe('5s ago')
  })

  it('renders minutes under an hour', () => {
    expect(relativeTime(NOW - 5 * 60_000, NOW)).toBe('5m ago')
  })

  it('renders hours under a day', () => {
    expect(relativeTime(NOW - 3 * 3_600_000, NOW)).toBe('3h ago')
  })

  it('falls back to an absolute date past a day', () => {
    const out = relativeTime(NOW - 3 * 86_400_000, NOW)
    expect(out).not.toMatch(/ago$/)
  })

  it('accepts an ISO string as well as epoch ms', () => {
    expect(relativeTime(new Date(NOW - 30_000).toISOString(), NOW)).toBe('30s ago')
  })

  it('never goes negative when a clock is slightly ahead', () => {
    expect(relativeTime(NOW + 5_000, NOW)).toBe('0s ago')
  })

  it('returns the input unchanged when it is not a date', () => {
    expect(relativeTime('not-a-date', NOW)).toBe('not-a-date')
  })
})

describe('summarise', () => {
  const transfers = [
    { sender: ALICE, receiver: BOB, amount: '25', instrument: 'Amulet' },
    { sender: BOB, receiver: ALICE, amount: '10', instrument: 'Amulet' },
    { sender: ALICE, receiver: CAROL, amount: '5', instrument: 'Amulet' },
    { sender: CAROL, receiver: ALICE, amount: '1', instrument: 'c8BTC' },
  ]

  it('splits sent from received relative to the selected party', () => {
    const s = summarise(transfers, ALICE)
    expect(s.outCount).toBe(2) // 25 + 5 Amulet out
    expect(s.inCount).toBe(2) // 10 Amulet + 1 c8BTC in
  })

  it('keeps amounts per instrument and never sums across them', () => {
    const s = summarise(transfers, ALICE)
    expect(s.byInstrument.get('Amulet')).toEqual({ sent: 30, received: 10 })
    expect(s.byInstrument.get('c8BTC')).toEqual({ sent: 0, received: 1 })
    // Two instruments in view => no single headline amount.
    expect(s.single).toBeNull()
    expect(s.instruments).toEqual(['Amulet', 'c8BTC'])
  })

  it('names the instrument when exactly one is in view', () => {
    const s = summarise(transfers.filter((t) => t.instrument === 'Amulet'), ALICE)
    expect(s.single).toBe('Amulet')
  })

  it('treats a non-numeric amount as zero rather than NaN', () => {
    const s = summarise([{ sender: ALICE, receiver: BOB, amount: null, instrument: 'Amulet' }], ALICE)
    expect(s.byInstrument.get('Amulet').sent).toBe(0)
  })

  it('handles an empty list', () => {
    const s = summarise([], ALICE)
    expect(s.instruments).toEqual([])
    expect(s.inCount).toBe(0)
    expect(s.outCount).toBe(0)
    expect(s.single).toBeNull()
  })

  it('counts a transfer as incoming when the party is not the sender', () => {
    const s = summarise([{ sender: BOB, receiver: CAROL, amount: '3', instrument: 'Amulet' }], ALICE)
    expect(s.inCount).toBe(1)
  })
})

describe('breakdown', () => {
  it('says so when there is nothing to break down', () => {
    expect(breakdown(summarise([], ALICE), (e) => e.sent)).toBe('No transfers')
  })

  it('joins one entry per instrument, signing positives', () => {
    const s = summarise(
      [
        { sender: BOB, receiver: ALICE, amount: '10', instrument: 'Amulet' },
        { sender: CAROL, receiver: ALICE, amount: '1', instrument: 'c8BTC' },
      ],
      ALICE,
    )
    expect(breakdown(s, (e) => e.received)).toBe('+10 Amulet · +1 c8BTC')
  })
})

describe('shortParty', () => {
  const partyMap = new Map([[ALICE, { party_id: ALICE, display_name: 'Alice' }]])

  it('prefers a known display name', () => {
    expect(shortParty(ALICE, partyMap)).toBe('Alice')
  })

  it('falls back to the hint before the "::"', () => {
    expect(shortParty(BOB, partyMap)).toBe('bob-demo')
  })

  it('renders a dash for a missing party', () => {
    expect(shortParty(null, partyMap)).toBe('—')
  })

  it('tolerates no map at all', () => {
    expect(shortParty(BOB, undefined)).toBe('bob-demo')
  })
})

describe('partyLabel', () => {
  it('prefers display_name, falling back to the raw id', () => {
    expect(partyLabel({ party_id: ALICE, display_name: 'Alice' })).toBe('Alice')
    expect(partyLabel({ party_id: ALICE })).toBe(ALICE)
  })
})

describe('fmt', () => {
  it('groups numbers and passes everything else through', () => {
    expect(fmt(1234)).toBe('1,234')
    expect(fmt('—')).toBe('—')
    expect(fmt(null)).toBeNull()
  })
})

describe('isStalePending', () => {
  const pending = (ageSecs) => ({
    update_id: 'demo-u4',
    status: 'pending',
    recorded_at: new Date(NOW - ageSecs * 1000).toISOString(),
  })

  it('is false for a settled transfer no matter how old', () => {
    expect(isStalePending({ status: 'settled', recorded_at: '2000-01-01T00:00:00Z' }, NOW)).toBe(false)
  })

  it('is false for a pending offer inside the threshold', () => {
    expect(isStalePending(pending(60), NOW, 300)).toBe(false)
  })

  it('is true for a pending offer past the threshold', () => {
    expect(isStalePending(pending(420), NOW, 300)).toBe(true)
  })

  it('trusts the server-supplied stale id set even when the age looks fine', () => {
    const ids = new Set(['demo-u4'])
    expect(isStalePending(pending(1), NOW, 300, ids)).toBe(true)
  })

  it('is false on an unparseable timestamp rather than throwing', () => {
    expect(isStalePending({ status: 'pending', recorded_at: 'nope' }, NOW, 300)).toBe(false)
  })

  it('is false for null/undefined rows', () => {
    expect(isStalePending(null, NOW)).toBe(false)
    expect(isStalePending(undefined, NOW)).toBe(false)
  })
})
