"""FastAPI surface: GET /supported, POST /verify, POST /settle, GET /health."""

from __future__ import annotations

import collections
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .logging import log, setup_logging
from .service import FacilitatorService, RequestError
from .store import Database, ReplayStore

logger = logging.getLogger("humanapi.facilitator.app")


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, collections.deque[float]] = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.time()
        with self.lock:
            q = self.hits[ip]
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def build_service(settings: Settings) -> FacilitatorService:
    from .rails import build

    store = ReplayStore(Database(settings.database_url))
    facilitator, rails = build(settings, store)
    return FacilitatorService(settings, store, facilitator, rails)


def create_app(settings: Settings | None = None, service: FacilitatorService | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service or build_service(settings)
        app.state.service = svc
        try:
            n = svc.recover()
            log(logger, logging.INFO, "startup recovery", resolved=n, rails=list(svc.rails))
        except Exception as e:
            log(logger, logging.ERROR, "startup recovery failed", error=str(e)[:300])

        def loop() -> None:
            while not stop.wait(60):
                try:
                    svc.recover()
                except Exception as e:
                    log(logger, logging.WARNING, "recovery loop error", error=str(e)[:300])

        t = threading.Thread(target=loop, name="recovery", daemon=True)
        t.start()
        yield
        stop.set()

    app = FastAPI(title="H402 facilitator", version=__version__, lifespan=lifespan)

    def svc(request: Request) -> FacilitatorService:
        return request.app.state.service

    def client_ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def error(status: int, reason: str, message: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": reason, "message": message})

    @app.exception_handler(RequestError)
    async def _request_error(_: Request, e: RequestError):
        return error(e.status, e.reason, e.message)

    @app.get("/")
    async def root():
        return {"service": "humanapi-facilitator", "version": __version__, "endpoints": ["/supported", "/verify", "/settle", "/gas", "/health"]}

    @app.get("/supported")
    async def supported(request: Request):
        return svc(request).supported()

    @app.get("/health")
    async def health(request: Request):
        data = await run_in_threadpool(svc(request).health)
        return JSONResponse(status_code=200 if data["ok"] else 503, content=data)

    async def body_of(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            raise RequestError(400, "invalid_request", "body must be JSON") from None
        if not isinstance(body, dict):
            raise RequestError(400, "invalid_request", "body must be a JSON object")
        return body

    @app.post("/verify")
    async def verify(request: Request):
        if not limiter.allow(client_ip(request)):
            return error(429, "rate_limited", "too many requests")
        body = await body_of(request)
        return await run_in_threadpool(svc(request).verify, body)

    @app.post("/settle")
    async def settle(request: Request):
        if settings.settle_token:
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {settings.settle_token}":
                return error(401, "unauthorized", "a bearer token is required to settle")
        if not limiter.allow(client_ip(request)):
            return error(429, "rate_limited", "too many requests")
        body = await body_of(request)
        return await run_in_threadpool(svc(request).settle, body)

    @app.post("/gas")
    async def gas(request: Request):
        """A gas grant for a worker's wallet. Same token as settle: only the task service may ask."""
        if settings.settle_token:
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {settings.settle_token}":
                return error(401, "unauthorized", "a bearer token is required")
        if not limiter.allow(client_ip(request)):
            return error(429, "rate_limited", "too many requests")
        body = await body_of(request)
        return await run_in_threadpool(svc(request).gas_grant, body)

    return app
