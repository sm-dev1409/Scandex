# Endpoint & data map

Every significant Cantor8 endpoint Scandex reads, what question it answers, the
data it yields, and how that maps onto a Scandex database. This is the catalogue
the diagnostic checks against; `python scripts/check_cantor8.py --write-report`
emits the live PASS/FAIL status of these same endpoints alongside it.

Two directions are catalogued here, and they are easy to confuse because some
route names coincide:

- **Inbound (most of this file):** what Scandex *reads from Cantor8* - the
  Ledger API, the token registry, Cantor8's scanner, and the public Scan API.
- **Outbound (the "Local API" section):** what Scandex *serves to its own
  frontend* on localhost, out of the database the indexer filled.

**Nothing here is a claim of live coverage.** Endpoints the diagnostic does not
exercise are labelled. Write and streaming endpoints are catalogued but **never
executed** by the diagnostic — see the "Not tested / manual only" section.

Legend for **Data kind**: `state` = current ledger state · `history` = time
series · `metadata` = configuration/reference · `health` = operational health.

Legend for **Demo need**: `required` = the demo does not work without it ·
`useful` = materially better with it · `optional` = nice to have.

---

## Ledger API — `C8_BASE` (Canton JSON Ledger API v2)

The Ledger API is the **authoritative** source for current ledger state. A local
database is a cache of what this node is entitled to see, never the source of
truth.

### `GET /v2/state/ledger-end`
- **Auth:** Keycloak token. **Demo need:** required. **Data kind:** state/health.
- **Question it answers:** "Is the ledger reachable, and what is the current
  offset to read a consistent snapshot at?"
