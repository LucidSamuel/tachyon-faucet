"""Private, durable claim admission and wallet-operation recovery state."""

import fcntl
import json
import os
import secrets
from pathlib import Path
import sqlite3
import stat
import uuid


class Rejected(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode) or mode & 0o077:
        raise ValueError(f"Directory must be real and mode 0700: {path}")
    return path


def private_file(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    mode = os.fstat(fd).st_mode
    if not stat.S_ISREG(mode) or mode & 0o077:
        os.close(fd)
        raise ValueError(f"File must be regular and mode 0600: {path}")
    return os.fdopen(fd, "a+")


class Store:
    def __init__(self, path):
        self.path = private_dir(path)
        self.lock = private_file(self.path / "faucet.lock")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another faucet process owns this state directory") from None
        with private_file(self.path / "faucet.sqlite"):
            pass
        self.db = sqlite3.connect(self.path / "faucet.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ip_cooldowns(ip_hash TEXT PRIMARY KEY, until_ms INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS ip_cooldowns_expiry ON ip_cooldowns(until_ms);
            CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY, token_hash TEXT UNIQUE, kind TEXT NOT NULL,
                address TEXT, amount_zat INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                txid TEXT, receipt TEXT, created_ms INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS operations_state ON operations(state,created_ms);
        """)

    def close(self):
        self.db.close()
        self.lock.close()

    def pin(self, key, value):
        encoded = json.dumps(value, sort_keys=True)
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row and row[0] != encoded:
            raise RuntimeError(f"Faucet {key} changed; restore its original wallet and state")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", (key, encoded))

    def deadline(self, ip_hash):
        # The old global cooldown table, if present, is intentionally unused:
        # earlier claims did not record an IP, so their deadlines cannot migrate.
        row = self.db.execute("SELECT until_ms FROM ip_cooldowns WHERE ip_hash=?", (ip_hash,)).fetchone()
        return row[0] if row else 0

    def challenge_key(self):
        # Persist alongside claims so an in-progress puzzle survives a restart.
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('pow_key', ?)",
                            (secrets.token_hex(32),))
        return bytes.fromhex(self.db.execute("SELECT value FROM metadata WHERE key='pow_key'").fetchone()[0])

    def by_token(self, digest):
        row = self.db.execute("SELECT * FROM operations WHERE token_hash=?", (digest,)).fetchone()
        return dict(row) if row else None

    def active(self):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM operations WHERE state IN ('queued','pending') ORDER BY created_ms,id")]

    def get(self, operation_id):
        return dict(self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def claim(self, digest, address, ip_hash, config, now_ms, ready):
        # No await between these checks and commit: a concurrent visitor cannot
        # pass the cooldown before the winning claim reserves it.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.by_token(digest)
            if existing:
                if existing["address"] != address:
                    raise Rejected(409, "This claim token already belongs to another address")
                self.db.commit()
                return existing
            if now_ms < self.deadline(ip_hash):
                raise Rejected(429, "The faucet cooldown for your IP address is still running")
            if self.active():
                raise Rejected(503, "The faucet is finishing an earlier payment")
            if not ready:
                raise Rejected(503, "The faucet is not ready; please try again shortly")
            operation_id = "faucet:" + uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO operations(id,token_hash,kind,address,amount_zat,created_ms) VALUES(?,?,'send',?,?,?)",
                (operation_id, digest, address, config.amount_zat, now_ms))
            self.db.execute("DELETE FROM ip_cooldowns WHERE until_ms<=?", (now_ms,))
            self.db.execute("INSERT INTO ip_cooldowns(ip_hash,until_ms) VALUES(?,?) "
                            "ON CONFLICT(ip_hash) DO UPDATE SET until_ms=excluded.until_ms",
                            (ip_hash, now_ms + config.cooldown_seconds * 1000))
            self.db.commit()
            return self.get(operation_id)
        except BaseException:
            self.db.rollback()
            raise

    def refill(self, amount, now_ms):
        with self.db:
            if self.active():
                return
            self.db.execute(
                "INSERT INTO operations(id,kind,amount_zat,created_ms) VALUES(?,'shield',?,?)",
                ("refill:" + uuid.uuid4().hex, amount, now_ms))

    def update(self, operation_id, **fields):
        if not fields or set(fields) - {"state", "txid", "receipt"}:
            raise ValueError("Unsupported operation update")
        with self.db:
            self.db.execute("UPDATE operations SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                            (*fields.values(), operation_id))

    def reconcile(self, transactions):
        with self.db:
            for tx in transactions:
                operation_id = tx.get("operation_id")
                row = self.db.execute("SELECT txid FROM operations WHERE id=?", (operation_id,)).fetchone()
                if row is None:
                    continue
                if row["txid"] is not None and row["txid"] != tx["txid"]:
                    raise RuntimeError("Wallet operation changed its transaction ID")
                if tx["status"] not in ("pending", "mined", "expired"):
                    raise RuntimeError("Unsupported wallet transaction status")
                # Revisit confirmed records too, so a reorg restores pending status.
                self.db.execute("UPDATE operations SET state=?,txid=? WHERE id=?",
                                (tx["status"], tx["txid"], operation_id))

    def needs_receipt(self):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM operations WHERE kind='send' AND txid IS NOT NULL AND receipt IS NULL")]
