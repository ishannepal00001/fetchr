"""
RollingCodeMiddleware: pure ASGI middleware that checks and rotates the client's code.

Why pure ASGI (not BaseHTTPMiddleware): it only touches the response-start message,
so streamed bodies pass through untouched.

Per request:
1. If scope["path"] is exempt (/health, /docs, /openapi.json), pass the request straight through.
2. Read the headers X-Client-Id and X-Fetchr-Code. If either is missing -> 401.
3. Open a DB session and call modules.auth.services.verify_and_rotate(session, client_id, code).
     - returns None -> send 401 {"detail": "unauthorized"}; the endpoint never runs.
     - returns new_code -> continue.
4. scope["state"]["client_id"] = client_id
   scope["state"]["next_code"] = new_code     (visible as request.state.* in routers)
5. Wrap `send`: on the "http.response.start" message, append the header
   X-Fetchr-Next-Code: <new_code>. The code has already rotated, so even
   4xx/5xx responses carry it and the client stays in sync.
6. await self.app(scope, receive, wrapped_send)
"""
