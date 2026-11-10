"""Send social posts and persist their delivery outcome."""

import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from threading import Lock
from typing import Any

import requests
from sqlalchemy.orm import Session, sessionmaker

from config import Settings, load_settings
from database.db import SessionLocal
from database.models import Platform, Post, PostStatus
from services.tenchat import topic_hashtag

logger = logging.getLogger(__name__)
PublishHandler = Callable[[Post], None]
HTTP_TIMEOUT = (5, 15)


class PublicationError(RuntimeError):
    """A publication error whose message is safe to persist and display."""


class VkSession(requests.Session):
    """Apply a timeout to HTTP requests made internally by vk_api."""

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Bound network waits and keep VK requests on the original host."""
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)


class TenChatPublisher:
    """Save a plain-text copy for manual publication through TenChat."""

    def publish(self, post: Post) -> None:
        """Attach the formatted export to the post for the delivery transaction."""
        body = post.content.strip().replace("\r\n", "\n").replace("\r", "\n")
        body = re.sub(r"\n(?:[ \t]*\n)+", "\n\n", body)
        if not re.search(r"(?<!\S)#[^\W_]", body):
            body = f"{body}\n\n{topic_hashtag(post.topic)}"
        post.exported_text = f"{post.title.strip()}\n\n{body}"


class PublisherService:
    """Share delivery logic and post locks across Flask and the background worker."""

    def __init__(
        self,
        handlers: Mapping[Platform, PublishHandler] | None = None,
        *,
        settings: Settings | None = None,
        sessions: sessionmaker[Session] = SessionLocal,
    ) -> None:
        """Wire platform adapters and the session factory for this process."""
        self.settings = settings or load_settings()
        self.sessions = sessions
        self.handlers = {
            Platform.TELEGRAM: self._publish_telegram,
            Platform.VK: self._publish_vk,
            Platform.TENCHAT: TenChatPublisher().publish,
        }
        self.handlers.update(handlers or {})
        self._post_locks = tuple(Lock() for _ in range(64))

    @contextmanager
    def guard(self, post_id: int) -> Iterator[None]:
        """Serialize mutations for a post without holding a SQLite write lock over HTTP."""
        with self._post_locks[post_id % len(self._post_locks)]:
            yield

    def publish_post(
        self, post_id: int, *, due_by: datetime | None = None
    ) -> tuple[bool, str | None]:
        """Deliver once per attempt and record the result before releasing the lock.

        A scheduler passes its UTC cutoff so a post rescheduled or removed after
        the due list was queried cannot be sent by the stale job.
        """
        with self.guard(post_id), self.sessions.begin() as session:
            post = session.get(Post, post_id)
            if post is None:
                return False, "Пост не найден."
            if post.status == PostStatus.PUBLISHED:
                return False, "Пост уже опубликован."
            if due_by is not None and (
                post.status != PostStatus.SCHEDULED
                or post.scheduled_at is None
                or post.scheduled_at > due_by
            ):
                return False, "Пост больше не ожидает отправки."

            error = None
            try:
                self.publish(post)
            except PublicationError as exc:
                error = str(exc)
            except Exception as exc:
                logger.error("Post %s: unexpected adapter error (%s)", post_id, type(exc).__name__)
                error = "Внутренняя ошибка при отправке поста."

            if error is not None:
                post.status = PostStatus.FAILED
                post.published_at = None
                post.error_log = error
                return False, error

            post.status = PostStatus.PUBLISHED
            post.published_at = datetime.now(timezone.utc).replace(tzinfo=None)
            post.error_log = None
            exported = post.platform == Platform.TENCHAT and post.exported_text is not None

        if exported:
            logger.info("TenChat Sandbox: экспорт поста %s успешно сохранён", post_id)
        return True, None

    def publish(self, post: Post) -> None:
        """Validate the text and dispatch it to the post's platform adapter."""
        handler = self.handlers.get(post.platform)
        if handler is None:
            raise PublicationError("Площадка больше не поддерживается. Выберите другую при редактировании.")
        if not post.content.strip():
            raise PublicationError("Добавьте текст перед публикацией.")
        handler(post)

    def _publish_telegram(self, post: Post) -> None:
        """Send escaped text via Telegram Bot API and check the delivery result."""
        secret = self.settings.telegram_bot_token
        token = secret.get_secret_value().strip() if secret else ""
        channel = (self.settings.telegram_channel_id or "").strip()
        if not token or not channel:
            raise PublicationError("Укажите TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID.")
        text = post.content.strip()
        if len(text) > 4096:
            raise PublicationError("Telegram принимает до 4096 символов в сообщении.")

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": channel, "text": escape(text, quote=False), "parse_mode": "HTML"},
                timeout=HTTP_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise PublicationError("Telegram Bot API недоступен. Проверьте подключение.") from None

        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            if 200 <= response.status_code < 300 and payload.get("ok") is True:
                return
            description = payload.get("description")
            if isinstance(description, str) and description.strip():
                safe_description = description.replace(token, "[скрыто]").strip()[:300]
                raise PublicationError(f"Telegram: {safe_description}")
        raise PublicationError(f"Telegram вернул неожиданный ответ (HTTP {response.status_code}).")

    def _publish_vk(self, post: Post) -> None:
        """Publish on the VK community wall when both credentials are configured."""
        secret = self.settings.vk_access_token
        token = secret.get_secret_value().strip() if secret else ""
        group_id = (self.settings.vk_group_id or "").strip()
        if not token or not group_id:
            self._simulate(post)
            return
        try:
            owner_id = -abs(int(group_id))
        except ValueError:
            raise PublicationError("VK_GROUP_ID должен быть числовым ID сообщества.") from None
        if owner_id == 0:
            raise PublicationError("VK_GROUP_ID должен быть ненулевым ID сообщества.")

        try:
            import vk_api
            from vk_api.exceptions import ApiError, VkApiError
        except ImportError:
            raise PublicationError("Для публикации во ВКонтакте установите vk_api.") from None

        try:
            with VkSession() as http:
                api = vk_api.VkApi(token=token, api_version="5.199", session=http).get_api()
                result = api.wall.post(
                    owner_id=owner_id, from_group=1, message=post.content.strip()
                )
        except ApiError as exc:
            raise PublicationError(f"ВКонтакте отклонил публикацию (код {exc.code}).") from None
        except (VkApiError, requests.RequestException, ValueError):
            raise PublicationError(
                "ВКонтакте не подтвердил публикацию. Проверьте подключение и настройки."
            ) from None
        if not isinstance(result, dict) or not result.get("post_id"):
            raise PublicationError("ВКонтакте не вернул ID публикации.")

    @staticmethod
    def _simulate(post: Post) -> None:
        """Log a successful local simulation for unconfigured VK."""
        logger.info("Имитация публикации: пост %s, площадка %s", post.id, post.platform.value)
