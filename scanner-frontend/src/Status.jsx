import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import './Dashboard.css'
import { API_BASE, fetchHealth, fetchMetrics, fetchStale } from './api.js'
import { fmt, relativeTime } from './format.js'

// Scanner health / status page (challenge P7).
//
// Everything here is read straight from GET /health, GET /metrics and
// GET /tokens/transfers/stale. In particular the "Data mode" banner reads
// /health's `data_mode` field, which the server reports — it is deliberately
// NOT guessed from the port or the API base, because the whole point is to be
// able to tell fabricated demo data from real ledger data at a glance.

const POLL_MS = 2000
const STALE_MS = 10000

function Row({ label, value, hint }) {
  return (
    <li className="status-row">
      <span className="status-label">{label}</span>
      <span className="status-value" title={hint}>{value}</span>
    </li>
  )
}

export default function Status() {
  const [health, setHealth] = useState(null)
  const [metrics, setMetrics] = useState(null)
  const [stale, setStale] = useState({ olderThanSeconds: null, transfers: [] })
  const [error, setError] = useState(null)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    let alive = true
    async function poll() {
      try {
        const [hp, mt, st] = await Promise.all([
          fetchHealth(),
          fetchMetrics(),
          fetchStale(),
        ])
        if (!alive) return
        setHealth(hp)
        setMetrics(mt)
        setStale(st)
        setError(null)
      } catch (err) {
        if (alive) setError(String(err.message || err))
      }
    }
    poll()
    const id = setInterval(poll, POLL_MS)
    const tick = setInterval(() => setNow(Date.now()), 1000)
    return () => {
      alive = false
      clearInterval(id)
      clearInterval(tick)
    }
  }, [])

  const checkpointMs = health?.last_updated
    ? new Date(health.last_updated).getTime()
    : NaN
  const scannerStale = Number.isFinite(checkpointMs) && now - checkpointMs > STALE_MS
  const mode = health?.data_mode

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">C</div>
          <div>
            <div className="brand-name">Scanner status</div>
            <div className="brand-sub">Health &amp; metrics</div>
          </div>
        </div>
        <div className="topbar-status">
          <Link className="btn-link" to="/dashboard">← Dashboard</Link>
        </div>
      </header>

      <main className="main">
        {/* Data mode is the first thing on the page on purpose. */}
        <section className={`card span-12 mode-banner ${mode === 'test' ? 'mode-test' : 'mode-real'}`}>
          <div>
            <p className="hero-label">Data mode</p>
            <div className="mode-value">{mode ? mode.toUpperCase() : 'UNKNOWN'}</div>
            <p className="mode-note">
              {mode === 'test'
                ? 'Fabricated, deterministic demo data seeded by demo_data.py. No ledger connection is used in this mode, and none of these figures come from Cantor8.'
                : mode === 'real'
                  ? 'Serving the SQLite database the indexer filled from the Cantor8 ledger. Ledger-drift fields below are null unless C8_CLIENT_SECRET is configured.'
                  : 'The server did not report a data mode. Is it an older build, or is the API unreachable?'}
            </p>
          </div>
          <code className="mode-base" title="Set VITE_API_BASE to change this">{API_BASE}</code>
        </section>

        {error && (
          <section className="card span-12">
            <p className="empty">
              Backend unreachable at <code>{API_BASE}</code> — {error}
            </p>
          </section>
        )}

        <section className="card span-6">
          <div className="panel-head">
            <h2 className="panel-title">Scanner health</h2>
            <span className={`chip ${
              !health ? 'chip-mute'
                : health.status === 'no_data' ? 'chip-mute'
                  : scannerStale ? 'chip-neg' : 'chip-pos'
            }`}>
              {!health ? 'Loading…'
                : health.status === 'no_data' ? 'No data indexed'
                  : scannerStale ? 'Scanner stale' : 'Live'}
            </span>
          </div>
          {health && (
            <ul className="status-list">
              <Row label="Status" value={health.status} />
              <Row label="Scanner offset" value={health.scanner_offset ?? '—'} />
              <Row
                label="Ledger offset"
                value={health.ledger_offset ?? '—'}
                hint={health.ledger_offset_note}
              />
              <Row
                label="Drift (offsets)"
                value={health.scanner_delay_offsets ?? '—'}
                hint="null when offsets are not numeric on this deployment"
              />
              <Row
                label="Last checkpoint"
                value={health.last_updated ? relativeTime(health.last_updated, now) : '—'}
                hint={health.last_updated}
              />
              <Row label="Active holdings" value={fmt(health.active_holdings)} />
              <Row label="Archived holdings" value={fmt(health.archived_holdings)} />
              <Row label="Total transfers" value={fmt(health.total_transfers)} />
              <Row label="Total events" value={fmt(health.total_events)} />
              <Row label="Tracked parties" value={fmt(health.tracked_parties)} />
            </ul>
          )}
          {health?.ledger_offset_note && (
            <p className="status-note">{health.ledger_offset_note}</p>
          )}
        </section>

        <section className="card span-6">
          <div className="panel-head">
            <h2 className="panel-title">Metrics</h2>
            <span className="chip chip-mute">GET /metrics</span>
          </div>
          {metrics && (
            <ul className="status-list">
              <Row label="Total transfers" value={fmt(metrics.total_transfers)} />
              <Row label="Tracked parties" value={fmt(metrics.tracked_parties)} />
              <Row label="Active holdings" value={fmt(metrics.active_holdings)} />
              <Row label="Stale pending" value={fmt(metrics.stale_pending_transfers)} />
              {metrics.volume_by_instrument?.map((v) => (
                <Row
                  key={`vol-${v.instrument}`}
                  label={`Volume · ${v.instrument}`}
                  value={`${fmt(Number(Number(v.volume).toFixed(4)))} (${fmt(v.count)})`}
                />
              ))}
              {metrics.locked_by_instrument?.map((v) => (
                <Row
                  key={`lock-${v.instrument}`}
                  label={`Locked · ${v.instrument}`}
                  value={`${fmt(Number(Number(v.locked_total).toFixed(4)))} (${fmt(v.count)})`}
                />
              ))}
            </ul>
          )}
        </section>

        <section className="card span-12">
          <div className="panel-head">
            <h2 className="panel-title">Stale pending transfers</h2>
            <span className={`chip ${stale.transfers.length ? 'chip-neg' : 'chip-pos'}`}>
              {stale.transfers.length}
            </span>
          </div>
          <p className="status-note">
            Offers still <code>pending</code> after{' '}
            {stale.olderThanSeconds ?? '—'}s. A row that never leaves this list is
            the &ldquo;pending forever&rdquo; drift case.
          </p>
          <ul className="rows">
            {stale.transfers.length === 0 ? (
              <li className="empty">Nothing stale.</li>
            ) : (
              stale.transfers.map((t, i) => (
                <li className="row" key={`${t.update_id}:${i}`}>
                  <span className="chip chip-neg">stale</span>
                  <span className="row-amount">
                    {t.amount}
                    <span className="row-instrument">{t.instrument}</span>
                  </span>
                  <span className="row-party">
                    <span title={t.sender}>{t.sender?.split('::')[0]}</span>
                    <span>→</span>
                    <span title={t.receiver}>{t.receiver?.split('::')[0]}</span>
                  </span>
                  <span className="row-kind">{t.transfer_kind}</span>
                  <span className="row-time" title={t.recorded_at}>
                    {t.age_seconds != null
                      ? `${fmt(Math.round(t.age_seconds))}s old`
                      : relativeTime(t.recorded_at, now)}
                  </span>
                </li>
              ))
            )}
          </ul>
        </section>
      </main>
    </div>
  )
}
