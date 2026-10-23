"""
Auth services, called by app/middlewares/auth.py.

Planned contents:
- seed_client(session, client_id, seed_code) -> None
    On startup: if no row exists for client_id, insert one with code_hash = hash(seed_code).

- verify_and_rotate(session, client_id, code) -> str | None
    new_code = generate_code()
    Atomic, race-safe rotation in a single statement:
        UPDATE clientcredential
           SET prev_code_hash = code_hash, code_hash = :new_hash, rotated_at = now
         WHERE client_id = :client_id AND code_hash = :old_hash
    rowcount == 1 -> return new_code
    rowcount == 0 -> grace check: if hash(code) == prev_code_hash and
                     rotated_at is within FETCHR_CODE_GRACE_SECONDS, accept once
                     (rotate again, clear prev_code_hash) and return the fresh code;
                     otherwise return None.

- reset_code(session, client_id) -> str
    Admin recovery: set a brand-new code and return it (later exposed as a CLI command).
"""
