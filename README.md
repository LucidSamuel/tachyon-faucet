# Tachyon faucet

A small Python 3.11+ service for the Tachyon Alpha Testnet. Tachyscan accepts a
payment key, this service calls `zcash-devtool`, and the visitor downloads the
recipient note JSON. Devtool retains all keys, signing, proof construction,
input selection, and chain validation. There is no trial decryption: the recipient
must import that JSON into the wallet that owns the payment key.

Defaults: **0.01 testnet ZEC per claim**, **one claim every five minutes per IP**.
New claims also require a browser Equihash puzzle. Tachyscan solves it in a Web
Worker and displays an animated activity bar, elapsed time, and a cancel button.
Each IP's cooldown starts when its claim is accepted, survives restarts, and is
shown only to visitors using that IP. A pending payment or wallet refill can keep the faucet unavailable
after the timer expires. This service uses the experimental mock-proof wallet and
must only hold testnet funds.

## Install and configure

```sh
cd ~/tachyon-faucet
python3 -m venv .venv
.venv/bin/python -m pip install .
cp config.example.toml config.local.toml
```

Edit `config.local.toml`. Set `devtool` to the binary built with Tachyon support,
`wallet_dir` to your **existing dedicated faucet wallet**, and `state_dir` to a
new private service-state directory. Paths can be absolute or relative to the
config file. The wallet's RPC URL and credentials come from its own initialization;
the HTTP service does not take arbitrary RPC URLs or devtool commands from clients.

To create a wallet only if you do not already have one:

```sh
umask 077
mkdir -p ~/tachyon-faucet-data
~/zcash-devtool/target/release/zcash-devtool tachyon \
  --wallet-dir ~/tachyon-faucet-data/wallet init \
  --rpc-url http://127.0.0.1:18232 --activation-height 8 --accept-mock-proofs
~/zcash-devtool/target/release/zcash-devtool tachyon \
  --wallet-dir ~/tachyon-faucet-data/wallet address
```

Fund the returned **`funding_address`**, or keep the workload's `[faucet].address`
pointing to that address. Reuse the wallet that owns your configured funding
address; creating a new wallet does not recover the old one's funds. The service
syncs automatically and shields transparent funds when it needs another spendable
Tachyon note. No manual receipt import is needed for incoming transparent funding.

Devtool selects one input per payment. Automatic refill needs a confirmed,
unreserved **non-coinbase UTXO** large enough for the refill plus its 50,000-zatoshi
fee; separate smaller UTXOs are not consolidated. The default refill is up to
1 ZEC. Set `shield_amount_zat` lower for smaller funding payments, but keep it at
least `amount_zat + 50000`. If funding directly from coinbases, shield those in
full with devtool before starting the service. Do not operate the same wallet
concurrently with another service or manual spending commands.

| Setting | Default | Purpose |
| --- | --- | --- |
| `devtool` | Required | Path to the executable |
| `wallet_dir` | Required | Dedicated initialized devtool wallet |
| `state_dir` | Required | Private durable claims and receipt storage |
| `host`, `port` | `127.0.0.1`, `4142` | Local HTTP listener |
| `allowed_origins` | `["https://tachyscan.cnode.phd"]` | Exact frontend origins, without trailing slashes |
| `trusted_proxy_ips` | `[]` | Literal IPs of reverse proxies allowed to supply `X-Real-IP` |
| `amount_zat` | `1000000` | Recipient amount; fees are additional |
| `cooldown_seconds` | `300` | Minimum interval between accepted claims from the same IP |
| `pow_difficulty_bits` | `7` | Equihash solution target, 0–16; each extra bit doubles expected work |
| `pow_challenge_seconds` | `600` | Challenge lifetime, 60–3600 seconds |
| `auto_shield` | `true` | Replenish Tachyon funds from transparent funds |
| `shield_amount_zat` | `100000000` | Maximum refill amount |
| `poll_seconds` | `15` | Wallet sync/recovery interval |
| `command_timeout` | `1800` | Maximum seconds per devtool command |

Restart after configuration changes. Existing claims retain their original
amounts, recipients, and cooldown deadlines. Wallet and state directories must be
mode `0700`; the service creates its SQLite files with mode `0600`.

## Per-IP cooldown and reverse proxy

Keep `cooldown_seconds = 300` for five minutes per IP. There is no global timer.
The wallet still handles one payment at a time: other visitors can see
`processing` until that payment confirms, then claim immediately without waiting
for the first IP's cooldown. People sharing a public IP share the limit. IPv6
addresses are counted individually; equivalent IPv6 spellings and IPv4-mapped
IPv6 addresses are normalized.

For the existing local nginx deployment, add this to the faucet's config:

```toml
trusted_proxy_ips = ["127.0.0.1", "::1"]
```

Add this line inside nginx's existing `/api/faucet/` location (also included in
`deploy/nginx-faucet.conf`):

