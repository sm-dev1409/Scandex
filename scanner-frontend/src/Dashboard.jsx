import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import './Dashboard.css'
import {
  API_BASE,
  fetchBalances,
  fetchHealth,
  fetchMetrics,
  fetchParties,
  fetchStale,
  fetchTransfers,
} from './api.js'
import {
  breakdown,
  fmt,
  isStalePending,
  partyLabel,
  relativeTime,
  shortParty,
  summarise,
} from './format.js'

// Canton scanner dashboard — reads the Scandex local JSON API (webapi.py) and
// shows the selected party's per-instrument balances, transfer history and
// scanner metrics.
//
// All HTTP and all response-envelope unwrapping lives in api.js; all pure
// formatting lives in format.js. This file is the component only.
//
// Every field rendered below comes from store.py's documented return shapes.
// The two things the API does NOT give us, and that we derive here instead:
//
//   • transfer direction per row — the rows carry sender/receiver, not a
//     direction flag, so we compare them against the selected party.
//   • connected/stale — /health has no boolean for this, so we age
//     last_updated against the wall clock.
//
// The server sets Access-Control-Allow-Origin: *, so no dev proxy is needed.

const POLL_MS = 1500
const TRANSFER_LIMIT = 50
// The scanner checkpoints on every batch; a gap this long means it stopped.
const STALE_MS = 10000

export default function Dashboard() {
  const [parties, setParties] = useState([])
  const [selectedParty, setSelectedParty] = useState('')

  const [balances, setBalances] = useState([]) // last known good
  const [transfers, setTransfers] = useState([]) // last known good
  const [health, setHealth] = useState(null)
  const [metrics, setMetrics] = useState(null)
  // update_ids the server itself considers stale, so a row badge agrees with
  // GET /tokens/transfers/stale rather than being a second opinion.
  const [staleIds, setStaleIds] = useState(() => new Set())
  const [staleSeconds, setStaleSeconds] = useState(300)

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
        const rows = await fetchParties()
        if (!alive) return
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

    async function poll() {
      try {
        const [bal, hist, hp, mt, st] = await Promise.all([
          // Balances stay unfiltered so the instrument dropdown below always
          // has the party's full instrument list to offer.
          fetchBalances(selectedParty),
          fetchTransfers(selectedParty, {
            limit: TRANSFER_LIMIT,
            instrument,
            direction,
          }),
          fetchHealth(),
          fetchMetrics(),
          fetchStale(),
        ])
        if (!alive) return
        setBalances(bal)
        setTransfers(hist)
        setHealth(hp)
        setMetrics(mt)
        setStaleIds(new Set(st.transfers.map((t) => t.update_id)))
        if (st.olderThanSeconds != null) setStaleSeconds(st.olderThanSeconds)
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

  // Reported by the server (/health.data_mode), never guessed from the port —
  // so a fabricated demo number can never be mistaken for ledger data.
  const dataMode = health?.data_mode

  const emptyText = !loading
    ? 'No transfers match these filters.'
    : online === false
      ? `Waiting for the backend at ${API_BASE}`
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

          {dataMode && (
            <span
              className={`chip ${dataMode === 'test' ? 'chip-warn' : 'chip-mute'}`}
              title={
                dataMode === 'test'
                  ? 'Fabricated demo data seeded by demo_data.py — not ledger data.'
                  : 'Serving data the indexer read from the Cantor8 ledger.'
              }
            >
              Data mode: {dataMode.toUpperCase()}
            </span>
          )}

          {lastUpdated && (
            <span className="synced">
              Polled {relativeTime(lastUpdated, now)}
            </span>
          )}
          <span className={`chip ${statusChip.tone}`}>
            <span className="dot" />
            {statusChip.text}
          </span>
          <Link className="btn-link" to="/status">
            Status
          </Link>
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

        {/* Scanner metrics (challenge P9). Every number here comes from
            GET /metrics — none of it is recomputed client-side from the
            transfer list, which only holds the newest TRANSFER_LIMIT rows for
            one party and so could not produce network-wide totals. */}
        <section className="card span-12">
          <div className="panel-head">
            <h2 className="panel-title">Scanner metrics</h2>
            <span className="chip chip-mute">GET /metrics</span>
          </div>

          {!metrics ? (
            <p className="empty">{online === false ? 'Backend unreachable' : 'Loading…'}</p>
          ) : (
            <>
              <div className="metric-grid">
                <div className="metric">
                  <span className="metric-label">Total transfers</span>
                  <span className="metric-value">{fmt(metrics.total_transfers)}</span>
                </div>
                <div className="metric">
                  <span className="metric-label">Tracked parties</span>
                  <span className="metric-value">{fmt(metrics.tracked_parties)}</span>
                </div>
                <div className="metric">
                  <span className="metric-label">Active holdings</span>
                  <span className="metric-value">{fmt(metrics.active_holdings)}</span>
                </div>
                <div className="metric">
                  <span className="metric-label">Stale pending</span>
                  <span
                    className={
                      metrics.stale_pending_transfers > 0
                        ? 'metric-value neg'
                        : 'metric-value'
                    }
                  >
                    {fmt(metrics.stale_pending_transfers)}
                  </span>
                </div>
                <div className="metric">
                  <span className="metric-label">Scanner delay</span>
                  <span className="metric-value">
                    {metrics.scanner_delay_offsets == null
                      ? '—'
                      : `${fmt(metrics.scanner_delay_offsets)} offsets`}
                  </span>
                </div>
              </div>

              <div className="metric-cols">
                <div>
                  <h3 className="metric-sub">Volume by instrument</h3>
                  {metrics.volume_by_instrument?.length ? (
                    <ul className="metric-list">
                      {metrics.volume_by_instrument.map((v) => (
                        <li key={v.instrument}>
                          <span>{v.instrument}</span>
                          <span>
                            {fmt(Number(Number(v.volume).toFixed(4)))}
                            <span className="metric-count"> · {fmt(v.count)}</span>
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="empty">No transfer volume yet</p>
                  )}
                </div>

                <div>
                  <h3 className="metric-sub">Locked by instrument</h3>
                  {metrics.locked_by_instrument?.length ? (
                    <ul className="metric-list">
                      {metrics.locked_by_instrument.map((v) => (
                        <li key={v.instrument}>
                          <span>{v.instrument}</span>
                          <span>
                            {fmt(Number(Number(v.locked_total).toFixed(4)))}
                            <span className="metric-count"> · {fmt(v.count)}</span>
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="empty">Nothing locked</p>
                  )}
                </div>
              </div>
            </>
          )}
        </section>

        <section className="card span-12">
          <div className="panel-head">
            <h2 className="panel-title">Transfers</h2>
            <div className="panel-tools">
              {/* instrument and direction are real optional query params on
                  GET /tokens/transfers/{party} — the server filters in SQL
                  before LIMIT, so we never post-filter a truncated page. */}
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
                  const isStale = isStalePending(t, now, staleSeconds, staleIds)
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
                      {/* A pending offer past the stale threshold is the A2
                          drift case — badge it instead of leaving "pending" as
                          plain text indistinguishable from a settled row. */}
                      {isStale ? (
                        <span
                          className="chip chip-neg"
                          title={`Still pending after ${staleSeconds}s — the offer may never settle.`}
                        >
                          stale pending
                        </span>
                      ) : t.status && t.status !== 'settled' ? (
                        <span className="chip chip-warn">{t.status}</span>
                      ) : (
                        <span className="row-status-ok">{t.status}</span>
                      )}
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
