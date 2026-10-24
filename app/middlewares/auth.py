"""
RollingCodeMiddleware: pure ASGI middleware that checks and rotates the client's code.

Why pure ASGI (not BaseHTTPMiddleware): it only touches the response-start message,
so streamed bodies and background tasks pass through untouched.

Per request:
1. Exempt paths (/health, docs) pass straight through.
2. Read X-Client-Id and X-Fetchr-Code; AuthService.verify_and_rotate() runs in a
   worker thread (it does blocking DB work). Missing headers or an invalid code
   -> 401, and the endpoint never runs.
3. scope["state"]["client_id"] / ["next_code"] are set (request.state.* in routers).
4. Every response from here on, including 4xx/5xx, carries X-Fetchr-Next-Code:
   the code has already rotated, so the client must always receive the new one.
   An exception nobody caught would otherwise become a 500 written by Starlette's
   outermost ServerErrorMiddleware, which never passes through here. So this
   middleware sends that 500 itself (with the header), then re-raises so the
   traceback is still logged.
"""

from collections.abc import Iterable

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.modules.auth.services import AuthService

CLIENT_ID_HEADER = "X-Client-Id"
CODE_HEADER = "X-Fetchr-Code"
NEXT_CODE_HEADER = "X-Fetchr-Next-Code"

DEFAULT_EXEMPT_PATHS = frozenset({"/health", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


class RollingCodeMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        auth: AuthService,
        exempt_paths: Iterable[str] = DEFAULT_EXEMPT_PATHS,
    ) -> None:
        self.app = app
        self.auth = auth
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        client_id = headers.get(CLIENT_ID_HEADER)
        code = headers.get(CODE_HEADER)
        next_code = None
        if client_id and code:
            next_code = await run_in_threadpool(self.auth.verify_and_rotate, client_id, code)

        if next_code is None:
            response = JSONResponse({"detail": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["client_id"] = client_id
        state["next_code"] = next_code

        response_started = False

        async def send_with_next_code(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                MutableHeaders(scope=message).append(NEXT_CODE_HEADER, next_code)
            await send(message)

        try:
            await self.app(scope, receive, send_with_next_code)
        except Exception:
            # Already started (e.g. a background task failing after the response was
            # sent): the client has its code, nothing more to send.
            if not response_started:
                response = JSONResponse({"detail": "internal server error"}, status_code=500)
                await response(scope, receive, send_with_next_code)
            raise