```nginx
proxy_set_header X-Real-IP $remote_addr;
```

Both changes are required. nginx must **overwrite** the header, never copy a
visitor-supplied value. The faucet trusts it only when the actual socket peer is
in `trusted_proxy_ips`; other peers are identified by their socket address and
their forwarding headers are ignored. A trusted peer with a missing, duplicate,
or malformed `X-Real-IP` gets HTTP 400 rather than silently sharing one proxy
cooldown. Keep port 4142 local. If a CDN sits in front of nginx, configure nginx's
real-IP handling for that CDN's trusted addresses first; otherwise the CDN's
egress IP will be counted. Without a proxy, leave `trusted_proxy_ips = []`.

Cooldown keys are HMACs of canonical IPs, using the private persistent service
key; raw IPs are not written to SQLite or returned by the API. Expired entries
are removed when a new claim is admitted. A retry with an already accepted claim
token recovers that payment without consuming a cooldown on a new IP.

Upgrading from the global timer creates the per-IP table automatically. The old
global deadline is ignored because earlier claims have no recorded IP; existing
claims and receipts are preserved. **Do not reset either database.**

## Browser proof of work

The puzzle is Equihash(96,5), with Zcash's BLAKE2b personalization, collision
rules, distinct indices, and canonical subtree ordering. It is separate from
Zakura's block mining; no node or devtool changes are required. The browser uses
bounded typed-array tables and `@noble/hashes` in a dedicated Worker, without a
WASM toolchain, native extension, or cross-origin isolation requirement. The
Python service verifies just 32 leaves using the standard-library `hashlib`.

Difficulty is a SHA-256 target over the challenge input, nonce, and entire
solution. Seven bits accepts about 1 in 128 valid Equihash solutions; zero still
requires a valid Equihash solution. The default is a starting point for a short
interactive wait, **not a calibrated 30-second M4 guarantee**. Measure several
claims in the target browsers; increase/decrease the setting by one to roughly
double/halve expected solve time without increasing table sizes. The search is
random, so the UI shows activity rather than a misleading completion percentage.
Cancellation and navigation immediately terminate the Worker.

Challenges are HMAC-signed, expire, and bind the recipient and private claim
token. Issuing one does not reserve funds or start an IP cooldown. If another
visitor claims first, wait until the wallet is ready and retry the saved proof
before it expires; only a claim from the same IP starts that IP's cooldown.
The signing key is created
automatically in the existing private SQLite metadata and survives restarts.
An outstanding signed challenge retains its issued difficulty after a config
change, until expiry. Once accepted, its token only recovers the original claim;
retries do not require another proof, even after expiry. Both unsigned claims and
proofs moved to another token/recipient are rejected.

Deploy **both the faucet service and the rebuilt Tachyscan frontend** for this
change; old frontend code cannot create new claims. Existing claims and receipt
downloads keep working. No wallet/database reset is needed. Keep the existing
nginx `/api/faucet/` proxy with the IP-header configuration above; the new
challenge endpoint uses the same route.

After copying/pulling the changes and updating the faucet and nginx configs above:

```sh
cd ~/tachyon-faucet
.venv/bin/python -m pip install .
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl restart tachyon-faucet
cd ~/tachyscan
bun install
bun run build
sudo systemctl restart tachyscan
```

Tachyscan's Rust binary is unchanged and serves the rebuilt `dist/` assets.

## Allow the workload miner to include faucet transactions

If `tachyon-workload` controls mining with `generate`, update it to the version
with this option and add it **at the top level** of its config, before any tables:

```toml
allow_external_transactions = true
```

Restart the workload service. Its default strict mode rejects transactions from
other wallets, including faucet refills and sends. With this option, its blocks
may contain user/faucet payments in addition to the sampled workload count.
Keep it as the sole miner, with node-side `tachyon_workload = false`.
Live workload mode with an internal miner already accepts outside traffic.

## Run with systemd

First run in the foreground to check the paths and wallet:

```sh
.venv/bin/python -m tachyon_faucet --config config.local.toml
# In a second terminal:
curl -s -H 'X-Real-IP: 127.0.0.1' http://127.0.0.1:4142/api/faucet/status
```

Initial synchronization can take time. Status reports `syncing`, `funding`, or
`empty` until the wallet is ready. Stop the foreground process before installing
the service. Edit `deploy/tachyon-faucet.service` if your paths differ, including
`ReadWritePaths` for the actual wallet and service-state directories:

```sh
sudo install -m 644 deploy/tachyon-faucet.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tachyon-faucet
journalctl -u tachyon-faucet -f -o cat
```

The service invokes one wallet command at a time. Timeouts and shutdown terminate
the child process; the next run recovers its saved operation before trying again.
Logs omit wallet output, note contents, claim tokens, and RPC credentials. On a
persistent devtool failure, stop the service and run `sync`/`balance` for this wallet
directly to inspect its error. Fix the cause and restart with the same state.

