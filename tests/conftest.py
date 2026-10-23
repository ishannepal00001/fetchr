"""
Shared test fixtures.

Planned fixtures:
- session: in-memory SQLite (StaticPool) with tables created
- client: TestClient(app) with get_session overridden
- seeded_client: a ClientCredential with a known seed code; returns (client_id, code)
- b2: moto mock_aws S3 bucket standing in for Backblaze B2
- fake_ytdlp: monkeypatch that "downloads" by writing a small file into tmp/<id>/
"""
