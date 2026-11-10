"""Create the project SQLite engine and a session factory for each unit of work."""

from sqlalchemy import URL, Engine, column, create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import table

from config import PROJECT_ROOT
from database.models import Base, Post

engine = create_engine(
    URL.create("sqlite", database=str(PROJECT_ROOT / "social_poster.db"))
)
SessionLocal = sessionmaker(bind=engine)


def init_db(bind: Engine = engine) -> None:
    """Create the schema or upgrade existing post tables without rewriting their data."""
    with bind.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        inspector = inspect(connection)
        if not inspector.has_table("posts"):
            Base.metadata.create_all(connection)
            return

        columns = [item["name"] for item in inspector.get_columns("posts")]
        if any(
            check["name"] == "platform" and "'tenchat'" not in check["sqltext"]
            for check in inspector.get_check_constraints("posts")
        ):
            connection.exec_driver_sql("ALTER TABLE posts RENAME TO posts_before_tenchat")
            connection.exec_driver_sql("DROP INDEX IF EXISTS ix_posts_status_scheduled_at")
            posts = Post.__table__
            posts.create(connection)
            shared_columns = [name for name in posts.columns.keys() if name in columns]
            old_posts = table("posts_before_tenchat", *(column(name) for name in shared_columns))
            connection.execute(posts.insert().from_select(shared_columns, select(*old_posts.c)))
            connection.exec_driver_sql("DROP TABLE posts_before_tenchat")
        elif "exported_text" not in columns:
            connection.exec_driver_sql("ALTER TABLE posts ADD COLUMN exported_text TEXT")
