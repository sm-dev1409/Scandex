# Architecture & design rationale

Why Scandex is built the way it is. This is the "why", not the "how" — for the
schema see [API_INTEGRATION.md](API_INTEGRATION.md), for the endpoints see
[ENDPOINT_DATA_MAP.md](ENDPOINT_DATA_MAP.md), and for running it see the
[root README](../README.md#running-the-full-stack).

Nothing here is invented justification. Each section points at the code or the
challenge text it comes from.

## The shape

```
   Cantor8 DevNet                  one machine, three processes
 ┌────────────────────┐   ┌──────────────────────────────────────────────┐
 │ Canton JSON        │   │                                              │
 │ Ledger API v2      │   │  indexer.py ──write──▶ scandex.db ◀──read──  │
 │                    │   │  (check_cantor8       (SQLite,      webapi.py│
 │ /v2/state/         │◀──┼── --index --follow)    WAL mode)    (serve_  │
 │   active-contracts │   │                                      api.py) │
 │ /v2/updates/trees  │   │                                         │    │
 │ /v2/parties        │   │                                    HTTP │    │
 └────────────────────┘   │                                         ▼    │
        read-only         │                              scanner-frontend│
                          │                                (React/Vite)  │
                          └──────────────────────────────────────────────┘
```

One arrow into the ledger, and it only ever reads. One writer to the database,
many readers.

## Why a scanner has to exist at all

From [CHALLENGES.md](../CHALLENGES.md) A1: Canton has **no block explorer by
design**. A node only sees data for the parties it hosts — that privacy model
is the product, not a gap. There is no global chain to scrape, so there is no
"just query the explorer" option.

The consequence is direct: anyone who wants a dashboard has to build and
maintain their own index of the slice of the ledger they can legitimately see.
That is what `indexer.py` is. It also means the honest scope of every number
Scandex shows is "what this node's party is entitled to see", never "the
network" — which is why `get_owners()` carries that caveat in its own
docstring.

## Why scanner → DB → API → frontend, rather than talking to Cantor8 directly

The obvious shortcut — let the React app (or the API) call the Canton ledger
directly — fails on four counts:

**1. The ledger stream is not queryable.** `indexer.py` seeds from the ACS
(`POST /v2/state/active-contracts`) once, then follows
`POST /v2/updates/trees` forward from a saved offset. That is an *append-only
stream of transaction trees*, not a query interface. "Alice's Amulet balance"
is not a request you can make of it; it is a fold over every event you have
ever seen. Something has to do that fold once and keep the result. That is the
database.

**2. Credentials.** Reaching the ledger needs `C8_CLIENT_SECRET` through a
Keycloak client-credentials exchange (`auth.py`). Putting that in a browser
would publish it. The secret stays in one server-side process.

**3. Restartability.** A browser tab cannot hold a ledger offset across a
refresh. `checkpoint` can — which is what makes P6 (resume) possible at all.

**4. Decoupling "is the demo up" from "is DevNet up".** `webapi.py` never
writes and never *requires* the ledger. When the ledger is unreachable,
`_ledger_end()` degrades `ledger_offset` to `null` **with a note saying why**,
and every locally-computed number in the response stays correct. A DevNet
outage costs you the live-drift figure, not the dashboard. This is deliberate
and is asserted in the tests.

## Why SQLite, and why WAL specifically

From `store.py`'s docstring and the README's "two processes, one SQLite file":

* **SQLite** — zero-configuration, single file, in the standard library.
  `pyproject.toml` declares `dependencies = []` and the backend keeps it that
  way; a hackathon-scale index does not justify operating a second database
  server.
* **WAL mode** (`PRAGMA journal_mode=WAL`, set in `ScannerDB._setup`) — this is
  the load-bearing part. The arrangement is **one writer (the indexer) and many
  concurrent readers (the API)** against one file. In SQLite's default rollback
  journal, a writer blocks readers and you get intermittent `database is
  locked` errors under exactly the polling pattern the dashboard uses. WAL lets
  readers proceed against the last committed snapshot while a write is in
  flight. It is what makes running `--index --follow` and `serve_api.py` side by
  side safe without a queue or a lock file.

Two smaller consequences of the same choice:

* `webapi.py` opens **one** connection for the process lifetime and serialises
  reads with `_DB_LOCK`, because `sqlite3` connections are not safe for truly
  concurrent use even with `check_same_thread=False` — and a lock around short
  read queries is cheaper than a connection per request.
* Balances are stored as a **UTXO-style set of holding rows**, not a running
  total. A balance is `SUM(amount) WHERE active = 1`. Archived rows are kept
  (`active = 0`) so history can be reconstructed rather than overwritten.

## Why these nine features, in this order

The order is core value first, then operational reliability, then
observability — matching A1's stated "good submission" bar (balances correct,
resumes cleanly, transfer history) rather than a generic feature list.

