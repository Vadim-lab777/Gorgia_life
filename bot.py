import asyncio
import json
import os
import re
import time
from pathlib import Path
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    BotCommandScopeDefault,
)
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_ID = 580885320
CHANNEL_ID = -1004471760987

if not BOT_TOKEN:
    raise SystemExit("Не найден BOT_TOKEN. Проверь переменные окружения")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

BOT_USERNAME = "gruzia_life_bot"
PRIVATE_ONLY = F.chat.type == "private"


# ============================================================
# ХРАНИЛИЩЕ
# ============================================================
TOPICS_FILE = Path("/data/topics.json") if Path("/data").exists() else Path(__file__).parent / "topics.json"

RUBRIC_DEFS = {
    "work_remote":    ("💼 Работа удалённая", "💼 Работа"),
    "work_offline":   ("🏢 Офлайн-работа",    "🏢 Офлайн"),
    "services_offer": ("🛠 Услуги",           "🛠 Услуги"),
    "housing_rent":   ("🏠 Жильё",            "🏠 Жильё"),
    "events":         ("🎉 Мероприятия",       "🎉 События"),
    "dating":         ("👋 Знакомства",        "👋 Знакомства"),
    "other":          ("📢 Разное",            "📢 Разное"),
}


def load_data() -> dict:
    if TOPICS_FILE.exists():
        try:
            data = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
            if "rubrics" not in data:
                old = data
                data = {"menu_message_id": None, "rubrics": {}}
                for k, v in old.items():
                    if isinstance(v, dict) and "thread_id" in v:
                        v.setdefault("count", 0)
                        v.setdefault("messages", [])
                        data["rubrics"][k] = v
            for r in data.get("rubrics", {}).values():
                r.setdefault("messages", [])
            return data
        except Exception:
            return {"menu_message_id": None, "rubrics": {}}
    return {"menu_message_id": None, "rubrics": {}}


def save_data(data: dict):
    TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOPICS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============================================================
# АНТИСПАМ
# ============================================================
COOLDOWN_SECONDS = 3600
MIN_LENGTH = 20
MAX_LINKS = 2

user_ad_history: dict[int, dict] = {}


def count_links(text: str) -> int:
    if not text:
        return 0
    patterns = [r"https?://\S+", r"t\.me/\S+", r"@[A-Za-z0-9_]{3,}"]
    return sum(len(re.findall(p, text)) for p in patterns)


def check_spam(user_id: int, text: str) -> str | None:
    now = time.time()
    hist = user_ad_history.get(user_id, {})
    last_time = hist.get("last_time", 0)
    if now - last_time < COOLDOWN_SECONDS:
        left = int(COOLDOWN_SECONDS - (now - last_time))
        return f"⏳ Слишком часто. Следующее через {left // 60} мин."
    if len(text.strip()) < MIN_LENGTH:
        return f"✏️ Слишком коротко. Минимум {MIN_LENGTH} символов."
    if count_links(text) > MAX_LINKS:
        return f"🚫 Слишком много ссылок. Максимум {MAX_LINKS}."
    if hist.get("last_text", "").strip() == text.strip():
        return "🔁 Это объявление вы уже отправляли."
    return None


def remember_ad(user_id: int, text: str):
    user_ad_history[user_id] = {"last_time": time.time(), "last_text": text}


class AdForm(StatesGroup):
    waiting_text = State()


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def dashboard_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=title, callback_data=f"ad_start:{key}")]
        for key, (title, _) in RUBRIC_DEFS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ad_cancel")],
    ])


