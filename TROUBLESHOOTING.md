# Troubleshooting

Every error below was hit for real while building this toolkit, in this order.
If you are stuck, `python3 c8lab.py` first. It checks auth, the ledger and your
parties in one go, and tells you which layer is broken.

## Setup

### `Bad CPU type in executable` when installing Daml

Apple Silicon, no Rosetta. The SDK is an Intel binary.

```bash
softwareupdate --install-rosetta --agree-to-license
```

### `daml build` works but `daml test` says "Unable to locate a Java Runtime"

You edited `~/.zshrc` but are still in the old terminal. The compiler shells out
to `java`, which is not on the old shell's PATH. Open a new terminal, or
`source ~/.zshrc`.

### LocalNet web pages load but nothing works

Docker cannot bind-mount from `Documents`, `Desktop` or `Downloads`. The web UI
containers need no mounts so they start; postgres, canton and splice all die.

```
error while creating mount source path '/host_mnt/Users/.../Documents/...':
operation not permitted
```

Copy the localnet directory to `~/localnet` and run from there.

### nginx will not start, `Bind for 0.0.0.0:3000 failed`

Something else owns port 3000. The ledger still works, but the scan registry is
unreachable, so **transfers will fail**. Restart with:

```bash
export APP_PROVIDER_UI_PORT=3001
```

### `PARTY_HINT is required`

It must look like `word-word-number`, e.g. `myteam-dev-1`.

## Auth

### 401 on every call

The Ledger API is authenticated. On LocalNet the token is a self-signed HS256
JWT with the secret `unsafe`. `c8lab.py` mints it for you.

A 401 from a fresh LocalNet is also the correct "it is up" signal before you have
a token. Do not panic at it.

### 403, with a token that works elsewhere

**This is the one that wastes the most time.** Your token proves who you are. It
does not grant you anything over a party's contracts. Two separate things.

The user in the token's `sub` needs rights on that party. On LocalNet use
`ledger-api-user` or `app-user`. Or grant them:

```
POST /v2/users/{userId}/rights
{"rights":[{"kind":{"CanActAs":{"value":{"party":"..."}}}}]}
```

`c8lab.py` has `grant_act_as(user, party)`.

## Parties

### `Party already exists`

You ran an allocate script twice. Look for an existing party with that hint
instead. `allocate_party()` does this.

### `NO_SYNCHRONIZER_ON_WHICH_ALL_SUBMITTERS_CAN_SUBMIT`

You are submitting as a party this node does not host. A node lists every party
it has heard about, including other people's. Only use ones where `isLocal` is
true.

## Reading data

### Empty holdings, HTTP 200, no error

You used a `TemplateFilter`. `Holding` is a Daml **interface**, not a template,
so the filter matches nothing and returns an empty list that looks exactly like a
zero balance.

Use `InterfaceFilter` with `includeInterfaceView: true` and:

```
#splice-api-token-holding-v1:Splice.Api.Token.HoldingV1:Holding
```

### My indexer shows nothing, or a zero balance

You streamed updates from the current ledger end. That gives you the future, not
the past. Query the **active contract set** first for the balance, then stream
forward from that same offset to stay current.

### 401 on `scanner-ledger-read-api` (`/tokens/balance/{party}` etc.)

Expected, not a bug. The scanner-ledger-read-api is Cantor8's own reference
implementation of A1 ("Build a scanner") — the thing participants are
rebuilding — and hackathon credentials are not provisioned to call its data
endpoints. `/health` is open and passes; the data endpoints will return 401
for a participant token and always will.

Do not chase this. The authoritative balance and history for your own party
come from the Ledger API directly:

```bash
python scripts/check_cantor8.py --index   --party <your-party>   # seed + catch up
python scripts/check_cantor8.py --balance --party <your-party>
python scripts/check_cantor8.py --history --party <your-party>
```

The diagnostic still probes `/tokens/balance/{party}` so a 401 is surfaced,
labelled, and understood — it just is not the same class of failure as a 401
on the Ledger API.

## Frontend

### The dashboard is empty: "No parties", "No holdings", "No transfers"

The backend is probably fine — both causes below return **HTTP 200** the whole
time, which is what makes this confusing. Work through it in this order.

**1. Is the frontend calling the right port?**

