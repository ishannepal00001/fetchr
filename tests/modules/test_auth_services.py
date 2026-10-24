"""modules/auth/services tests."""

import threading
from datetime import timedelta

from sqlmodel import select

from app.core.database import create_db_and_tables, make_engine, make_session_factory
from app.core.security import hash_code
from app.modules.auth.model import ClientCredential, utcnow
from app.modules.auth.services import AuthService


def credential(session_factory, client_id="alice") -> ClientCredential:
    with session_factory() as session:
        return session.exec(select(ClientCredential).where(ClientCredential.client_id == client_id)).one()


def test_seed_client_stores_hash_and_is_idempotent(session_factory):
    auth = AuthService(session_factory)

    auth.seed_client("alice", "seed")
    auth.seed_client("alice", "other-seed")  # existing client is left alone

    stored = credential(session_factory)
    assert stored.code_hash == hash_code("seed")
    assert "seed" not in stored.code_hash  # plaintext never stored


def test_verify_and_rotate_returns_new_code_and_invalidates_old(session_factory):
    auth = AuthService(session_factory, grace_seconds=0)
    auth.seed_client("alice", "seed")

    new_code = auth.verify_and_rotate("alice", "seed")

    assert new_code and new_code != "seed"
    assert credential(session_factory).code_hash == hash_code(new_code)
    assert auth.verify_and_rotate("alice", "seed") is None  # old code is dead
    assert auth.verify_and_rotate("alice", new_code) is not None


def test_wrong_code_or_unknown_client_is_rejected(session_factory):
    auth = AuthService(session_factory)
    auth.seed_client("alice", "seed")

    assert auth.verify_and_rotate("alice", "wrong") is None
    assert auth.verify_and_rotate("bob", "seed") is None
    assert credential(session_factory).code_hash == hash_code("seed")  # unchanged


def test_grace_window_accepts_previous_code_once(session_factory):
    auth = AuthService(session_factory, grace_seconds=60)
    auth.seed_client("alice", "seed")
    lost_code = auth.verify_and_rotate("alice", "seed")  # response "lost": client never sees it

    recovered = auth.verify_and_rotate("alice", "seed")

    assert recovered is not None
    assert auth.verify_and_rotate("alice", "seed") is None  # only once
    assert auth.verify_and_rotate("alice", lost_code) is None  # lost code superseded
    assert auth.verify_and_rotate("alice", recovered) is not None


def test_grace_window_expires(session_factory):
    auth = AuthService(session_factory, grace_seconds=60)
    auth.seed_client("alice", "seed")
    auth.verify_and_rotate("alice", "seed")
    with session_factory() as session:
        stored = session.exec(select(ClientCredential)).one()
        stored.rotated_at = utcnow() - timedelta(seconds=61)
        session.add(stored)
        session.commit()

    assert auth.verify_and_rotate("alice", "seed") is None


def test_concurrent_requests_with_same_code_only_one_succeeds(tmp_path):
    # A file DB, so each thread gets its own real connection.
    engine = make_engine(f"sqlite:///{tmp_path / 'race.db'}")
    create_db_and_tables(engine)
    auth = AuthService(make_session_factory(engine), grace_seconds=0)
    auth.seed_client("alice", "seed")

    barrier = threading.Barrier(8)
    results = []

    def attempt():
        barrier.wait()
        results.append(auth.verify_and_rotate("alice", "seed"))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    engine.dispose()

    assert len(results) == 8  # every attempt finished (none crashed on a locked DB)
    assert len([r for r in results if r is not None]) == 1


def test_reset_code_replaces_code(session_factory):
    auth = AuthService(session_factory)
    auth.seed_client("alice", "seed")

    fresh = auth.reset_code("alice")

    assert auth.verify_and_rotate("alice", "seed") is None
    assert auth.verify_and_rotate("alice", fresh) is not None


def test_reset_code_creates_missing_client(session_factory):
    auth = AuthService(session_factory)

    fresh = auth.reset_code("new-client")

    assert auth.verify_and_rotate("new-client", fresh) is not None
