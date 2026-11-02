"""Equihash(96,5) client puzzles, independent of the chain's mining rules.

Uses the Zcash Equihash BLAKE2b personalization and recursive collision/order
rules. The difficulty hash commits to the entire canonical solution, not just
the nonce. Only verification runs here; browsers do the memory-heavy search.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import struct

from .store import Rejected

N, K = 96, 5
PERSON = b"ZcashPoW" + struct.pack("<II", N, K)
INPUT_DOMAIN = b"TachyonFaucet/Equihash/v1\0"
TARGET_DOMAIN = b"TachyonFaucet/Target/v1\0"
HEX32 = re.compile(r"[0-9a-f]{64}\Z")


def valid_equihash(data, nonce, indices):
    if (not isinstance(indices, list) or len(indices) != 32 or
            any(type(i) is not int or not 0 <= i < 131072 for i in indices) or
            len(set(indices)) != 32):
        return False
    # Five 96-bit leaves per 60-byte digest; the group index is little-endian.
    state = hashlib.blake2b(data + nonce, digest_size=60, person=PERSON)
    rows = []
    for index in indices:
        group = state.copy()
        group.update(struct.pack("<I", index // 5))
        start = index % 5 * 12
        rows.append((group.digest()[start:start + 12], index))
    while len(rows) > 1:
        merged = []
        for a, b in zip(rows[::2], rows[1::2]):
            if a[1] >= b[1] or a[0][:2] != b[0][:2]:
                return False
            merged.append((bytes(x ^ y for x, y in zip(a[0][2:], b[0][2:])), a[1]))
        rows = merged
    return rows[0][0] == b"\0\0"


def meets_target(data, nonce, indices, bits):
    digest = hashlib.sha256(TARGET_DOMAIN + data + nonce + struct.pack(">32I", *indices)).digest()
    return int.from_bytes(digest, "big") < 1 << (256 - bits)


class Challenges:
    def __init__(self, key, config, clock):
        self.key, self.config, self.clock = key, config, clock

    def issue(self, token_hash, recipient):
        expires = self.clock() + self.config.pow_challenge_seconds * 1000
        payload = dict(version=1, salt=secrets.token_hex(32), token_hash=token_hash,
                       address=recipient, expires_ms=expires,
                       difficulty_bits=self.config.pow_difficulty_bits)
        encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True,
                                                     separators=(",", ":")).encode()).decode()
        signature = hmac.digest(self.key, encoded.encode(), "sha256").hex()
        ticket = encoded + "." + signature
        return dict(ticket=ticket, input=hashlib.sha256(INPUT_DOMAIN + ticket.encode()).hexdigest(),
                    n=N, k=K, difficulty_bits=payload["difficulty_bits"], expires_ms=expires,
                    valid_for_ms=self.config.pow_challenge_seconds * 1000)

    def verify(self, proof, token_hash, recipient):
        if not isinstance(proof, dict) or set(proof) != {"ticket", "nonce", "indices"}:
            raise Rejected(400, "An Equihash proof is required for a new claim")
        ticket, nonce_hex, indices = proof["ticket"], proof["nonce"], proof["indices"]
        if (not isinstance(ticket, str) or len(ticket) > 1024 or
                not isinstance(nonce_hex, str) or not HEX32.fullmatch(nonce_hex) or
                not isinstance(indices, list) or len(indices) != 32 or
                any(type(i) is not int or not 0 <= i < 131072 for i in indices)):
            raise Rejected(400, "Invalid Equihash proof")
        try:
            encoded, signature = ticket.split(".")
            if not HEX32.fullmatch(signature):
                raise ValueError("Invalid signature")
            expected = hmac.digest(self.key, encoded.encode("ascii"), "sha256").hex()
            if not hmac.compare_digest(expected, signature):
                raise ValueError("Invalid signature")
            payload = json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True))
        except (ValueError, UnicodeError):
            raise Rejected(400, "Invalid Equihash challenge") from None
        if (not isinstance(payload, dict) or payload.get("version") != 1 or
                payload.get("token_hash") != token_hash or payload.get("address") != recipient or
                type(payload.get("expires_ms")) is not int or
                type(payload.get("difficulty_bits")) is not int or
                not 0 <= payload["difficulty_bits"] <= 16):
            raise Rejected(400, "This Equihash challenge belongs to another request")
        if self.clock() >= payload["expires_ms"]:
            raise Rejected(410, "The puzzle expired. Please start again")
        data = hashlib.sha256(INPUT_DOMAIN + ticket.encode()).digest()
        nonce = bytes.fromhex(nonce_hex)
        if not meets_target(data, nonce, indices, payload["difficulty_bits"]):
            raise Rejected(400, "Equihash proof does not meet the challenge difficulty")
        if not valid_equihash(data, nonce, indices):
            raise Rejected(400, "Invalid Equihash solution")
