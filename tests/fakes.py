"""Synthetic wallet protocol fixtures; never connects to a node or holds funds."""

import asyncio
import hashlib
import json
from pathlib import Path

from tachyon_faucet.devtool import CliError

KEY = "01" + "00" * 31
RECIPIENT = "tachyon-test:" + KEY
NETWORK = "ab" * 32


class Wallet:
    def __init__(self):
        self.operations = {}
        self.balance = {"notes": [{"status": "available", "value_zat": 100_000_000}],
                        "transparent": {"available_zat": 0}}
        self.height = 100
        self.caught_up = True
        self.send_count = 0
        self.fail_after_save = False
        self.wrong_receipt = False
        self.receipt_locator = None
        self.calls = []

    async def lookup(self, operation_id):
        return self.operations.get(operation_id)

    async def call(self, *args):
        self.calls.append(args)
        await asyncio.sleep(0)
        command = args[0]
        if command == "address":
            return dict(network_id=NETWORK, tachyon_address="tachyon-test:" + "02" + "00" * 31,
                        funding_address="synthetic-testnet-funding-address")
        if command == "sync":
            return dict(caught_up=self.caught_up, height=self.height)
        if command == "transactions":
            return {"transactions": list(self.operations.values())}
        if command == "balance":
            return self.balance
        if command in ("send", "shield"):
            self.send_count += 1
            operation_id = args[args.index("--operation-id") + 1]
            if operation_id not in self.operations:
                request = (dict(kind="send", to=args[args.index("--to") + 1],
                                amount_zat=args[args.index("--amount-zat") + 1])
                           if command == "send" else
                           dict(kind="shield", amount_zat=args[1], txid=None, index=None))
                self.operations[operation_id] = dict(
                    operation_id=operation_id, request=request,
                    txid=hashlib.sha256(operation_id.encode()).hexdigest(), status="pending")
            if self.fail_after_save:
                self.fail_after_save = False
                raise CliError(command, 1, "broadcast timed out after saving payment")
            return dict(status="submitted")
        if command == "submit":
            return dict(status="submitted")
        if command == "export-receipt":
            saved = next(op for op in self.operations.values() if op["txid"] == args[2])
            receipt = dict(version=1, network_id=NETWORK, payment_key=KEY,
                           value_zat=saved["request"]["amount_zat"],
                           psi="03" + "00" * 31, rcm="04" + "00" * 31)
            if self.wrong_receipt:
                receipt["value_zat"] += 1
            if self.receipt_locator is not None:
                receipt["locator"] = {"txid": saved["txid"], **self.receipt_locator}
            path = Path(args[4])
            path.write_text(json.dumps(receipt))
            path.chmod(0o600)
            return dict(txid=saved["txid"], receipt=str(path))
        raise AssertionError(command)
