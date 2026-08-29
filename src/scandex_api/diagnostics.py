"""Diagnostics: run the checks, format the results, write reports.

This module orchestrates the per-service clients and turns each probe into a
:class:`~scandex_api.models.CheckResult`. It does **not** know how to talk HTTP
(that is ``http``), or how to authenticate (``auth``), or the shape of any one
service (``ledger`` / ``registry`` / ``scanner``). It only sequences them and
formats the outcome.

Hard safety rule: nothing here ever writes to the ledger. No transfer, no party
allocation, no grant, no accept/reject/withdraw, no ``submit-and-wait``. Those
are surfaced as ``EXPECTED MANUAL ACTION`` entries and never executed.
"""
from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

from . import redaction
from .auth import Authenticator
from .config import Config
from .errors import (
    AuthError, HttpError, PermissionError_, ScandexError, TimeoutError_,
    UnreachableError,
)
from .http import HttpClient
from .ledger import LedgerClient
from .models import CheckResult, Importance, Outcome, TransferPreview
from .registry import RegistryClient
from .scanner import ScannerClient

# Endpoints deliberately NOT exercised by any diagnostic run. Reported every run
# so nobody can mistake a green run for "fully tested".
NOT_TESTED = [
    "POST /v2/commands/submit-and-wait (write)",
    "WS /v2/updates (streaming)",
]

