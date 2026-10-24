"""ClientCredential(SQLModel, table=True): the current rolling code for one client."""

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class ClientCredential(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    client_id: str = Field(unique=True, index=True)
    code_hash: str  # sha256 of the current valid code
    prev_code_hash: str | None = None  # sha256 of the previous code (grace-window recovery)
    rotated_at: datetime = Field(default_factory=utcnow)  # when code_hash last changed
    created_at: datetime = Field(default_factory=utcnow)
