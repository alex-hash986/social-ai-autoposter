"""Generate structured social posts with OpenAI or a local editorial fallback."""

import logging

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field, PrivateAttr, ValidationError

from config import Settings
from database.models import Platform
from services.tenchat import topic_hashtag

logger = logging.getLogger(__name__)
MAX_PLAN_DAYS = 30
TENCHAT_INSTRUCTIONS = (
    " Для TenChat пиши для предпринимателей и специалистов. Используй экспертный "
    "деловой тон и практические примеры. Раздели текст на блоки «Проблема», "
    "«Решение» и «Вывод». Заголовок передай отдельно, а в конце текста добавь "
    "2–4 релевантных тематических хэштега."
)


class GenerationError(RuntimeError):
    """The provider did not return a complete plan for the requested inputs."""


class PlannedPost(BaseModel):
    """A ready-to-publish draft and its proposed day relative to the plan start."""

    title: str = Field(min_length=1, max_length=200)
    platform: Platform
    content: str = Field(min_length=1, max_length=2200)
    scheduled_time_offset_days: int = Field(ge=0, lt=MAX_PLAN_DAYS)


class ContentPlan(BaseModel):
    """Structured response with a list of posts and a local-only fallback marker."""

    posts: list[PlannedPost]
    _offline: bool = PrivateAttr(default=False)

    @property
    def offline(self) -> bool:
        """Indicate that the five local posts were used for this request."""
        return self._offline


