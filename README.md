# Scandex — Cantor8 connectivity check

Scandex talks to the Cantor8 network (a Canton Network node) to read balances,
instruments and activity. Before any of that can work, a handful of separate
services have to be reachable and answering correctly. **This tool checks them
for you and explains, in plain English, what is working and what is not** — and
it never changes anything on the ledger.

If you have never touched Canton, start here. The [Concepts](#concepts-in-plain-words)
section at the bottom explains every term.

## Quick start

1. Clone the repo and open a terminal in it.
2. Set the environment variables for this session (see below).
3. Run:

```bash
python scripts/check_cantor8.py
```

No `pip install` is needed — the tool uses only the Python standard library and
puts itself on the path automatically. Python 3.11+ is recommended.

If you have **no client secret yet**, the tool still runs: it reports the
authenticated checks as `FAIL`/`SKIP` and still checks the public services. That
is a valid first run.

### Set the variables (PowerShell, Windows)

These set variables **for the current shell session only** — close the window
and they are gone. Nothing is written to disk.

```powershell
$env:C8_BASE          = "https://api.validator.dev.digik.cantor8.tech/api/ledger"
$env:C8_IDP           = "https://auth.dev.digik.cantor8.tech"
$env:C8_CLIENT_ID     = "hackathon"
$env:C8_CLIENT_SECRET = "<enter-secret-locally>"
$env:C8_REGISTRY      = "https://sv-proxy.dev.digik.cantor8.tech"
$env:C8_PARTY         = "<your-party-id>"
python scripts/check_cantor8.py --summary
```

### Set the variables (bash / zsh, macOS / Linux)

```bash
export C8_BASE="https://api.validator.dev.digik.cantor8.tech/api/ledger"
export C8_IDP="https://auth.dev.digik.cantor8.tech"
export C8_CLIENT_ID="hackathon"
export C8_CLIENT_SECRET="<enter-secret-locally>"
export C8_REGISTRY="https://sv-proxy.dev.digik.cantor8.tech"
export C8_PARTY="<your-party-id>"
python scripts/check_cantor8.py --summary
```

Ask the Cantor8 team for `C8_CLIENT_SECRET`. **Never commit it or paste it into
a file that is tracked by git.** You can also copy `.env.example` to `.env`
(which is gitignored) and fill it in there.

## Commands and flags

Every run is **read-only**. None of these flags can move money or change the
ledger.

| Command | What it does | Example |
|---|---|---|
| *(no flags)* | Full check, one line per service, grouped and labelled. | `python scripts/check_cantor8.py` |
| `--summary` | Just the totals and any failures. Fastest way to see "is it green". | `python scripts/check_cantor8.py --summary` |
| `--verbose` | Adds a plain-English meaning under each check, plus per-request logs. | `python scripts/check_cantor8.py --verbose` |
| `--json` | The full result set as JSON, for scripts or dashboards. | `python scripts/check_cantor8.py --json` |
| `--party <id>` | Inspect a specific party's holdings (overrides `C8_PARTY`). | `python scripts/check_cantor8.py --party myteam-dev-1::1220...` |
| `--write-report` | Writes timestamped JSON + Markdown reports into `reports/`. | `python scripts/check_cantor8.py --write-report` |
| `--timeout <s>` | Per-request timeout in seconds (default 30). | `python scripts/check_cantor8.py --timeout 20` |
| `--preview-transfer <from> <to> <amount> [--instrument Amulet]` | **Dry-run** analysis of a transfer. Submits **nothing**. | `python scripts/check_cantor8.py --preview-transfer alice bob 25` |
| `--index [--follow]` | Run the A1 scanner: seed the active contract set, then stream updates into the local database. Read-only. | `python scripts/check_cantor8.py --index --follow --party alice::1220...` |
| `--balance` | Print indexed balances for a party from the local database. | `python scripts/check_cantor8.py --balance --party alice::1220...` |
| `--history` | Print indexed transfer history for a party. | `python scripts/check_cantor8.py --history --party alice::1220... --limit 25` |
| `--serve [--port 8787]` | Serve the indexed database as a local JSON API for a frontend. See below. | `python scripts/check_cantor8.py --serve --port 8787` |

Exit codes: `0` = all non-skipped checks passed · `1` = one or more failed ·
`2` = configuration error (could not even start).

## A sample run, annotated

```
Scandex x Cantor8 check
  ledger base   https://api.validator.dev.digik.cantor8.tech/api/ledger
  ...
  secret set    NO - auth checks will fail          <- no C8_CLIENT_SECRET this session

[Auth]
  FAIL   POST /realms/master/protocol/openid-connect/token
         C8_CLIENT_SECRET is not set. Ask the Cantor8 team ...   <- fix: set the secret
[Ledger]
  SKIP   GET /v2/state/ledger-end
         Skipped: no token.                          <- can't check the ledger without a token
[Registry]
  PASS   GET /registry/metadata/v1/info 200 (421 ms)
         Registry info readable (admin=DSO::1220...) <- public service works with no token
  PASS   GET /registry/metadata/v1/instruments 200
         1 instrument(s): Amulet(...)
[Scanner]
  PASS   GET /health 200 (74 ms)
         status=ok, db=ok, scannerDelaySecs=0.66     <- index is ~0.7s behind the ledger
  SKIP   GET /tokens/balance/{party}
         Skipped: no token.
[Public Scan]
  PASS   GET /api/scan/v0/splice-instance-names 200
         Public network info readable (Canton Network)
[Ledger]
  MANUAL POST /v2/commands/submit-and-wait
         Not run by the diagnostic - write/mutating action.   <- never automatic; needs you

Passed: 4   Failed: 1   Skipped: 4   Manual action required: 5
NOT TESTED: POST /v2/commands/submit-and-wait (write), WS /v2/updates (streaming)
```

Every check reports exactly one of **`PASS`**, **`FAIL`**, **`SKIPPED`**, or
**`EXPECTED MANUAL ACTION`**, and each carries: the service, HTTP method,
endpoint, whether a token was required, the status code, a short result, what it
means, how much the demo needs it, and the latency.

A run with `--write-report` produces the same information as a saved report; see
[`reports/sample-cantor8-check.md`](reports/sample-cantor8-check.md) for a real
example.

## How to read the results

- **Auth** — Can Keycloak give us a token? If this `FAIL`s, everything that needs
  a token is skipped. The fix is almost always: set `C8_CLIENT_SECRET`.
- **Ledger** — Is the Canton ledger reachable, can we read the current offset,
  list parties, find your party, confirm it is *local* (only local parties can
  submit), and read holdings? These are the checks the Scandex demo depends on.
- **Registry** — Can we read token metadata: which instruments exist, their
  administrators and decimals, and which transfer features are advertised?
- **Scanner and public Scan** — Is the off-ledger index healthy and how far
  behind is it (`scannerDelaySecs`)? Is public network info readable? On a
  protected endpoint, the tool separates **authentication** failure (401) from
  **permission** failure (403).

**When a check fails:**
- `Auth FAIL` → set `C8_CLIENT_SECRET` (and check `C8_CLIENT_ID`).
- `Unreachable` / `Timed out` → a network or VPN problem, not credentials. Try
  `--timeout 20`, check your connection.
- `403` on a party's holdings → normal for a party you do not own; not a bug.
- Configured party `not found` or `REMOTE` → you cannot submit for it; use a
  local party.

The footer always ends with **`NOT TESTED`**, listing the write and streaming
endpoints this tool deliberately never exercises. A green run means the *read*
paths are healthy — never that the whole system is proven.

## Concepts in plain words

- **An API** is a way for one program to ask another for data or actions over
  the network, using plain HTTP requests.
- **Keycloak** is the login service. You give it a client id and secret; it
  gives back a token. It answers *who you are*, nothing more.
- **A token** is a short-lived pass that says who you are. You attach it to each
  request (`Authorization: Bearer ...`). It expires; the tool fetches and caches
  it for you and never prints it.
- **A ledger** is the shared record of contracts. Canton's Ledger API is the
  **authoritative** source of what is true right now.
- **A party** is an identity on the ledger (looks like `hint::fingerprint`).
  Your node knows about many parties but can only *submit* for its **local**
  ones.
- **A holding** is a contract that says "this party holds this amount of this
  token". Your balance is a *set of holdings*, not a single number.
- **The registry** is how a wallet gets what it needs to build a transfer:
  instrument metadata, and — for a transfer — the issuer's config handed over as
  one-time *disclosed contracts*.
- **Why several services?** Login (Keycloak), the ledger (truth), the registry
  (token rules), and the scanner (a fast pre-computed index) each do one job.
  Scandex uses all of them.
- **Why a valid token can still give HTTP 403.** A token proves *who you are*; it
  does not grant rights over a party's contracts. **401 = "who are you"** (no
  valid token). **403 = "not yours"** (valid token, no rights, or a
  machine-to-machine-only endpoint).
