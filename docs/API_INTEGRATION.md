# API integration & database design

How the `scandex_api` package is put together, and the database Scandex should
keep behind it. Pairs with [ENDPOINT_DATA_MAP.md](ENDPOINT_DATA_MAP.md), which
lists every endpoint and the fields it yields.

## The package, one concern per module

```
src/scandex_api/
  config.py       env + .env loading, validation, frozen Config
  redaction.py    the redact() backstop; masks secrets/tokens/JWTs everywhere
  errors.py       typed errors (ConfigError, AuthError, HttpError, ...)
  http.py         urllib transport: timeouts, retries, JSON, status capture
  auth.py         Keycloak client-credentials + in-memory token cache
  ledger.py       Canton JSON Ledger API v2 (read-only for diagnostics)
  registry.py     token standard registry (transfer preview only)
  scanner.py      scanner read API + public Scan API
  models.py       dataclasses: Party, Holding, Instrument, CheckResult, ...
  diagnostics.py  orchestrates checks, formats results, writes reports
  db.py           sqlite3 wrapper: schema, offsets, holdings, transfers
  indexer.py      A1 scanner: seed the ACS, poll /v2/updates/trees forward
  cli.py          argument parsing + human/JSON output (diagnostic + scanner)
```

Authentication, HTTP transport, per-service logic and report formatting are kept
strictly separate. `http.py` knows nothing about Cantor8; `auth.py` knows
nothing about the ledger; `diagnostics.py` sequences the clients but never talks
HTTP itself. This is what lets the whole thing be tested offline: tests inject a
fake transport at the `http.py` boundary and never touch the network.

The package is standalone. It does **not** import the root `c8lab.py`, and
`c8lab.py` does not import it. `c8lab.py` stays the quick manual scratch tool.

## Authority: which source is the truth

- The **Ledger API is authoritative** for current ledger state. When the ledger
  and the scanner disagree about a balance, the ledger wins.
- The **Scanner API is an indexed read model** and can lag. Its `/health`
  reports `scannerDelaySecs`; store that number with anything sourced from the
  scanner so a stale read is never mistaken for a current one.
- Every snapshot row records the **ledger offset** it was read at (`read_at_offset`).
  Read `ledger-end` first, query active contracts at that offset, and keep the
  two together so the snapshot is internally consistent.
- A local database copy is **not automatically public.** A node only sees what
  its parties are entitled to see. Re-publishing that data (an API, a dashboard)
  is a deliberate decision with privacy consequences — decide per party what is
  exposed.

## Schema

SQLite is fine for the demo (the A1 challenge suggests it) and is what
[`db.py`](../src/scandex_api/db.py) creates. Types below are SQLite-flavoured;
use the obvious equivalents on Postgres. Tables the A1 scanner actively writes
today are marked **[implemented]**; others are left as targets for the next
extension (e.g. adding an instruments cache once the registry is wired into
the indexer).

### `parties`  **[implemented]**
| column | type | notes |
|---|---|---|
| `party_id` | TEXT PRIMARY KEY | full `hint::fingerprint` id |
| `hint` | TEXT | the `word-word-n` prefix |
| `is_local` | INTEGER | 1 if this node can submit for it |
| `display_name` | TEXT NULL | |
| `first_seen` | TEXT | ISO-8601 UTC |
| `last_seen` | TEXT | ISO-8601 UTC |

Index: `(is_local)`.

### `instruments`
| column | type | notes |
|---|---|---|
| `instrument_id` | TEXT PRIMARY KEY | e.g. `Amulet`, `c8ETH` |
| `name` | TEXT NULL | |
| `administrator` | TEXT NULL | issuer party; DSO for Amulet |
| `decimals` | INTEGER NULL | needed to format amounts |
| `registry_base` | TEXT | which registry served it |
| `raw_json` | TEXT | verbatim metadata payload |

### `holdings`  (current state; a UTXO set, not a number)  **[implemented]**
| column | type | notes |
|---|---|---|
| `contract_id` | TEXT PRIMARY KEY | the holding contract |
| `party_id` | TEXT | FK → `parties` |
| `amount` | TEXT | keep as string/decimal, not float |
| `instrument_id` | TEXT | FK → `instruments` |
| `administrator` | TEXT | issuer party |
| `locked` | INTEGER | 1 if escrowed/locked |
| `lock_expiry` | TEXT NULL | when the lock lifts |
| `read_at_offset` | TEXT | ledger offset of the snapshot |
| `observed_at` | TEXT | ISO-8601 UTC |

