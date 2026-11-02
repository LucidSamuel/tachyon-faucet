import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict

from tachyon_faucet.client_ip import canonical_ip, request_ip
from tachyon_faucet.config import Config, load_config
from tachyon_faucet.http import create_app
from tachyon_faucet.service import Faucet
from tachyon_faucet.store import Rejected, Store
from .fakes import RECIPIENT, Wallet

FIRST_IP, SECOND_IP = "192.0.2.10", "198.51.100.20"
TOKEN = "11" * 32
PROOF = json.loads((Path(__file__).parent / "fixtures/equihash.json").read_text())["proof"]


class ClientIpTests(unittest.TestCase):
    def test_direct_peers_cannot_spoof_forwarding_headers(self):
        request = SimpleNamespace(remote=FIRST_IP, headers=CIMultiDict({
            "X-Real-IP": SECOND_IP, "X-Forwarded-For": SECOND_IP, "Forwarded": "for=" + SECOND_IP}))
        self.assertEqual(request_ip(request, {"127.0.0.1"}), FIRST_IP)

    def test_trusted_proxy_requires_exactly_one_literal_address(self):
        for headers in ([], [("X-Forwarded-For", FIRST_IP)],
                        [("X-Real-IP", FIRST_IP), ("X-Real-IP", SECOND_IP)],
                        [("X-Real-IP", FIRST_IP + ", " + SECOND_IP)],
                        [("X-Real-IP", "example.com")], [("X-Real-IP", FIRST_IP + ":8080")],
                        [("X-Real-IP", "fe80::1%eth0")]):
            with self.subTest(headers=headers), self.assertRaises(Rejected):
                request_ip(SimpleNamespace(remote="127.0.0.1", headers=CIMultiDict(headers)), {"127.0.0.1"})

    def test_equivalent_ip_spellings_share_an_identity(self):
        self.assertEqual(canonical_ip("::ffff:192.0.2.10"), FIRST_IP)
        self.assertEqual(canonical_ip("2001:0db8:0:0:0:0:0:1"), "2001:db8::1")
        request = SimpleNamespace(remote="::ffff:127.0.0.1",
                                  headers=CIMultiDict({"X-Real-IP": "::ffff:192.0.2.10"}))
        self.assertEqual(request_ip(request, {"127.0.0.1"}), FIRST_IP)

    def test_proxy_configuration_is_opt_in_and_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            base = 'devtool="binary"\nwallet_dir="wallet"\nstate_dir="state"\n'
            path.write_text(base)
            self.assertEqual(load_config(path).trusted_proxy_ips, ())
            for value in ('"127.0.0.1"', '["*"]', '["0.0.0.0/0"]', '["localhost"]',
                          '[123]', '["fe80::1%eth0"]'):
                path.write_text(base + "trusted_proxy_ips=" + value)
                with self.assertRaises(ValueError):
                    load_config(path)
            path.write_text(base + 'trusted_proxy_ips=["127.0.0.1", "::1"]')
            self.assertEqual(load_config(path).trusted_proxy_ips, ["127.0.0.1", "::1"])


class PerIpAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.config = Config(devtool=root / "unused", wallet_dir=root / "wallet", state_dir=root / "state",
                             trusted_proxy_ips=("127.0.0.1",))
        self.time = 1_000_000
        self.store = Store(self.config.state_dir)
        with self.store.db:
            self.store.db.execute("INSERT INTO metadata VALUES('pow_key', ?)", ("42" * 32,))
        self.wallet = Wallet()
        self.faucet = Faucet(self.config, self.store, self.wallet, lambda: self.time)
        await self.faucet.tick()
        self.client = TestClient(TestServer(create_app(self.faucet, start_worker=False)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self.directory.cleanup()

    async def post(self, ip=FIRST_IP, token=TOKEN, proof=PROOF):
        return await self.client.post("/api/faucet/claims", headers={"X-Real-IP": ip},
                                      json=dict(token=token, address=RECIPIENT, proof=proof))

    async def status(self, ip):
        result = await self.client.get("/api/faucet/status", headers={"X-Real-IP": ip})
        self.assertEqual(result.status, 200)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        return await result.json()

    async def confirm_payment(self):
        await self.faucet.tick()
        for op in self.wallet.operations.values():
            op["status"] = "mined"
        self.time += 1000
        await self.faucet.tick()

    async def test_real_proof_charges_only_requesting_ip(self):
        self.assertEqual((await self.post()).status, 202)
        self.assertEqual((await self.status(FIRST_IP))["remaining_ms"], 300_000)
        other = await self.status(SECOND_IP)
        self.assertEqual(other["remaining_ms"], 0)
        self.assertEqual(other["cooldown_scope"], "ip")
        self.assertEqual(other["reason"], "processing")  # Wallet still serializes payments.
        await self.confirm_payment()
        self.assertTrue((await self.status(SECOND_IP))["accepting"])
        self.assertFalse((await self.status(FIRST_IP))["accepting"])
        # Isolate IP admission; real new-claim proof enforcement is tested below.
        with patch.object(self.faucet.challenges, "verify"):
            self.assertEqual((await self.post(SECOND_IP, "22" * 32)).status, 202)
        self.assertEqual((await self.status(FIRST_IP))["remaining_ms"], 299_000)
        self.assertEqual((await self.status(SECOND_IP))["remaining_ms"], 300_000)

    async def test_same_ip_different_tokens_have_one_cooldown_and_precise_retry_after(self):
        with patch.object(self.faucet.challenges, "verify"):
            results = await asyncio.gather(*(self.post(token=f"{i:064x}") for i in range(10)))
        self.assertEqual(sorted(result.status for result in results), [202] + [429] * 9)
        self.time += 50_001
        with patch.object(self.faucet.challenges, "verify"):
            rejected = await self.post(token="ff" * 32)
        self.assertEqual(rejected.status, 429)
        self.assertEqual(rejected.headers["Retry-After"], "250")
        self.assertEqual((await rejected.json())["status"]["remaining_ms"], 249_999)

    async def test_cooldown_challenge_and_invalid_proof_do_not_charge_other_ip(self):
        await self.post()
        blocked = await self.client.post("/api/faucet/challenge", headers={"X-Real-IP": FIRST_IP},
                                          json=dict(token="22" * 32, address=RECIPIENT))
        self.assertEqual(blocked.status, 429)
        await self.confirm_payment()
        for proof in (None, PROOF):  # A proof bound to the old token is not reusable.
            self.assertEqual((await self.post(SECOND_IP, "22" * 32, proof)).status, 400)
        self.assertEqual((await self.status(SECOND_IP))["remaining_ms"], 0)
        self.assertTrue((await self.status(SECOND_IP))["accepting"])

    async def test_lost_response_retry_from_new_ip_is_same_claim_without_new_cooldown(self):
        first = await (await self.post()).json()
        self.time += 50_000
        again = await (await self.post(SECOND_IP, proof=None)).json()
        self.assertEqual(first["claim"], again["claim"])
        self.assertEqual(again["status"]["remaining_ms"], 0)
        self.assertEqual((await self.status(FIRST_IP))["remaining_ms"], 250_000)
        self.assertEqual(len(self.store.active()), 1)

    async def test_ip_keys_are_normalized_private_and_survive_restart(self):
        await self.post()
        self.assertEqual((await self.status("::ffff:192.0.2.10"))["remaining_ms"], 300_000)
        self.assertEqual(self.faucet.client_key("2001:0db8::1"), self.faucet.client_key("2001:db8::1"))
        rows = self.store.db.execute("SELECT ip_hash,until_ms FROM ip_cooldowns").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertRegex(rows[0][0], r"^[0-9a-f]{64}$")
        self.assertNotIn(FIRST_IP, str(rows[0][:]))
        public = json.dumps(await self.status(FIRST_IP))
        self.assertNotIn(FIRST_IP, public)
        self.assertNotIn(rows[0][0], public)
        self.store.close()
        self.store = Store(self.config.state_dir)
        restarted = Faucet(replace(self.config, cooldown_seconds=1), self.store, self.wallet, lambda: self.time)
        self.assertEqual(restarted.status(FIRST_IP)["remaining_ms"], 300_000)
        self.assertEqual(restarted.status(SECOND_IP)["remaining_ms"], 0)

    async def test_missing_proxy_header_fails_closed_and_never_charges_loopback(self):
        result = await self.client.post("/api/faucet/claims", json=dict(token=TOKEN, address=RECIPIENT, proof=PROOF))
        self.assertEqual(result.status, 400)
        self.assertIn("X-Real-IP", (await result.json())["error"])
        self.assertEqual(self.store.active(), [])

    async def test_legacy_global_deadline_does_not_block_new_ip_or_lose_claim_recovery(self):
        await self.post()
        with self.store.db:
            self.store.db.execute("CREATE TABLE cooldown(id INTEGER PRIMARY KEY, until_ms INTEGER NOT NULL)")
            self.store.db.execute("INSERT INTO cooldown VALUES(1,9999999999)")
            self.store.db.execute("DROP TABLE ip_cooldowns")
        self.store.close()
        self.store = Store(self.config.state_dir)
        restarted = Faucet(self.config, self.store, self.wallet, lambda: self.time)
        self.assertEqual(restarted.status(FIRST_IP)["remaining_ms"], 0)
        self.assertEqual(restarted.claim(TOKEN, RECIPIENT, FIRST_IP)["state"], "queued")
        self.assertEqual(len(self.store.active()), 1)

    async def test_expired_ip_rows_are_reclaimed_on_next_admission(self):
        await self.post()
        await self.confirm_payment()
        self.time += 300_000
        await self.faucet.tick()
        with patch.object(self.faucet.challenges, "verify"):
            self.assertEqual((await self.post(SECOND_IP, "22" * 32)).status, 202)
        rows = self.store.db.execute("SELECT ip_hash FROM ip_cooldowns").fetchall()
        self.assertEqual([row[0] for row in rows], [self.faucet.client_key(SECOND_IP)])