- **Why you see parties you do not own.** A node lists every party it has heard
  about on the network, including ones hosted elsewhere. Seeing one is not the
  same as being able to act for it.
- **Why locked holdings cannot be spent.** A holding gets locked when it is
  escrowed for a pending transfer offer. It still shows in your balance but
  cannot be spent until the lock expires — so *spendable* excludes locked.
- **Why a transfer can become an offer.** If the receiver has a live
  preapproval, money moves immediately (`direct`). If not, the transfer creates
  a `TransferInstruction` (`offer`) and the receiver's balance does **not**
  change until they accept it.
- **Ledger vs scanner.** The ledger is authoritative and current. The scanner is
  an indexed *copy* that can lag; its `scannerDelaySecs` tells you by how much.
  When they disagree, trust the ledger.
- **Which endpoints are safe to read.** Everything this tool runs is a read.
  `ledger-end`, `parties`, `active-contracts`, registry metadata, scanner health
  and public Scan info change nothing.
- **Which actions need manual approval.** Transfers, party allocation, granting
  rights, accepting/rejecting/withdrawing offers, and any `submit-and-wait`
  write. The tool never does these — see Safety.

## Running the local API for the frontend

The diagnostic answers "is Cantor8 reachable". The **scanner** answers "what do
my parties actually hold, and what moved". It is two processes sharing one
SQLite file:

1. the **indexer** — reads the ledger and writes the database (one writer);
2. the **API server** — serves that database as JSON on localhost (many readers).

WAL mode is enabled on the database, which is what makes running both against
one file safe. The API server never writes.

### Start them side by side

Terminal 1 — the indexer, following the ledger forever:

```bash
python scripts/check_cantor8.py --index --follow --tick 5 \
    --party "myteam-dev-1::1220de..." --db scandex.db
```

Terminal 2 — the JSON API the frontend calls:

```bash
python scripts/serve_api.py --db scandex.db --port 8787
```

Both also work as installed console scripts (`check-cantor8 --index --follow`,
`serve-scandex-api`), and the API can be started from the diagnostic CLI
instead if you prefer one entry point: `python scripts/check_cantor8.py --serve
--db scandex.db --port 8787`.

First run seeds from the active contract set, so balances are correct
immediately rather than starting at zero. Every run after that resumes from the
saved offset — kill either process and restart it, nothing is lost and the ACS
is never re-read.

### Environment variables that matter

| Variable | Needed by | Effect |
|---|---|---|
| `C8_CLIENT_SECRET` | indexer (required), API (optional) | Without it the indexer cannot read the ledger at all. The API still serves everything from the local database; only `/health` and `/metrics` lose their live `ledger_offset`, reporting `null` with a note. |
| `C8_PARTY` | indexer | Default party to index; `--party` overrides it. |
| `C8_BASE`, `C8_IDP`, `C8_CLIENT_ID`, `C8_USER` | indexer | Ledger and auth endpoints. See [.env.example](.env.example). |

Other useful flags: `--db PATH` (default `scandex.db`), `--port` (default
`8787`), `--host` (default `127.0.0.1`), and `--stale-seconds` (default `300`
— how old a still-pending transfer must be to count as stale). Pass
`--no-ledger` to `serve_api.py` to skip contacting the ledger entirely.

### The routes

| Method | Path | Answers |
|---|---|---|
| `GET` | `/health` | Is the scanner up, how far behind the ledger, how many stale transfers |
| `GET` | `/parties` | Every party the scanner has seen |
| `GET` | `/tokens/balance/{party}` | Balance per instrument: `total` vs `spendable` |
| `GET` | `/tokens/holdings/{party}` | The individual holding contracts, with their `locked` flag |
| `GET` | `/tokens/transfers/{party}` | Transfer history, newest first |
| `GET` | `/tokens/transfers/stale` | Offers stuck `pending` past the threshold |
| `GET` | `/tokens/owners` | Every known party and its balances |
| `GET` | `/metrics` | Counts, per-instrument volume and locked totals, scanner delay |

### Sample requests