# ============================================================
# ДАШБОРД В ГРУППЕ
# ============================================================
def build_group_dashboard() -> tuple[str, InlineKeyboardMarkup]:
    data = load_data()
    rubrics = data.get("rubrics", {})

    chat_str = str(CHANNEL_ID)
    chat_slug = chat_str[4:] if chat_str.startswith("-100") else chat_str.lstrip("-")

    buttons = []
    for key, (full_title, short_title) in RUBRIC_DEFS.items():
        topic = rubrics.get(key, {})
        thread_id = topic.get("thread_id")
        count = len(topic.get("messages", []))

        text = f"{short_title} ({count})"

        if thread_id:
            url = f"https://t.me/c/{chat_slug}/{thread_id}"
            buttons.append([InlineKeyboardButton(text=text, url=url)])
        else:
            buttons.append([InlineKeyboardButton(
                text=text + " — не готова",
                callback_data="noop",
            )])

    total = sum(len(r.get("messages", [])) for r in rubrics.values())

    text = (
        "📊 <b>Объявления по рубрикам</b>\n\n"
        f"Всего: <b>{total}</b>\n\n"
        "👇 Выберите рубрику — откроется тема с объявлениями.\n\n"
        f"📝 Разместить объявление — в боте @{BOT_USERNAME}"
    )

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def update_group_dashboard():
    data = load_data()
    text, kb = build_group_dashboard()
    menu_id = data.get("menu_message_id")

    if menu_id:
        try:
            await bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=menu_id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
        except Exception:
            pass

    sent = await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        reply_markup=kb,
        disable_notification=True,
    )
    data["menu_message_id"] = sent.message_id
    save_data(data)

    try:
        await bot.pin_chat_message(
            chat_id=CHANNEL_ID,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception:
        pass


# ============================================================
# КОМАНДЫ БОТА
# ============================================================
async def set_commands():
    commands = [
        BotCommand(command="start", description="Разместить объявление"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())


WELCOME = (
    "👋 <b>Добро пожаловать!</b>\n\n"
    "Выберите рубрику — и напишите своё объявление.\n"
    "Оно появится в группе в нужной теме.\n\n"
    "👇 Рубрики:"
)


@dp.message(Command("start"), PRIVATE_ONLY)
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME, reply_markup=dashboard_kb(), parse_mode="HTML")


@dp.message(Command("cancel"), PRIVATE_ONLY)
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=dashboard_kb())


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(
        f"Твой ID: <code>{message.from_user.id}</code>\n"
        f"Чат ID: <code>{message.chat.id}</code>\n"
        f"Thread ID: <code>{message.message_thread_id}</code>",
        parse_mode="HTML",
    )


# ============================================================
# АДМИН: настройка тем
# ============================================================
@dp.message(Command("setup_topics"), PRIVATE_ONLY)
async def cmd_setup_topics(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для админа.")
        return

    try:
        chat = await bot.get_chat(CHANNEL_ID)
    except Exception as e:
        await message.answer(f"⚠️ Не могу получить инфо о группе: {e}")
        return

    if not getattr(chat, "is_forum", False):
        await message.answer(
            "⚠️ В группе <b>не включены темы</b>.\n\n"
            "Включи вручную: группа → Управление → Темы.",
            parse_mode="HTML",
        )
        return

    await message.answer("⏳ Создаю темы и дашборд…")

    data = load_data()
    rubrics = data.setdefault("rubrics", {})
    created = []
    skipped = []
    errors = []

    for key, (full_title, _) in RUBRIC_DEFS.items():
        existing = rubrics.get(key, {})
        if existing.get("thread_id"):
            existing.setdefault("count", 0)
            existing.setdefault("messages", [])
            existing["title"] = full_title
            skipped.append(full_title)
            continue

        try:
            result = await bot.create_forum_topic(
                chat_id=CHANNEL_ID,
                name=full_title,
            )
            rubrics[key] = {
                "title": full_title,
                "thread_id": result.message_thread_id,
                "count": 0,
                "messages": [],
            }
            created.append(f"{full_title} (id={result.message_thread_id})")
            await asyncio.sleep(2)
        except Exception as e:
            errors.append(f"{full_title}: {e}")

    save_data(data)

    try:
        await update_group_dashboard()
    except Exception as e:
        errors.append(f"Дашборд: {e}")

    lines = ["✅ <b>Готово!</b>\n"]
    if created:
        lines.append("<b>Создано тем:</b>\n" + "\n".join(created) + "\n")
    if skipped:
        lines.append("<b>Уже было:</b>\n" + "\n".join(skipped) + "\n")
    if errors:
        lines.append("<b>Ошибки:</b>\n" + "\n".join(errors) + "\n")

    lines.append("📊 Дашборд закреплён в группе.")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("update_dashboard"), PRIVATE_ONLY)
