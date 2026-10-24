"""core/storage tests against moto's fake S3, standing in for Backblaze B2."""

import hashlib
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from moto import mock_aws

from app.core.storage import B2Storage, UploadStopped, region_from_endpoint

ENDPOINT = "https://s3.us-west-004.backblazeb2.com"
BUCKET = "fetchr-test"
KEY = "downloads/alice/abc/video.mp4"
DATA = b"fake video bytes"
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def s3(monkeypatch):
    """moto intercepting the B2 endpoint; yields a raw client for inspecting the bucket."""
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", ENDPOINT)
    with mock_aws():
        client = boto3.client(
            "s3",
            endpoint_url=ENDPOINT,
            region_name="us-west-004",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-west-004"})
        yield client


@pytest.fixture
def storage(s3) -> B2Storage:
    return B2Storage(ENDPOINT, "test", "test", BUCKET, presign_expiry=600)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(DATA)
    return path


@pytest.mark.parametrize(
    "endpoint, region",
    [
        ("https://s3.us-west-004.backblazeb2.com", "us-west-004"),
        ("https://s3.eu-central-003.backblazeb2.com", "eu-central-003"),
        ("http://localhost:9000", "us-east-1"),
    ],
)
def test_region_from_endpoint(endpoint, region):
    assert region_from_endpoint(endpoint) == region


def test_upload_stores_bytes_metadata_and_content_type(storage, s3, video):
    storage.upload_file(video, KEY, SHA, "video/mp4")

    obj = s3.get_object(Bucket=BUCKET, Key=KEY)
    assert obj["Body"].read() == DATA
    assert obj["Metadata"] == {"sha256": SHA}
    assert obj["ContentType"] == "video/mp4"


def test_upload_without_content_type_uses_octet_stream(storage, s3, video):
    storage.upload_file(video, KEY, SHA, None)

    assert s3.head_object(Bucket=BUCKET, Key=KEY)["ContentType"] == "application/octet-stream"


def test_upload_aborts_when_should_stop_turns_true(storage, video):
    # A small file goes up in one PUT and the callback fires after it, so the object
    # can still land; that's why process_download deletes the key after any stop.
    with pytest.raises(UploadStopped):
        storage.upload_file(video, KEY, SHA, "video/mp4", should_stop=lambda: True)


def test_upload_completes_while_should_stop_is_false(storage, video):
    storage.upload_file(video, KEY, SHA, "video/mp4", should_stop=lambda: False)

    assert storage.verify_upload(KEY, len(DATA), SHA) is True


def test_verify_passes_after_upload(storage, video):
    storage.upload_file(video, KEY, SHA, "video/mp4")

    assert storage.verify_upload(KEY, len(DATA), SHA) is True


@pytest.mark.parametrize(
    "key, size, sha",
    [
        (KEY, len(DATA) + 1, SHA),  # wrong size
        (KEY, len(DATA), "0" * 64),  # wrong sha256
        ("downloads/alice/abc/missing.mp4", len(DATA), SHA),  # not uploaded
    ],
)
def test_verify_fails_on_mismatch_or_missing(storage, video, key, size, sha):
    storage.upload_file(video, KEY, SHA, "video/mp4")

    assert storage.verify_upload(key, size, sha) is False


def test_verify_returns_false_instead_of_raising_when_bucket_missing(s3):
    storage = B2Storage(ENDPOINT, "test", "test", "no-such-bucket")

    assert storage.verify_upload(KEY, len(DATA), SHA) is False


def test_presigned_url_is_signed_and_downloads_as_attachment(storage):
    url = storage.presigned_url(KEY)

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.hostname in {"s3.us-west-004.backblazeb2.com", f"{BUCKET}.s3.us-west-004.backblazeb2.com"}
    assert parsed.path.endswith(KEY)
    assert query["X-Amz-Expires"] == ["600"]
    assert "X-Amz-Signature" in query
    assert "us-west-004" in query["X-Amz-Credential"][0]
    assert query["response-content-disposition"] == ['attachment; filename="video.mp4"']


@pytest.mark.parametrize("versioned", [True, False])
def test_delete_removes_every_version(storage, s3, video, versioned):
    if versioned:  # B2 buckets keep every version by default
        s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    storage.upload_file(video, KEY, SHA, "video/mp4")
    storage.upload_file(video, KEY, SHA, "video/mp4")  # a retry: second version
    storage.upload_file(video, KEY + ".part2", SHA, "video/mp4")  # shares the prefix; must survive

    storage.delete_object(KEY)

    listing = s3.list_object_versions(Bucket=BUCKET)
    remaining = {v["Key"] for v in listing.get("Versions", []) + listing.get("DeleteMarkers", [])}
    assert remaining == {KEY + ".part2"}


def test_delete_missing_key_is_a_no_op(storage):
    storage.delete_object("downloads/nothing/here.mp4")


def test_download_service_end_to_end_with_b2_storage(storage, s3, session_factory, tmp_path):
    """The real B2Storage (on moto) plugged into DownloadService, with a fake yt-dlp."""
    from app.modules.downloads.model import DownloadStatus
    from app.modules.downloads.services import DownloadService, FetchedMedia

    class FakeDownloader:
        def download(self, url, dest_dir, format=None, should_stop=None):
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / "123.mp4"
            path.write_bytes(DATA)
            return FetchedMedia(path=path, title="A video", content_type="video/mp4")

    service = DownloadService(session_factory, FakeDownloader(), storage, tmp_path / "tmp")
    created = service.create_download("alice", "https://facebook.com/watch?v=123")

    service.process_download(created.id)

    download = service.get_download("alice", created.id)
    assert download.status == DownloadStatus.COMPLETED
    assert download.b2_bucket == BUCKET
    assert s3.get_object(Bucket=BUCKET, Key=download.b2_key)["Body"].read() == DATA
    assert not (tmp_path / "tmp" / str(created.id)).exists()
    assert download.b2_key in service.file_link("alice", created.id)
