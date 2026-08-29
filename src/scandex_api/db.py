"""SQLite persistence for the A1 scanner.

A local database is a **cache** of what this node is entitled to see, never
the source of truth. The Ledger API is authoritative; when a query into
:mod:`~scandex_api.ledger` disagrees with what is stored here, the ledger
wins and the row here is corrected.

Tables mirror :doc:`docs/ENDPOINT_DATA_MAP.md`. Schema creation is
**idempotent**: opening the same file twice is safe, and adding a new column
or index does not require a migration for the fields we already write.

Standard library only (``sqlite3``). Threading model: one connection per
:class:`Database` instance; ``check_same_thread=False`` is left off because
the indexer is single-threaded and the read CLI opens its own connection.
"""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Iterable

# The single row id we use for the ``updates`` stream. Every indexer instance
# writes/reads this row so a restart resumes from the last offset.
UPDATES_STREAM = "updates"


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_offsets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stream     TEXT NOT NULL UNIQUE,
    offset     TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parties (
    party_id     TEXT PRIMARY KEY,
    hint         TEXT,
    is_local     INTEGER NOT NULL DEFAULT 0,
    display_name TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS parties_is_local_idx ON parties(is_local);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id TEXT PRIMARY KEY,
    name          TEXT,
    administrator TEXT,
    decimals      INTEGER,
    registry_base TEXT,
    raw_json      TEXT
);

CREATE TABLE IF NOT EXISTS holdings (
    contract_id     TEXT PRIMARY KEY,
    party_id        TEXT NOT NULL,
    amount          TEXT,
    instrument_id   TEXT,
    administrator   TEXT,
    locked          INTEGER NOT NULL DEFAULT 0,
    lock_expiry     TEXT,
    read_at_offset  TEXT,
    observed_at     TEXT NOT NULL,
    archived_at_offset TEXT
);
CREATE INDEX IF NOT EXISTS holdings_party_instrument_idx
    ON holdings(party_id, instrument_id);
CREATE INDEX IF NOT EXISTS holdings_locked_idx ON holdings(locked);
CREATE INDEX IF NOT EXISTS holdings_active_idx ON holdings(archived_at_offset);

CREATE TABLE IF NOT EXISTS transfers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id          TEXT,
    sender             TEXT,
    receiver           TEXT,
    instrument_id      TEXT,
    amount             TEXT,
    transfer_kind      TEXT,
    status             TEXT,
    source             TEXT NOT NULL,
    scanner_delay_secs REAL,
    observed_at        TEXT NOT NULL,
    UNIQUE(update_id, sender, receiver, instrument_id, amount)
);
CREATE INDEX IF NOT EXISTS transfers_sender_idx   ON transfers(sender);
CREATE INDEX IF NOT EXISTS transfers_receiver_idx ON transfers(receiver);
CREATE INDEX IF NOT EXISTS transfers_update_idx   ON transfers(update_id);