| # | Feature | Why it is where it is |
|---|---|---|
| 1 | Wallet balance | The single number the whole thing exists to show. Nothing else matters if this is wrong. |
| 2 | Spendable vs locked | A balance that ignores locked holdings is *wrong*, not merely incomplete — it tells someone they can spend money they cannot. Correctness, not a nicety. |
| 3 | Known parties | Without it the UI needs a hardcoded party id, and nothing is hardcoded. |
| 4 | Transfer history | The second half of A1's bar; also what makes a wrong balance diagnosable. |
| 5 | SQLite persistence | Everything above is worthless if it evaporates on exit. |
| 6 | Resume after restart | A scanner that re-reads the ACS on every start is not a scanner. This is the reliability line. |
| 7 | Health / status | Once it runs unattended you must be able to answer "is it keeping up?" — and, after this branch, "am I looking at real data?" |
| 8 | Stale / pending transfers | A2's drift case: a row that says `pending` forever. Detecting it is the difference between a dashboard and a lie. |
| 9 | Metrics | Aggregate observability, genuinely last: useful, but nothing above depends on it. |

## What is honestly mocked or limited

A1's judging criteria explicitly reward saying what is mocked over hoping
nobody asks. So, plainly:

* **`--data-mode test` data is fabricated.** Every party id, contract id,
  amount and timestamp in [`demo_data.py`](../src/scandex_api/demo_data.py) is
  invented and deterministic. It never touches the ledger. This is why the
  server reports `data_mode` and the UI shows a `Data mode: TEST` chip — so a
  seeded number can never be mistaken for a real one.
* **Balance history starts when the scanner did.** `get_balance_history`'s own
  docstring says it: history is reconstructed by replaying `ledger_events`, and
  those only exist from the first run onward — not from ledger genesis.
* **Scope is one node's view.** `get_owners()` shows parties this node has
  rights to see, not the network. See "why a scanner has to exist" above.
* **`scanner_delay_offsets` is a difference in offsets, not seconds**, and is
  `null` when either offset is non-numeric on this deployment — reporting
  unknown rather than a nonsense subtraction.
* **The `TransferInstruction`-vs-`Holding` distinction for stale-offer
  detection is `NOT VERIFIED LIVE`.** The logic is implemented and tested
  offline against `tests/_fakes.py`'s `FakeTransport`, but confirming that a
  real Cantor8 `TransferInstruction` archive event flips a pending row exactly
  as modelled requires `C8_CLIENT_SECRET` and a live DevNet connection.

## What this is not, and what it would take

This is a **local demo architecture**, and `webapi.py`'s own docstring says so.
As it stands it has no authentication, no TLS, no rate limiting, and sends
`Access-Control-Allow-Origin: *` on every response so any localhost origin can
call it during a demo. It binds `127.0.0.1` by default and should stay there.

Turning it into something network-facing is not a configuration change. It
would need, at minimum:

* **auth** on the API (there is none — every route is open to whoever can reach
  the port);
* **an allow-listed CORS origin** instead of the wildcard;
* **TLS**, and a real WSGI/ASGI server in front rather than
  `http.server.ThreadingHTTPServer`, which is not written for hostile traffic;
* **a different database** once there is more than one writer or more than one
  machine — the single-writer WAL arrangement above is precisely what does not
  survive horizontal scaling;
* **retention and backfill policy** for `ledger_events`, which currently grows
  without bound.

None of that is implemented, and none of it is pretended to be.
