"""One wallet worker, durable payments, and a global admission cooldown."""

import asyncio
import json
import logging
from pathlib import Path
import tempfile
import time

from .config import FEE, HEX32, address, field, token_digest
from .store import Rejected
from .pow import Challenges

LOG = logging.getLogger("tachyon_faucet")


def now_ms():
    return int(time.time() * 1000)


def public_claim(operation):
    return dict(state=operation["state"], txid=operation["txid"],
                amount_zat=operation["amount_zat"], address=operation["address"],
                receipt_available=operation["receipt"] is not None and operation["state"] != "expired",
                created_ms=operation["created_ms"])


class Faucet:
    def __init__(self, config, store, cli, clock=now_ms):
        self.config, self.store, self.cli, self.clock = config, store, cli, clock
        self.phase = "syncing"
        self.last_checked = 0
        self.identity = None
        self.wake = asyncio.Event()
        self.last_submit = {}
        self.store.pin("wallet_dir", str(config.wallet_dir.resolve()))
        self.challenges = Challenges(store.challenge_key(), config, clock)

    def status(self):
        now = self.clock()
        remaining = max(0, self.store.deadline() - now)
        active = self.store.active()
        fresh = self.last_checked > 0 and now - self.last_checked <= max(60, self.config.poll_seconds * 3) * 1000
        wallet_ready = self.phase == "ready" and fresh
        if remaining:
            reason = "cooldown"
        elif active:
            reason = "funding" if active[0]["kind"] == "shield" else "processing"
        elif not fresh and self.phase == "ready":
            reason = "syncing"
        else:
            reason = self.phase
        return dict(amount_zat=self.config.amount_zat, cooldown_seconds=self.config.cooldown_seconds,
                    server_time_ms=now, next_claim_at_ms=self.store.deadline(),
                    remaining_ms=remaining, accepting=wallet_ready and not active and not remaining,
                    reason=reason)

    def validate_request(self, token, recipient):
        digest, recipient = token_digest(token), address(recipient)
        if self.identity and recipient == self.identity["tachyon_address"]:
            raise Rejected(400, "Use your own wallet's payment key, not the faucet's")
        return digest, recipient

    def challenge(self, token, recipient):
        digest, recipient = self.validate_request(token, recipient)
        status = self.status()
        if not status["accepting"]:
            raise Rejected(429 if status["remaining_ms"] else 503, "The faucet is not ready for a new puzzle")
        return self.challenges.issue(digest, recipient)

    def claim(self, token, recipient, proof=None):
        digest, recipient = self.validate_request(token, recipient)
        # Existing durable claims are their own replay protection: an accepted
        # token can only recover that payment, even after its puzzle expires.
        if self.store.by_token(digest) is None:
            self.challenges.verify(proof, digest, recipient)
        result = self.store.claim(digest, recipient, self.config, self.clock(), self.status()["accepting"])
        self.wake.set()
        return public_claim(result)

    def get_claim(self, token):
        operation = self.store.by_token(token_digest(token))
        if operation is None:
            raise Rejected(404, "Claim not found")
        return operation

    async def initialize(self):
        identity = await self.cli.call("address")
        if not HEX32.fullmatch(identity["network_id"]):
            raise ValueError("Invalid wallet network identity")
        identity["tachyon_address"] = address(identity["tachyon_address"])
        self.store.pin("identity", {key: identity[key] for key in
                                   ("network_id", "tachyon_address", "funding_address")})
        self.identity = identity
        LOG.info("Wallet identity verified")

    async def receipt(self, operation):
        with tempfile.TemporaryDirectory(prefix="export-", dir=self.store.path) as directory:
            output = Path(directory) / "note.json"
            await self.cli.call("export-receipt", "--txid", operation["txid"], "--out", output)
            with output.open("rb") as source:
                raw = source.read(16385)
            if len(raw) > 16384:
                raise ValueError("Receipt exceeds 16 KiB")
            receipt = json.loads(raw)
            required = {"version", "network_id", "payment_key", "value_zat", "psi", "rcm"}
            if (not isinstance(receipt, dict) or not required <= receipt.keys() or
                    receipt.keys() - required - {"locator"} or
                    receipt["version"] != 1 or
                    receipt["network_id"] != self.identity["network_id"] or
                    receipt["payment_key"] != operation["address"].removeprefix("tachyon-test:") or
                    type(receipt["value_zat"]) is not int or
                    receipt["value_zat"] != operation["amount_zat"]):
                raise ValueError("Exported receipt does not match the claim")
            locator = receipt.get("locator")
            if locator is not None:
                # Preserve devtool's optional lookup hints, bound to this payment.
                if (not isinstance(locator, dict) or
                        locator.keys() - {"txid", "height", "block_hash"} or
                        locator.get("txid") != operation["txid"]):
                    raise ValueError("Receipt locator does not match the payment")
                height, block_hash = locator.get("height"), locator.get("block_hash")
                if height is not None and (type(height) is not int or not 0 <= height <= 0xFFFFFFFF):
                    raise ValueError("Invalid receipt block height")
                if block_hash is not None and (not isinstance(block_hash, str) or not HEX32.fullmatch(block_hash)):
                    raise ValueError("Invalid receipt block hash")
            field(receipt["payment_key"])
            field(receipt["psi"])
            if int(field(receipt["rcm"]), 16) == 0:
                raise ValueError("Invalid receipt commitment trapdoor")
            self.store.update(operation["id"], receipt=json.dumps(receipt, indent=2))

    async def execute(self, operation, height):
        saved = await self.cli.lookup(operation["id"])
        expected = (dict(kind="send", to=operation["address"], amount_zat=operation["amount_zat"])
                    if operation["kind"] == "send" else
                    dict(kind="shield", amount_zat=operation["amount_zat"], txid=None, index=None))
        if saved is None:
            if operation["txid"] is not None:
                raise RuntimeError("Saved faucet payment is missing from its wallet")
            args = (["send", "--to", operation["address"], "--amount-zat", operation["amount_zat"]]
                    if operation["kind"] == "send" else ["shield", operation["amount_zat"]])
            # The journal already contains this exact ID and request. An exception
            # may follow a successful broadcast; the next tick looks up this ID.
            await self.cli.call(*args, "--operation-id", operation["id"])
            saved = await self.cli.lookup(operation["id"])
        if (saved is None or saved["request"] != expected or
                saved["operation_id"] != operation["id"] or
                not HEX32.fullmatch(saved["txid"]) or
                saved["status"] not in ("pending", "mined", "expired") or
                (operation["txid"] is not None and saved["txid"] != operation["txid"])):
            raise RuntimeError("Devtool operation does not match the faucet journal")
        self.store.update(operation["id"], txid=saved["txid"], state=saved["status"])
        operation = self.store.get(operation["id"])
        if operation["kind"] == "send" and operation["receipt"] is None:
            await self.receipt(operation)
        if saved["status"] == "pending" and self.last_submit.get(operation["id"]) != height:
            await self.cli.call("submit", "--txid", saved["txid"])
            self.last_submit[operation["id"]] = height
        LOG.info("Payment %s state=%s", operation["id"], saved["status"])

    async def tick(self):
        if self.identity is None:
            await self.initialize()
        synced = await self.cli.call("sync", "--max-blocks", 10000)
        if synced.get("caught_up") is not True:
            self.phase = "syncing"
            return
        transactions = await self.cli.call("transactions")
        self.store.reconcile(transactions["transactions"])
        missing = self.store.needs_receipt()
        if missing:
            await self.receipt(missing[0])
        active = self.store.active()
        if active:
            self.phase = "funding" if active[0]["kind"] == "shield" else "processing"
            await self.execute(active[0], synced["height"])
            return
        balance = await self.cli.call("balance")
        self.last_checked = self.clock()
        if any(note["status"] == "available" and note["value_zat"] >= self.config.amount_zat + FEE
               for note in balance["notes"]):
            self.phase = "ready"
            return
        self.phase = "empty"
        transparent = balance.get("transparent") or {}
        capacity = transparent.get("available_zat", 0) - FEE
        if self.config.auto_shield and capacity >= self.config.amount_zat + FEE:
            self.store.refill(min(capacity, self.config.shield_amount_zat), self.clock())
            self.phase = "funding"
            self.wake.set()

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.tick()
            except Exception as error:
                self.phase = "unavailable"
                # Do not expose CLI arguments, stderr, RPC credentials, or notes.
                LOG.warning("Wallet work will retry; error_type=%s command=%s",
                            type(error).__name__, getattr(error, "command", "unknown"))
            try:
                await asyncio.wait_for(self.wake.wait(), self.config.poll_seconds)
            except TimeoutError:
                pass
