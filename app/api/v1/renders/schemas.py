"""
Request schemas for v1 renders. A render is returned in exactly the shape of a download
(DownloadResponse), so clients parse both the same way; source_url is the source_key.

Checks that need the client id or settings (whose files source_key may point at, the
total-length limit) live in DownloadService.create_render.
"""

from pydantic import BaseModel, Field, model_validator

from app.modules.downloads.services import MAX_CLIPS, MIN_CLIP_SECONDS, Caption, Clip, RenderSpec


class ClipIn(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False, description="seconds into the source")
    end: float = Field(allow_inf_nan=False, description="seconds into the source")

    @model_validator(mode="after")
    def _long_enough(self) -> "ClipIn":
        if self.end - self.start < MIN_CLIP_SECONDS:
            raise ValueError(f"end must be at least {MIN_CLIP_SECONDS}s after start")
        return self


class CaptionIn(BaseModel):
    headline: str = Field(min_length=1, max_length=200)
    details: str = Field(min_length=1, max_length=600, description="may contain \\n")
    hold: float = Field(default=6, ge=0, le=600, allow_inf_nan=False, description="seconds fully visible")
    fade: float = Field(default=1.5, ge=0, le=60, allow_inf_nan=False, description="seconds to fade out")

    @model_validator(mode="after")
    def _visible(self) -> "CaptionIn":
        if not self.headline.strip():
            raise ValueError("headline must not be blank")
        if self.hold + self.fade <= 0:
            raise ValueError("hold + fade must be more than 0")
        return self


class RenderCreate(BaseModel):
    source_key: str = Field(
        min_length=1, max_length=1024, description="B2 key of the source: downloads/<client_id>/<download_id>/<filename> or uploads/<client_id>/…"
    )
    clips: list[ClipIn] = Field(min_length=1, max_length=MAX_CLIPS, description="in output order; may overlap")
    caption: CaptionIn | None = None

    def to_spec(self) -> RenderSpec:
        return RenderSpec(
            clips=tuple(Clip(start=c.start, end=c.end) for c in self.clips),
            caption=Caption(**self.caption.model_dump()) if self.caption else None,
        )