> The responses below come from a **seeded test database**, not a live DevNet
> call. Party ids and contract ids are made up. Use them to see the shape of
> the JSON, not as evidence of live data.

```console
$ curl -s http://127.0.0.1:8787/tokens/balance/alice::1220de
{
  "party": "alice::1220de",
  "byInstrument": [
    {
      "instrument": "Amulet",
      "total": 100.0,
      "spendable": 80.0,
      "holding_count": 3,
      "locked_count": 1
    }
  ]
}
```

`total` includes locked holdings; `spendable` does not. Here one 20-Amulet
holding is locked, so 100 is held but only 80 can be spent.

```console
$ curl -s http://127.0.0.1:8787/tokens/transfers/stale
{
  "olderThanSeconds": 300,
  "count": 1,
  "transfers": [
    {
      "id": 3,
      "update_id": "upd-91",
      "contract_id": "ti-0c4",
      "sender": "alice::1220de",
      "receiver": "bob::9f31aa",
      "amount": "5",
      "instrument": "Amulet",
      "transfer_kind": "offer",
      "status": "pending",
      "ledger_offset": "1044",
      "recorded_at": "2026-08-29T16:22:32.801133+00:00",
      "age_seconds": 929.0
    }
  ]
}
```

That is the "nobody notices until a user complains" case: an offer that has sat
unaccepted for 929 seconds.

```console
$ curl -s http://127.0.0.1:8787/health
{
  "status": "ok",
  "scanner_offset": "1044",
  "ledger_offset": null,
  "scanner_delay_offsets": null,
  "last_updated": "2026-08-29T16:37:32.801526+00:00",
  "active_holdings": 5,
  "archived_holdings": 0,
  "total_transfers": 3,
  "total_events": 5,
  "tracked_parties": 2,
  "stale_pending_transfers": 1,
  "ledger_offset_note": "no ledger client configured (set C8_CLIENT_SECRET to report drift)"
}
```

`ledger_offset` is `null` here because that server was started with
`--no-ledger`. With a secret configured it carries the live ledger end and
`scanner_delay_offsets` shows the real drift; if DevNet is unreachable the
field degrades back to `null` with a note rather than taking the API down.

An unknown party returns `404` with `{"error": "unknown party: ..."}`. A party
the scanner knows but which holds nothing yet returns `200` with an empty list
— a new account is a normal state, not an error.

### Note for the frontend

Import the database class from the **package path**, not as a bare module:

```python
from scandex_api.store import ScannerDB      # correct
import store                                  # will not resolve
```

Most frontends should call the HTTP API rather than importing `ScannerDB` at
all — one process writing and one reading is the arrangement WAL is designed
for.

**This server is for local demo use only:** no auth, no TLS, and a wildcard
`Access-Control-Allow-Origin: *` on every response so any localhost origin can
call it. Keep it bound to `127.0.0.1`.

## Running the full stack

The sections above cover the diagnostic and the API on their own. This is how
to run **the whole application** — scanner, database, API and dashboard.

### Test mode — two terminals, no ledger, no secret, no network

The fastest way to see everything working. Nothing here contacts Cantor8, so it
needs no `C8_CLIENT_SECRET` and no DevNet access. There is **no indexer** in
this mode — the demo dataset is seeded directly.

```bash
# terminal 1 — the API, serving fabricated demo data
python scripts/serve_api.py --data-mode test --port 8787

# terminal 2 — the dashboard
cd scanner-frontend
npm install          # first time only
npm run dev
```

