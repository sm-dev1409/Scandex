import { useCallback, useEffect, useRef, useState } from 'react'
import './Dashboard.css'

// Canton scanner dashboard — reads the Scandex API (api_server.py) and shows
// the selected party's per-instrument balances and transfer history.
//
// /health is still polled, but only to drive the status chip in the topbar —
// none of its counts are rendered.
//
// Every field rendered below comes from store.py's documented return shapes.
// The two things the API does NOT give us, and that we derive here instead:
//
//   • transfer direction — the rows carry sender/receiver, not a direction
//     flag, so we compare them against the selected party.
//   • connected/stale — /health has no boolean for this, so we age
//     last_updated against the wall clock.
//
// The server sets CORS to allow_origins=["*"], so no proxy is needed from the
// Vite dev server.

const API = 'http://localhost:8000'
const POLL_MS = 1500
const TRANSFER_LIMIT = 50
// The scanner checkpoints on every batch; a gap this long means it stopped.
const STALE_MS = 10000

async function getJSON(url) {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

// `now` is passed in rather than read from the clock so rendering stays a pure
// function of state — the 1s tick below is what advances it.
function relativeTime(value, now) {
  const then = typeof value === 'number' ? value : new Date(value).getTime()
  if (Number.isNaN(then)) return value
  const secs = Math.max(0, Math.round((now - then) / 1000))
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return new Date(then).toLocaleString()
}

function fmt(n) {
  return typeof n === 'number' ? n.toLocaleString() : n
}

function partyLabel(p) {
  return p.display_name || p.party_id
}

// Canton party ids are "<hint>::<fingerprint>". The fingerprint is noise in a
// table row, so show the hint (or the known display_name) and keep the full id
// on the title attribute.
function shortParty(partyId, partyMap) {
  if (!partyId) return '—'
  const known = partyMap.get(partyId)
  if (known?.display_name) return known.display_name
  const hint = partyId.split('::')[0]
  return hint || partyId
}

// Amounts are per-instrument, so they are summed per-instrument and never
// added across instruments — "1,200 Amulet + 40 c8ETH" is not a number.
function summarise(transfers, party) {
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

function breakdown(stats, pick) {
  if (stats.instruments.length === 0) return 'No transfers'
  return stats.instruments
    .map((i) => {
      const e = stats.byInstrument.get(i)
      const v = pick(e)
      return `${v > 0 ? '+' : ''}${fmt(Number(v.toFixed(4)))} ${i}`
    })
    .join(' · ')
}

export default function Dashboard() {
  const [parties, setParties] = useState([])
  const [selectedParty, setSelectedParty] = useState('')

  const [balances, setBalances] = useState([]) // last known good
  const [transfers, setTransfers] = useState([]) // last known good
  const [health, setHealth] = useState(null)

  const [instrument, setInstrument] = useState('') // '' = all
  const [direction, setDirection] = useState('') // '' = both

  const [loading, setLoading] = useState(true)
  const [online, setOnline] = useState(null) // null until the first poll settles
  const [lastUpdated, setLastUpdated] = useState(null)
  const [flash, setFlash] = useState(false)
  const [showHistory, setShowHistory] = useState(true)
  const [now, setNow] = useState(() => Date.now()) // ticks so relative times stay live

  // /parties feeds the selector and is the only source of party ids — nothing
  // here is hardcoded. Retried until it returns a non-empty list, since the
  // scanner may not have indexed a party yet when the page first loads.
  useEffect(() => {
    if (parties.length > 0) return undefined
    let alive = true

    async function load() {
      try {
        const list = await getJSON(`${API}/parties`)
        if (!alive) return
        const rows = Array.isArray(list) ? list : []
        if (rows.length === 0) return
        setParties(rows)
        // Default to a local party — that's the one this scanner runs as.
        setSelectedParty(
          (cur) => cur || rows.find((p) => p.is_local)?.party_id || rows[0].party_id,
        )
      } catch (err) {
        console.warn('parties failed:', err)
        if (alive) setOnline(false)
      }
    }

    load()
    const id = setInterval(load, 5000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [parties.length])

  // The effect owns the polling loop; pollRef lets the refresh buttons fire an
  // off-schedule poll without hoisting the fetch logic out of it.
  const pollRef = useRef(() => {})
  const refresh = useCallback(() => pollRef.current(), [])

  useEffect(() => {
    if (!selectedParty) return undefined
    let alive = true
    let flashTimer

    const path = encodeURIComponent(selectedParty)
    const query = new URLSearchParams({ limit: String(TRANSFER_LIMIT) })
    if (instrument) query.set('instrument', instrument)
    if (direction) query.set('direction', direction)

    async function poll() {
      try {
        const [bal, hist, hp] = await Promise.all([
          // Balances stay unfiltered so the instrument dropdown below always
          // has the party's full instrument list to offer.
          getJSON(`${API}/tokens/balance/${path}`),
          getJSON(`${API}/tokens/transfers/${path}?${query}`),
          getJSON(`${API}/health`),
        ])
        if (!alive) return
        setBalances(Array.isArray(bal) ? bal : [])
        setTransfers(Array.isArray(hist) ? hist : [])
        setHealth(hp)
        setLastUpdated(Date.now())
        setOnline(true)
        setLoading(false)
        // Pulse the balances so a refresh is visible even when nothing changed.
        setFlash(true)
        clearTimeout(flashTimer)
        flashTimer = setTimeout(() => alive && setFlash(false), 400)
      } catch (err) {
        // Keep the last known good data on screen — a backend restart should
        // flip the status chip, not blank out the dashboard.
        if (!alive) return
        console.warn('poll failed:', err)
        setOnline(false)
      }
    }

    pollRef.current = poll
    poll()
    const pollId = setInterval(poll, POLL_MS)
    return () => {
      alive = false
      clearInterval(pollId)
      clearTimeout(flashTimer)
    }
  }, [selectedParty, instrument, direction])

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  function changeParty(partyId) {
    setSelectedParty(partyId)
    setInstrument('') // instrument lists differ per party
    setBalances([])
    setTransfers([])
    setLoading(true)
  }

  const partyMap = new Map(parties.map((p) => [p.party_id, p]))
  const stats = summarise(transfers, selectedParty)

  // /health carries no connected flag, so staleness is derived from how old
  // the scanner's last checkpoint is.
  const checkpointMs = health?.last_updated ? new Date(health.last_updated).getTime() : NaN
  const stale = Number.isFinite(checkpointMs) && now - checkpointMs > STALE_MS

  const statusChip =
    online === null
      ? { tone: 'chip-mute', text: 'Connecting…' }
      : online === false
        ? { tone: 'chip-neg', text: 'Backend unreachable' }
        : health?.status === 'no_data'
          ? { tone: 'chip-mute', text: 'No data indexed' }
          : stale
            ? { tone: 'chip-neg', text: 'Scanner stale' }
            : { tone: 'chip-pos', text: 'Live' }

  const emptyText = !loading
    ? 'No transfers match these filters.'
    : online === false
      ? `Waiting for the backend at ${API}`
      : 'Loading…'

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">C</div>
          <div>
            <div className="brand-name">Canton Scanner</div>
            <div className="brand-sub">Ledger activity</div>
          </div>
        </div>

        <div className="topbar-status">
          <label className="party-picker">
            <span className="party-picker-label">Party</span>
            <select
              className="select"
              value={selectedParty}
              onChange={(e) => changeParty(e.target.value)}
              disabled={parties.length === 0}
            >
              {parties.length === 0 && <option value="">No parties indexed</option>}
              {parties.map((p) => (
                <option key={p.party_id} value={p.party_id}>
                  {partyLabel(p)}
                  {p.is_local ? ' (local)' : ''}
                </option>
              ))}
            </select>
          </label>

          {lastUpdated && (
            <span className="synced">
              Polled {relativeTime(lastUpdated, now)}
            </span>
          )}
          <span className={`chip ${statusChip.tone}`}>
            <span className="dot" />
            {statusChip.text}
          </span>
        </div>
      </header>

      <main className="main">
        <section className="card hero span-12">
          <div className="hero-body">
            <p className="hero-label">Balances</p>

            {loading ? (
              <div className="amount">
                <span className="amount-value placeholder">
                  {online === false ? '—' : 'Loading…'}
                </span>
              </div>
            ) : balances.length === 0 ? (
              <div className="amount">
                <span className="amount-value placeholder">No holdings</span>
              </div>
            ) : (
              <div className={flash ? 'bal-grid flash' : 'bal-grid'}>
                {balances.map((b) => (
                  <div className="bal-card" key={b.instrument}>
                    <div className="bal-head">
                      <span className="bal-instrument">{b.instrument}</span>
                      {b.locked_count > 0 && (
                        <span className="chip chip-mute">{b.locked_count} locked</span>
                      )}
                    </div>
                    <div className="bal-total">{fmt(b.total)}</div>
                    {b.spendable !== b.total && (
                      <div className="bal-spendable">{fmt(b.spendable)} spendable</div>
                    )}
                    <div className="bal-foot">
                      {b.holding_count === 1 ? '1 holding' : `${fmt(b.holding_count)} holdings`}
                    </div>
                  </div>
                ))}
              </div>
            )}

            <p className="hero-party">
              Party <code title={selectedParty}>{shortParty(selectedParty, partyMap)}</code>
            </p>
          </div>

          <div className="hero-actions">
            <button type="button" className="btn-primary" onClick={refresh}>
              Refresh now
            </button>
          </div>
        </section>

        <section className="card span-4">
          <div className="stat-head">
            <span className="stat-label">Received</span>
            <span className="chip chip-pos">↓ {stats.inCount}</span>
          </div>
          <div className="stat-value pos">
            {loading
              ? '—'
              : stats.single
                ? fmt(Number(stats.byInstrument.get(stats.single).received.toFixed(4)))
                : stats.inCount}
          </div>
          <div className="stat-foot">
            {stats.single
              ? `${stats.single} · ${stats.inCount} incoming`
              : breakdown(stats, (e) => e.received)}
          </div>
        </section>

        <section className="card span-4">
          <div className="stat-head">
            <span className="stat-label">Sent</span>
            <span className="chip chip-neg">↑ {stats.outCount}</span>
          </div>
          <div className="stat-value neg">
            {loading
              ? '—'
              : stats.single
                ? fmt(Number(stats.byInstrument.get(stats.single).sent.toFixed(4)))
                : stats.outCount}
          </div>
          <div className="stat-foot">
            {stats.single
              ? `${stats.single} · ${stats.outCount} outgoing`
              : breakdown(stats, (e) => e.sent)}
          </div>
        </section>

        <section className="card span-4">
          <div className="stat-head">
            <span className="stat-label">Net flow</span>
            <span className="chip chip-mute">{transfers.length}</span>
          </div>
          <div className="stat-value">
            {loading
              ? '—'
              : stats.single
                ? (() => {
                    const e = stats.byInstrument.get(stats.single)
                    const net = Number((e.received - e.sent).toFixed(4))
                    return `${net > 0 ? '+' : ''}${fmt(net)}`
                  })()
                : transfers.length}
          </div>
          <div className="stat-foot">
            {stats.single
              ? `${stats.single} · across ${transfers.length} transfers`
              : breakdown(stats, (e) => e.received - e.sent)}
          </div>
        </section>

        <section className="card span-12">
          <div className="panel-head">
            <h2 className="panel-title">Transfers</h2>
            <div className="panel-tools">
              {/* instrument and direction are real optional query params on
                  GET /tokens/transfers/{party} — the server filters, not us. */}
              <select
                className="select select-sm"
                value={instrument}
                onChange={(e) => setInstrument(e.target.value)}
                aria-label="Filter by instrument"
              >
                <option value="">All instruments</option>
                {balances.map((b) => (
                  <option key={b.instrument} value={b.instrument}>
                    {b.instrument}
                  </option>
                ))}
              </select>

              <div className="toggle" role="group" aria-label="Filter by direction">
                {[
                  ['', 'Both'],
                  ['sent', 'Sent'],
                  ['received', 'Received'],
                ].map(([value, label]) => (
                  <button
                    key={label}
                    type="button"
                    className={direction === value ? 'toggle-btn on' : 'toggle-btn'}
                    onClick={() => setDirection(value)}
                    aria-pressed={direction === value}
                  >
                    {label}
                  </button>
                ))}
              </div>

              <span className="chip chip-mute">{transfers.length}</span>
              <button
                type="button"
                className="btn-icon"
                onClick={refresh}
                title="Refresh"
                aria-label="Refresh transfers"
              >
                ↻
              </button>
              <button
                type="button"
                className="btn-icon"
                onClick={() => setShowHistory((v) => !v)}
                title={showHistory ? 'Collapse' : 'Expand'}
                aria-expanded={showHistory}
                aria-label={showHistory ? 'Collapse transfers' : 'Expand transfers'}
              >
                {showHistory ? '⌃' : '⌄'}
              </button>
            </div>
          </div>

          {showHistory && (
            <ul className="rows">
              {transfers.length === 0 ? (
                <li className="empty">{emptyText}</li>
              ) : (
                transfers.map((t, i) => {
                  // No direction field in the response — derive it.
                  const outgoing = t.sender === selectedParty
                  const dir = outgoing ? 'out' : 'in'
                  const other = outgoing ? t.receiver : t.sender
                  return (
                    // One update_id can produce several transfer rows, so the
                    // key has to include position to stay unique.
                    <li className="row" key={`${t.update_id}:${i}`}>
                      <span className={`row-arrow ${dir}`} aria-hidden="true">
                        {dir === 'in' ? '↓' : '↑'}
                      </span>
                      <span className={`row-amount ${dir}`}>
                        {outgoing ? '−' : '+'}
                        {t.amount}
                        <span className="row-instrument">{t.instrument}</span>
                      </span>
                      <span className="row-party">
                        <span>{outgoing ? 'to' : 'from'}</span>
                        <span title={other}>{shortParty(other, partyMap)}</span>
                      </span>
                      <span className="row-kind">{t.transfer_kind}</span>
                      <span className="row-time" title={`${t.recorded_at} · offset ${t.ledger_offset}`}>
                        {relativeTime(t.recorded_at, now)}
                      </span>
                    </li>
                  )
                })
              )}
            </ul>
          )}
        </section>
      </main>
    </div>
  )
}
