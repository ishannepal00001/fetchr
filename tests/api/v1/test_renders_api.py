"""
/api/v1/renders tests: the real app (middleware, auth, DB, routes) with only B2, the
renderer and the thumbnailer swapped for fakes through the get_downloads dependency.
"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_downloads
from app.core.config import Settings
from app.main import create_app
from app.middlewares.auth import NEXT_CODE_HEADER
from app.modules.downloads.services import DownloadService, FetchedMedia, RenderFailed

RENDER_FIELDS = {
    "id",
    "source_url",
    "status",
    "title",
    "filename",
    "content_type",
    "filesize",
    "thumbnail",
    "error",
    "created_at",
    "updated_at",
}
SOURCE_KEY = "downloads/alice/0b9d/123.mp4"


class FakeStorage:
    bucket = "bucket"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[int, str]] = {}

    def download_file(self, key, local_path, should_stop=None):
        local_path.write_bytes(b"source")

    def upload_file(self, local_path, key, sha256, content_type, should_stop=None):
        self.objects[key] = (local_path.stat().st_size, sha256)

    def verify_upload(self, key, expected_size, expected_sha256):
        return self.objects.get(key) == (expected_size, expected_sha256)

    def presigned_url(self, key):
        return f"https://b2.example/{key}?sig=abc"

    def delete_object(self, key):
        self.objects.pop(key, None)


class FakeRenderer:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.specs = []

    def render(self, source: Path, spec, dest_dir: Path) -> FetchedMedia:
        self.specs.append(spec)
        if self.error:
            raise self.error
        path = dest_dir / "clip.mp4"
        path.write_bytes(b"rendered")
        return FetchedMedia(path=path, content_type="video/mp4")


class FakeThumbnailer:
    def make(self, video, poster, out_dir):
        path = out_dir / "thumbnail.jpg"
        path.write_bytes(b"jpeg")
        return path


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
        render_max_seconds=60,
    )
    app = create_app(settings)
    app.state.fake_storage = FakeStorage()
    app.state.fake_renderer = FakeRenderer()
    service = DownloadService(
        app.state.session_factory,
        None,
        app.state.fake_storage,
        settings.tmp_dir,
        thumbnailer=FakeThumbnailer(),
        renderer=app.state.fake_renderer,
        render_max_seconds=settings.render_max_seconds,
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


def body(**overrides) -> dict:
    return {
        "source_key": SOURCE_KEY,
        "clips": [{"start": 12.4, "end": 31.0}, {"start": 1, "end": 2}],
        "caption": {"headline": "Breaking", "details": "Line one\nLine two"},
        **overrides,
    }


def create(client: CodeClient, **overrides) -> dict:
    response = client.post("/api/v1/renders", json=body(**overrides))
    assert response.status_code == 202, response.json()
    return response.json()


def test_post_queues_and_background_task_completes(alice, app):
    response = alice.post("/api/v1/renders", json=body())

    assert response.status_code == 202
    created = response.json()
    assert set(created) == RENDER_FIELDS | {"next_code"}
    assert created["status"] == "queued"
    assert created["source_url"] == SOURCE_KEY
    assert created["thumbnail"] is None
    assert created["next_code"] == response.headers[NEXT_CODE_HEADER]

    # TestClient runs background tasks before returning, so processing is done.
    detail = alice.get(f"/api/v1/renders/{created['id']}").json()
    assert set(detail) == RENDER_FIELDS | {"next_code"}
    assert detail["status"] == "completed"
    assert (detail["filename"], detail["content_type"], detail["filesize"]) == ("clip.mp4", "video/mp4", 8)
    assert detail["thumbnail"] == "thumbnail.jpg"
    assert detail["error"] is None


def test_outputs_go_under_renders_prefix(alice, app):
    render_id = create(alice)["id"]

    assert set(app.state.fake_storage.objects) == {
        f"renders/alice/{render_id}/clip.mp4",
        f"renders/alice/{render_id}/thumbnail.jpg",
    }


def test_body_reaches_the_renderer_with_defaults(alice, app):
    create(alice)

    spec = app.state.fake_renderer.specs[0]
    assert [(c.start, c.end) for c in spec.clips] == [(12.4, 31.0), (1, 2)]
    assert (spec.caption.headline, spec.caption.details) == ("Breaking", "Line one\nLine two")
    assert (spec.caption.hold, spec.caption.fade) == (6, 1.5)


def test_caption_is_optional(alice, app):
    create(alice, caption=None)
    create(alice, caption={"headline": "H", "details": "D", "hold": 2, "fade": 0})
    body_without = body()
    del body_without["caption"]
    assert alice.post("/api/v1/renders", json=body_without).status_code == 202

    specs = app.state.fake_renderer.specs
    assert specs[0].caption is None
    assert (specs[1].caption.hold, specs[1].caption.fade) == (2, 0)
    assert specs[2].caption is None


def test_failed_render_reports_a_short_error(alice, app):
    app.state.fake_renderer.error = RenderFailed("Clip ends after the video does")

    detail = alice.get(f"/api/v1/renders/{create(alice)['id']}").json()

    assert detail["status"] == "failed"
    assert detail["error"] == "Clip ends after the video does"
    assert detail["thumbnail"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_key": None},
        {"clips": []},
        {"clips": [{"start": i, "end": i + 1} for i in range(21)]},
        {"clips": [{"start": -1, "end": 2}]},
        {"clips": [{"start": 5, "end": 5}]},
        {"clips": [{"start": 5, "end": 4}]},
        {"clips": [{"start": 5, "end": 5.4}]},
        {"clips": [{"start": 0}]},
        {"clips": [{"start": "x", "end": 1}]},
        {"caption": {"headline": "", "details": "D"}},
        {"caption": {"headline": "   ", "details": "D"}},
        {"caption": {"headline": "H" * 201, "details": "D"}},
        {"caption": {"headline": "H", "details": ""}},
        {"caption": {"headline": "H", "details": "D" * 601}},
        {"caption": {"headline": "H", "details": "D", "hold": -1}},
        {"caption": {"headline": "H", "details": "D", "fade": -1}},
        {"caption": {"headline": "H", "details": "D", "hold": 0, "fade": 0}},
    ],
)
def test_invalid_body_is_422_and_still_rotates_code(alice, app, overrides):
    response = alice.post("/api/v1/renders", json=body(**overrides))

    assert response.status_code == 422
    assert NEXT_CODE_HEADER in response.headers
    assert app.state.fake_renderer.specs == []
    assert alice.get("/api/v1/downloads").status_code == 200  # client stayed in sync


def test_clips_of_exactly_half_a_second_and_overlapping_are_fine(alice):
    create(alice, clips=[{"start": 0, "end": 0.5}, {"start": 0.25, "end": 3}])


@pytest.mark.parametrize(
    "source_key",
    ["downloads/bob/0b9d/123.mp4", "downloads/alice/../bob/x.mp4", "renders/alice/0b9d/clip.mp4", "downloads/alice/"],
)
def test_source_outside_your_downloads_is_422_with_a_message(alice, source_key):
    response = alice.post("/api/v1/renders", json=body(source_key=source_key))

    assert response.status_code == 422
    assert response.json() == {"detail": "source_key must be a file under downloads/alice/"}
    assert NEXT_CODE_HEADER in response.headers


def test_total_length_over_render_max_seconds_is_422(alice):
    response = alice.post("/api/v1/renders", json=body(clips=[{"start": 0, "end": 40}, {"start": 20, "end": 41}]))

    assert response.status_code == 422
    assert response.json() == {"detail": "clips add up to 61s; the limit is 60s"}
    assert NEXT_CODE_HEADER in response.headers


def test_requests_without_a_valid_code_are_401_without_a_next_code(test_client, alice):
    render_id = create(alice)["id"]
    wrong = {"X-Client-Id": "alice", "X-Fetchr-Code": "wrong"}

    for response in (
        test_client.post("/api/v1/renders", json=body()),
        test_client.get(f"/api/v1/renders/{render_id}"),
        test_client.post("/api/v1/renders", json=body(), headers=wrong),
        test_client.get(f"/api/v1/renders/{render_id}", headers=wrong),
    ):
        assert response.status_code == 401
        assert NEXT_CODE_HEADER not in response.headers


def test_every_authenticated_reply_carries_the_next_code(alice, bob):
    render_id = create(alice)["id"]

    for client, method, url, kwargs, expected in (
        (alice, "GET", f"/api/v1/renders/{render_id}", {}, 200),
        (bob, "GET", f"/api/v1/renders/{render_id}", {}, 404),
        (alice, "GET", "/api/v1/renders/not-a-uuid", {}, 422),
        (alice, "POST", "/api/v1/renders", {"json": {}}, 422),
    ):
        response = client.request(method, url, **kwargs)
        assert response.status_code == expected
        assert NEXT_CODE_HEADER in response.headers


def test_get_unknown_or_other_clients_render_is_404(alice, bob):
    render_id = create(alice)["id"]

    assert bob.get(f"/api/v1/renders/{render_id}").json() == {"detail": "render not found"}
    assert alice.get(f"/api/v1/renders/{uuid.uuid4()}").status_code == 404


def test_renders_and_downloads_are_separate(alice, app):
    render_id = create(alice)["id"]
    download = app.state.test_service.create_download("alice", "https://facebook.com/watch?v=9")

    assert alice.get(f"/api/v1/downloads/{render_id}").status_code == 404
    assert alice.post(f"/api/v1/downloads/{render_id}/cancel").status_code == 404
    assert alice.get(f"/api/v1/renders/{download.id}").status_code == 404
    assert [d["id"] for d in alice.get("/api/v1/downloads").json()["items"]] == [str(download.id)]


def test_openapi_lists_the_routes(test_client):
    paths = test_client.get("/openapi.json").json()["paths"]

    assert set(paths) >= {"/api/v1/renders", "/api/v1/renders/{render_id}"}
