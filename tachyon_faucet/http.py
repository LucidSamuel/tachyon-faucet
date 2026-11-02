"""Small same-origin API; confidential receipts never appear in public status."""

import asyncio
from contextlib import suppress
import json
import math

from aiohttp import web

from .service import public_claim
from .store import Rejected


def response(data, status=200, headers=None):
    return web.json_response(data, status=status, headers=headers)


def create_app(faucet, *, start_worker=True):
    @web.middleware
    async def boundaries(request, handler):
        try:
            origin = request.headers.get("Origin")
            if ((origin and origin not in faucet.config.allowed_origins) or
                    request.headers.get("Sec-Fetch-Site") == "cross-site"):
                raise Rejected(403, "Cross-origin requests are not allowed")
            result = await handler(request)
        except Rejected as error:
            headers = {}
            if error.status == 429:
                headers["Retry-After"] = str(max(1, math.ceil(faucet.status()["remaining_ms"] / 1000)))
            result = response(dict(error=str(error), status=faucet.status()), error.status, headers)
        except (ValueError, json.JSONDecodeError):
            result = response(dict(error="Invalid request: enter a canonical Tachyon payment key and claim token"), 400)
        except TimeoutError:
            result = response(dict(error="Request body timed out"), 408)
        except web.HTTPException as error:
            result = response(dict(error=error.reason), error.status)
        result.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })
        return result

    def claim_token(request):
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise Rejected(401, "A private claim token is required")
        return authorization[7:]

    async def status(request):
        return response(faucet.status())

    async def body(request):
        if request.content_type != "application/json":
            raise Rejected(415, "Use application/json")
        async with asyncio.timeout(10):
            data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid request")
        return data

    async def challenge(request):
        data = await body(request)
        if set(data) != {"address", "token"}:
            raise ValueError("Invalid challenge request")
        return response(faucet.challenge(data["token"], data["address"]))

    async def create(request):
        data = await body(request)
        if not {"address", "token"} <= data.keys() or data.keys() - {"address", "token", "proof"}:
            raise ValueError("Invalid claim request")
        claim = faucet.claim(data["token"], data["address"], data.get("proof"))
        return response(dict(claim=claim, status=faucet.status()), 202)

    async def claim(request):
        return response(public_claim(faucet.get_claim(claim_token(request))))

    async def receipt(request):
        operation = faucet.get_claim(claim_token(request))
        if not operation["receipt"] or operation["state"] == "expired":
            raise Rejected(409, "This claim does not have an available note receipt")
        return web.Response(text=operation["receipt"] + "\n", content_type="application/json",
                            headers={"Content-Disposition": 'attachment; filename="faucet-note.json"'})

    async def lifecycle(app):
        task = asyncio.create_task(faucet.run())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app = web.Application(client_max_size=2048, middlewares=[boundaries])
    app.router.add_get("/api/faucet/status", status)
    app.router.add_post("/api/faucet/challenge", challenge)
    app.router.add_post("/api/faucet/claims", create)
    app.router.add_get("/api/faucet/claim", claim)
    app.router.add_get("/api/faucet/claim/receipt", receipt)
    if start_worker:
        app.cleanup_ctx.append(lifecycle)
    return app
