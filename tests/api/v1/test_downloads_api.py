"""
/api/v1/downloads tests: the real app (middleware, auth, DB, routes) with only
yt-dlp and B2 swapped for fakes through the get_downloads dependency.
"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_downloads
from app.core.config import Settings
from app.main import create_app
from app.middlewares.auth import NEXT_CODE_HEADER
from app.modules.downloads.services import DownloadService, FetchedMedia


class FakeDownloader:
    def __init__(self) -> None:
        self.fail = False

    def download(self, url: str, dest_dir: Path, format: str | None = None, should_stop=None) -> FetchedMedia:
        if self.fail:
            raise RuntimeError("Unsupported URL")
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / "123.mp4"
        path.write_bytes(b"video")
        return FetchedMedia(path=path, title="A video", content_type="video/mp4")


class FakeStorage:
    bucket = "bucket"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[int, str]] = {}

    def upload_file(self, local_path, key, sha256, content_type, should_stop=None):
        self.objects[key] = (local_path.stat().st_size, sha256)

    def verify_upload(self, key, expected_size, expected_sha256):
        return self.objects.get(key) == (expected_size, expected_sha256)

    def presigned_url(self, key):
        return f"https://b2.example/{key}?sig=abc"

    def delete_object(self, key):
        self.objects.pop(key, None)


class CodeClient:
    """Wraps TestClient and follows the rolling code like a real client would."""

    def __init__(self, client: TestClient, client_id: str, code: str) -> None:
        self.client = client
        self.client_id = client_id
        self.code = code

    def request(self, method: str, url: str, **kwargs):
        headers = {"X-Client-Id": self.client_id, "X-Fetchr-Code": self.code}
        response = self.client.request(method, url, headers=headers, **kwargs)
        if NEXT_CODE_HEADER in response.headers:
            self.code = response.headers[NEXT_CODE_HEADER]
        return response

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)


@pytest.fixture
def app(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'fetchr.db'}",
        tmp_dir=tmp_path / "tmp",
        client_id="alice",
        seed_code="alice-seed",
        b2_endpoint_url="https://b2.example",
        b2_key_id="id",
        b2_application_key="key",
        b2_bucket="bucket",
        b2_presign_expiry=900,
    )
    app = create_app(settings)
    app.state.fake_downloader = FakeDownloader()
    service = DownloadService(
        app.state.session_factory, app.state.fake_downloader, FakeStorage(), settings.tmp_dir
    )
    app.dependency_overrides[get_downloads] = lambda: service
    app.state.test_service = service
    return app


@pytest.fixture
def test_client(app):
    with TestClient(app) as client:  # runs lifespan: tables + seed code
        app.state.auth.seed_client("bob", "bob-seed")
        yield client


@pytest.fixture
def alice(test_client):
    return CodeClient(test_client, "alice", "alice-seed")


@pytest.fixture
def bob(test_client):
    return CodeClient(test_client, "bob", "bob-seed")


def create(client: CodeClient, url: str = "https://facebook.com/watch?v=123") -> dict:
    response = client.post("/api/v1/downloads", json={"url": url})
    assert response.status_code == 202
    return response.json()


def test_post_queues_and_background_task_completes(alice):
    response = alice.post("/api/v1/downloads", json={"url": "https://facebook.com/watch?v=123", "format": "b"})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["source_url"] == "https://facebook.com/watch?v=123"
    assert body["next_code"] == response.headers[NEXT_CODE_HEADER]

    # TestClient runs background tasks before returning, so processing is done.
    detail = alice.get(f"/api/v1/downloads/{body['id']}").json()
    assert detail["status"] == "completed"
    assert detail["title"] == "A video"
    assert detail["filename"] == "123.mp4"
    assert detail["filesize"] == 5


def test_internal_fields_are_not_exposed(alice):
    body = alice.get(f"/api/v1/downloads/{create(alice)['id']}").json()

    for field in ("client_id", "b2_key", "b2_bucket", "sha256", "format"):
        assert field not in body


def test_failed_download_reports_error(alice, app):
    app.state.fake_downloader.fail = True

    detail = alice.get(f"/api/v1/downloads/{create(alice)['id']}").json()

    assert detail["status"] == "failed"
    assert detail["error"] == "download failed: Unsupported URL"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"url": "not a url"},
        {"url": "ftp://example.com/video.mp4"},
        {"url": "https://example.com/" + "a" * 2100},
        {"url": "https://example.com/v", "format": "x" * 201},
    ],
)
def test_post_rejects_invalid_body_but_still_rotates_code(alice, body):
    response = alice.post("/api/v1/downloads", json=body)

    assert response.status_code == 422
    assert NEXT_CODE_HEADER in response.headers
    assert alice.get("/api/v1/downloads").status_code == 200  # client stayed in sync


def test_requests_without_code_are_rejected(test_client):
    assert test_client.get("/api/v1/downloads").status_code == 401
    assert test_client.post("/api/v1/downloads", json={"url": "https://facebook.com/x"}).status_code == 401


def test_get_unknown_or_other_clients_download_is_404(alice, bob):
    alices = create(alice)

    assert bob.get(f"/api/v1/downloads/{alices['id']}").status_code == 404
    assert alice.get(f"/api/v1/downloads/{uuid.uuid4()}").status_code == 404
    assert alice.get("/api/v1/downloads/not-a-uuid").status_code == 422


def test_list_is_scoped_newest_first_and_paged(alice, bob):
    first = create(alice, "https://facebook.com/watch?v=1")
    second = create(alice, "https://facebook.com/watch?v=2")
    create(bob, "https://facebook.com/watch?v=3")

    body = alice.get("/api/v1/downloads").json()
    assert [d["id"] for d in body["items"]] == [second["id"], first["id"]]
    assert (body["offset"], body["limit"]) == (0, 50)
    assert "next_code" in body

    page = alice.get("/api/v1/downloads", params={"offset": 1, "limit": 1}).json()
    assert [d["id"] for d in page["items"]] == [first["id"]]

    assert alice.get("/api/v1/downloads", params={"limit": 101}).status_code == 422
    assert alice.get("/api/v1/downloads", params={"offset": -1}).status_code == 422


def test_file_link_after_completion(alice):
    download_id = create(alice)["id"]

    response = alice.get(f"/api/v1/downloads/{download_id}/file")

    assert response.status_code == 200
    body = response.json()
    assert body["url"] == f"https://b2.example/downloads/alice/{download_id}/123.mp4?sig=abc"
    assert body["expires_in"] == 900
    assert body["next_code"] == response.headers[NEXT_CODE_HEADER]


def test_file_link_redirect(alice):
    download_id = create(alice)["id"]

    response = alice.get(f"/api/v1/downloads/{download_id}/file", params={"redirect": True}, follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith(f"https://b2.example/downloads/alice/{download_id}/")
    assert NEXT_CODE_HEADER in response.headers


def test_file_link_before_completion_is_409(alice, app):
    queued = app.state.test_service.create_download("alice", "https://facebook.com/watch?v=9")  # never processed

    assert alice.get(f"/api/v1/downloads/{queued.id}/file").status_code == 409


def test_file_link_for_failed_download_is_409(alice, app):
    app.state.fake_downloader.fail = True

    assert alice.get(f"/api/v1/downloads/{create(alice)['id']}/file").status_code == 409


def test_file_link_unknown_or_other_client_is_404(alice, bob):
    download_id = create(alice)["id"]

    assert bob.get(f"/api/v1/downloads/{download_id}/file").status_code == 404
    assert alice.get(f"/api/v1/downloads/{uuid.uuid4()}/file").status_code == 404


def test_cancel_queued_download(alice, app):
    queued = app.state.test_service.create_download("alice", "https://facebook.com/watch?v=9")  # never processed

    response = alice.post(f"/api/v1/downloads/{queued.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["next_code"] == response.headers[NEXT_CODE_HEADER]
    assert alice.post(f"/api/v1/downloads/{queued.id}/cancel").status_code == 200  # again: no-op
    assert alice.get(f"/api/v1/downloads/{queued.id}/file").status_code == 409


def test_cancel_finished_download_is_409(alice, app):
    assert alice.post(f"/api/v1/downloads/{create(alice)['id']}/cancel").status_code == 409  # completed
    app.state.fake_downloader.fail = True
    assert alice.post(f"/api/v1/downloads/{create(alice)['id']}/cancel").status_code == 409  # failed


def test_cancel_unknown_or_other_clients_download_is_404(alice, bob, app):
    queued = app.state.test_service.create_download("alice", "https://facebook.com/watch?v=9")

    assert bob.post(f"/api/v1/downloads/{queued.id}/cancel").status_code == 404
    assert alice.post(f"/api/v1/downloads/{uuid.uuid4()}/cancel").status_code == 404
    assert alice.get(f"/api/v1/downloads/{queued.id}").json()["status"] == "queued"


def test_openapi_lists_the_routes(test_client):
    paths = test_client.get("/openapi.json").json()["paths"]

    assert set(paths) >= {
        "/api/v1/downloads",
        "/api/v1/downloads/{download_id}",
        "/api/v1/downloads/{download_id}/file",
        "/api/v1/downloads/{download_id}/cancel",
    }
