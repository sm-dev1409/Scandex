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
  recommended database schema.
- [docs/ENDPOINT_DATA_MAP.md](docs/ENDPOINT_DATA_MAP.md) — every endpoint, what
  it answers, and how it maps to the database.
- [API.md](API.md) — tested endpoint cheat sheet.
- [SETUP.md](SETUP.md) — LocalNet + Daml toolchain setup.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — every real error and its fix.
- [CHALLENGES.md](CHALLENGES.md) — the hackathon tracks (A1 "Build a scanner"
  feeds this work).
