"""Start the local Flask server and its background publication scheduler."""

import logging

from config import load_settings
from database.db import init_db
from services.publisher_service import PublisherService
from services.scheduler_service import SchedulerService
from web.app import create_app


def main() -> None:
    """Create the database, start one scheduler and run Flask on port 5000."""
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    init_db()
    publisher = PublisherService(settings=settings)
    app = create_app(settings, publisher=publisher)
    scheduler = SchedulerService(publisher)
    scheduler.start()
    try:
        app.run(host="127.0.0.1", port=5000, use_reloader=False)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