# Write/mutating actions surfaced as manual-only. The diagnostic must never do
# any of these; each needs a separate, explicitly-invoked, human-approved step.
MANUAL_ACTIONS = [
    ("Ledger", "POST", "/v2/commands/submit-and-wait",
     "Submitting a command (a transfer, an accept, any write) moves real value."),
    ("Ledger", "POST", "/v2/parties",
     "Allocating a party changes ledger topology."),
    ("Ledger", "POST", "/v2/users/{userId}/rights",
     "Granting act-as rights changes who can spend a party's assets."),
    ("Registry", "POST", "/registry/transfer-instruction/v1/transfer-factory (exercise)",
     "Exercising the returned factory executes the transfer. Preview only here."),
    ("Registry", "POST", "/registry/transfer-instruction/v1/{id}/choice-contexts/accept",
     "Accepting an offer moves value to the receiver."),
]


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Diagnostics:
    def __init__(self, config: Config, http: HttpClient | None = None,
                 verbose: bool = False):
        self.config = config
        self.verbose = verbose
        logger = (lambda m: print(f"    . {m}")) if verbose else None
        self.http = http or HttpClient(timeout=config.timeout, logger=logger)
        self.auth = Authenticator(config, self.http)
        self.ledger = LedgerClient(config, self.auth, self.http)
        self.registry = RegistryClient(config, self.http)
        self.scanner = ScannerClient(config, self.auth, self.http)
        self.results: list[CheckResult] = []
        self._token_available = False

    # -- result helpers ---------------------------------------------------

    def _add(self, service, method, endpoint, auth_required, outcome, summary,
             meaning, importance, status_code=None, latency_ms=None) -> CheckResult:
        r = CheckResult(
            service=service, method=method, endpoint=endpoint,
            auth_required=auth_required, outcome=outcome,
            summary=redaction.redact(summary), meaning=meaning,
            importance=importance, status_code=status_code, latency_ms=latency_ms,
        )
        self.results.append(r)
        return r

    @staticmethod
    def _classify_exception(exc: Exception) -> tuple[Outcome, str]:
        """Map a raised error to an outcome + short summary."""
        if isinstance(exc, UnreachableError):
            return Outcome.FAIL, f"Unreachable: {exc}"
        if isinstance(exc, TimeoutError_):
            return Outcome.FAIL, f"Timed out: {exc}"
        if isinstance(exc, AuthError):
            return Outcome.FAIL, str(exc)
        if isinstance(exc, (HttpError, ScandexError)):
            return Outcome.FAIL, str(exc)
        return Outcome.FAIL, redaction.redact(f"Unexpected error: {exc}")

    # ================================================================
    #  Groups
    # ================================================================

    def run(self, party: str | None = None) -> list[CheckResult]:
        """Run every read-only check, in labelled groups."""
        party = party or self.config.party
        self.check_authentication()
        self.check_ledger(party)
        self.check_registry()
        self.check_scanner()
        self.add_manual_actions()
        return self.results

    # -- Authentication ---------------------------------------------------

    def check_authentication(self) -> None:
        if not self.config.has_secret:
            self._add(
                "Auth", "POST",
                "/realms/master/protocol/openid-connect/token",
                True, Outcome.FAIL,
                self.config.missing_secret_message(),
                "No client secret is set, so no token can be requested; every "
                "authenticated check below is skipped.",
                Importance.REQUIRED,
            )
            self._token_available = False
            return

        start = time.monotonic()
        try:
            info = self.auth.token_info()
            latency = (time.monotonic() - start) * 1000.0
            self._token_available = True
            self._add(
                "Auth", "POST",
                "/realms/master/protocol/openid-connect/token",
                True, Outcome.PASS,
                f"Token issued for sub={info.sub!r}, expires in {info.expires_in}s.",
                "Keycloak accepted the client credentials and returned a valid "
                "token; the token is cached and reused until shortly before it "
                "expires.",
                Importance.REQUIRED, status_code=200, latency_ms=latency,
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            outcome, summary = self._classify_exception(exc)
            self._token_available = False
            status = getattr(exc, "status", None)
            self._add(
                "Auth", "POST",
                "/realms/master/protocol/openid-connect/token",
                True, outcome, summary,
                "Could not obtain a token; authenticated checks are skipped.",
                Importance.REQUIRED, status_code=status, latency_ms=latency,
            )

    # -- Ledger -----------------------------------------------------------

    def check_ledger(self, party: str | None) -> None:
        if not self._token_available:
            for method, ep, meaning in [
                ("GET", "/v2/state/ledger-end", "Current offset / connectivity."),
                ("GET", "/v2/parties", "Party list."),
                ("POST", "/v2/state/active-contracts", "Active contracts / holdings."),
            ]:
                self._add("Ledger", method, ep, True, Outcome.SKIPPED,
                          "Skipped: no token.",
                          "Needs a token, which was not available.",
                          Importance.REQUIRED)
            return

        offset = self._check_ledger_end()
        parties = self._check_parties(party)
        if offset is not None:
            self._check_active_and_holdings(party, parties, offset)

    def _check_ledger_end(self):
        start = time.monotonic()
        try:
            offset = self.ledger.ledger_end()
            latency = (time.monotonic() - start) * 1000.0
            self._add("Ledger", "GET", "/v2/state/ledger-end", True, Outcome.PASS,
                      f"Ledger end offset = {offset}.",
                      "The ledger is reachable and this is a consistent point to "
                      "read a snapshot at; also the cheapest health check.",
                      Importance.REQUIRED, status_code=200, latency_ms=latency)
            return offset
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            outcome, summary = self._classify_exception(exc)
            self._add("Ledger", "GET", "/v2/state/ledger-end", True, outcome,
                      summary, "Could not read the ledger end.",
                      Importance.REQUIRED,
                      status_code=getattr(exc, "status", None), latency_ms=latency)
            return None

    def _check_parties(self, party):
        start = time.monotonic()
        try:
            parties = self.ledger.parties()
            latency = (time.monotonic() - start) * 1000.0
            local = [p for p in parties if p.is_local]
            self._add("Ledger", "GET", "/v2/parties", True, Outcome.PASS,
                      f"{len(parties)} parties visible, {len(local)} local.",
                      "The node lists every party it has heard about; only local "
                      "(isLocal) parties can submit.",
                      Importance.USEFUL, status_code=200, latency_ms=latency)
            # Configured party lookup
            if party:
                match = next((p for p in parties
                              if p.party == party or p.hint == party
                              or p.party.startswith(party)), None)
                if match is None:
                    self._add("Ledger", "GET", "/v2/parties", True, Outcome.FAIL,
                              f"Configured party {party!r} not found.",
                              "The party set via --party / C8_PARTY is not visible "
                              "to this node.", Importance.USEFUL)
                else:
                    outcome = Outcome.PASS if match.is_local else Outcome.FAIL
                    note = ("is local - it can submit."
                            if match.is_local else
                            "is REMOTE - it cannot submit here "
                            "(NO_SYNCHRONIZER_ON_WHICH_ALL_SUBMITTERS_CAN_SUBMIT).")
                    self._add("Ledger", "GET", "/v2/parties", True, outcome,
                              f"Configured party found; {note}",
                              "Only isLocal parties can submit commands.",
                              Importance.USEFUL)
            return parties
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            outcome, summary = self._classify_exception(exc)
            self._add("Ledger", "GET", "/v2/parties", True, outcome, summary,
                      "Could not list parties.", Importance.USEFUL,
                      status_code=getattr(exc, "status", None), latency_ms=latency)
            return []

    def _check_active_and_holdings(self, party, parties, offset):
        # Choose a party to inspect: the configured one, else the first local.
        target = None
        if party:
            target = next((p.party for p in parties
                           if p.party == party or p.hint == party
                           or p.party.startswith(party)), party)
        elif parties:
            local = [p for p in parties if p.is_local]
            target = (local[0].party if local else None)

        if not target:
            self._add("Ledger", "POST", "/v2/state/active-contracts", True,
                      Outcome.SKIPPED, "No party to inspect.",
                      "No configured party and no local party to read holdings for.",
                      Importance.REQUIRED)
            return

        start = time.monotonic()
        try:
            summary = self.ledger.holdings(target, offset=offset)
            latency = (time.monotonic() - start) * 1000.0
            msg = (f"{len(summary.holdings)} holding(s) for "
                   f"{target.split('::')[0]} at offset {summary.offset}: "
                   f"total {summary.total}, spendable {summary.spendable}"
                   + (f", {summary.locked_count} locked" if summary.locked_count else ""))
            self._add("Ledger", "POST", "/v2/state/active-contracts", True,
                      Outcome.PASS, msg,
                      "Balances read via the Holding INTERFACE filter at a fixed "
                      "offset; spendable excludes locked holdings.",
                      Importance.REQUIRED, status_code=200, latency_ms=latency)
        except PermissionError_ as exc:
            latency = (time.monotonic() - start) * 1000.0
            self._add("Ledger", "POST", "/v2/state/active-contracts", True,
                      Outcome.SKIPPED,
                      f"403 reading holdings for {target.split('::')[0]} - "
                      "not this token's party.",
                      "A 403 here is normal for a party you do not own; it is not "
                      "a broken environment.", Importance.REQUIRED,
                      status_code=403, latency_ms=latency)
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            outcome, summary = self._classify_exception(exc)
            self._add("Ledger", "POST", "/v2/state/active-contracts", True,
                      outcome, summary, "Could not read holdings.",
                      Importance.REQUIRED,
                      status_code=getattr(exc, "status", None), latency_ms=latency)

    # -- Registry ---------------------------------------------------------

    def check_registry(self) -> None:
        start = time.monotonic()
        try:
            info = self.registry.info()
            latency = (time.monotonic() - start) * 1000.0
            admin = info.get("adminId") or info.get("admin") or info.get("administrator")
            self._add("Registry", "GET", "/registry/metadata/v1/info", False,
                      Outcome.PASS,
                      f"Registry info readable (admin={admin}).",
                      "The registry advertises its admin party and supported "
                      "transfer features; it is public (no token).",
                      Importance.USEFUL, status_code=200, latency_ms=latency)
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            outcome, summary = self._classify_exception(exc)
            self._add("Registry", "GET", "/registry/metadata/v1/info", False,
                      outcome, summary, "Could not read registry info.",
                      Importance.USEFUL,
                      status_code=getattr(exc, "status", None), latency_ms=latency)

        start = time.monotonic()
        try:
            instruments = self.registry.instruments()
            latency = (time.monotonic() - start) * 1000.0
            names = ", ".join(
                f"{i.id}(admin={str(i.administrator)[:12]}...,dec={i.decimals})"
                for i in instruments[:5]) or "none listed"
            self._add("Registry", "GET", "/registry/metadata/v1/instruments", False,
                      Outcome.PASS,
                      f"{len(instruments)} instrument(s): {names}",
                      "Lists every token this registry serves with its admin and "
                      "decimals - stops Scandex assuming every asset is Amulet.",
                      Importance.USEFUL, status_code=200, latency_ms=latency)
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            outcome, summary = self._classify_exception(exc)
            self._add("Registry", "GET", "/registry/metadata/v1/instruments", False,
                      outcome, summary, "Could not list instruments.",
                      Importance.USEFUL,
                      status_code=getattr(exc, "status", None), latency_ms=latency)

    # -- Scanner / public Scan -------------------------------------------

    def check_scanner(self) -> None:
        # Scanner health (open)
        start = time.monotonic()
        try:
            resp = self.scanner.health()
            latency = resp.latency_ms
            if resp.ok:
                data = resp.json()
                db = data.get("db", {})
                delay = db.get("scannerDelaySecs")
                outcome = Outcome.PASS
                warn = ""
                if isinstance(delay, (int, float)) and delay > 60:
                    warn = f" WARNING: index is {delay:.0f}s behind the ledger."
                self._add("Scanner", "GET", "/health", False, outcome,
                          f"status={data.get('status')}, db={db.get('status')}, "
                          f"scannerDelaySecs={delay}.{warn}",
                          "The scanner is an INDEXED read model and can lag; "
                          "scannerDelaySecs says by how much.",
                          Importance.USEFUL, status_code=resp.status,
                          latency_ms=latency)
            else:
                self._add("Scanner", "GET", "/health", False, Outcome.FAIL,
                          f"Health returned HTTP {resp.status}.",
                          "The scanner health endpoint did not return OK.",
                          Importance.USEFUL, status_code=resp.status,
                          latency_ms=latency)
        except Exception as exc:
            outcome, summary = self._classify_exception(exc)
            self._add("Scanner", "GET", "/health", False, outcome, summary,
                      "Could not reach the scanner health endpoint.",
                      Importance.USEFUL)

        # Protected scanner data endpoint: separate 401 from 403 clearly.
        # Without a token there is nothing to send, so skip rather than fail.
        if not self._token_available:
            self._add("Scanner", "GET", "/tokens/balance/{party}", True,
                      Outcome.SKIPPED, "Skipped: no token.",
                      "This endpoint needs a token; with one, a 401 would mean "
                      "'who are you' and a 403 'not yours / m2m only'.",
                      Importance.OPTIONAL)
            self._check_public_scan()
            return

        start = time.monotonic()
        probe_party = self.config.party or "unknown-party"
        try:
            resp = self.scanner.balance(probe_party)
            if resp.ok:
                self._add("Scanner", "GET", "/tokens/balance/{party}", True,
                          Outcome.PASS, "Balance endpoint readable.",
                          "Indexed balance for the party was returned.",
                          Importance.OPTIONAL, status_code=resp.status,
                          latency_ms=resp.latency_ms)
            elif resp.status in (401, 403):
                outcome = Outcome.SKIPPED if resp.status == 403 else Outcome.FAIL
                self._add("Scanner", "GET", "/tokens/balance/{party}", True,
                          outcome, self.scanner.interpret_auth_status(resp.status),
                          "401 means 'who are you' (auth); 403 means 'not yours / "
                          "m2m only' (permission).",
                          Importance.OPTIONAL, status_code=resp.status,
                          latency_ms=resp.latency_ms)
            else:
                self._add("Scanner", "GET", "/tokens/balance/{party}", True,
                          Outcome.FAIL, f"HTTP {resp.status}.",
                          "Unexpected status from the scanner balance endpoint.",
                          Importance.OPTIONAL, status_code=resp.status,
                          latency_ms=resp.latency_ms)
        except Exception as exc:
            outcome, summary = self._classify_exception(exc)
            self._add("Scanner", "GET", "/tokens/balance/{party}", True, outcome,
                      summary, "Could not reach the scanner balance endpoint.",
                      Importance.OPTIONAL)

        self._check_public_scan()

    def _check_public_scan(self) -> None:
        """Public Scan API (no auth). Split out so it always runs, even when the
        authenticated scanner probe was skipped for lack of a token."""
        try:
            resp = self.scanner.splice_instance_names()
            if resp.ok:
                data = resp.json()
                name = data.get("networkName") or data.get("name") or data
                self._add("Public Scan", "GET", "/api/scan/v0/splice-instance-names",
                          False, Outcome.PASS,
                          f"Public network info readable ({str(name)[:60]}).",
                          "Network-wide branding published by super validators; "
                          "no token needed.", Importance.OPTIONAL,
                          status_code=resp.status, latency_ms=resp.latency_ms)
            else:
                self._add("Public Scan", "GET", "/api/scan/v0/splice-instance-names",
                          False, Outcome.FAIL, f"HTTP {resp.status}.",
                          "Public Scan info endpoint did not return OK.",
                          Importance.OPTIONAL, status_code=resp.status,
                          latency_ms=resp.latency_ms)
        except Exception as exc:
            outcome, summary = self._classify_exception(exc)
            self._add("Public Scan", "GET", "/api/scan/v0/splice-instance-names",
                      False, outcome, summary,
                      "Could not reach the public Scan API.", Importance.OPTIONAL)

    # -- Manual actions ---------------------------------------------------

    def add_manual_actions(self) -> None:
        for service, method, endpoint, meaning in MANUAL_ACTIONS:
            self._add(service, method, endpoint, True, Outcome.MANUAL,
                      "Not run by the diagnostic - write/mutating action.",
                      meaning + " Needs a separate, explicitly-invoked command "
                      "with human approval; never runs in CI.",
                      Importance.OPTIONAL)

    # ================================================================
    #  Transfer preview (dry run, submits nothing)
    # ================================================================

    def preview_transfer(self, sender: str, receiver: str, amount: str,
                         instrument: str = "Amulet") -> TransferPreview:
        notes: list[str] = []
        try:
            amt = float(amount)
        except ValueError:
            amt = 0.0
            notes.append(f"amount {amount!r} is not a number; treated as 0.")

        # Resolve parties to full ids if possible.
        sender_id, receiver_id = sender, receiver
        available = 0.0
        spendable = 0.0
        has_locked = False
        preapproved: bool | None = None
        transfer_kind = "unknown"

        if sender == receiver:
            transfer_kind = "self"
            notes.append("Sender and receiver are the same party.")

        if self._token_available or self.config.has_secret:
            try:
                parties = self.ledger.parties()
                sender_id = next((p.party for p in parties
                                  if p.party == sender or p.hint == sender
                                  or p.party.startswith(sender)), sender)
                receiver_id = next((p.party for p in parties
                                    if p.party == receiver or p.hint == receiver
                                    or p.party.startswith(receiver)), receiver)
            except Exception as exc:
                notes.append(redaction.redact(f"could not resolve parties: {exc}"))

            try:
                summary = self.ledger.holdings(sender_id)
                same = [h for h in summary.holdings if h.instrument == instrument]
                available = sum(h.amount_float for h in same)
                spendable = sum(h.amount_float for h in same if not h.locked)
                has_locked = any(h.locked for h in same)
                if has_locked:
                    notes.append("Sender has locked holdings; locked amounts show "
                                 "in the balance but cannot be spent until the lock "
                                 "expires.")
                if spendable < amt:
                    notes.append(f"Spendable {instrument} ({spendable}) is less than "
                                 f"the requested amount ({amt}).")
            except Exception as exc:
                notes.append(redaction.redact(f"could not read sender holdings: {exc}"))
        else:
            notes.append("No token available, so balances could not be read; "
                         "preview shows structure only.")

        # Try a registry preview to learn transferKind / preapproval, without
        # ever exercising the factory. Best-effort only.
        if transfer_kind != "self":
            try:
                admin = self.config.admin_party
                if admin:
                    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
                    choice_args = {
                        "expectedAdmin": admin,
                        "transfer": {
                            "sender": sender_id, "receiver": receiver_id,
                            "amount": str(amount),
                            "instrumentId": {"admin": admin, "id": instrument},
                            "requestedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "executeBefore": (now + datetime.timedelta(hours=24)
                                              ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "inputHoldingCids": [],
                            "meta": {"values": {}},
                        },
                        "extraArgs": {"context": {"values": {}}, "meta": {"values": {}}},
                    }
                    fac = self.registry.transfer_factory_preview(choice_args)
                    transfer_kind = fac.get("transferKind", transfer_kind)
                    preapproved = (transfer_kind == "direct")
                    notes.append("Registry preview succeeded; factory was NOT "
                                 "exercised.")
                else:
                    notes.append("C8_ADMIN_PARTY not set, so the registry preview "
                                 "was skipped; transferKind is inferred, not "
                                 "confirmed.")
            except Exception as exc:
                notes.append(redaction.redact(
                    f"registry preview skipped: {exc}"))

        next_step = {
            "direct": "The receiver is preapproved - money would move immediately.",
            "offer": "No preapproval - a TransferInstruction would be created and "
                     "the receiver's balance would NOT change until they accept.",
            "self": "Sender and receiver are the same party.",
        }.get(transfer_kind,
              "transferKind could not be confirmed; if the receiver has a live "
              "TransferPreapproval it would be 'direct', otherwise 'offer'.")

        return TransferPreview(
            sender=sender_id, receiver=receiver_id, instrument=instrument,
            amount=amt, available=available, spendable_after_locks=spendable,
            has_locked_holdings=has_locked, receiver_preapproved=preapproved,
            transfer_kind=transfer_kind, next_step=next_step, notes=notes,
        )

    # ================================================================
    #  Formatting / reporting
    # ================================================================

    @staticmethod
    def counts(results: list[CheckResult]) -> dict:
        c = {o: 0 for o in Outcome}
        for r in results:
            c[r.outcome] += 1
        return {
            "passed": c[Outcome.PASS],
            "failed": c[Outcome.FAIL],
            "skipped": c[Outcome.SKIPPED],
            "manual": c[Outcome.MANUAL],
        }

    @staticmethod
    def exit_code(results: list[CheckResult]) -> int:
        if any(r.outcome == Outcome.FAIL for r in results):
            return 1
        return 0

    def report_dict(self, results: list[CheckResult] | None = None) -> dict:
        results = results if results is not None else self.results
        return {
            "generatedAtUtc": _utc_now_iso(),
            "mode": "DevNet / Keycloak",
            "ledgerBase": self.config.base,
            "counts": self.counts(results),
            "notTested": NOT_TESTED,
            "checks": [r.as_dict() for r in results],
        }

    def write_reports(self, directory: str | Path = "reports") -> tuple[Path, Path]:
        """Write timestamped JSON and Markdown reports, both redacted.

        Returns (json_path, md_path).
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = _utc_now_iso().replace(":", "-")
        report = self.report_dict()

        json_path = directory / f"cantor8-check-{stamp}.json"
        json_text = redaction.redact(json.dumps(report, indent=2))
        json_path.write_text(json_text, encoding="utf-8")

        md_path = directory / f"cantor8-check-{stamp}.md"
        md_path.write_text(redaction.redact(self.render_markdown(report)),
                           encoding="utf-8")
        return json_path, md_path

    def render_markdown(self, report: dict | None = None) -> str:
        report = report or self.report_dict()
        c = report["counts"]
        lines = [
            "# Cantor8 connectivity check",
            "",
            f"- Generated: `{report['generatedAtUtc']}`",
            f"- Mode: {report['mode']}",
            f"- Ledger base: `{report['ledgerBase']}`",
            "",
            f"**Passed: {c['passed']}   Failed: {c['failed']}   "
            f"Skipped: {c['skipped']}   Manual action required: {c['manual']}**",
            "",
            "## Checks",
            "",
            "| Service | Method | Endpoint | Auth | Status | Outcome | Demo | Latency | Result |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in report["checks"]:
            lines.append(
                f"| {r['service']} | {r['method']} | `{r['endpoint']}` | "
                f"{'yes' if r['authRequired'] else 'no'} | "
                f"{r['statusCode'] if r['statusCode'] is not None else '-'} | "
                f"{r['outcome']} | {r['demoImportance']} | "
                f"{str(r['latencyMs']) + ' ms' if r['latencyMs'] is not None else '-'} | "
                f"{r['summary'].replace('|', '/')} |"
            )
        lines += [
            "",
            "## What each result means",
            "",
        ]
        for r in report["checks"]:
            lines.append(f"- **{r['service']} {r['method']} {r['endpoint']}** "
                         f"({r['outcome']}): {r['meaning']}")
        lines += [
            "",
            "## Not tested",
            "",
            "This run never exercises a write or streaming endpoint. The system "
            "is therefore **not** fully tested:",
            "",
        ]
        for item in report["notTested"]:
            lines.append(f"- `{item}`")
        lines.append("")
        return "\n".join(lines)