async def cmd_update_dashboard(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для админа.")
        return
    try:
        await update_group_dashboard()
        await message.answer("✅ Дашборд обновлён.")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")


# ============================================================
# АДМИН: пересчёт по факту
# ============================================================
@dp.message(Command("recount"), PRIVATE_ONLY)
async def cmd_recount(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для админа.")
        return

    await message.answer("⏳ Пересчитываю…")

    data = load_data()
    rubrics = data.get("rubrics", {})
    total_removed = 0
    report = []

    for key, (full_title, _) in RUBRIC_DEFS.items():
        rubric = rubrics.get(key, {})
        msgs = rubric.get("messages", [])
        alive = []
        removed_here = 0

        for m in msgs:
            mid = m.get("id")
            username = m.get("user")
            kb = None
            if username:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="✉️ Написать автору",
                        url=f"https://t.me/{username}",
                    )],
                ])
            try:
                await bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID,
                    message_id=mid,
                    reply_markup=kb,
                )
                alive.append(m)
            except TelegramBadRequest as e:
                err = str(e).lower()
                if "not modified" in err:
                    alive.append(m)
                elif "not found" in err or "message_id_invalid" in err:
                    removed_here += 1
                else:
                    alive.append(m)
            except Exception:
                alive.append(m)
            await asyncio.sleep(0.3)

        rubric["messages"] = alive
        rubric["count"] = len(alive)
        total_removed += removed_here
        report.append(f"{full_title}: {len(alive)} (удалено {removed_here})")

    save_data(data)

    try:
        await update_group_dashboard()
    except Exception:
        pass

    await message.answer(
        "✅ <b>Пересчёт завершён.</b>\n\n"
        + "\n".join(report)
        + f"\n\n<b>Всего удалено:</b> {total_removed}",
        parse_mode="HTML",
    )