class AIService:
    """Prefer Chat Completions and keep draft generation available offline."""

    def __init__(self, settings: Settings) -> None:
        """Keep the settings used to create an OpenAI client on demand."""
        self.settings = settings

    def generate_plan(self, niche: str, tone: str, days: int, platform: Platform) -> ContentPlan:
        """Return the requested online plan or five varied local drafts."""
        if not 1 <= days <= MAX_PLAN_DAYS:
            raise ValueError(f"days must be between 1 and {MAX_PLAN_DAYS}")
        if platform == Platform.INSTAGRAM:
            raise ValueError("Instagram is no longer available for generation")

        secret = self.settings.openai_api_key
        key = secret.get_secret_value().strip() if secret else ""
        if key:
            try:
                return self._request_plan(key, niche, tone, days, platform)
            except (OpenAIError, ValidationError, GenerationError) as exc:
                logger.warning("Автономная генерация после сбоя OpenAI (%s)", type(exc).__name__)
        return self._mock_plan(niche, platform)

    def generate_post(self, topic: str, tone: str, platform: Platform) -> str:
        """Return the text of the first draft for a single-post request."""
        return self.generate_plan(topic, tone, 1, platform).posts[0].content.strip()

    def _request_plan(
        self, key: str, niche: str, tone: str, days: int, platform: Platform
    ) -> ContentPlan:
        """Ask an SMM editor model for one structured post per requested day."""
        instructions = (
            "Ты опытный SMM-маркетолог. Пиши готовые посты по-русски, "
            "учитывай площадку и тон. Чередуй полезные, вовлекающие "
            "и анонсирующие форматы. Не выдумывай реальные отзывы, "
            "цифры и результаты. Используй обычный текст без разметки."
        )
        if platform == Platform.TENCHAT:
            instructions += TENCHAT_INSTRUCTIONS
        with OpenAI(api_key=key, timeout=30.0, max_retries=0) as client:
            completion = client.chat.completions.parse(
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": instructions},
                    {
                        "role": "user",
                        "content": (
                            f"Ниша: {niche}\nТон: {tone}\nПлощадка: {platform.value}\n"
                            f"Нужно ровно {days} постов для этой площадки, по одному "
                            f"на каждый день. Смещения дней от 0 до {days - 1}."
                        ),
                    },
                ],
                response_format=ContentPlan,
                store=False,
            )
        plan = completion.choices[0].message.parsed if completion.choices else None
        if (
            plan is None
            or len(plan.posts) != days
            or {post.scheduled_time_offset_days for post in plan.posts} != set(range(days))
            or any(
                post.platform != platform or not post.title.strip() or not post.content.strip()
                for post in plan.posts
            )
        ):
            raise GenerationError("OpenAI вернул неполный или некорректный контент-план.")
        plan.posts.sort(key=lambda post: post.scheduled_time_offset_days)
        return plan

    @staticmethod
    def _mock_plan(niche: str, platform: Platform) -> ContentPlan:
        """Build five complete drafts using the platform's editorial format."""
        posts = (
            AIService._tenchat_mock_posts(niche)
            if platform == Platform.TENCHAT
            else AIService._social_mock_posts(niche, platform)
        )
        plan = ContentPlan(
            posts=[
                PlannedPost(
                    title=title,
                    platform=platform,
                    content=content,
                    scheduled_time_offset_days=offset,
                )
                for offset, (title, content) in enumerate(posts)
            ]
        )
        plan._offline = True
        return plan

    @staticmethod
    def _tenchat_mock_posts(niche: str) -> list[tuple[str, str]]:
        """Write five business posts with a practical takeaway and topical tags."""
        posts = [
            (
                "Как проверить гипотезу за одну рабочую неделю",
                f"В проектах по теме «{niche}» команды часто меняют несколько шагов сразу "
                "и теряют возможность понять, какое изменение сработало.",
                "Выберите один повторяющийся процесс. Зафиксируйте исходное время выполнения "
                "и число переделок. Измените один шаг и проверьте его на трёх сопоставимых задачах.",
                "Сравните результаты с исходными. Если выигрыша нет, верните прежний порядок "
                "и сформулируйте следующую гипотезу.",
                "#проверкагипотез",
            ),
            (
                "Учебный кейс: как сократить задержки при передаче задач",
                f"Представим команду, которая ведёт проекты по теме «{niche}». "
                "Задачи останавливаются при передаче между исполнителями из-за неполных данных.",
                "Команда добавляет к каждой задаче краткий бриф: цель, критерий готовности, "
                "ответственный и срок. На следующем цикле она считает возвраты на уточнение.",
                "Количество возвратов покажет, на каком этапе возникают задержки. "
                "По итогам цикла оставьте в брифе только те поля, которые помогают принять задачу.",
                "#управлениепроектами",
            ),
            (
                "Три инструмента для контроля рабочих процессов",
                f"В работе по теме «{niche}» договорённости часто остаются в переписке, "
                "поэтому команде трудно отследить причины задержек.",
                "Заведите общую таблицу решений, доску со статусами задач и короткий "
                "чек-лист приёмки. Начните с одного проекта и назначьте ответственных за обновления.",
                "Через неделю проверьте, стало ли проще ответить на три вопроса: "
                "кто ведёт задачу, что её блокирует и по каким признакам её можно принять.",
                "#бизнеспроцессы",
            ),
            (
                "Обсуждение: где возникает узкое место в команде?",
                f"В проектах по теме «{niche}» сроки могут сдвигаться из-за нечётких задач, "
                "ожидания согласований или повторных правок.",
                "Возьмите последнюю задачу, которая вышла за срок. Восстановите её этапы "
                "и отметьте, сколько времени ушло на работу и сколько на ожидание.",
                "Начните с этапа, где ожидание было самым долгим. "
                "Если сталкивались с подобным, поделитесь, какой шаг помог сократить задержку.",
                "#эффективностькоманды",
            ),
            (
                "Анонс практикума: от задачи до критерия готовности",
                f"В работе по теме «{niche}» спор о готовности результата часто начинается "
                "после того, как команда уже потратила время на выполнение.",
                "На практикуме разберём, как сформулировать критерий готовности до старта, "
                "согласовать его с заказчиком и проверить на одном рабочем примере.",
                "Подготовьте задачу, по которой недавно возникли разногласия. "
                "На ней будет проще проверить, достаточно ли точно описан ожидаемый результат.",
                "#деловаяпрактика",
            ),
        ]
        niche_tag = topic_hashtag(niche)
        return [
            (
                title,
                f"Проблема\n{problem}\n\nРешение\n{solution}\n\nВывод\n{takeaway}\n\n"
                + " ".join(dict.fromkeys((niche_tag, tag))),
            )
            for title, problem, solution, takeaway, tag in posts
        ]

    @staticmethod
    def _social_mock_posts(niche: str, platform: Platform) -> list[tuple[str, str]]:
        """Build the existing VK and Telegram drafts with their reader prompts."""
        prompts = {
            Platform.TELEGRAM: (
                "Сохраните сообщение в «Избранное», чтобы вернуться к нему через неделю.",
                "Выберите вариант реакцией: 👍 — А, 🔥 — Б, ❤️ — В, 👀 — Г.",
                "Загляните в канал завтра за продолжением.",
            ),
            Platform.VK: (
                "Сохраните запись и поделитесь в комментариях, что взяли в работу.",
                "Напишите букву варианта в комментариях. Если есть свой вариант, добавьте его.",
                "Подпишитесь на сообщество, чтобы увидеть продолжение в ленте.",
            ),
        }
        save, vote, follow = prompts[platform]
        return [
            (
                "Экспертный гайд: первый результат за неделю",
                f"Осваиваете тему «{niche}»? Выберите задачу, которую можно закончить "
                "за семь дней, и заранее определите, по какому признаку поймёте, что она решена.\n\n"
                "1. Запишите, как вы справляетесь с ней сейчас.\n"
                "2. Измените один шаг и попробуйте новый вариант три раза.\n"
                "3. В конце недели сравните время, качество и то, сколько раз пришлось переделывать.\n\n"
                f"Оставьте изменение, если оно помогает хотя бы по одному показателю. {save}",
            ),
            (
                "Кейс внедрения: от хаоса к понятному процессу",
                f"Разберём учебный кейс по теме «{niche}». Задача повторяется каждую "
                "неделю, но каждый раз приходится заново искать материалы и проверять, "
                "всё ли готово.\n\n"
                "Собираем исходники в одном месте, переносим постоянные шаги в шаблон "
                "и добавляем один пункт финальной проверки. На следующих двух запусках "
                "записываем время и отмечаем, на каком шаге возникла задержка.\n\n"
                "Так видно, что улучшать дальше: сам шаблон, доступ к исходникам или "
                f"порядок проверки. {save}",
            ),
            (
                "Подборка: три инструмента для начала",
                f"Для первых задач по теме «{niche}» хватит трёх привычных инструментов.\n\n"
                "• Google Таблицы — колонки «задача», «следующий шаг», «результат» "
                "помогут сравнить несколько попыток.\n"
                "• Trello — доска «в очереди / в работе / готово» покажет, где работа застряла.\n"
                "• Таймер на телефоне — выделите 25 минут на один шаг и запишите, "
                "сколько успели.\n\n"
                f"Начните с инструмента, который закрывает вашу главную трудность. {save}",
            ),
            (
                "Опрос: что сейчас мешает продвинуться?",
                f"Что сложнее всего в теме «{niche}» прямо сейчас?\n\n"
                "А. Выбрать первый шаг.\nБ. Найти время на практику.\n"
                "В. Понять, хороший ли получился результат.\nГ. Повторить удачный подход.\n\n"
                f"{vote} По самому частому ответу можно собрать отдельную шпаргалку.",
            ),
            (
                "Анонс: практикум с разбором одной задачи",
                f"В следующем посте по теме «{niche}» разберём одну задачу от "
                "постановки до проверки результата. Покажем, как выбрать критерий "
                "готовности, на чём обычно теряется время и что записать после первой попытки.\n\n"
                "Подготовьте задачу, которую давно откладываете. Сформулируйте её "
                f"одним предложением, чтобы пройти разбор на своём примере. {follow}",
            ),
        ]
