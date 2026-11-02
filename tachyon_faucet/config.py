"""Strict configuration and canonical experimental payment-key encoding."""

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from .client_ip import canonical_ip

FEE = 50_000
MAX_MONEY = 21_000_000 * 100_000_000
# pasta_curves::Fp uses canonical little-endian 32-byte encodings.
PALLAS_BASE = 0x40000000000000000000000000000000224698FC094CF91B992D30ED00000001
HEX32 = re.compile(r"[0-9a-fA-F]{64}\Z")


def field(value):
    if not isinstance(value, str) or not HEX32.fullmatch(value):
        raise ValueError("Expected a 64-character hexadecimal payment key")
    if int.from_bytes(bytes.fromhex(value), "little") >= PALLAS_BASE:
        raise ValueError("Payment key is not a canonical Tachyon field encoding")
    return value.lower()


def address(value):
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("Enter a Tachyon payment key or tachyon-test: address")
    value = value.strip()
    return "tachyon-test:" + field(value.removeprefix("tachyon-test:"))


def token_digest(value):
    import hashlib
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("A 32-byte random claim token is required")
    return hashlib.sha256(bytes.fromhex(value)).hexdigest()


@dataclass(frozen=True)
class Config:
    devtool: Path
    wallet_dir: Path
    state_dir: Path
    host: str = "127.0.0.1"
    port: int = 4142
    allowed_origins: tuple = ("https://tachyscan.cnode.phd",)
    trusted_proxy_ips: tuple = ()
    amount_zat: int = 1_000_000
    cooldown_seconds: int = 300
    auto_shield: bool = True
    shield_amount_zat: int = 100_000_000
    poll_seconds: int = 15
    command_timeout: int = 1800
    pow_difficulty_bits: int = 7
    pow_challenge_seconds: int = 600


def load_config(path):
    path = Path(path).resolve()
    with path.open("rb") as source:
        values = tomllib.load(source)
    unknown = set(values) - set(Config.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown config fields: {sorted(unknown)}")
    for key in ("devtool", "wallet_dir", "state_dir"):
        value = values.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a nonempty path")
        values[key] = (path.parent / Path(value).expanduser()).absolute()
    config = Config(**values)
    for key, low, high in (
        ("port", 1, 65535), ("amount_zat", 1, MAX_MONEY - FEE),
        ("cooldown_seconds", 1, 86400 * 30), ("poll_seconds", 1, 300),
        ("command_timeout", 10, 7200), ("shield_amount_zat", 1, MAX_MONEY - FEE),
        ("pow_difficulty_bits", 0, 16), ("pow_challenge_seconds", 60, 3600),
    ):
        value = getattr(config, key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer in {low}..{high}")
    if type(config.auto_shield) is not bool:
        raise ValueError("auto_shield must be true or false")
    if config.shield_amount_zat < config.amount_zat + FEE:
        raise ValueError("shield_amount_zat must cover a claim plus its fee")
    if not isinstance(config.host, str) or not config.host:
        raise ValueError("host must be nonempty")
    if not isinstance(config.trusted_proxy_ips, (list, tuple)):
        raise ValueError("trusted_proxy_ips must be a list of literal IP addresses")
    for proxy in config.trusted_proxy_ips:
        canonical_ip(proxy)
    if not isinstance(config.allowed_origins, (list, tuple)) or not config.allowed_origins:
        raise ValueError("allowed_origins must be a nonempty list")
    for origin in config.allowed_origins:
        if not isinstance(origin, str):
            raise ValueError("allowed_origins must contain HTTP(S) origins")
        url = urlsplit(origin)
        if (url.scheme not in ("http", "https") or not url.netloc or url.path or
                url.query or url.fragment or url.username or url.password):
            raise ValueError("allowed_origins must contain HTTP(S) origins without paths")
    return config