- **Important response fields:** `offset`.
- **Table written:** `checkpoint` (one row: the resume bookmark).
- **Fields:** `last_offset`, `updated_at`.
- **Future Scandex feature:** connectivity check; the resume point for an
  indexer (save the offset, restart from it, don't re-read everything).
- **Why selected:** cheapest health check and the anchor for every consistent
  read.
- **Privacy / permission:** offset is not sensitive; the call still needs a
  valid token.
- **Known limitations:** an offset alone tells you nothing about *what* changed.

### `GET /v2/parties`
- **Auth:** token. **Demo need:** useful. **Data kind:** metadata/state.
- **Question:** "Which parties does this node know, and which can it submit for?"
- **Important response fields:** `party`, `isLocal`, display name / annotations.
- **Proposed table(s):** `parties`.
- **Proposed fields:** `party_id` (PK), `hint`, `is_local`, `display_name`,
  `first_seen`, `last_seen`.
- **Future feature:** account setup, party selection UI, permission diagnostics.
- **Why selected:** you must know which parties are local before you try to read
  or submit for them.
- **Privacy / permission:** a node lists parties it has *heard about*, including
  remote ones it cannot act for. Listing a party is not the same as being able
  to see its contracts.
- **Known limitations:** only `isLocal: true` parties can submit; a remote one
  gives `NO_SYNCHRONIZER_ON_WHICH_ALL_SUBMITTERS_CAN_SUBMIT`.

### `POST /v2/state/active-contracts`  (holdings via the Holding interface)
- **Auth:** token. **Demo need:** required. **Data kind:** state.
- **Question:** "What does a party hold right now, and how much is spendable?"
- **Important response fields:** contract id, template/interface id, amount,
  `instrumentId.id`, `instrumentId.admin`, `lock` (present ⇒ locked), lock
  `expiresAt`, created event info.
- **Table written:** `holdings`.
- **Fields:** `contract_id` (PK), `party_id`, `amount`, `instrument`, `admin`,
  `locked`, `active`, `created_at_offset`, `archived_at_offset`, `created_at`,
  `archived_at`. (`lock_expiry` is read from the interface view but not yet
  persisted - only the boolean `locked` is.)
- **Future feature:** portfolio view, spendable-balance calculation, asset list,
  the A1 scanner's balance layer.
- **Why selected:** this is the balance. Everything Scandex shows about holdings
  comes from here.
- **Privacy / permission:** you only see contracts your parties are entitled to;
  a 403 for another party's holdings is expected, not an error.
- **Known limitations & traps:**
  - `Holding` is a Daml **interface**. Query it with an `InterfaceFilter` and
    `includeInterfaceView: true` and id
    `#splice-api-token-holding-v1:Splice.Api.Token.HoldingV1:Holding`. A
    `TemplateFilter` returns `200 OK` with `[]` — indistinguishable from a zero
    balance.
  - Always read `ledger-end` first and query **at that offset**; store the
    offset with the snapshot.
  - Locked holdings show in the balance but cannot be spent until the lock
    expires — exclude them from *spendable*.

### `POST /v2/commands/submit-and-wait`  — CATALOGUE ONLY, NEVER EXECUTED
- **Auth:** token + act-as rights. **Demo need:** optional (write path).
- **Question:** "Commit a command (a transfer, an accept) and block until done."
- **Important response fields:** transaction id, completion status, errors.
- **Proposed table(s):** `transfers` — though a scanner never needs this: the
  indexer records a transfer when it *observes* the resulting events on the
  ledger, not when a command is submitted.
- **Future feature:** actually moving value — a separate, human-approved action.
- **Why catalogued:** so the shape is documented and a test asserts the
  diagnostic never calls it.
- **Privacy / permission:** writes require act-as rights on the submitting party.
- **Known limitations:** the diagnostic must never run this. It is surfaced as
  `EXPECTED MANUAL ACTION`.

### `POST /v2/updates/trees`  — IMPLEMENTED (A1 scanner)
- **Auth:** token. **Demo need:** useful. **Data kind:** history/state.
- **Question:** "Give me every transaction tree in `(beginExclusive, endInclusive]`."
- **Important response fields:** per update, `TransactionTree.updateId`,
  `TransactionTree.offset`, `TransactionTree.eventsById` (a map of node-id →
  `CreatedTreeEvent` / `ExercisedTreeEvent`).
- **Tables written:** `checkpoint`, `holdings`, `transfers`, `ledger_events`,
  `parties`.
- **Feature backed:** [A1 scanner](#a1-scanner-implementation) — reads the ACS
  first for the present, then polls this endpoint forward from that same
  offset to stay current. The offset is checkpointed per update so a kill
  and restart resumes cleanly with no gap and no re-seed.
- **Why POST-batch, not WebSocket:** the v2 API exposes `/v2/updates/trees`
  both as a WebSocket stream (open-ended) and as an HTTP POST batch when
  `endInclusive` is set. The batch shape is exactly what a polling indexer
  wants and keeps the toolkit standard-library-only. See
  `LedgerClient.updates` in [`ledger.py`](../src/scandex_api/ledger.py) for
  the wire body and the tradeoff.
- **Known traps (all A1's):**
  - Transactions are trees, not flat lists — walk `eventsById`.
  - `Holding` is an interface; use the `InterfaceFilter` here too or created
    Holdings arrive with no view.
  - Streaming from the current end only gives you the future; seed from the
    ACS first.

### Setup / mutation endpoints — MANUAL ONLY
- `POST /v2/parties` (allocate a party) and
  `POST /v2/users/{userId}/rights` (grant `CanActAs`, the 403 fix).
- **Auth:** admin token. **Demo need:** optional. **Data kind:** metadata.
- **Why catalogued, never auto-run:** they change topology / permissions. A test
  asserts the diagnostic never POSTs to `/v2/parties`.

---

## Token standard registry — `C8_REGISTRY`

Send a `Host` header when `C8_REGISTRY_HOST` is set. Most `/registry/...`
endpoints are public (no token).

### `GET /registry/metadata/v1/info`
- **Auth:** none. **Demo need:** useful. **Data kind:** metadata.
- **Question:** "Who administers this registry, and what does it support?"
- **Important response fields:** admin party, supported APIs/features, version.
- **Proposed table(s):** none yet - no registry metadata is persisted. Would
  need a new `registries` table; `ScannerDB` has no equivalent today.
- **Future feature:** knowing which registry serves which token.
- **Privacy / permission:** public network metadata.
- **Known limitations:** describes one registry; the network has several.

### `GET /registry/metadata/v1/instruments`
- **Auth:** none. **Demo need:** useful. **Data kind:** metadata.
- **Question:** "Which tokens does this registry serve, and with what decimals?"
- **Important response fields:** instrument id, name, administrator, decimals.
- **Proposed table(s):** none yet - `ScannerDB` stores only the instrument id
  on each holding. Would need a new `instruments` table (`instrument` (PK),
  `name`, `administrator`, `decimals`, `registry_base`, `raw_json`) before
  amounts of any token but Amulet can be formatted correctly.
- **Future feature:** correct formatting of any token, not just Amulet; stops
  Scandex assuming every asset is Canton Coin.
- **Why selected:** decimals and admin are needed to display and to build
  transfers correctly.
- **Privacy / permission:** public.
- **Known limitations:** different tokens have different registries. Canton Coin
  (`Amulet`) is served by the scan app / sv-proxy with the **DSO** as admin;
  Cantor8's own tokens (`c8ETH`, `c8BTC`) live under the token-factory registry
  with **their own admin party**, not the DSO.

### `GET /registry/metadata/v1/instruments/{id}`
- **Auth:** none. **Demo need:** useful. **Data kind:** metadata.
- **Question:** "Full detail for one instrument."
- **Maps to:** `instruments` (same row, enriched).

### `POST /registry/transfer-instruction/v1/transfer-factory` — PREVIEW ONLY
- **Auth:** none (public). **Demo need:** useful. **Data kind:** metadata.
- **Question:** "For a proposed transfer, what kind is it and what context is
  needed?"
- **Important response fields:** `factoryId`, `transferKind`
  (`direct`/`offer`/`self`), `choiceContext` (`choiceContextData`,
  `disclosedContracts`).
- **Proposed table(s):** `transfers` with `transfer_kind='offer'` and
  `status='pending'` - the same rows the indexer already writes when it sees a
  `TransferInstruction` created on the ledger. (Preview is read-only and
  persists nothing today; there is no `raw_api_responses` table.)
- **Future feature:** the transfer preview screen — tell the user *before* they
  sign whether money moves now (`direct`) or waits for acceptance (`offer`).
- **Why selected:** it answers the single most confusing question in a Canton
  transfer without moving anything.
- **Privacy / permission:** the registry hands you the issuer's config as
  **disclosed contracts**, valid for one transaction, because you cannot see
  them directly.
- **Known limitations:** the diagnostic **previews only** and never exercises
  the returned factory (that would move value). Shape trap: in the choice-context
  requests `meta` is a **flat** map — `{"meta": {}}`, not `{"meta": {"values": {}}}`.

### `POST /registry/transfer-instruction/v1/{id}/choice-contexts/{accept,reject,withdraw}` — MANUAL ONLY
- **Auth:** none. **Demo need:** optional. Catalogued and implemented; never
  called automatically because they belong to the write path.

---

## A1 scanner implementation

The tables above marked "IMPLEMENTED" are populated by
[`scandex_api.indexer`](../src/scandex_api/indexer.py), a stdlib-only scanner
that satisfies challenge A1. Persistence goes through
[`ScannerDB`](../src/scandex_api/store.py) — see
[API_INTEGRATION.md](API_INTEGRATION.md) for the full schema.

1. On first run for a party, `Indexer.run_once` calls `LedgerClient.ledger_end`
   and then `LedgerClient.holdings(party)` — seeding `holdings`, registering the
   party in `parties`, and recording the offset in the `checkpoint` table.
2. On every run after that, it reads `ledger_end` again, then
   `LedgerClient.updates(begin_exclusive=saved, end_inclusive=current)` and
   walks each transaction tree. `Holding` created/archived events go into
   `holdings` and append `credit` / `debit` rows to `transfers`; a
   `TransferInstruction` created event appends an `offer` row with
   `status='pending'`, and its archive flips that row to `resolved`. Replays are
   idempotent (see the two transfer constraints in API_INTEGRATION.md). The
   offset is saved per applied update.
3. A restart with a non-null `checkpoint` row **never** re-reads the ACS.

`--index`, `--balance`, and `--history` on
[`scripts/check_cantor8.py`](../scripts/check_cantor8.py) drive it.

---

## Local API — what Scandex *serves*, not what it reads

> **Read this heading carefully — it is the opposite direction from every other
> section in this file.** Everything above and below catalogues endpoints
> Scandex **calls on Cantor8** (ledger, registry, scanner, public scan). This
> section is the small JSON API Scandex **exposes on localhost** to its own
> frontend, out of the database the indexer filled.
>
> The route names deliberately echo Cantor8's own scanner read API
> (`/health`, `/tokens/balance/{party}`, `/tokens/transfers/{party}`). They are
> **not the same service**: Cantor8's scanner lives at `C8_SCANNER_BASE`, needs
> a token, and is not provisioned for hackathon credentials; ours lives at
> `http://127.0.0.1:8787`, needs no auth, and answers from our own SQLite file.
> If a balance looks wrong, check which one you called.

Served by [`scandex_api.webapi`](../src/scandex_api/webapi.py) —
`http.server` only, no Flask/FastAPI, matching the package's zero-runtime-
dependency rule. Start it with any of:

```bash
python scripts/serve_api.py --db scandex.db --port 8787
python scripts/serve_api.py --data-mode test          # fabricated demo data, offline
python scripts/check_cantor8.py --serve --db scandex.db --port 8787
serve-scandex-api --db scandex.db          # if pip-installed
```

`--data-mode` selects the dataset: `real` (default) serves whatever the indexer
wrote to `--db` (default `scandex.db`); `test` seeds and serves a deterministic
fabricated dataset from [`demo_data.py`](../src/scandex_api/demo_data.py)
(default `scandex-test.db`) and never constructs a ledger client at all. The
mode is reported back on `/health` and `/` — see `data_mode` below.

Read-only: no route writes to the database or to the ledger. Every response
carries `Access-Control-Allow-Origin: *` because this is a local demo server —
see the security note in `webapi.py` before reusing that anywhere else.

| Method | Path | Backed by | Feature | Notes |
|---|---|---|---|---|
| `GET` | `/health` | `get_health(ledger_end)` | P7 | offsets, counts, staleness, live drift, `data_mode` |
| `GET` | `/parties` | `get_parties()` | P3 | party selector |
| `GET` | `/tokens/balance/{party}` | `get_balance(party)` | P1, P2 | `?instrument=` filter; `total` vs `spendable` |
| `GET` | `/tokens/holdings/{party}` | `get_holdings_raw(party)` | P2 | individual "banknotes"; `?active_only=0` includes archived |
| `GET` | `/tokens/transfers/{party}` | `get_transfers(party)` | P4 | `?limit=` (default 50), `?instrument=`, `?direction=sent\|received`; newest first |
| `GET` | `/tokens/transfers/stale` | `get_stale_transfers()` | P8 | `?older_than_seconds=` overrides the threshold |
| `GET` | `/tokens/owners` | `get_owners()` | bonus | `?instrument=` filter |
| `GET` | `/metrics` | `get_metrics(ledger_end)` | P9 | per-instrument volume and locked totals |
| `GET` | `/` | — | — | lists the routes above, plus `dataMode` |

### Response envelopes — what the frontend unwraps

Every **list** route wraps its payload in an object rather than returning a
bare array, because the wrapper carries metadata the frontend uses:

| Route | Envelope | The list is under |
|---|---|---|
| `/parties` | `{"parties": [...]}` | `.parties` |
| `/tokens/balance/{party}` | `{"party": ..., "byInstrument": [...]}` | `.byInstrument` |
| `/tokens/holdings/{party}` | `{"party": ..., "activeOnly": ..., "holdings": [...]}` | `.holdings` |
| `/tokens/transfers/{party}` | `{"party": ..., "count": ..., "transfers": [...]}` | `.transfers` |
| `/tokens/transfers/stale` | `{"olderThanSeconds": ..., "count": ..., "transfers": [...]}` | `.transfers` |
| `/tokens/owners` | `{"owners": [...]}` | `.owners` |

`/health` and `/metrics` are the exception: they return their fields **flat**,
with no envelope, and are read as-is.

> This distinction is load-bearing. The dashboard originally tested
> `Array.isArray(wholeResponse)`, which is `false` for every envelope above, so
> it rendered "No parties" / "No holdings" / "No transfers" forever against a
> healthy backend returning HTTP 200. Unwrapping now lives in one place,
> [`scanner-frontend/src/api.js`](../scanner-frontend/src/api.js), and the
> shapes are pinned by `tests/test_webapi_contract.py`. **If you flatten a route
> to a bare array, change all three: the route, `api.js`, and this table.**

### `data_mode`

`/health` carries `data_mode` and `/` carries `dataMode`, each `"test"` or
`"real"`. It is reported by the server so a client never has to infer which
dataset it is looking at from the port number. `"test"` means every figure in
the response is fabricated demo data, not ledger data; the frontend surfaces it
as a `Data mode: TEST` chip and a banner on `/status`.

Error contract:

- unknown route → `404` with `{"error": "no such route: ...", "routes": [...]}`
- party the indexer has never seen → `404` with
  `{"error": "unknown party: ...", "hint": "..."}`
- party that is known but holds nothing yet → `200` with an **empty list**, not
  an error. A new party with no activity is a normal state, not a failure.
- an unparseable query parameter falls back to its default rather than `500`

`/health` and `/metrics` call `LedgerClient.ledger_end()` on each request behind
a short timeout, so `ledger_offset` and `scanner_delay_offsets` reflect real
drift. If the ledger is unreachable — or no `C8_CLIENT_SECRET` is set — both
fields come back `null` with a `ledger_offset_note` saying why, and every
locally-computed number in the response stays correct. The API deliberately
stays up when DevNet is down.

`scanner_delay_offsets` is a difference **in offsets, not seconds**, and is
`null` whenever either offset is not numeric on this deployment — an opaque
string offset reports unknown rather than a nonsense subtraction.

---

## Scanner read API — `C8_SCANNER_BASE`  (reference-only for A1)

The Cantor8 scanner-ledger-read-api is Cantor8's own reference implementation
of challenge A1 ("Build a scanner"). Hackathon credentials are not provisioned
against it — data endpoints return `401` for the participant token, and that
is expected, not a bug. `scanner.py` still hits `/health` (open) and probes
`/tokens/balance/{party}` as a best-effort call that degrades gracefully; the
authoritative balance/history for our own party comes from the local
`scandex_api.indexer` reading the Ledger API directly.


### `GET /health`
- **Auth:** none. **Demo need:** useful. **Data kind:** health.
- **Question:** "Is the index up, is its database healthy, and how far behind is
  it?"
- **Important response fields:** `status`, `db.status`, `db.scannerDelaySecs`.
- **Proposed table(s):** none yet - health is computed on demand by
  `ScannerDB.get_health()` and not stored as a time series. Persisting it would
  need a new `service_health` table (`service`, `status`, `db_status`,
  `scanner_delay_secs`, `recorded_at`).
- **Future feature:** a freshness banner; a rule that anything read from the
  scanner is stored with the delay it was read at.
- **Why selected:** the scanner is a read model and *will* lag; you must record
  by how much.
- **Known limitations:** health being OK says nothing about entitlement to data.

### `GET /tokens/balance/{party}`, `/tokens/balance-history/{party}`
- **Auth:** token. **Demo need:** optional/useful. **Data kind:** state/history.
- **Question:** "Indexed balance / balance over time for a party."
- **Important response fields:** amounts per instrument, timestamps.
- **Proposed table(s):** `holdings` (cross-check), `balance_history`.
- **Future feature:** balance charts without walking the ledger yourself.
- **Privacy / permission:** **401 = "who are you"** (no/invalid token);
  **403 = "not yours / m2m only"** (valid token, no rights). Different fixes.
- **Known limitations:** lags the ledger by `scannerDelaySecs`; the Ledger API
  is authoritative when they disagree.

### `GET /tokens/transfers/{party}`, `/tokens/transfers/history/{party}`
- **Auth:** token. **Demo need:** useful. **Data kind:** history.
- **Question:** "Transfers involving a party, and the unified history."
- **Important response fields:** transfer records, counterparties, amounts,
  timestamps, update ids.
- **Proposed table(s):** `transfers`.
- **Future feature:** the activity feed / transfer history in A1.
- **Privacy / permission:** same 401 vs 403 distinction.
- **Known limitations:** indexed; reconcile against the ledger for correctness.

### `GET /contracts/active`
- **Auth:** token. **Demo need:** optional. **Data kind:** state.
- **Question:** "Active contracts as the index sees them."
- **Maps to:** `contracts` (cross-check against the ACS).

---

## Public Scan API — `C8_SCAN_BASE` (sv-proxy, no auth)

### `GET /api/scan/v0/scans`
- **Auth:** none. **Demo need:** optional. **Data kind:** metadata.
- **Question:** "Every scan node on the network."
- **Maps to:** nothing persisted today; would need a `scan_nodes` reference
  table.

### `GET /api/scan/v0/splice-instance-names`
- **Auth:** none. **Demo need:** optional. **Data kind:** metadata.
- **Question:** "Network name and branding."
- **Important response fields:** `network_name`, favicon/branding.
- **Future feature:** label the UI with the real network name; a zero-credential
  first call to prove connectivity.

### `POST /api/scan/v0/amulet-rules`, `POST /api/scan/v0/open-and-issuing-mining-rounds`
- **Auth:** none. **Demo need:** optional. **Data kind:** metadata.
- **Note:** these are **POST-only**. A `GET` returns `405` — a wrong-verb
  signal, not a failure. The scanner client uses the correct verb.

---

## Not tested / manual only (every run says so)

- `POST /v2/commands/submit-and-wait` — write path. Never executed by the
  diagnostic.
- `WS /v2/updates` — the WebSocket variant is not opened by the stdlib client;
  the A1 scanner uses `POST /v2/updates/trees` as a bounded batch instead
  (same events, no WS dependency). See the `POST /v2/updates/trees` entry.
- `POST /v2/parties`, `POST /v2/users/{userId}/rights`,
  registry `transfer-factory` **exercise**, and the accept/reject/withdraw
  choice contexts — all mutating; surfaced as `EXPECTED MANUAL ACTION`.

A green diagnostic run means the **read** paths are healthy. It never means the
system is fully tested.

---

See [API_INTEGRATION.md](API_INTEGRATION.md) for the full database schema and
the ledger-vs-scanner authority rules.
