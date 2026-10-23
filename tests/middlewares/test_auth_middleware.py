"""
RollingCodeMiddleware tests (planned):
- valid code -> 2xx and an X-Fetchr-Next-Code header
- missing/wrong code -> 401 and the endpoint is not called
- reused code -> 401
- exempt paths (/health, /docs) need no code
- an error response (e.g. 422) still carries X-Fetchr-Next-Code
"""
