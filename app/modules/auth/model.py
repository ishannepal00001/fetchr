"""
ClientCredential(SQLModel, table=True)

Planned fields:
- id: int, primary key
- client_id: str, unique, indexed
- code_hash: str            sha256 of the current valid code
- prev_code_hash: str | None   sha256 of the previous code (grace-window recovery)
- rotated_at: datetime      when code_hash last changed
- created_at: datetime
"""
