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
  store.py        ScannerDB: THE database class - schema, the write side the
                  indexer drives, and the read side the API and CLI call
  indexer.py      A1 scanner: seed the ACS, poll /v2/updates/trees forward
  webapi.py       the local JSON API this repo serves to its own frontend
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

One database class, one schema: **`ScannerDB`** in
[`store.py`](../src/scandex_api/store.py). This is the single source of truth
for the shape below, and the interface the frontend codes against by name:

```python
from scandex_api.store import ScannerDB      # note the package path
db = ScannerDB("scandex.db")
db.get_balance(party_id)                     # not a bare `import store`
```

History: an earlier `db.py` (class `Database`) held a second, parallel schema
for the same data. It has been **deleted**. Two of its properties were carried
forward into `ScannerDB` rather than dropped — the idempotent transfer insert
and the `transfers.status` column — both called out below. Nothing in the repo
imports `db.py` any more; if you have a `scandex.db` file from before the merge,
delete it (or call `ScannerDB.reset()`) and re-run the indexer. The schema is
versioned in `schema_version` so a stale file is identifiable; current version
is **3**.

SQLite, standard library only. WAL mode is enabled on open — the indexer writes
continuously while the HTTP API reads concurrently, and WAL is what keeps that
from throwing "database is locked".

### Read methods the local API is built on

These are the methods [`webapi.py`](../src/scandex_api/webapi.py) exposes as
HTTP routes. Signatures matter to the frontend, so they are listed here rather
than left to be rediscovered from the source:

```python
db.get_balance(party_id, instrument=None)
db.get_holdings_raw(party_id, active_only=True)
db.get_transfers(party_id, limit=50, instrument=None, direction=None)
db.get_stale_transfers(older_than_seconds=None)
db.get_owners(instrument=None)
db.get_parties()
db.get_health(current_ledger_offset=None)
db.get_metrics(current_ledger_offset=None)
db.get_offset()
```

`get_transfers`' `instrument` and `direction` (`'sent'` / `'received'` /
`None`) arguments filter **in SQL, before `LIMIT`**. That ordering is the
point: filtering an already-limited page in the client would answer "the newest
50 transfers, of which 3 are c8BTC" when the question was "the newest 50 c8BTC
transfers". The dashboard's instrument and direction controls map straight onto
these.

