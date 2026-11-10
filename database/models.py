"""SQLAlchemy mappings for social posts and their publication lifecycle."""

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SqlEnum, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared registry for the project's database models."""


class Platform(str, Enum):
    """Active platforms and the historical Instagram value kept for old posts."""

    TELEGRAM = "telegram"
    VK = "vk"
    TENCHAT = "tenchat"
    INSTAGRAM = "instagram"


class PostStatus(str, Enum):
    """States from initial drafting to a successful or failed publication."""

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"


class Post(Base):
    """A draft or publication with optional media, UTC timestamps and failure details.

    SQLite stores scheduled_at and published_at as naive UTC datetimes.
    """

    __tablename__ = "posts"
    __table_args__ = (Index("ix_posts_status_scheduled_at", "status", "scheduled_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    topic: Mapped[str] = mapped_column(String(200))
    platform: Mapped[Platform] = mapped_column(
        SqlEnum(
            Platform,
            values_callable=lambda members: [item.value for item in members],
            create_constraint=True,
            validate_strings=True,
        )
    )
    content: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[PostStatus] = mapped_column(
        SqlEnum(
            PostStatus,
            values_callable=lambda members: [item.value for item in members],
            create_constraint=True,
            validate_strings=True,
        ),
        default=PostStatus.DRAFT,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_log: Mapped[str | None] = mapped_column(Text)
    exported_text: Mapped[str | None] = mapped_column(Text)