Open the URL Vite prints (http://localhost:5173 by default), then go to
**/dashboard**. You should see three parties (Alice, Bob, Carol), Alice's
balance of **100 Amulet total / 80 spendable / 1 locked** plus 2 c8BTC, five
transfer rows one of which is badged **stale pending**, and a
**`Data mode: TEST`** chip in the topbar. **/status** shows scanner health and
metrics.

### Real mode — three terminals

Serves data the indexer actually read from the Cantor8 ledger. Requires
`C8_CLIENT_SECRET` (see [Quick start](#quick-start)).

```bash
# terminal 1 — the scanner: the only writer
python scripts/check_cantor8.py --index --follow --party <your-party-id>

# terminal 2 — the API: read-only, same file
python scripts/serve_api.py --data-mode real --port 8787

# terminal 3 — the dashboard
cd scanner-frontend && npm run dev
```

Both processes share `scandex.db`. That is safe because `ScannerDB` enables WAL
mode — one writer, many readers. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#why-sqlite-and-why-wal-specifically).

Without a secret, drop the indexer and add `--no-ledger`: the API still serves
whatever is already in `scandex.db`, and `/health` reports a null
`ledger_offset` with a note explaining why.

### Pointing the frontend somewhere else

The API base URL is **not** hardcoded. It comes from `VITE_API_BASE`, defaulting
to `http://127.0.0.1:8787` — the port `serve_api.py` actually defaults to:

```bash
cd scanner-frontend
cp .env.example .env.local
echo 'VITE_API_BASE=http://127.0.0.1:8790' > .env.local   # e.g. a second server
```

`.env.local` is gitignored. Vite inlines `import.meta.env` at build time, so
restart `npm run dev` after changing it. Switching between a test-mode and a
real-mode backend is a config change, never a source edit.

## Test mode vs real mode

| | `--data-mode test` | `--data-mode real` (default) |
|---|---|---|
| Database | `scandex-test.db` | `scandex.db` |
| Data source | `demo_data.py`, fabricated | whatever the indexer wrote |
| Needs `C8_CLIENT_SECRET` | no | yes, for live drift |
| Network access | **none** | ledger reads (unless `--no-ledger`) |
| Indexer required | no | yes, to have any data |
| Reported as | `data_mode: "test"` | `data_mode: "real"` |

```bash
python scripts/serve_api.py --data-mode test              # offline demo
python scripts/serve_api.py --data-mode real              # live drift, needs the secret
python scripts/serve_api.py --data-mode real --no-ledger  # local data only, no ledger calls
```

**Test mode guarantees.** The seeded data is deterministic (re-seeding rebuilds
the same dataset rather than stacking a second copy) and **no network call is
made**. That is structural, not a promise: `main()` never constructs a
`LedgerClient` at all when `--data-mode test`, so there is no object on which a
ledger call could be made. `tests/test_webapi_contract.py::TestModeIsOfflineTests`
asserts exactly that, by failing if `build_ledger_client` is ever invoked in
test mode.

**Real mode guarantees.** Real mode never falls back to test data. On a ledger
error it degrades `ledger_offset` to `null` with a `ledger_offset_note` — it
does **not** change which SQLite file it reads.

> **`--data-mode test` data is fabricated.** Every figure it serves is invented.
> The UI labels it `Data mode: TEST` for exactly this reason — never present a
> test-mode number as ledger data.

**Real mode needs a secret you must request.** `C8_CLIENT_SECRET` is issued by
the Cantor8 team; it is not in this repository and never should be.

## Verification

One command runs everything:

```bash
bash scripts/check_all.sh
```

It keeps going after a failure (so a lint error cannot hide a broken test) and
prints a PASS/WARN/FAIL summary. `SKIP_FRONTEND=1` skips the Node steps.

A clean run ends with:

```
══ summary ══
  PASS  compile (python)
  PASS  tests (pytest)
  PASS  frontend lint
  PASS  frontend tests
  PASS  frontend build
  WARN  cantor8 summary (no C8_CLIENT_SECRET)

All required checks passed.
```

That `WARN` is expected on a clone with no secret configured — the live-ledger
check is advisory and does not fail the script. Everything else is fully
offline.

The individual commands, if you prefer to run them one at a time:

```bash
python -m compileall src scripts tests      # syntax
python -m pytest -q                          # backend tests (offline)
python -m unittest discover -s tests         # same suite, no pytest needed
python scripts/check_cantor8.py --summary    # live ledger; needs the secret

cd scanner-frontend
npm install
npm run lint
npm run test:run                             # vitest, offline
npm run build
```

### End-to-end render check (needs a running backend)

The frontend suite is offline by default. To assert that data actually reaches
the DOM — the check that would have caught both integration bugs, since each
returned HTTP 200 while rendering nothing:

```bash
# terminal 1
python scripts/serve_api.py --data-mode test --port 8790
# terminal 2
cd scanner-frontend
VITE_API_BASE=http://127.0.0.1:8790 npx vitest run src/e2e.render.test.jsx
```

It mounts the real `Dashboard` and `Status` components against the real server
and asserts the party list, the 100/80/locked balance split, the transfer rows,
the stale badge, the metrics panel and the data-mode chip all render. Without
`VITE_API_BASE` set it skips itself, so `npm run test:run` and CI stay offline.

## The nine features, and where each one lives

| # | Feature | Backend | Route | Frontend |
|---|---|---|---|---|
| 1 | Wallet balance | `ScannerDB.get_balance` | `GET /tokens/balance/{party}` | Balance cards, `Dashboard.jsx` |
| 2 | Spendable vs locked | `get_balance` (`total`/`spendable`), `get_holdings_raw` (`locked`) | same, plus `/tokens/holdings/{party}` | "N spendable" line + "N locked" chip per card |
| 3 | Known parties | `get_parties` | `GET /parties` | Topbar party selector |
| 4 | Transfer history | `get_transfers` | `GET /tokens/transfers/{party}` | Transfers panel, with instrument/direction filters |
| 5 | SQLite persistence | `ScannerDB` + WAL | — | — (the file is the state; it survives restarts) |
| 6 | Resume after restart | `get_offset` / `save_offset`, `Indexer`'s seed-vs-catch-up branch | — | — (backend only) |
| 7 | Health / status | `get_health` (+ `data_mode`) | `GET /health` | `/status` page, and the topbar status chip |
| 8 | Stale / pending transfers | `get_stale_transfers`, `DEFAULT_STALE_SECONDS` | `GET /tokens/transfers/stale` | "stale pending" badge on a row; full list on `/status` |
| 9 | Metrics | `get_metrics` | `GET /metrics` | Scanner-metrics panel on `/dashboard`, and on `/status` |

## Stack & design rationale

Why SQLite and why WAL, why a scanner has to exist on Canton at all, why the
data flows scanner → DB → API → frontend rather than the browser calling
Cantor8, why the nine features are ordered as they are, and what is honestly
mocked or limited:

**→ [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

## Deployment — what this is, and what it is not

**This is a local-demo architecture.** It is not a deployment target, and this
section will not pretend otherwise. As shipped:

- the API binds `127.0.0.1` and has **no authentication**, no TLS and no rate
  limiting;
- it sends `Access-Control-Allow-Origin: *` on every response so any localhost
  origin can call it during a demo;
- state is a **single SQLite file** written by exactly one process;
- the frontend is a static bundle (`npm run build` → `dist/`) that any static
  host can serve, but it is useless without an API it can reach.

Making this network-facing is not a configuration change. What it would take —
auth, an allow-listed CORS origin, TLS and a real WSGI/ASGI server, moving off
a single-writer SQLite file, and a retention policy for `ledger_events` — is
spelled out in
[docs/ARCHITECTURE.md § What this is not](docs/ARCHITECTURE.md#what-this-is-not-and-what-it-would-take).
None of it is implemented.

## Safety — what this tool will never do on its own

Under **any** flag, the diagnostic will never:

- send a real transfer,
- allocate a party,
- grant permissions,
- accept, reject, or withdraw a transfer offer,
- spend or lock assets,
- call `POST /v2/commands/submit-and-wait`.

Each of those is shown as `EXPECTED MANUAL ACTION`. Moving value is always a
separate, explicitly-invoked step that requires human approval, and it never
runs in CI. Even `--preview-transfer` only *analyses* a transfer and prints
`NOTHING WAS SUBMITTED.`

## More docs

- [docs/TOOLKIT.md](docs/TOOLKIT.md) — the original Canton hackathon toolkit
  guide (the `c8lab.py` scratch tool and the six-step lab).
- [docs/API_INTEGRATION.md](docs/API_INTEGRATION.md) — package design and the
  `ScannerDB` database schema (the single source of truth for the tables).
- [docs/ENDPOINT_DATA_MAP.md](docs/ENDPOINT_DATA_MAP.md) — every Cantor8 endpoint
  Scandex reads, plus the local API Scandex serves to its own frontend.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — why the stack is shaped this
  way: SQLite + WAL, why a scanner is necessary on Canton, the feature
  ordering, and what is honestly mocked or limited.
- [scanner-frontend/README.md](scanner-frontend/README.md) — the dashboard:
  `VITE_API_BASE`, its routes, and its test setup.
- [API.md](API.md) — tested endpoint cheat sheet.
- [SETUP.md](SETUP.md) — LocalNet + Daml toolchain setup.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — every real error and its fix.
- [CHALLENGES.md](CHALLENGES.md) — the hackathon tracks (A1 "Build a scanner"
  feeds this work).
