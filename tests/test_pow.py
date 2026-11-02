import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from tachyon_faucet.config import Config
from tachyon_faucet.http import create_app
from tachyon_faucet.pow import valid_equihash, meets_target
from tachyon_faucet.service import Faucet
from tachyon_faucet.store import Store
from .fakes import RECIPIENT, Wallet

# The same fixture lives in Tachyscan. Its worker generated this proof for a
# signed challenge with synthetic key 42*32, token 11*32 and clock 1,000,000 ms.
VECTOR = json.loads((Path(__file__).parent / "fixtures/equihash.json").read_text())
TOKEN = "11" * 32


class EquihashTests(unittest.TestCase):
    def test_upstream_vector_and_invalid_variants(self):
        # zcash/librustzcash components/equihash/src/test_vectors/valid.rs
        indices = [1911, 96020, 94086, 96830, 7895, 51522, 56142, 62444,
                   15441, 100732, 48983, 64776, 27781, 85932, 101138, 114362,
                   4497, 14199, 36249, 41817, 23995, 93888, 35798, 96337,
                   5530, 82377, 66438, 85247, 39332, 78978, 83015, 123505]
        nonce = b"\x01" + bytes(31)
        self.assertTrue(valid_equihash(b"block header", nonce, indices))
        self.assertFalse(valid_equihash(b"other header", nonce, indices))
        self.assertFalse(valid_equihash(b"block header", bytes(32), indices))
        for invalid in (indices[::-1], indices[16:] + indices[:16],
                        indices[:16] * 2, indices[:-1], [False] + indices[1:],
                        [131072] + indices[1:], [-1] + indices[1:]):
            self.assertFalse(valid_equihash(b"block header", nonce, invalid))
        for position in range(32):
            changed = indices.copy()
            changed[position] ^= 1
            self.assertFalse(valid_equihash(b"block header", nonce, changed))

    def test_browser_target_encoding(self):
        data = bytes.fromhex(VECTOR["challenge"]["input"])
        proof = VECTOR["proof"]
        nonce = bytes.fromhex(proof["nonce"])
        self.assertTrue(meets_target(data, nonce, proof["indices"], 3))
        self.assertFalse(meets_target(data, nonce, proof["indices"], 16))
        self.assertTrue(valid_equihash(data, nonce, proof["indices"]))


class PowAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.config = Config(devtool=root / "devtool", wallet_dir=root / "wallet",
                             state_dir=root / "state", pow_difficulty_bits=3)
        self.time = 1_000_000
        self.store = Store(self.config.state_dir)
        with self.store.db:
            self.store.db.execute("INSERT INTO metadata VALUES('pow_key', ?)", ("42" * 32,))
        self.faucet = Faucet(self.config, self.store, Wallet(), lambda: self.time)
        await self.faucet.tick()
        self.client = TestClient(TestServer(create_app(self.faucet, start_worker=False)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self.directory.cleanup()

    async def post(self, proof=VECTOR["proof"], token=TOKEN, recipient=RECIPIENT):
        return await self.client.post("/api/faucet/claims", json=dict(token=token, address=recipient, proof=proof))

    async def test_challenge_is_signed_and_does_not_reserve_faucet(self):
        with patch("tachyon_faucet.pow.secrets.token_hex", return_value="ab" * 32):
            result = await self.client.post("/api/faucet/challenge", json=dict(token=TOKEN, address=RECIPIENT))
        self.assertEqual(result.status, 200)
        self.assertEqual(await result.json(), VECTOR["challenge"])
        self.assertEqual(self.store.deadline(self.faucet.client_key("127.0.0.1")), 0)
        self.assertEqual(self.store.active(), [])
        await self.post()
        blocked = await self.client.post("/api/faucet/challenge", json=dict(token="22" * 32, address=RECIPIENT))
        self.assertEqual(blocked.status, 429)

    async def test_valid_browser_proof_and_lost_response_replay(self):
        responses = await asyncio.gather(self.post(), self.post())
        self.assertEqual([r.status for r in responses], [202, 202])
        self.assertEqual(len(self.store.active()), 1)
        deadline = self.store.deadline(self.faucet.client_key("127.0.0.1"))
        self.time = VECTOR["challenge"]["expires_ms"] + 1
        replay = await self.client.post("/api/faucet/claims", json=dict(token=TOKEN, address=RECIPIENT))
        self.assertEqual(replay.status, 202)  # No second proof or payment.
        self.assertEqual(self.store.deadline(self.faucet.client_key("127.0.0.1")), deadline)
        self.assertEqual(len(self.store.active()), 1)

    async def test_no_proof_no_payment(self):
        result = await self.client.post("/api/faucet/claims", json=dict(token=TOKEN, address=RECIPIENT))
        self.assertEqual(result.status, 400)
        self.assertEqual((await self.post(None)).status, 400)
        self.assertEqual(self.store.deadline(self.faucet.client_key("127.0.0.1")), 0)

    async def test_proof_cannot_move_to_another_token_or_recipient(self):
        self.assertEqual((await self.post(token="22" * 32)).status, 400)
        self.assertEqual((await self.post(recipient="tachyon-test:" + "03" + "00" * 31)).status, 400)
        self.assertEqual(self.store.active(), [])

    async def test_expired_puzzle_and_malformed_proofs_do_not_consume_cooldown(self):
        for changes in ({"nonce": "ff" * 32}, {"indices": list(range(32))},
                        {"indices": [True] * 32}, {"indices": [0] * 33},
                        {"ticket": "unsigned"}, {"ticket": "x." + "é" * 64},
                        {"ticket": "x" * 1025}, {"extra": 1}):
            proof = {**VECTOR["proof"], **changes}
            self.assertEqual((await self.post(proof)).status, 400, changes)
        self.time = VECTOR["challenge"]["expires_ms"]
        self.assertEqual((await self.post()).status, 410)
        self.assertEqual(self.store.deadline(self.faucet.client_key("127.0.0.1")), 0)
        self.assertEqual(self.store.active(), [])

    async def test_target_is_enforced_in_addition_to_equihash(self):
        proof = deepcopy(VECTOR["proof"])
        proof["indices"] = [0] * 32
        # Even if an invalid solution passes the cheap target filter, the full
        # recursive Equihash validation must still reject it.
        with patch("tachyon_faucet.pow.meets_target", return_value=True):
            self.assertEqual((await self.post(proof)).status, 400)
        with patch("tachyon_faucet.pow.meets_target", return_value=False):
            self.assertEqual((await self.post()).status, 400)
        self.assertEqual(self.store.active(), [])

    async def test_in_progress_puzzle_survives_restart_and_difficulty_change(self):
        self.store.close()
        self.store = Store(self.config.state_dir)
        restarted = Faucet(replace(self.config, pow_difficulty_bits=8), self.store, Wallet(), lambda: self.time)
        await restarted.tick()
        self.assertEqual(restarted.claim(TOKEN, RECIPIENT, "127.0.0.1", VECTOR["proof"])["state"], "queued")
