"""
Pure crypto helpers for the rolling code. No DB and no HTTP.

Planned contents:
- generate_code() -> str: secrets.token_urlsafe(32)
- hash_code(code) -> str: sha256 hex digest; plaintext codes are never stored
- codes_match(a, b) -> bool: hmac.compare_digest (constant-time comparison)
"""
