import { Link } from 'react-router-dom'
import './Landing.css'

// Splash page at /. Two columns over one full-bleed ambient glow: copy left, the
// embedded crystal scene right. Nothing below the fold. Fonts (Manrope,
// JetBrains Mono) are linked from index.html.

export default function Landing() {
  return (
    <div className="landing">
      <div className="landing-glow" aria-hidden="true" />

      <header className="landing-header">
        <div className="landing-mark" aria-hidden="true">C</div>
        <span className="landing-wordmark">Canton Scanner</span>
      </header>

      <main className="landing-hero">
        <div className="landing-copy">
          <div className="status-pill">
            <span className="status-dot" aria-hidden="true" />
            Canton mainnet · 1.2s lag
          </div>

          <h1 className="landing-title">
            Your Canton ledger,
            <br />
            <em>indexed</em>
          </h1>

          <p className="landing-body">
            Scanner watches balances and transfers on the Canton ledger and keeps a live,
            readable record of every movement — no node queries, no log spelunking.
          </p>

          <div className="landing-actions">
            <Link className="btn-solid" to="/dashboard">
              Open Dashboard
            </Link>
            <button type="button" className="btn-ghost">
              View the API <span className="chev" aria-hidden="true">›</span>
            </button>
          </div>
        </div>

        <div className="landing-scene">
          <iframe
            src="/token-scene.html"
            style={{ width: '100%', height: '100%', border: 'none', background: 'transparent' }}
            title="Cantor8 token scene"
          />
        </div>
      </main>
    </div>
  )
}