## Connect Tachyscan

Add the location in [deploy/nginx-faucet.conf](deploy/nginx-faucet.conf) **inside
the existing HTTPS server block** for `tachyscan.cnode.phd`. Keep Tachyscan's other
locations. The browser uses same-origin `/api/faucet/`; do not expose port 4142
publicly or route wallet/state files through nginx.

```sh
sudo nginx -t
sudo systemctl reload nginx
```

Build and restart Tachyscan using its normal deployment workflow. Its faucet page
now accepts a bare payment key or `tachyon-test:` address, polls its IP's status,
and downloads the exact note JSON that devtool's `receive --receipt` accepts.
The browser saves a private random claim token in session storage before sending.
Refreshing the same tab recovers its payment; a lost response can be retried
without creating a second payment. Download the note before closing the tab.

For frontend development, set `trusted_proxy_ips = []`, add
`"http://127.0.0.1:5173"` to `allowed_origins`, run
this service on 4142, and run `bun run dev` in Tachyscan. Vite proxies the faucet
API separately from the explorer API. HTTP on localhost works; public deployment
needs HTTPS for the browser's secure random-number API.
Local development through Vite counts requests as localhost; use nginx to test
multiple clients' real IPs.

## Recovery and storage

Keep **both the entire wallet directory and the entire faucet state directory**.
The journal binds itself to the wallet path and chain/address identity. A restart
does not reset the cooldown or create replacement payments. An expired payment is
marked expired, and its receipt is no longer offered; the visitor can explicitly
request a new one after the cooldown. Reorgs can return mined claims to pending.

The state directory holds private receipt JSON inside SQLite. Never commit or
publicly serve it. Receipts are retained for recovery; there is no automatic
retention policy in this first version. Do not erase either database to fix a
failed payment, or run multiple service instances against the same wallet. One
process lock protects a state directory; separate state directories cannot safely
share a wallet. After regenesis, use a new wallet and new state together.

## HTTP API

All responses use `Cache-Control: no-store`. Claim bodies are limited to 2 KiB.
Clients cannot choose payment amounts. Cross-origin browser requests are rejected.

| Request | Result |
| --- | --- |
| `GET /api/faucet/status` | Public amount, availability, server time, `cooldown_scope: "ip"`, and this IP's `remaining_ms` |
| `POST /api/faucet/challenge` | `{ "address": "tachyon-test:…", "token": "<32 random bytes as lowercase hex>" }`; returns a signed puzzle while the faucet is available |
| `POST /api/faucet/claims` | Same address/token plus `proof: {ticket, nonce, indices}`; returns claim and status. Existing accepted tokens may omit the proof |
| `GET /api/faucet/claim` | Private claim state; requires `Authorization: Bearer <token>` |
| `GET /api/faucet/claim/receipt` | Exact recipient note JSON attachment; same authorization |

A new claim returns 202; a per-IP cooldown returns 429 with `Retry-After`; an
unready wallet returns 503. Reusing the same token and address returns the same
claim, including during cooldown. Never put the token in a URL. Status contains
no IP addresses, IP hashes, recipient keys, or receipts. Every new claim still
requires a valid Equihash proof, regardless of which IP submits it.

The challenge response contains `ticket`, `input` (32-byte hex), `n: 96`, `k: 5`,
`difficulty_bits`, `expires_ms`, and `valid_for_ms`. Expired proofs return 410;
missing/invalid proofs return 400, without consuming the cooldown. Proofs use a
32-byte hex nonce and 32 integer indices (each less than 131072), in Equihash tree
order. The input is `SHA256("TachyonFaucet/Equihash/v1\0" || ASCII(ticket))`.
The difficulty hash is `SHA256("TachyonFaucet/Target/v1\0" || input || nonce ||
indices_as_big_endian_u32)` and must have at least `difficulty_bits` leading zero
bits. Here `\0` denotes one NUL byte. Each leaf uses the standard Equihash
`BLAKE2b-480(input || nonce || LE32(index / 5))` with personalization
`"ZcashPoW" || LE32(96) || LE32(5)`, taking the index's 12-byte segment.

## Tests

```sh
.venv/bin/python -m unittest discover -v
```

Tests use synthetic wallets and temporary directories: concurrent admission,
cooldown persistence, ambiguous broadcasts, restart/reorg/expiry recovery,
refilling, receipt validation/privacy, and subprocess cancellation. They do not
connect to a live node or send funds.
Equihash tests include upstream librustzcash vectors, a shared browser/Python
fixture, malformed/expired/bound proofs, concurrent replay, and restart recovery.
IP tests cover independent cooldowns, same-IP races, trusted proxy boundaries,
address normalization, persistence, legacy migration, and private storage.
