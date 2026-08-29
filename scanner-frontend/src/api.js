// The single place that knows how to talk to the Scandex local JSON API.
//
// WHY THIS FILE EXISTS (two integration bugs it fixes and then prevents):
//
//  1. The API base used to be a hardcoded `http://localhost:8000` inside
//     Dashboard.jsx, while the backend (webapi.py's build_parser) defaults to
//     port 8787 and there is no dev proxy in vite.config.js. The frontend was
//     polling a port nothing listens on. The base is now configurable via
//     VITE_API_BASE and defaults to the port the backend actually uses.
//
//  2. Every list route wraps its payload in an object - `{parties: [...]}`,
//     `{byInstrument: [...]}`, `{transfers: [...]}` - but the dashboard used to
//     test `Array.isArray(response)` on the whole envelope. An object is never
//     an array, so it silently rendered "No parties"/"No holdings"/"No
//     transfers" forever, even against a fully populated backend. The unwrap
//     helpers below are the fix, kept as pure functions so they are unit
//     testable without a running server or a rendered component.
//
// The wrappers are deliberately kept on the backend rather than flattened to
// bare arrays: they carry metadata the frontend uses (`count`, `party`,
// `olderThanSeconds`) that a bare array would lose.

// import.meta.env is replaced at build time by Vite, so this is a build-time
// switch, not a runtime fetch. Set VITE_API_BASE in scanner-frontend/.env.local
// to point the same source at a test-mode or a real-mode server.
// 127.0.0.1 rather than localhost: the backend binds 127.0.0.1, and on some
// systems "localhost" resolves to ::1 first, which nothing is listening on.
export const API_BASE =
  import.meta.env.VITE_API_BASE?.replace(/\/+$/, '') || 'http://127.0.0.1:8787'

export async function getJSON(url) {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

// ── Envelope unwrapping ────────────────────────────────────────────────────
// Each helper takes a parsed response body and returns the list inside it.
// They tolerate three shapes on purpose: the documented envelope, a bare array
// (in case a route is ever flattened), and null/undefined/an error body. The
// point is that a shape change degrades to an empty list rather than throwing
// inside a render.

function pickList(payload, key) {
  if (Array.isArray(payload)) return payload
  const inner = payload?.[key]
  return Array.isArray(inner) ? inner : []
}

/** GET /parties -> {parties: [...]} */
export const unwrapParties = (payload) => pickList(payload, 'parties')

/** GET /tokens/balance/{party} -> {party, byInstrument: [...]} */
export const unwrapBalances = (payload) => pickList(payload, 'byInstrument')

/** GET /tokens/transfers/{party} -> {party, count, transfers: [...]} */
export const unwrapTransfers = (payload) => pickList(payload, 'transfers')

/** GET /tokens/holdings/{party} -> {party, activeOnly, holdings: [...]} */
export const unwrapHoldings = (payload) => pickList(payload, 'holdings')

// /health and /metrics return their fields flat, with no envelope, so they
// have no unwrap helper - reading them as-is is correct.

// ── Typed fetchers ─────────────────────────────────────────────────────────

export async function fetchParties() {
  return unwrapParties(await getJSON(`${API_BASE}/parties`))
}

export async function fetchBalances(party) {
  return unwrapBalances(
    await getJSON(`${API_BASE}/tokens/balance/${encodeURIComponent(party)}`),
  )
}

export async function fetchTransfers(party, { limit = 50, instrument, direction } = {}) {
  const query = new URLSearchParams({ limit: String(limit) })
  // instrument and direction are real optional query params on the transfers
  // route - the server filters in SQL before LIMIT, so we never post-filter an
  // already-truncated page here.
  if (instrument) query.set('instrument', instrument)
  if (direction) query.set('direction', direction)
  return unwrapTransfers(
    await getJSON(
      `${API_BASE}/tokens/transfers/${encodeURIComponent(party)}?${query}`,
    ),
  )
}

export async function fetchHealth() {
  return getJSON(`${API_BASE}/health`)
}

export async function fetchMetrics() {
  return getJSON(`${API_BASE}/metrics`)
}

export async function fetchStale() {
  const payload = await getJSON(`${API_BASE}/tokens/transfers/stale`)
  return {
    olderThanSeconds: payload?.olderThanSeconds ?? null,
    transfers: unwrapTransfers(payload),
  }
}
