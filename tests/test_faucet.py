import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from tachyon_faucet.config import Config, PALLAS_BASE, address, load_config, token_digest
from tachyon_faucet.devtool import CliError, Devtool
from tachyon_faucet.http import create_app
from tachyon_faucet.service import Faucet
from tachyon_faucet.store import Rejected, Store
from .fakes import KEY, NETWORK, RECIPIENT, Wallet

TOKEN = "11" * 32


class FaucetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = Config(devtool=self.root / "devtool", wallet_dir=self.root / "wallet",
                             state_dir=self.root / "state")
        self.time = 1_000_000
        self.store = Store(self.config.state_dir)
        self.wallet = Wallet()
        self.faucet = Faucet(self.config, self.store, self.wallet, lambda: self.time)
        await self.faucet.tick()
        self.client = TestClient(TestServer(create_app(self.faucet, start_worker=False)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self.directory.cleanup()

    async def post(self, token=TOKEN, recipient=RECIPIENT):
        # These tests isolate payment/cooldown behavior. test_pow exercises the
        # real admission gate using a browser-generated Equihash proof.
        with patch.object(self.faucet.challenges, "verify"):
            return await self.client.post("/api/faucet/claims", json=dict(token=token, address=recipient))

    async def claim(self, token=TOKEN):
        return await self.client.get("/api/faucet/claim", headers={"Authorization": "Bearer " + token})

    def operation(self):
        return self.store.by_token(token_digest(TOKEN))

    async def test_per_ip_cooldown_allows_one_of_ten_simultaneous_requests(self):
        responses = await asyncio.gather(*(self.post(f"{index:064x}") for index in range(10)))
        self.assertEqual(sorted(r.status for r in responses), [202] + [429] * 9)
        status = await (await self.client.get("/api/faucet/status")).json()
        self.assertEqual(status["remaining_ms"], 300_000)
        self.assertFalse(status["accepting"])
        self.assertEqual(len(self.store.active()), 1)
        self.assertEqual(self.wallet.send_count, 0)  # HTTP never runs the wallet.
        for response in responses:
            if response.status == 429:
                self.assertEqual(response.headers["Retry-After"], "300")

    async def test_duplicate_request_recovers_same_claim_without_extending_cooldown(self):
        first = await (await self.post()).json()
        self.time += 50_000
        again = await (await self.post()).json()
        self.assertEqual(first["claim"], again["claim"])
        self.assertEqual(again["status"]["remaining_ms"], 250_000)
        self.assertEqual(len(self.store.active()), 1)
        changed = await self.post(recipient="tachyon-test:" + "03" + "00" * 31)
        self.assertEqual(changed.status, 409)

    async def test_cooldown_survives_restart_and_config_edit(self):
        await self.post()
        self.store.close()
        self.store = Store(self.config.state_dir)
        restarted = Faucet(replace(self.config, cooldown_seconds=1, amount_zat=2_000_000),
                           self.store, self.wallet, lambda: self.time)
        self.assertEqual(restarted.status("127.0.0.1")["remaining_ms"], 300_000)
        self.assertEqual(restarted.claim(TOKEN, RECIPIENT, "127.0.0.1")["amount_zat"], 1_000_000)
        with self.assertRaises(Rejected):
            restarted.claim("22" * 32, RECIPIENT, "127.0.0.1")

    async def test_pending_payment_still_blocks_when_cooldown_ends(self):
        await self.post()
        self.time += 300_001
        self.assertEqual(self.faucet.status("127.0.0.1")["reason"], "processing")
        self.assertEqual((await self.post("22" * 32)).status, 503)

    async def test_invalid_key_does_not_consume_cooldown(self):
        for recipient in ("tmAnAddress", "$(touch owned)", "ff" * 32,
                          PALLAS_BASE.to_bytes(32, "little").hex()):
            self.assertEqual((await self.post(recipient=recipient)).status, 400)
            self.assertEqual(self.store.deadline(self.faucet.client_key("127.0.0.1")), 0)
        self.assertEqual((await self.post(recipient=KEY.upper())).status, 202)
        self.assertEqual(self.operation()["address"], RECIPIENT)

    async def test_rejects_self_payment(self):
        self.assertEqual((await self.post(recipient=self.faucet.identity["tachyon_address"])).status, 400)
        self.assertEqual(self.store.deadline(self.faucet.client_key("127.0.0.1")), 0)

    async def test_unavailable_wallet_rejects_without_charging_cooldown(self):
        self.faucet.phase = "empty"
        self.assertEqual((await self.post()).status, 503)
        self.assertEqual(self.store.deadline(self.faucet.client_key("127.0.0.1")), 0)
        self.faucet.phase = "ready"
        self.time += 61_000
        self.assertFalse(self.faucet.status("127.0.0.1")["accepting"])

    async def test_receipt_download_is_private_and_exact_devtool_format(self):
        await self.post()
        await self.faucet.tick()
        claim = await (await self.claim()).json()
        self.assertEqual(claim["state"], "pending")
        self.assertTrue(claim["receipt_available"])
        self.assertEqual((await self.client.get("/api/faucet/claim/receipt")).status, 401)
        wrong = await self.client.get("/api/faucet/claim/receipt", headers={"Authorization": "Bearer " + "22" * 32})
        self.assertEqual(wrong.status, 404)
        downloaded = await self.client.get("/api/faucet/claim/receipt", headers={"Authorization": "Bearer " + TOKEN})
        note = await downloaded.json()
        self.assertEqual(note["payment_key"], KEY)
        self.assertEqual(note["value_zat"], self.config.amount_zat)
        self.assertEqual(set(note), {"version", "network_id", "payment_key", "value_zat", "psi", "rcm"})
        self.assertEqual(downloaded.headers["Cache-Control"], "no-store")
        self.assertIn("faucet-note.json", downloaded.headers["Content-Disposition"])
        public = await (await self.client.get("/api/faucet/status")).text()
        for secret in (TOKEN, RECIPIENT, note["psi"], note["rcm"]):
            self.assertNotIn(secret, public)
        self.assertNotIn("receipt", claim)
        self.assertEqual(self.wallet.send_count, 1)

    async def test_disconnect_and_lost_broadcast_result_recover_same_operation(self):
        self.wallet.fail_after_save = True
        await self.post()
        with self.assertRaises(CliError):
            await self.faucet.tick()
        self.assertEqual(self.operation()["state"], "queued")
        self.store.close()
        self.store = Store(self.config.state_dir)
        recovered = Faucet(self.config, self.store, self.wallet, lambda: self.time)
        await recovered.tick()
        self.assertEqual(self.wallet.send_count, 1)
        self.assertEqual(self.operation()["state"], "pending")
        self.assertIsNotNone(self.operation()["receipt"])

    async def test_confirmation_reorg_and_expiry_never_create_replacement(self):
        await self.post()
        await self.faucet.tick()
        op = self.operation()
        self.wallet.operations[op["id"]]["status"] = "mined"
        await self.faucet.tick()
        self.time += 300_000
        await self.faucet.tick()
        self.assertTrue(self.faucet.status("127.0.0.1")["accepting"])
        self.wallet.operations[op["id"]]["status"] = "pending"
        self.wallet.height += 1
        await self.faucet.tick()
        self.assertFalse(self.faucet.status("127.0.0.1")["accepting"])
        self.wallet.operations[op["id"]]["status"] = "expired"
        await self.faucet.tick()
        self.assertEqual(self.operation()["state"], "expired")
        self.assertEqual(self.wallet.send_count, 1)
        self.assertFalse((await (await self.claim()).json())["receipt_available"])
        self.assertEqual((await self.client.get("/api/faucet/claim/receipt",
                         headers={"Authorization": "Bearer " + TOKEN})).status, 409)

    async def test_corrupt_or_wrong_recipient_receipt_is_not_served(self):
        self.wallet.wrong_receipt = True
        await self.post()
        with self.assertRaises(ValueError):
            await self.faucet.tick()
        self.assertIsNone(self.operation()["receipt"])

    async def test_receipt_download_preserves_optional_transaction_locator(self):
        self.wallet.receipt_locator = dict(height=123, block_hash="ab" * 32)
        await self.post()
        await self.faucet.tick()
        downloaded = await self.client.get("/api/faucet/claim/receipt", headers={"Authorization": "Bearer " + TOKEN})
        note = await downloaded.json()
        self.assertEqual(note["locator"], dict(txid=self.operation()["txid"], height=123, block_hash="ab" * 32))

    async def test_invalid_locator_is_not_served(self):
        await self.post()
        for locator in ({"txid": "ab" * 32}, {"height": -1}, {"height": True},
                        {"height": 2**32}, {"block_hash": "invalid"}, {"unexpected": 1}):
            with self.subTest(locator=locator):
                self.wallet.receipt_locator = locator
                with self.assertRaises(ValueError):
                    await self.faucet.tick()
                self.assertIsNone(self.operation()["receipt"])

    async def test_auto_shield_journals_once_and_waits_for_confirmation(self):
        self.wallet.balance = dict(notes=[], transparent=dict(available_zat=200_000_000))
        await self.faucet.tick()
        refill = self.store.active()[0]
        self.assertEqual(refill["kind"], "shield")
        self.assertEqual(refill["amount_zat"], 100_000_000)
        self.assertEqual(self.store.deadline(self.faucet.client_key("127.0.0.1")), 0)
        await self.faucet.tick()
        await self.faucet.tick()
        self.assertEqual(self.wallet.send_count, 1)
        self.assertEqual(self.faucet.status("127.0.0.1")["reason"], "funding")
        self.wallet.operations[refill["id"]]["status"] = "mined"
        self.wallet.balance["notes"] = [dict(status="available", value_zat=100_000_000)]
        await self.faucet.tick()
        self.assertTrue(self.faucet.status("127.0.0.1")["accepting"])

    async def test_no_funds_or_partial_sync_does_not_create_payments(self):
        self.wallet.caught_up = False
        await self.faucet.tick()
        self.assertEqual(self.faucet.status("127.0.0.1")["reason"], "syncing")
        self.wallet.caught_up = True
        self.wallet.balance = dict(notes=[], transparent=None)
        await self.faucet.tick()
        self.assertEqual(self.faucet.status("127.0.0.1")["reason"], "empty")
        self.assertFalse(self.store.active())

    async def test_state_is_exclusive_and_pinned_to_wallet(self):
        with self.assertRaises(RuntimeError):
            Store(self.config.state_dir)
        with self.assertRaises(RuntimeError):
            Faucet(replace(self.config, wallet_dir=self.root / "other"), self.store, self.wallet)
        self.assertEqual((self.config.state_dir / "faucet.sqlite").stat().st_mode & 0o777, 0o600)

    async def test_http_boundaries(self):
        origin = await self.client.post("/api/faucet/claims", json=dict(token=TOKEN, address=RECIPIENT),
                                        headers={"Origin": "https://unrelated.example"})
        self.assertEqual(origin.status, 403)
        self.assertEqual((await self.client.post("/api/faucet/claims", data="anything")).status, 415)
        too_large = await self.client.post("/api/faucet/claims", data=" " * 3000,
                                           headers={"Content-Type": "application/json"})
        self.assertEqual(too_large.status, 413)
        malformed = await self.client.post("/api/faucet/claims", json=dict(token=TOKEN, address=RECIPIENT, amount_zat=999))
        self.assertEqual(malformed.status, 400)
        self.assertEqual(self.store.deadline(self.faucet.client_key("127.0.0.1")), 0)


class ConfigTests(unittest.TestCase):
    def test_field_encoding_matches_little_endian_boundary(self):
        self.assertEqual(address(KEY), RECIPIENT)
        self.assertEqual(address("  " + RECIPIENT + " "), RECIPIENT)
        address((PALLAS_BASE - 1).to_bytes(32, "little").hex())
        with self.assertRaises(ValueError):
            address(PALLAS_BASE.to_bytes(32, "little").hex())

    def test_defaults_paths_and_strict_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            base = 'devtool="binary"\nwallet_dir="wallet"\nstate_dir="state"\n'
            path.write_text(base)
            config = load_config(path)
            self.assertEqual(config.cooldown_seconds, 300)
            self.assertEqual(config.wallet_dir, Path(temp).resolve() / "wallet")
            for extra in ('cooldown_seconds=0', 'cooldown_seconds=true', 'unknown=1',
                          'allowed_origins=["https://example.com/path"]', 'auto_shield="yes"',
                          'shield_amount_zat=10', 'pow_difficulty_bits=-1',
                          'pow_difficulty_bits=17', 'pow_difficulty_bits=true',
                          'pow_challenge_seconds=0'):
                path.write_text(base + extra)
                with self.assertRaises(ValueError, msg=extra):
                    load_config(path)


class SubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def test_arguments_are_not_a_shell_and_timeout_reaps_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "fake-devtool"
            pidfile = root / "pid"
            script.write_text(f"""#!{sys.executable}
import json, os, sys, time
if sys.argv[-1] == 'hang':
    open({str(pidfile)!r}, 'w').write(str(os.getpid()))
    time.sleep(60)
else:
    print(json.dumps({{'args': sys.argv}}))
""")
            script.chmod(0o700)
            config = Config(devtool=script, wallet_dir=root / "wallet", state_dir=root / "state")
            cli = Devtool(config)
            value = "$(touch should-not-exist)"
            self.assertEqual((await cli.call("example", value))["args"][-1], value)
            self.assertFalse((root / "should-not-exist").exists())
            cli = Devtool(replace(config, command_timeout=0.2))
            with self.assertRaises(TimeoutError):
                await cli.call("hang")
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
