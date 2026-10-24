"""
Auth services: rolling one-time codes. Framework agnostic: no HTTP, no FastAPI.
Called by app/middlewares/auth.py; the session factory and grace window are injected.
"""

from collections.abc import Callable
from datetime import timedelta

from sqlmodel import Session, select, update

from app.core.security import generate_code, hash_code
from app.modules.auth.model import ClientCredential, utcnow


class AuthService:
    def __init__(self, session_factory: Callable[[], Session], grace_seconds: int = 60) -> None:
        self._session_factory = session_factory
        self._grace = timedelta(seconds=grace_seconds)

    def seed_client(self, client_id: str, seed_code: str) -> None:
        """On startup: store the hash of the hardcoded first code, unless the client already exists."""
        with self._session_factory() as session:
            exists = session.exec(select(ClientCredential).where(ClientCredential.client_id == client_id)).first()
            if exists is None:
                session.add(ClientCredential(client_id=client_id, code_hash=hash_code(seed_code)))
                session.commit()

    def verify_and_rotate(self, client_id: str, code: str) -> str | None:
        """
        Valid code -> replace it and return the new one. Invalid -> None.

        Each check is a single UPDATE ... WHERE code_hash = <hash of sent code>, so two
        requests racing with the same code can't both succeed: the first changes the
        row, the second matches nothing.

        Grace window: if the client lost the response carrying its new code, it will
        resend the previous one. That is accepted once within grace_seconds of the last
        rotation; prev_code_hash is then cleared so it can't be replayed.
        """
        sent_hash = hash_code(code)
        new_code = generate_code()
        now = utcnow()

        with self._session_factory() as session:
            rotated = session.exec(
                update(ClientCredential)
                .where(ClientCredential.client_id == client_id, ClientCredential.code_hash == sent_hash)
                .values(prev_code_hash=sent_hash, code_hash=hash_code(new_code), rotated_at=now)
            )
            if rotated.rowcount == 0:
                rotated = session.exec(
                    update(ClientCredential)
                    .where(
                        ClientCredential.client_id == client_id,
                        ClientCredential.prev_code_hash == sent_hash,
                        ClientCredential.rotated_at >= now - self._grace,
                    )
                    .values(prev_code_hash=None, code_hash=hash_code(new_code), rotated_at=now)
                )
            session.commit()

        return new_code if rotated.rowcount == 1 else None

    def reset_code(self, client_id: str) -> str:
        """Admin recovery: set a brand-new code for the client (creating it if needed) and return it."""
        new_code = generate_code()
        with self._session_factory() as session:
            credential = session.exec(select(ClientCredential).where(ClientCredential.client_id == client_id)).first()
            if credential is None:
                credential = ClientCredential(client_id=client_id, code_hash="")
            credential.code_hash = hash_code(new_code)
            credential.prev_code_hash = None
            credential.rotated_at = utcnow()
            session.add(credential)
            session.commit()
        return new_code
