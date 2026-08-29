import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  API_BASE,
  fetchBalances,
  fetchParties,
  fetchStale,
  fetchTransfers,
  unwrapBalances,
  unwrapHoldings,
  unwrapParties,
  unwrapTransfers,
} from './api.js'

// ── Bug B regression tests ────────────────────────────────────────────────
//
// The dashboard used to do `Array.isArray(response) ? response : []` on the
// whole response body of /parties, /tokens/balance/{party} and
// /tokens/transfers/{party}. Those routes wrap their payload in an object, and
// an object is never an array, so the UI rendered "No parties" / "No holdings"
// / "No transfers" forever against a perfectly healthy backend.
//
// The fixtures below are copied from real responses of a running
// `serve_api.py --data-mode test` server, so if the envelope ever changes
// these fail rather than the UI silently emptying.

const PARTIES_RESPONSE = {
  parties: [
    { party_id: 'alice-demo::1220alice', display_name: 'Alice', is_local: 1 },
    { party_id: 'bob-demo::1220bob', display_name: 'Bob', is_local: 0 },
  ],
}

const BALANCE_RESPONSE = {
  party: 'alice-demo::1220alice',
  byInstrument: [
    { instrument: 'Amulet', total: 100, spendable: 80, holding_count: 3, locked_count: 1 },
    { instrument: 'c8BTC', total: 2, spendable: 2, holding_count: 1, locked_count: 0 },
  ],
}

const TRANSFERS_RESPONSE = {
  party: 'alice-demo::1220alice',
  count: 2,
  transfers: [
    { update_id: 'demo-u4', amount: '5', instrument: 'Amulet', status: 'pending' },
    { update_id: 'demo-u1', amount: '25', instrument: 'Amulet', status: 'settled' },
  ],
}

describe('response envelope unwrapping (Bug B regression)', () => {
  it('pulls the list out of the /parties envelope', () => {
    const rows = unwrapParties(PARTIES_RESPONSE)
    expect(rows).toHaveLength(2)
    expect(rows[0].party_id).toBe('alice-demo::1220alice')
  })

  it('pulls byInstrument out of the balance envelope', () => {
    const rows = unwrapBalances(BALANCE_RESPONSE)
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({ instrument: 'Amulet', total: 100, spendable: 80 })
  })

  it('pulls transfers out of the transfers envelope', () => {
    const rows = unwrapTransfers(TRANSFERS_RESPONSE)
    expect(rows).toHaveLength(2)
    expect(rows[0].update_id).toBe('demo-u4')
  })

  it('pulls holdings out of the holdings envelope', () => {
    const rows = unwrapHoldings({ party: 'x', activeOnly: true, holdings: [{ contract_id: 'h1' }] })
    expect(rows).toHaveLength(1)
  })

  // This is the exact assertion that would have caught the bug: the old code
  // path was Array.isArray(wholeResponse), which is false for every envelope.
  it('an envelope object is not itself an array (why the old check failed)', () => {
    expect(Array.isArray(PARTIES_RESPONSE)).toBe(false)
    expect(Array.isArray(BALANCE_RESPONSE)).toBe(false)
    expect(Array.isArray(TRANSFERS_RESPONSE)).toBe(false)
  })

  it('still accepts a bare array, in case a route is ever flattened', () => {
    expect(unwrapParties([{ party_id: 'a' }])).toHaveLength(1)
    expect(unwrapBalances([{ instrument: 'Amulet' }])).toHaveLength(1)
    expect(unwrapTransfers([{ update_id: 'u1' }])).toHaveLength(1)
  })

  it('degrades to an empty list rather than throwing on junk', () => {
    for (const junk of [null, undefined, {}, { error: 'unknown party' }, 42, 'nope']) {
      expect(unwrapParties(junk)).toEqual([])
      expect(unwrapBalances(junk)).toEqual([])
      expect(unwrapTransfers(junk)).toEqual([])
    }
  })
})

// ── Bug A regression: the API base ────────────────────────────────────────

describe('API_BASE', () => {
  it('defaults to the port the backend actually listens on', () => {
    // webapi.py's build_parser() defaults --port to 8787. The old hardcoded
    // http://localhost:8000 pointed at nothing, and vite.config.js has no
    // dev proxy to redirect it.
    expect(API_BASE).toBe('http://127.0.0.1:8787')
  })

  it('has no trailing slash, so `${API_BASE}/health` never doubles up', () => {
    expect(API_BASE.endsWith('/')).toBe(false)
  })
})

// ── The fetchers, with fetch stubbed ──────────────────────────────────────

function stubFetch(payload) {
  const spy = vi.fn().mockResolvedValue({ ok: true, json: async () => payload })
  vi.stubGlobal('fetch', spy)
  return spy
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchers', () => {
  it('fetchParties returns the unwrapped list', async () => {
    stubFetch(PARTIES_RESPONSE)
    await expect(fetchParties()).resolves.toHaveLength(2)
  })

  it('fetchBalances returns the unwrapped list', async () => {
    stubFetch(BALANCE_RESPONSE)
    await expect(fetchBalances('alice-demo::1220alice')).resolves.toHaveLength(2)
  })

  it('url-encodes the party id, which contains "::"', async () => {
    const spy = stubFetch(BALANCE_RESPONSE)
    await fetchBalances('alice-demo::1220alice')
    expect(spy.mock.calls[0][0]).toContain('alice-demo%3A%3A1220alice')
  })

  it('sends instrument and direction as query params when given', async () => {
    const spy = stubFetch(TRANSFERS_RESPONSE)
    await fetchTransfers('alice-demo::1220alice', {
      limit: 10,
      instrument: 'c8BTC',
      direction: 'sent',
    })
    const url = spy.mock.calls[0][0]
    expect(url).toContain('limit=10')
    expect(url).toContain('instrument=c8BTC')
    expect(url).toContain('direction=sent')
  })

  it('omits empty filters rather than sending instrument=', async () => {
    const spy = stubFetch(TRANSFERS_RESPONSE)
    await fetchTransfers('alice-demo::1220alice', { instrument: '', direction: '' })
    const url = spy.mock.calls[0][0]
    expect(url).not.toContain('instrument=')
    expect(url).not.toContain('direction=')
  })

  it('fetchStale keeps the threshold alongside the rows', async () => {
    stubFetch({ olderThanSeconds: 300, count: 1, transfers: [{ update_id: 'demo-u4' }] })
    await expect(fetchStale()).resolves.toEqual({
      olderThanSeconds: 300,
      transfers: [{ update_id: 'demo-u4' }],
    })
  })

  it('throws on a non-2xx response instead of returning junk', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }))
    await expect(fetchParties()).rejects.toThrow('HTTP 404')
  })
})