The API defaults to **8787** (`webapi.py`'s `build_parser()`), and there is no
dev proxy in `vite.config.js`. If the frontend points anywhere else, every
request fails in the browser while `curl` against the real port looks perfect.

```bash
curl -s http://127.0.0.1:8787/health          # is anything listening?
```

The base URL is `VITE_API_BASE` (see `scanner-frontend/.env.example`),
defaulting to `http://127.0.0.1:8787`. Check the Network tab: if requests are
going to a different host or port, or are not being made at all, that is your
answer. Note `127.0.0.1` rather than `localhost` — the server binds `127.0.0.1`,
and on some systems `localhost` resolves to `::1` first, where nothing is
listening.

Remember Vite inlines `import.meta.env` at **build time**: restart `npm run dev`
after editing `.env.local`.

**2. Is the frontend unwrapping the response envelope?**

Every list route wraps its payload in an object; none of them return a bare
array:

```bash
curl -s http://127.0.0.1:8787/parties
# {"parties": [ ... ]}     <- an object, NOT [ ... ]
```

So `Array.isArray(response)` is `false` for `/parties`,
`/tokens/balance/{party}`, `/tokens/transfers/{party}`, `/tokens/holdings/{party}`
and `/tokens/transfers/stale`. Code that tests the whole response body for
array-ness silently renders an empty list forever. Read the list out of the
documented key instead — `.parties`, `.byInstrument`, `.transfers`, `.holdings`
— which is what `scanner-frontend/src/api.js` does in one place for all of them.

`/health` and `/metrics` are the exception: they are flat and are read as-is.

The full envelope table is in
[docs/ENDPOINT_DATA_MAP.md](docs/ENDPOINT_DATA_MAP.md#response-envelopes--what-the-frontend-unwraps),
and `tests/test_webapi_contract.py` pins every shape so a change to one fails
loudly rather than emptying the UI.

**3. Is there actually any data?**

An empty database renders exactly like a broken connection. Check:

```bash
curl -s http://127.0.0.1:8787/health     # "status": "no_data" means nothing is indexed
```

If it says `no_data`, the indexer has not run (or has not reached a checkpoint).
To rule the frontend out entirely, start the API in test mode — it seeds a known
dataset and needs no ledger, no secret and no network:

```bash
python scripts/serve_api.py --data-mode test --port 8787
```

If the dashboard fills up in test mode, the frontend is fine and the problem is
upstream in the indexer or its credentials.

### Am I looking at real data or demo data?

Check the **`Data mode`** chip in the dashboard topbar, or the banner on
`/status`. It reads `data_mode` from `GET /health`, reported by the server:

```bash
curl -s http://127.0.0.1:8787/health | grep data_mode
```

`TEST` means every figure on screen is fabricated by `demo_data.py` — not
ledger data. `REAL` means it came from whatever the indexer wrote to
`scandex.db`.

### CORS errors in the browser console

The server sends `Access-Control-Allow-Origin: *` on every response, so a CORS
error usually means the request never reached it — see "is the frontend calling
the right port?" above. A failed connection and a CORS rejection look similar in
the console.

### The instrument or direction filter seems to do nothing

Those are real server-side query parameters
(`?instrument=`, `?direction=sent|received`) applied in SQL before `LIMIT`.
Confirm the server honours them:

```bash
curl -s "http://127.0.0.1:8787/tokens/transfers/<party>?direction=sent"
```

If the count matches the unfiltered request, you are running a build of the API
from before the filters were wired up — the route used to read only `?limit`.

## Transfers

### The transfer succeeded but the receiver has no money

Check `transferKind` in the result.

- `direct`: the receiver had a live preapproval, money moved.
- `offer`: no preapproval. A `TransferInstruction` was created and **the receiver
  must accept it**. Their balance does not change until they do. Nothing is
  broken.

Creating a preapproval is not instant. You create the proposal, and the
validator's automation accepts it a moment later. Create it, wait, then transfer.

### `deadline-not-exceeded ... Lock.expiresAt`

You passed a **locked** holding as an input to a transfer. Locked holdings show
up in your balance but cannot be spent until the lock expires.

A holding gets locked when it is escrowed for a pending transfer offer. So if
you send an offer that nobody has accepted yet, part of your balance is locked
and your next transfer fails, with an error that says nothing about locks being
the problem.

Filter them out. `holdings()` returns a `locked` flag on every entry:

```python
spendable = [h for h in holdings(me) if not h["locked"]]
```

`transfer()` in `c8lab.py` does this. If you build your own, do it too.

### `LOCAL_VERDICT_LOCKED_CONTRACTS`

Contention. Another in-flight transaction is using the same holdings. Holdings
are UTXOs: a transfer archives the ones it spends, so two transfers touching the
same holding fight and one loses.

Retry after a moment. If it happens constantly, you have too few holdings for
your concurrency, which is a real Canton problem and the subject of one of the
challenges.

### The registry call fails or times out

On LocalNet the registry is the scan app behind nginx, routed by Host header.
Two things break it: nginx not running (see the port 3000 item above), and
`scan.localhost` not resolving. `c8lab.py` sends `Host: scan.localhost` to
`localhost:4000` to avoid the DNS problem.

### I want to build the transfer by hand without the registry

You cannot. Privacy means you cannot see the issuer's configuration contracts,
so the registry has to hand them to you as disclosed contracts for that one
transaction. That is the design, not a limitation of this toolkit.
