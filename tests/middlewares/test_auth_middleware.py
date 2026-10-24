"""RollingCodeMiddleware tests, against a small throwaway FastAPI app."""

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.middlewares.auth import NEXT_CODE_HEADER, RollingCodeMiddleware
from app.modules.auth.services import AuthService

CALLS: list[str] = []


@pytest.fixture
def client(session_factory):
    auth = AuthService(session_factory, grace_seconds=0)
    auth.seed_client("alice", "seed")

    app = FastAPI()
    app.add_middleware(RollingCodeMiddleware, auth=auth)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/whoami")
    def whoami(request: Request):
        CALLS.append(request.state.client_id)
        return {"client_id": request.state.client_id, "next_code": request.state.next_code}

    @app.get("/boom")
    def boom():
        raise HTTPException(status_code=404, detail="nope")

    @app.get("/crash")
    def crash():
        raise RuntimeError("database or disk is full")

    @app.get("/crash-after-response")
    def crash_after_response(background_tasks: BackgroundTasks):
        background_tasks.add_task(crash)
        return {"status": "ok"}

    @app.get("/items/{item_id}")
    def item(item_id: int):
        return {"id": item_id}

    CALLS.clear()
    return TestClient(app, raise_server_exceptions=False)


def auth_headers(code: str, client_id: str = "alice") -> dict:
    return {"X-Client-Id": client_id, "X-Fetchr-Code": code}


def test_valid_code_reaches_endpoint_and_returns_next_code(client):
    response = client.get("/whoami", headers=auth_headers("seed"))

    assert response.status_code == 200
    next_code = response.headers[NEXT_CODE_HEADER]
    assert response.json() == {"client_id": "alice", "next_code": next_code}
    assert next_code != "seed"


def test_chain_of_codes(client):
    code = "seed"
    for _ in range(3):
        response = client.get("/whoami", headers=auth_headers(code))
        assert response.status_code == 200
        code = response.headers[NEXT_CODE_HEADER]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Client-Id": "alice"},
        {"X-Fetchr-Code": "seed"},
        auth_headers("wrong"),
        auth_headers("seed", client_id="bob"),
    ],
)
def test_missing_or_bad_credentials_get_401_and_endpoint_not_called(client, headers):
    response = client.get("/whoami", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert NEXT_CODE_HEADER not in response.headers
    assert CALLS == []


def test_reused_code_is_rejected(client):
    client.get("/whoami", headers=auth_headers("seed"))

    assert client.get("/whoami", headers=auth_headers("seed")).status_code == 401


def test_exempt_paths_need_no_code(client):
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200


@pytest.mark.parametrize("path, status", [("/boom", 404), ("/items/not-an-int", 422)])
def test_error_responses_still_carry_next_code(client, path, status):
    response = client.get(path, headers=auth_headers("seed"))

    assert response.status_code == status
    next_code = response.headers[NEXT_CODE_HEADER]
    assert client.get("/whoami", headers=auth_headers(next_code)).status_code == 200


def test_unhandled_exception_gives_500_with_next_code(client):
    response = client.get("/crash", headers=auth_headers("seed"))

    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}
    next_code = response.headers[NEXT_CODE_HEADER]
    assert client.get("/whoami", headers=auth_headers(next_code)).status_code == 200


def test_exception_after_response_sent_keeps_original_response(client):
    response = client.get("/crash-after-response", headers=auth_headers("seed"))

    assert response.status_code == 200
    next_code = response.headers[NEXT_CODE_HEADER]
    assert client.get("/whoami", headers=auth_headers(next_code)).status_code == 200
