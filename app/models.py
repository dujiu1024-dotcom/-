from datetime import datetime
from pydantic import BaseModel, Field


class CommentRecord(BaseModel):
    platform: str
    comment_id: str
    content_id: str | None = None
    content_url: str | None = None
    content_title: str | None = None
    comment_text: str
    comment_created_at: datetime | None = None
    content_created_at: datetime | None = None
    parent_comment_id: str | None = None
    author_key: str | None = None
    like_count: int | None = None
    raw: dict = Field(default_factory=dict)


class ImportRequest(BaseModel):
    path: str
    platform: str
