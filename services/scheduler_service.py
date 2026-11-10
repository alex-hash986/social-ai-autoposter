"""Run a recurring APScheduler job against posts persisted in SQLite."""

from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from database.models import Post, PostStatus
from services.publisher_service import PublisherService


class SchedulerService:
    """Publish due posts serially in the single application process."""

    def __init__(self, publisher: PublisherService) -> None:
        """Configure a non-overlapping poll of the persisted schedule every 30 seconds."""
        self.publisher = publisher
        self.scheduler = BackgroundScheduler(timezone=timezone.utc)
        self.scheduler.add_job(
            self.publish_due_posts,
            trigger="interval",
            seconds=30,
            next_run_time=datetime.now(timezone.utc),
            max_instances=1,
            coalesce=True,
        )

    def start(self) -> None:
        """Start the background worker without blocking the web server."""
        self.scheduler.start()

    def shutdown(self) -> None:
        """Wait for the active poll to finish before exiting."""
        self.scheduler.shutdown(wait=True)

    def publish_due_posts(self) -> None:
        """Find overdue posts and commit each publication result independently."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.publisher.sessions() as session:
            due_ids = session.scalars(
                select(Post.id)
                .where(Post.status == PostStatus.SCHEDULED, Post.scheduled_at <= now)
                .order_by(Post.scheduled_at, Post.id)
            ).all()

        for post_id in due_ids:
            self.publisher.publish_post(post_id, due_by=now)
