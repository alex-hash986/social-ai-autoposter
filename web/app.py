"""Serve the content dashboard, publication actions and generation forms."""

from datetime import datetime, timedelta, timezone
from secrets import compare_digest, token_urlsafe

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from config import Settings, load_settings
from database.db import SessionLocal
from database.models import Platform, Post, PostStatus
from services.ai_service import AIService, MAX_PLAN_DAYS
from services.publisher_service import PublisherService

PLATFORMS = {"telegram": "Telegram", "vk": "ВКонтакте", "tenchat": "TenChat"}


def selected_platform() -> Platform | None:
    """Read an active platform from the form, excluding the historical value."""
    value = request.form.get("platform", "")
    return Platform(value) if value in PLATFORMS else None


def parse_schedule(value: str, *, require_future: bool = True) -> datetime:
    """Parse a datetime-local field as UTC and validate active schedules."""
    try:
        scheduled_at = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Укажите дату и время публикации в UTC.") from None
    if len(value) < 16 or scheduled_at.tzinfo is not None:
        raise ValueError("Укажите дату и время публикации в UTC.")
    if require_future and scheduled_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        raise ValueError("Выберите дату и время в будущем (UTC).")
    return scheduled_at


def create_app(
    settings: Settings | None = None,
    sessions: sessionmaker[Session] = SessionLocal,
    *,
    publisher: PublisherService | None = None,
) -> Flask:
    """Build the Flask application without starting a scheduler or creating tables."""
    settings = settings or load_settings()
    app = Flask(__name__)
    app.secret_key = settings.secret_key.get_secret_value()
    app.jinja_env.globals["platforms"] = PLATFORMS
    ai_service = AIService(settings)
    publisher_service = publisher or PublisherService(settings=settings, sessions=sessions)

    @app.template_global()
    def csrf_token() -> str:
        """Return a token shared by forms within this browser session."""
        token: str = session.setdefault("csrf_token", token_urlsafe(32))
        return token

    @app.before_request
    def protect_forms() -> None:
        """Require the browser session token on every state-changing form."""
        if request.method == "POST":
            expected = session.get("csrf_token")
            submitted = request.form.get("csrf_token", "").encode("utf-8")
            if not isinstance(expected, str) or not compare_digest(expected.encode(), submitted):
                abort(400, description="Обновите страницу и отправьте форму ещё раз.")

    @app.get("/")
    def index() -> str:
        """Show all posts, including scheduled publications and recent drafts."""
        with sessions() as db:
            posts = db.scalars(select(Post).order_by(Post.id.desc())).all()
        groups: dict[str, list[Post]] = {status.value: [] for status in PostStatus}
        for post in posts:
            groups[post.status.value].append(post)
        return render_template("index.html", groups=groups, total=len(posts))

    @app.post("/topics")
    def add_topic() -> Response:
        """Create an empty draft for a manually entered topic and platform."""
        topic = request.form.get("topic", "").strip()
        platform = selected_platform()
        if not 1 <= len(topic) <= 200 or platform is None:
            flash("Укажите тему до 200 символов и выберите площадку.", "error")
        else:
            with sessions.begin() as db:
                db.add(Post(title=topic, topic=topic, platform=platform))
            flash("Тема добавлена в черновики.", "success")
        return redirect(url_for("index"), code=303)

    @app.post("/posts/<int:post_id>/publish_now")
    def publish_now(post_id: int) -> Response:
        """Run the same publication path used by the scheduler and report its outcome."""
        success, error = publisher_service.publish_post(post_id)
        if success:
            with sessions() as db:
                post = db.get(Post, post_id)
                exported = post is not None and post.exported_text is not None
            flash("Текст TenChat сохранён в песочнице." if exported else "Пост опубликован.", "success")
        else:
            flash(error, "error")
        return redirect(url_for("index"), code=303)

    @app.post("/posts/<int:post_id>/delete")
    def delete_post(post_id: int) -> Response:
        """Delete a local post while excluding concurrent delivery for that ID."""
        with publisher_service.guard(post_id), sessions.begin() as db:
            post = db.get(Post, post_id)
            if post is None:
                abort(404)
            db.delete(post)
        flash("Пост удалён.", "success")
        return redirect(url_for("index"), code=303)

    @app.post("/posts/<int:post_id>/schedule")
    def schedule_post(post_id: int) -> Response:
        """Schedule an unpublished post at the future UTC time supplied by the form."""
        with publisher_service.guard(post_id), sessions.begin() as db:
            post = db.get(Post, post_id)
            if post is None:
                abort(404)
            try:
                if post.status == PostStatus.PUBLISHED:
                    raise ValueError("Этот пост уже опубликован.")
                if post.platform.value not in PLATFORMS:
                    raise ValueError("Перед планированием выберите действующую площадку.")
                if not post.content.strip():
                    raise ValueError("Перед планированием добавьте текст поста.")
                post.scheduled_at = parse_schedule(request.form.get("scheduled_at", ""))
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                post.status = PostStatus.SCHEDULED
                post.error_log = None
                flash("Пост запланирован.", "success")
        return redirect(url_for("index"), code=303)

    @app.route("/posts/<int:post_id>/edit", methods=["GET", "POST"])
    def edit_post(post_id: int) -> str | Response | tuple[str, int]:
        """Edit an unpublished post and preserve its schedule unless the date is cleared."""
        with publisher_service.guard(post_id), sessions.begin() as db:
            post = db.get(Post, post_id)
            if post is None:
                abort(404)
            if post.status == PostStatus.PUBLISHED:
                flash("Этот пост уже опубликован.", "error")
                return redirect(url_for("index"), code=303)
            if request.method == "GET":
                return render_template("edit.html", post=post)

            title = request.form.get("title", "").strip()
            topic = request.form.get("topic", post.topic).strip()
            platform = selected_platform()
            content = request.form.get("content", "").strip()
            date_value = request.form.get("scheduled_at")
            try:
                if not 1 <= len(title) <= 200 or not 1 <= len(topic) <= 200 or platform is None:
                    raise ValueError("Укажите заголовок, тему и площадку.")
                scheduled_at = post.scheduled_at
                if date_value is not None:
                    scheduled_at = (
                        parse_schedule(
                            date_value.strip(), require_future=post.status == PostStatus.SCHEDULED
                        )
                        if date_value.strip()
                        else None
                    )
                if post.status == PostStatus.SCHEDULED and scheduled_at and not content:
                    raise ValueError("У запланированного поста должен быть текст.")
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template("edit.html", post=post), 400

            post.title = title
            post.topic = topic
            post.platform = platform
            post.content = content
            post.scheduled_at = scheduled_at
            if post.status == PostStatus.SCHEDULED and scheduled_at is None:
                post.status = PostStatus.DRAFT
        flash("Изменения сохранены.", "success")
        return redirect(url_for("index"), code=303)

    @app.route("/generate", methods=["GET", "POST"])
    def generate() -> str | Response | tuple[str, int]:
        """Validate the form and save the complete content plan in one transaction."""
        if request.method == "GET":
            return render_template("generate.html", max_days=MAX_PLAN_DAYS)

        niche = request.form.get("niche", "").strip()
        tone = request.form.get("tone", "").strip()
        days = request.form.get("days", type=int)
        platform = selected_platform()
        if (
            not 1 <= len(niche) <= 200
            or not 1 <= len(tone) <= 200
            or days is None
            or not 1 <= days <= MAX_PLAN_DAYS
            or platform is None
        ):
            flash("Заполните все поля и проверьте количество дней.", "error")
            return render_template("generate.html", max_days=MAX_PLAN_DAYS), 400

        plan = ai_service.generate_plan(niche, tone, days, platform)
        tomorrow = datetime.now(timezone.utc).replace(
            hour=9, minute=0, second=0, microsecond=0, tzinfo=None
        ) + timedelta(days=1)
        with sessions.begin() as db:
            db.add_all(
                Post(
                    title=item.title.strip(),
                    topic=niche,
                    platform=item.platform,
                    content=item.content.strip(),
                    scheduled_at=tomorrow + timedelta(days=item.scheduled_time_offset_days),
                )
                for item in plan.posts
            )
        message = "Автономная генерация. " if plan.offline else ""
        flash(f"{message}Черновиков добавлено: {len(plan.posts)}.", "success")
        return redirect(url_for("index"), code=303)

    return app
