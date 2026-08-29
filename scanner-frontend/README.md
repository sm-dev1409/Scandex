# scanner-frontend

The React (Vite) dashboard for **Scandex**. It is a pure client of the local
JSON API in [`src/scandex_api/webapi.py`](../src/scandex_api/webapi.py) — it
never talks to the Cantor8 ledger itself, and it never writes anything.

**For how to run the whole stack (indexer + API + this frontend), see
["Running the full stack"](../README.md#running-the-full-stack) in the root
README.** This file only covers what is specific to the frontend.

## Quick start (test mode — no ledger, no secret, no network)

Two terminals from the repository root:

```bash
# 1) the API, serving fabricated demo data
python scripts/serve_api.py --data-mode test --port 8787

# 2) this frontend
cd scanner-frontend && npm install && npm run dev
```

Then open the URL Vite prints (http://localhost:5173 by default) and click
through to `/dashboard`.

## Configuration: `VITE_API_BASE`

The API base URL is **not** hardcoded. It is read from `import.meta.env`:

```js
// src/api.js
export const API_BASE =
  import.meta.env.VITE_API_BASE?.replace(/\/+$/, '') || 'http://127.0.0.1:8787'
```

To point the same source at a different server, copy `.env.example` to
`.env.local` and edit it:

```bash
cp .env.example .env.local
# then, e.g.
echo 'VITE_API_BASE=http://127.0.0.1:8790' > .env.local
```

`.env.local` is gitignored (`*.local`), so a local override is never committed.
Vite inlines `import.meta.env` **at build time**, so restart `npm run dev` — or
re-run `npm run build` — after changing it.

Switching between a test-mode and a real-mode backend is therefore a
`.env.local` change (or just running the backend on the default port), never a
source edit.

## Which mode am I looking at?

The frontend never guesses. The server reports `data_mode` on `GET /health`,
and it is displayed in two places:

* a **`Data mode: TEST` / `REAL` chip** in the dashboard topbar, and
* the **banner at the top of `/status`**, which spells out what the mode means.

`TEST` means every figure on screen is fabricated demo data from
[`demo_data.py`](../src/scandex_api/demo_data.py) — not ledger data.

## Routes

| Path | View |
|---|---|
| `/` | Landing splash |
| `/dashboard` | Party picker, balances (spendable vs locked), transfer history with filters, scanner metrics |
| `/status` | Data-mode banner, scanner health, metrics, stale pending transfers |
| anything else | Redirects to `/` |

## Layout

| File | What it holds |
|---|---|
| `src/api.js` | Every `fetch` and every response-envelope unwrap. The single place that knows the API's shape. |
| `src/format.js` | Pure presentation helpers (`summarise`, `relativeTime`, `shortParty`, `isStalePending`). No clock, no network — time is always passed in. |
| `src/Dashboard.jsx` | The main view. Component only. |
| `src/Status.jsx` | Health / metrics / data-mode page. |
| `src/main.jsx` | Router. |

`api.js` and `format.js` are split out from the component precisely so they can
be unit tested without a browser or a running backend — see below.

## Scripts

```bash
npm run dev       # Vite dev server on :5173
npm run build     # production build into dist/
npm run preview   # serve the built bundle on :4173
npm run lint      # eslint
npm run test      # vitest (unit tests, no backend required)
npm run test:run  # vitest once, non-watch (what CI runs)
```

`npm run test` needs **no** backend running: the tests cover the pure
unwrapping and formatting functions, with `fetch` stubbed where a fetcher is
exercised.