For the JSON envelopes these become on the wire, see
[ENDPOINT_DATA_MAP.md § Response envelopes](ENDPOINT_DATA_MAP.md#response-envelopes--what-the-frontend-unwraps).

### `checkpoint` — the restart bookmark

Exactly one row (`CHECK (id = 1)`).

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | always 1 |
| `last_offset` | TEXT NOT NULL | last ledger offset applied |
| `updated_at` | TEXT NOT NULL | ISO-8601 UTC |

`get_offset()` returning `None` is what makes `Indexer.run_once()` take the seed
path; anything else takes the catch-up path. That is the whole
resume-after-restart guarantee.

### `parties`

| column | type | notes |
|---|---|---|
| `party_id` | TEXT PK | full `hint::fingerprint` id |
| `display_name` | TEXT NULL | from `/v2/parties` metadata when available |
| `is_local` | INTEGER NOT NULL | 1 if this node can submit for it |
| `first_seen_at` | TEXT NOT NULL | ISO-8601 UTC |
| `last_seen_at` | TEXT NOT NULL | refreshed on every sighting |

Filled by the indexer from two places: `_seed` (every followed party, with
`/v2/parties` metadata) and `_apply_tree` (any `owner` / `sender` / `receiver`
discovered in a transaction — a counterparty is often not a party you follow).
`save_party` never clobbers a known `display_name` with `None`.

### `holdings` — current state; a UTXO set, not a number

| column | type | notes |
|---|---|---|
| `contract_id` | TEXT PK | the holding contract |
| `party_id` | TEXT NOT NULL | owner |
| `amount` | TEXT NOT NULL | kept as a string, not a float |
| `instrument` | TEXT NOT NULL | e.g. `Amulet`, `c8BTC` |
| `admin` | TEXT NOT NULL | issuer party; DSO for Amulet |
| `locked` | INTEGER NOT NULL | 1 if escrowed/locked |
| `active` | INTEGER NOT NULL | 0 once archived; the row is kept |
| `created_at_offset` | TEXT NULL | offset the create was seen at |
| `archived_at_offset` | TEXT NULL | null while active |
| `created_at` | TEXT NULL | ISO-8601 UTC |
| `archived_at` | TEXT NULL | ISO-8601 UTC |

Indexes: `(party_id, active)`, `(instrument, active)`. A balance is a `SUM` over
`active = 1`; **spendable** excludes `locked = 1`. Archived rows are never
deleted, so balance history can be reconstructed.

### `ledger_events` — the audit trail

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `ledger_offset` | TEXT NOT NULL | |
| `event_type` | TEXT NOT NULL | `created` / `archived` |
| `contract_id` | TEXT NOT NULL | |
| `template_id` | TEXT NULL | the Holding or TransferInstruction interface id |
| `party_id` | TEXT NULL | |
| `recorded_at` | TEXT NOT NULL | ISO-8601 UTC |
| `raw_data` | TEXT NULL | JSON, **redacted** before storage |

Indexes: `(ledger_offset)`, `(contract_id)`, `(party_id)`. `get_balance_history`
replays these chronologically. Honest limitation: history starts when the
scanner first ran, not at the beginning of the ledger.

### `transfers` — history, offers, and stale detection

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `update_id` | TEXT NULL | ledger update id |
| `contract_id` | TEXT NULL | the holding leg, or the TransferInstruction offer |
| `sender` | TEXT NULL | null on a `credit` leg |
| `receiver` | TEXT NULL | null on a `debit` leg |
| `amount` | TEXT NULL | string, not float |
| `instrument` | TEXT NULL | |
| `transfer_kind` | TEXT NULL | `credit` / `debit` / `offer` / `direct` |
| `status` | TEXT NOT NULL | default `settled`; see below — **ported from `db.py`** |
| `source` | TEXT NULL | `ledger` — **ported from `db.py`** |
| `scanner_delay_secs` | REAL NULL | **ported from `db.py`**; reserved for scanner-sourced rows |
| `ledger_offset` | TEXT NULL | |
| `recorded_at` | TEXT NOT NULL | ISO-8601 UTC; drives staleness |

Indexes: `(sender)`, `(receiver)`, `(update_id)`, `(status)`, `(contract_id)`.

**`status`** is one of `settled` (an applied Holding movement), `pending` (an
open `TransferInstruction` offer), or `resolved` (that offer was archived). The
schema also permits `withdrawn` / `rejected`, but the indexer does not currently
emit them — see the limitation under *TransferInstruction handling* below.

**Idempotent inserts (ported from `db.py`, then fixed).** Two constraints,
because one was not enough:

1. `UNIQUE(update_id, sender, receiver, instrument, amount)` — the constraint
   `db.py` had, carried forward verbatim.
2. `idx_transfers_dedupe`, a unique index over the same identity with NULLs
   coalesced to `''` and `contract_id` added.

The second exists because SQLite treats NULLs as **distinct** in a UNIQUE index,
so constraint 1 never fired for the indexer's per-leg rows — which deliberately
carry a NULL `sender` (a credit) or a NULL `receiver` (a debit). Replaying an
already-processed offset range, exactly what happens after a crash between
recording a transfer and saving the checkpoint, therefore double-counted every
leg. `contract_id` is in the key so that closing that hole does not create the
opposite bug: one update legitimately crediting a party twice for the same
amount is two contracts and must stay two rows.

`save_transfer` swallows the resulting `IntegrityError` and returns `False` for
"not newly inserted", the same contract `db.py`'s `insert_transfer` had.

### `schema_version`

| column | type | notes |
|---|---|---|
| `version` | INTEGER PK | currently `3` |

### Dropped from `db.py`, deliberately

`instruments`, `contracts`, `transfer_offers`, `service_health`,
`ledger_offsets` and `raw_api_responses` are gone. `ledger_offsets` is replaced
by `checkpoint` (one stream, one bookmark). `transfer_offers` is replaced by
`transfers` rows with `transfer_kind='offer'`, so an offer and its settlement
live in one table and one query. The rest were never written by anything — an
empty schema is worse than no schema, because it reads as a feature. Re-add
them when something actually fills them.

## TransferInstruction handling and stale transfers

The A1 scanner used to poll `/v2/updates/trees` with a Holding-only interface
filter, which meant offer contracts never appeared in the trees at all. The
filter now carries **both** the Holding and the TransferInstruction interface
filters, so:

- a **created** `TransferInstruction` becomes a `transfers` row with
  `transfer_kind='offer'`, `status='pending'`, and the offer's contract id;
- an **archived** `TransferInstruction` flips that row to `status='resolved'`.

**Limitation, stated plainly:** the archive event alone does not reliably say
whether the offer was accepted, rejected or withdrawn. Rather than guess, the
indexer writes `resolved`. Distinguishing the three needs the exercised choice
name (`TransferInstruction_Accept` / `_Reject` / `_Withdraw`) read out of the
tree, which is a follow-up.

`get_stale_transfers(older_than_seconds)` returns every row still `pending`
whose `recorded_at` is older than the threshold — the "nobody notices until a
user complains" problem from `CHALLENGES.md` (A2). The threshold is a
parameter, not a constant: `ScannerDB(path, stale_seconds=...)`,
`--stale-seconds` on both CLIs, or `?older_than_seconds=` on the endpoint. The
count also surfaces in `get_health()` and `get_metrics()` as
`stale_pending_transfers`.

> **NOT VERIFIED LIVE.** The `TRANSFER_INSTRUCTION_INTERFACE` id in
> [`ledger.py`](../src/scandex_api/ledger.py) follows the same naming pattern as
> the confirmed Holding interface id, but was **not** confirmed against a live
> DevNet response — no `C8_CLIENT_SECRET` was available in the environment that
> wrote this. The same applies to the interface view's field layout
> (`transfer.sender` / `receiver` / `amount` / `instrumentId`), which is read
> defensively. Both carry `# TODO: verify against DevNet` comments. Check them
> before demoing offer detection.

## The transfer record shape: per-leg, not one row

A settled transfer is recorded as **one row per leg** (`credit` / `debit`), not
as a single `direct` row. Alice sending Bob 25 out of a 50 holding is three rows
sharing an `update_id`: a `debit` of 50 from Alice (her holding archived), a
`credit` of 25 to Bob, and a `credit` of 25 back to Alice as change.

That is deliberate, and it is a real trade-off:

- **For it:** it is what the ledger actually shows. A holding create/archive is
  the event we observe; a "transfer" is our interpretation of several of them.
  Per-leg rows also make partial application after a crash detectable — you can
  see that one leg landed and another did not.
- **Against it:** a frontend wanting "Alice → Bob, 25 Amulet" has to reassemble
  it. `get_transfer_detail(update_id)` returns every leg of one transaction for
  exactly that purpose.

`transfer_kind='offer'` rows are the exception: a `TransferInstruction` really
is a single sender→receiver record, so it is stored as one row.

## Normalise vs retain verbatim

- **Normalise** the things you query and join on: parties, holdings (amount,
  instrument, locked), transfers, and the checkpoint offset. Instrument
  metadata (decimals, issuer) is not yet normalised - the registry is not
  wired into the indexer, so `holdings.instrument` carries the id only.
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
