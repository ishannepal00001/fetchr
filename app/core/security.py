"""Pure crypto helpers for the rolling code. No DB and no HTTP."""

import hashlib
import secrets


def generate_code() -> str:
    """A fresh 256-bit random code, URL-safe so it fits in a header."""
    return secrets.token_urlsafe(32)


def hash_code(code: str) -> str:
    """sha256 hex digest. Only hashes are stored; plaintext codes never are.

    A fast hash is fine here: codes are 256-bit random tokens, not passwords,
    so there is nothing to brute-force.
    """
    return hashlib.sha256(code.encode()).hexdigest()