CREATE TABLE IF NOT EXISTS service_health (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    service            TEXT NOT NULL,
    status             TEXT,
    db_status          TEXT,
    scanner_delay_secs REAL,
    latency_ms         REAL,
    observed_at        TEXT NOT NULL
);
"""


class Database:
    """Thin wrapper over ``sqlite3`` with typed helpers for the indexer.

    Nothing about this class knows Cantor8; it takes plain rows in and gives
    plain rows out. Which fields to fill and what they mean lives in
    :mod:`~scandex_api.indexer`.
    """

    def __init__(self, path: str | Path = "scandex.db"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.execute("PRAGMA journal_mode = WAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- offsets ----------------------------------------------------------

    def get_offset(self, stream: str = UPDATES_STREAM) -> str | None:
        """Return the last processed offset for ``stream``, or ``None`` if the
        indexer has never run against this database.

        The indexer's restart guarantee lives on this method: if it returns
        a value, the indexer must *not* re-read the ACS.
        """
        row = self.conn.execute(
            "SELECT offset FROM ledger_offsets WHERE stream = ?", (stream,)
        ).fetchone()
        return row["offset"] if row else None

    def set_offset(self, offset, stream: str = UPDATES_STREAM) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO ledger_offsets(stream, offset, observed_at) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(stream) DO UPDATE SET "
                "  offset = excluded.offset, observed_at = excluded.observed_at",
                (stream, str(offset), _utc_now_iso()),
            )

    # -- holdings ---------------------------------------------------------

    def upsert_holding(
        self,
        contract_id: str,
        party_id: str,
        amount,
        instrument_id: str | None,
        administrator: str | None,
        locked: bool,
        lock_expiry: str | None,
        read_at_offset,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO holdings("
                "  contract_id, party_id, amount, instrument_id, administrator,"
                "  locked, lock_expiry, read_at_offset, observed_at,"
                "  archived_at_offset"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(contract_id) DO UPDATE SET "
                "  amount         = excluded.amount,"
                "  instrument_id  = excluded.instrument_id,"
                "  administrator  = excluded.administrator,"
                "  locked         = excluded.locked,"
                "  lock_expiry    = excluded.lock_expiry,"
                "  read_at_offset = excluded.read_at_offset,"
                "  observed_at    = excluded.observed_at,"
                "  archived_at_offset = NULL",
                (
                    contract_id, party_id,
                    None if amount is None else str(amount),
                    instrument_id, administrator,
                    1 if locked else 0, lock_expiry,
                    None if read_at_offset is None else str(read_at_offset),
                    _utc_now_iso(),
                ),
            )

    def archive_holding(self, contract_id: str, archived_at_offset) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE holdings SET archived_at_offset = ?, observed_at = ? "
                "WHERE contract_id = ?",
                (str(archived_at_offset), _utc_now_iso(), contract_id),
            )

    def balance_for(self, party: str) -> list[sqlite3.Row]:
        """Per-instrument totals for ``party``, spendable and locked split out.

        Only currently-active holdings (``archived_at_offset IS NULL``) count.
        Amounts are summed as ``REAL`` so a caller can format them cheaply -
        the raw string amount stays intact per row for higher-precision needs.
        """
        return self.conn.execute(
            "SELECT instrument_id,"
            "       COALESCE(SUM(CAST(amount AS REAL)), 0.0) AS total,"
            "       COALESCE(SUM(CASE WHEN locked = 0 "
            "                         THEN CAST(amount AS REAL) ELSE 0 END), 0.0) "
            "                                          AS spendable,"
            "       COUNT(*) AS holding_count,"
            "       SUM(locked) AS locked_count "
            "FROM holdings "
            "WHERE party_id = ? AND archived_at_offset IS NULL "
            "GROUP BY instrument_id "
            "ORDER BY instrument_id",
            (party,),
        ).fetchall()

    def active_holdings_for(self, party: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT contract_id, amount, instrument_id, administrator, "
            "       locked, lock_expiry, read_at_offset, observed_at "
            "FROM holdings "
            "WHERE party_id = ? AND archived_at_offset IS NULL "
            "ORDER BY instrument_id, contract_id",
            (party,),
        ).fetchall()

    # -- transfers --------------------------------------------------------

    def insert_transfer(
        self,
        update_id: str | None,
        sender: str | None,
        receiver: str | None,
        instrument_id: str | None,
        amount,
        transfer_kind: str | None = None,
        status: str | None = "settled",
        source: str = "ledger",
        scanner_delay_secs: float | None = None,
    ) -> bool:
        """Insert one transfer row. Returns ``True`` if a new row was written.

        The ``UNIQUE(update_id, sender, receiver, instrument_id, amount)``
        constraint makes this idempotent - replaying the same update never
        double-counts.
        """
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO transfers("
                    "  update_id, sender, receiver, instrument_id, amount,"
                    "  transfer_kind, status, source, scanner_delay_secs, observed_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        update_id, sender, receiver, instrument_id,
                        None if amount is None else str(amount),
                        transfer_kind, status, source, scanner_delay_secs,
                        _utc_now_iso(),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def transfers_for(self, party: str, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, update_id, sender, receiver, instrument_id, amount,"
            "       transfer_kind, status, source, scanner_delay_secs, observed_at "
            "FROM transfers "
            "WHERE sender = ? OR receiver = ? "
            "ORDER BY id DESC "
            "LIMIT ?",
            (party, party, limit),
        ).fetchall()

    # -- parties (light) --------------------------------------------------

    def upsert_party(
        self, party_id: str, hint: str | None, is_local: bool,
        display_name: str | None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO parties(party_id, hint, is_local, display_name,"
                "                    first_seen, last_seen) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(party_id) DO UPDATE SET "
                "  hint         = excluded.hint,"
                "  is_local     = excluded.is_local,"
                "  display_name = excluded.display_name,"
                "  last_seen    = excluded.last_seen",
                (
                    party_id, hint, 1 if is_local else 0, display_name,
                    _utc_now_iso(), _utc_now_iso(),
                ),
            )

    def upsert_parties(self, parties: Iterable) -> None:
        for p in parties:
            self.upsert_party(
                party_id=p.party, hint=p.hint, is_local=p.is_local,
                display_name=p.display_name,
            )
