/**
 * End-to-end render check: mounts the REAL Dashboard and Status components
 * against a REAL running Scandex API and asserts that data actually reaches
 * the DOM.
 *
 * This is the test that distinguishes "the endpoint returns 200" from "the
 * frontend shows this" — the two Section 3 bugs (wrong port, unwrapped
 * envelope) both produced perfectly healthy 200s and a completely empty UI.
 *
 * It is SKIPPED unless a server is pointed at explicitly, so `npm run test`
 * and CI stay offline and self-contained:
 *
 *     # terminal 1
 *     python scripts/serve_api.py --data-mode test --port 8790
 *     # terminal 2
 *     VITE_API_BASE=http://127.0.0.1:8790 npx vitest run src/e2e.render.test.jsx
 *
 * The assertions below are written against the deterministic demo_data.py
 * seed (Alice: 100 Amulet total / 80 spendable / 1 locked, one stale pending
 * offer), so they only make sense in --data-mode test.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import Dashboard from './Dashboard.jsx'
import Status from './Status.jsx'
import { API_BASE } from './api.js'

// Only run when the caller explicitly pointed us at a server.
const ENABLED = Boolean(import.meta.env.VITE_API_BASE)
const maybe = ENABLED ? describe : describe.skip

const ui = (node) => render(<MemoryRouter>{node}</MemoryRouter>)

maybe(`end-to-end render against ${API_BASE}`, () => {
  it('renders the party selector from GET /parties', async () => {
    ui(<Dashboard />)
    // Bug A + Bug B: this used to stay "No parties indexed" forever.
    expect(await screen.findByRole('option', { name: /Alice/ }, { timeout: 15000 }))
      .toBeInTheDocument()
    expect(screen.getByRole('option', { name: /Bob/ })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /Carol/ })).toBeInTheDocument()
  }, 20000)

  it('renders Alice balances with the spendable-vs-locked split (P1/P2)', async () => {
    const { container } = ui(<Dashboard />)
    await waitFor(
      () => expect(container.querySelector('.bal-card')).toBeTruthy(),
      { timeout: 15000 },
    )
    const cards = container.textContent
    expect(cards).toContain('Amulet')
    expect(cards).toContain('100') // total, including the locked 20
    expect(cards).toContain('80 spendable') // total - locked
    expect(cards).toContain('1 locked')
    expect(cards).toContain('c8BTC') // per-instrument, never summed together
  }, 20000)

  it('renders transfer history rows (P4)', async () => {
    const { container } = ui(<Dashboard />)
    await waitFor(
      () => expect(container.querySelectorAll('li.row').length).toBeGreaterThan(0),
      { timeout: 15000 },
    )
    expect(container.querySelectorAll('li.row').length).toBe(5)
  }, 20000)

  it('badges the stale pending offer distinctly (P8)', async () => {
    ui(<Dashboard />)
    expect(await screen.findByText('stale pending', {}, { timeout: 15000 }))
      .toBeInTheDocument()
  }, 20000)

  it('renders the metrics panel from GET /metrics (P9)', async () => {
    const { container } = ui(<Dashboard />)
    await waitFor(
      () => expect(container.querySelector('.metric-grid')).toBeTruthy(),
      { timeout: 15000 },
    )
    const panel = container.querySelector('.metric-grid').parentElement.textContent
    expect(panel).toContain('Total transfers')
    expect(panel).toContain('Volume by instrument')
    expect(panel).toContain('Locked by instrument')
  }, 20000)

  it('shows the data mode reported by the server (P7)', async () => {
    ui(<Dashboard />)
    expect(await screen.findByText(/Data mode: TEST/, {}, { timeout: 15000 }))
      .toBeInTheDocument()
  }, 20000)

  it('renders the status page health fields and data-mode banner (P7)', async () => {
    const { container } = ui(<Status />)
    await waitFor(
      () => expect(container.querySelector('.status-list')).toBeTruthy(),
      { timeout: 15000 },
    )
    const text = container.textContent
    expect(text).toContain('Data mode')
    expect(text).toContain('TEST')
    expect(text).toContain('Scanner offset')
    expect(text).toContain('Tracked parties')
    expect(text).toContain('Stale pending transfers')
  }, 20000)
})
