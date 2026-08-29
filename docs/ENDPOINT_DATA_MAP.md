# Endpoint & data map

Every significant Cantor8 endpoint Scandex reads, what question it answers, the
data it yields, and how that maps onto a Scandex database. This is the catalogue
the diagnostic checks against; `python scripts/check_cantor8.py --write-report`
emits the live PASS/FAIL status of these same endpoints alongside it.

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
- **Proposed table(s):** `ledger_offsets`.
- **Proposed fields:** `offset`, `observed_at`, `source='ledger'`.
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
- **Proposed table(s):** `holdings`, `contracts`.
- **Proposed fields (`holdings`):** `contract_id` (PK), `party_id`, `amount`,
  `instrument_id`, `administrator`, `locked`, `lock_expiry`, `read_at_offset`,
  `observed_at`.
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
- **Proposed table(s):** `transfers`, `raw_api_responses`.
- **Future feature:** actually moving value — a separate, human-approved action.
- **Why catalogued:** so the shape is documented and a test asserts the
  diagnostic never calls it.
- **Privacy / permission:** writes require act-as rights on the submitting party.
- **Known limitations:** the diagnostic must never run this. It is surfaced as
  `EXPECTED MANUAL ACTION`.

### `WS /v2/updates`  — DOCUMENTED, NOT EXERCISED
- **Auth:** token. **Demo need:** useful (future). **Data kind:** history/state.
- **Question:** "Stream every created/archived contract from an offset onward."
- **Important response fields:** created/archived events, offsets.
- **Proposed table(s):** `contracts`, `ledger_offsets`, `transfers`.
- **Future feature:** live balances and event-driven indexing (A1's stream
  layer): read the ACS first for the present, then stream forward from that
  same offset to stay current.
- **Why catalogued:** it is the natural next step for a live index; the stdlib
  client does not open a WebSocket.
- **Known limitations:** not tested by this toolkit; streaming from the current
  end gives you the future, not the past — seed from the ACS first.

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
- **Proposed table(s):** `service_health` (or a small `registries` table).
- **Future feature:** knowing which registry serves which token.
- **Privacy / permission:** public network metadata.
- **Known limitations:** describes one registry; the network has several.

### `GET /registry/metadata/v1/instruments`
- **Auth:** none. **Demo need:** useful. **Data kind:** metadata.
- **Question:** "Which tokens does this registry serve, and with what decimals?"
- **Important response fields:** instrument id, name, administrator, decimals.
- **Proposed table(s):** `instruments`.
- **Proposed fields:** `instrument_id` (PK), `name`, `administrator`,
  `decimals`, `registry_base`, `raw_json`.
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
- **Proposed table(s):** `transfer_offers` (when an offer results), plus
  `raw_api_responses` for the context.
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

## Scanner read API — `C8_SCANNER_BASE`  (indexed read model, can lag)

### `GET /health`
- **Auth:** none. **Demo need:** useful. **Data kind:** health.
- **Question:** "Is the index up, is its database healthy, and how far behind is
  it?"
- **Important response fields:** `status`, `db.status`, `db.scannerDelaySecs`.
- **Proposed table(s):** `service_health`.
- **Proposed fields:** `service`, `status`, `db_status`, `scanner_delay_secs`,
  `observed_at`.
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
- **Maps to:** `service_health` / a `scan_nodes` reference table.

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
- `WS /v2/updates` — streaming. Documented, not exercised by the stdlib client.
- `POST /v2/parties`, `POST /v2/users/{userId}/rights`,
  registry `transfer-factory` **exercise**, and the accept/reject/withdraw
  choice contexts — all mutating; surfaced as `EXPECTED MANUAL ACTION`.

A green diagnostic run means the **read** paths are healthy. It never means the
system is fully tested.

---

See [API_INTEGRATION.md](API_INTEGRATION.md) for the full database schema and
the ledger-vs-scanner authority rules.