Indexes: `(party_id, instrument_id)`, `(locked)`. Spendable = unlocked rows.

### `contracts`  (generic active-contract cache, optional)
| column | type | notes |
|---|---|---|
| `contract_id` | TEXT PRIMARY KEY | |
| `template_or_interface` | TEXT | id string |
| `party_id` | TEXT | witness party |
| `created_at_offset` | TEXT | |
| `archived_at_offset` | TEXT NULL | null while active |
| `payload_json` | TEXT | verbatim |

### `transfers`  (history)  **[implemented]**
| column | type | notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `update_id` | TEXT | ledger/scanner update id |
| `sender` | TEXT | party |
| `receiver` | TEXT | party |
| `instrument_id` | TEXT | |
| `amount` | TEXT | |
| `transfer_kind` | TEXT | `direct` / `offer` / `self` |
| `status` | TEXT | `settled` / `offered` / `accepted` / ... |
| `source` | TEXT | `ledger` or `scanner` |
| `scanner_delay_secs` | REAL NULL | recorded when `source='scanner'` |
| `observed_at` | TEXT | ISO-8601 UTC |

Indexes: `(sender)`, `(receiver)`, `(update_id)`.

### `transfer_offers`  (pending offers, until accepted/rejected/withdrawn)
| column | type | notes |
|---|---|---|
| `instruction_cid` | TEXT PRIMARY KEY | the TransferInstruction contract |
| `sender` | TEXT | |
| `receiver` | TEXT | |
| `instrument_id` | TEXT | |
| `amount` | TEXT | |
| `state` | TEXT | `pending` / `accepted` / `rejected` / `withdrawn` |
| `created_at_offset` | TEXT | |
| `observed_at` | TEXT | |

### `ledger_offsets`  (resume points)  **[implemented]**
| column | type | notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY | usually a single row per stream |
| `stream` | TEXT | e.g. `acs` / `updates` |
| `offset` | TEXT | last processed offset |
| `observed_at` | TEXT | |

The A1 scanner saves its offset here and resumes from it after a restart instead
of re-reading everything.

### `service_health`  (operational health, time series)  **[implemented]** (empty schema; the diagnostic writes ad-hoc, no rows persisted yet)
| column | type | notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `service` | TEXT | `ledger` / `registry` / `scanner` / `scan` |
| `status` | TEXT | |
| `db_status` | TEXT NULL | scanner only |
| `scanner_delay_secs` | REAL NULL | scanner only |
| `latency_ms` | REAL | |
| `observed_at` | TEXT | |

### `raw_api_responses`  (optional, for debugging)
| column | type | notes |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `endpoint` | TEXT | |
| `status_code` | INTEGER | |
| `body_json` | TEXT | **redacted** before storage |
| `observed_at` | TEXT | |

## Normalise vs retain verbatim

- **Normalise** the things you query and join on: parties, instruments (with
  decimals), holdings (amount, instrument, locked), transfers, offsets.
- **Retain verbatim** the payloads you cannot fully model yet: instrument
  metadata (`raw_json`), transfer-factory choice contexts and disclosed
  contracts, and anything you might need to replay. Store these as JSON text.
- **Never store raw tokens or secrets.** If you keep `raw_api_responses`, pass
  every body through `redaction.redact` first — the package already does this
  for its own reports and logs.

## Secret hygiene (enforced)

- `C8_CLIENT_SECRET` and any bearer token are never hard-coded, printed, logged,
  written into reports, committed, or embedded in fixtures.
- `redaction.redact` masks literal registered secrets, JWTs (`eyJ...`),
  `Bearer` values, and any `*_SECRET` / `*_TOKEN` / password key–value pair. All
  logging and report writing route through it.
- A CI step (`.github/workflows/tests.yml`) greps the tree for committed secrets
  (private keys, real env-file secret values, stray JWTs) and fails the build if
  any are found.
- Tests assert that a secret and a JWT-shaped string never survive into log
  output or a serialized report.