# ============================================================
# АДМИН: ручная правка
# ============================================================
@dp.message(Command("minus"), PRIVATE_ONLY)
async def cmd_minus(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для админа.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        keys = ", ".join(RUBRIC_DEFS.keys())
        await message.answer(
            f"Использование: <code>/minus ключ</code>\n\n"
            f"Ключи:\n{keys}",
            parse_mode="HTML",
        )
        return

    key = parts[1].strip()
    if key not in RUBRIC_DEFS:
        await message.answer(f"⚠️ Неизвестный ключ: {key}")
        return

    data = load_data()
    rubrics = data.get("rubrics", {})
    if key not in rubrics:
        await message.answer(f"⚠️ Рубрика {key} не найдена.")
        return

    msgs = rubrics[key].get("messages", [])
    if not msgs:
        await message.answer("Рубрика уже пуста.")
        return

    msgs.pop()
    rubrics[key]["count"] = len(msgs)
    save_data(data)
    await update_group_dashboard()
    await message.answer(f"✅ {RUBRIC_DEFS[key][0]}: удалена 1 запись из счётчика.")


@dp.message(Command("setcount"), PRIVATE_ONLY)
async def cmd_setcount(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для админа.")
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer(
            "Использование: <code>/setcount ключ число</code>",
            parse_mode="HTML",
        )
        return

    key = parts[1].strip()
    try:
        value = int(parts[2])
    except ValueError:
        await message.answer("⚠️ Число должно быть целым.")
        return

    if key not in RUBRIC_DEFS:
        await message.answer(f"⚠️ Неизвестный ключ: {key}")
        return

    data = load_data()
    rubrics = data.setdefault("rubrics", {})
    if key not in rubrics:
        rubrics[key] = {
            "title": RUBRIC_DEFS[key][0],
            "thread_id": None,
            "count": value,
            "messages": [],
        }
    else:
        rubrics[key]["count"] = value

    save_data(data)
    await update_group_dashboard()
    await message.answer(f"✅ {RUBRIC_DEFS[key][0]}: установлено {value}.")


@dp.message(Command("stats"), PRIVATE_ONLY)
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для админа.")
        return

    data = load_data()
    rubrics = data.get("rubrics", {})

    lines = ["📊 <b>Счётчики по рубрикам</b>\n"]
    total = 0
    for key, (title, _) in RUBRIC_DEFS.items():
        count = len(rubrics.get(key, {}).get("messages", []))
        total += count
        lines.append(f"<code>{key}</code> — {title}: <b>{count}</b>")

    lines.append(f"\n<b>Всего:</b> {total}")
    lines.append("\n<b>Команды:</b>")
    lines.append("/recount — пересчитать по факту (учесть удалённые)")
    lines.append("/minus ключ — убрать 1 из счётчика")
    lines.append("/setcount ключ число — установить точное")

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "ad_cancel")
async def cb_ad_cancel(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer()
        return
    await state.clear()
    await callback.message.edit_text(WELCOME, reply_markup=dashboard_kb(), parse_mode="HTML")
    await callback.answer("Отменено")


@dp.callback_query(F.data.startswith("ad_start:"))
async def cb_ad_start(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer()
        return

    key = callback.data.split(":", 1)[1]
    title_pair = RUBRIC_DEFS.get(key)
    if not title_pair:
        await callback.answer("Неизвестная рубрика", show_alert=True)
        return

    title = title_pair[0]
    await state.update_data(rubric_key=key, rubric_title=title)
    await state.set_state(AdForm.waiting_text)

    await callback.message.edit_text(
        f"📝 <b>{title}</b>\n\n"
        "Напишите текст объявления одним сообщением.\n"
        "Можно фото с подписью.\n\n"
        "<b>Что указать:</b>\n"
        "• Кто вы / что предлагаете\n"
        "• Детали (район, оплата, опыт)\n"
        "• Контакты для связи\n\n"
        "<i>Для отмены — /cancel.</i>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


# ============================================================
# ПРИЁМ ОБЪЯВЛЕНИЯ
# ============================================================
@dp.message(AdForm.waiting_text, PRIVATE_ONLY)
async def process_ad(message: Message, state: FSMContext):
    data = await state.get_data()
    rubric_key = data.get("rubric_key")
    rubric_title = data.get("rubric_title", "📢 Объявление")
    user = message.from_user
    text = message.text or message.caption or ""

    spam_error = check_spam(user.id, text)
    if spam_error:
        await message.answer(spam_error)
        return

    file_data = load_data()
    rubrics = file_data.get("rubrics", {})
    topic_data = rubrics.get(rubric_key, {})
    thread_id = topic_data.get("thread_id")

    if thread_id is None:
        await message.answer(
            "⚠️ Рубрика ещё не настроена.\n"
            "Попросите администратора выполнить /setup_topics."
        )
        await state.clear()
        return

    header = (
        f"<b>{rubric_title}</b>\n"
        f"👤 {user.full_name}"
        f"{' (@' + user.username + ')' if user.username else ''}\n"
        f"─────\n"
    )
    full_text = header + (message.html_text if message.text else (message.caption or ""))

    contact_kb = None
    if user.username:
        contact_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✉️ Написать автору", url=f"https://t.me/{user.username}")],
        ])

    try:
        if message.photo:
            photo_file_id = message.photo[-1].file_id
            sent = await bot.send_photo(
                chat_id=CHANNEL_ID,
                message_thread_id=thread_id,
                photo=photo_file_id,
                caption=full_text[:1024],
                parse_mode="HTML",
                reply_markup=contact_kb,
            )
        else:
            sent = await bot.send_message(
                chat_id=CHANNEL_ID,
                message_thread_id=thread_id,
                text=full_text,
                parse_mode="HTML",
                reply_markup=contact_kb,
            )
    except TelegramRetryAfter as e:
        await message.answer(f"⏳ Подожди {e.retry_after} сек.")
        await state.clear()
        return
    except TelegramBadRequest as e:
        if "thread not found" in str(e).lower():
            rubrics.pop(rubric_key, None)
            save_data(file_data)
            await message.answer("⚠️ Тема удалена. Админ, запустите /setup_topics заново.")
        else:
            await message.answer(f"⚠️ Ошибка: {e}")
        await state.clear()
        return
    except Exception as e:
        await message.answer(f"⚠️ Не удалось: {e}")
        await state.clear()
        return

    # Сохраняем message_id и автора
    if "messages" not in rubrics[rubric_key]:
        rubrics[rubric_key]["messages"] = []
    rubrics[rubric_key]["messages"].append({
        "id": sent.message_id,
        "user": user.username or "",
    })
    rubrics[rubric_key]["count"] = len(rubrics[rubric_key]["messages"])
    save_data(file_data)

    try:
        await update_group_dashboard()
    except Exception:
        pass

    remember_ad(user.id, text)

    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"📢 Новое объявление в «{rubric_title}»\n"
                f"От: {user.full_name} (@{user.username or 'без username'})",
            )
        except Exception:
            pass

    await state.clear()
    await message.answer(
        f"✅ <b>Опубликовано в «{rubric_title}»</b>\n\n"
        "Следующее — через 1 час.",
        reply_markup=dashboard_kb(),
        parse_mode="HTML",
    )


# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    await set_commands()
    print("Бот запущен. Нажми Ctrl+C для остановки.")
    print(f"Данные: {TOPICS_FILE}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
