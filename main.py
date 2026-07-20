from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import date
from functools import lru_cache
from pathlib import Path

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ModuleNotFoundError:
    InlineKeyboardButton = InlineKeyboardMarkup = ReplyKeyboardMarkup = Update = None
    Application = CallbackQueryHandler = CommandHandler = ContextTypes = MessageHandler = filters = None


BASE_DIR = Path(__file__).resolve().parent
WORDS_PATH = BASE_DIR / "data" / "words.jsonl"
DB_PATH = BASE_DIR / "bot.db"
ENV_PATH = BASE_DIR / ".env"
LEVELS = ("A1", "A2", "B1", "B2", "C1")
LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}
LEARNED_STREAK_TARGET = 7
LEARNED_REVIEW_CHANCE = 0.15
PAGE_SIZE = 8
PERSIAN_TEXT_RE = re.compile(r"[\u0600-\u06ff]")
MEANING_STOPWORDS = {
    "از",
    "به",
    "با",
    "برای",
    "در",
    "را",
    "و",
    "یا",
    "یک",
    "کردن",
    "شدن",
    "دادن",
    "داشتن",
    "بودن",
}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def expand_compact_word(row: dict) -> dict:
    if "w" not in row:
        return row
    level = row.get("l", "A1")
    return {
        "id": row["i"],
        "word": row["w"],
        "meaning_fa": row.get("m", ""),
        "example": row.get("e", ""),
        "levels": [level],
        "primary_level": level,
    }


def load_words(path: Path = WORDS_PATH) -> list[dict]:
    with path.open("r", encoding="utf-8") as file:
        if path.suffix == ".jsonl":
            words = [
                expand_compact_word(json.loads(line))
                for line in file
                if line.strip()
            ]
        else:
            words = json.load(file)
    if not words:
        raise RuntimeError("Vocabulary file is empty.")

    for word in words:
        word.setdefault("meaning_fa", "")
        word.setdefault("example", make_example(word["word"]))
    return words


def make_example(word: str) -> str:
    clean_word = word.split("(")[0].strip()
    return f"I learned the word {clean_word} today. / من امروز کلمه «{clean_word}» را یاد گرفتم."


def example_has_persian_translation(example: str) -> bool:
    if " / " not in example:
        return False
    _, translation = example.split(" / ", 1)
    return bool(PERSIAN_TEXT_RE.search(translation)) and "?" not in translation


def has_persian_meaning(meaning: str) -> bool:
    return bool(meaning) and "?" not in meaning and bool(PERSIAN_TEXT_RE.search(meaning))


WORDS = load_words()
WORDS_BY_ID = {word["id"]: word for word in WORDS}
WORDS_BY_LEVEL = {
    level: [word for word in WORDS if level in word["levels"]] for level in LEVELS
}
TRAINABLE_WORDS = [word for word in WORDS if has_persian_meaning(word.get("meaning_fa", ""))]
TRAINABLE_WORD_IDS = {word["id"] for word in TRAINABLE_WORDS}
TRAINABLE_WORDS_BY_LEVEL = {
    level: [word for word in TRAINABLE_WORDS if level in word["levels"]] for level in LEVELS
}


class ProgressStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self._init_db()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS word_progress (
                    user_id INTEGER NOT NULL,
                    word_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'learning',
                    correct_streak INTEGER NOT NULL DEFAULT 0,
                    correct_total INTEGER NOT NULL DEFAULT 0,
                    wrong_total INTEGER NOT NULL DEFAULT 0,
                    seen_total INTEGER NOT NULL DEFAULT 0,
                    learned_at TEXT,
                    last_seen TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, word_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    min_level TEXT NOT NULL DEFAULT 'A1',
                    placement_offered INTEGER NOT NULL DEFAULT 0,
                    placement_done INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_stats (
                    user_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    learned_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, day)
                )
                """
            )

            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(word_progress)").fetchall()
            }
            migrations = {
                "correct_streak": "ALTER TABLE word_progress ADD COLUMN correct_streak INTEGER NOT NULL DEFAULT 0",
                "correct_total": "ALTER TABLE word_progress ADD COLUMN correct_total INTEGER NOT NULL DEFAULT 0",
                "wrong_total": "ALTER TABLE word_progress ADD COLUMN wrong_total INTEGER NOT NULL DEFAULT 0",
                "seen_total": "ALTER TABLE word_progress ADD COLUMN seen_total INTEGER NOT NULL DEFAULT 0",
                "learned_at": "ALTER TABLE word_progress ADD COLUMN learned_at TEXT",
                "updated_at": "ALTER TABLE word_progress ADD COLUMN updated_at TEXT",
            }
            for column, sql in migrations.items():
                if column not in columns:
                    connection.execute(sql)
            connection.execute(
                """
                UPDATE word_progress
                SET status = CASE
                    WHEN status = 'known' THEN 'learned'
                    WHEN status = 'review' THEN 'learning'
                    WHEN status = 'new' THEN 'learning'
                    ELSE status
                END
                """
            )

    def ensure_user(self, user_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)",
                (user_id,),
            )
            return connection.execute(
                "SELECT * FROM user_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()

    def set_placement_offered(self, user_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_settings (user_id, placement_offered)
                VALUES (?, 1)
                ON CONFLICT(user_id)
                DO UPDATE SET placement_offered = 1, updated_at = CURRENT_TIMESTAMP
                """,
                (user_id,),
            )

    def set_min_level(self, user_id: int, min_level: str, placement_done: bool = True) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_settings (user_id, min_level, placement_offered, placement_done)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET
                    min_level = excluded.min_level,
                    placement_offered = 1,
                    placement_done = excluded.placement_done,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, min_level, int(placement_done)),
            )

    def progress_for(self, user_id: int, word_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM word_progress WHERE user_id = ? AND word_id = ?",
                (user_id, word_id),
            ).fetchone()

    def seen(self, user_id: int, word_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO word_progress (user_id, word_id, status, seen_total, last_seen, updated_at)
                VALUES (?, ?, 'learning', 1, date('now'), CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, word_id)
                DO UPDATE SET
                    seen_total = seen_total + 1,
                    last_seen = excluded.last_seen,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, word_id),
            )

    def answer(self, user_id: int, word_id: int, is_correct: bool) -> dict:
        current = self.progress_for(user_id, word_id)
        previous_status = current["status"] if current else "learning"
        previous_streak = current["correct_streak"] if current else 0

        if is_correct:
            streak = previous_streak + 1
            learned_now = previous_status != "learned" and streak >= LEARNED_STREAK_TARGET
            status = "learned" if learned_now or previous_status == "learned" else "learning"
            learned_at = "date('now')" if learned_now else "learned_at"
            with self.connect() as connection:
                connection.execute(
                    f"""
                    INSERT INTO word_progress (
                        user_id, word_id, status, correct_streak, correct_total,
                        seen_total, learned_at, last_seen, updated_at
                    )
                    VALUES (?, ?, ?, ?, 1, 1, CASE WHEN ? THEN date('now') END, date('now'), CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, word_id)
                    DO UPDATE SET
                        status = excluded.status,
                        correct_streak = excluded.correct_streak,
                        correct_total = correct_total + 1,
                        seen_total = seen_total + 1,
                        learned_at = CASE WHEN ? THEN date('now') ELSE {learned_at} END,
                        last_seen = excluded.last_seen,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (user_id, word_id, status, streak, int(learned_now), int(learned_now)),
                )
                if learned_now:
                    self._increment_daily(connection, user_id)
            return {"streak": streak, "status": status, "learned_now": learned_now}

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO word_progress (
                    user_id, word_id, status, correct_streak, wrong_total,
                    seen_total, learned_at, last_seen, updated_at
                )
                VALUES (?, ?, 'learning', 0, 1, 1, NULL, date('now'), CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, word_id)
                DO UPDATE SET
                    status = 'learning',
                    correct_streak = 0,
                    wrong_total = wrong_total + 1,
                    seen_total = seen_total + 1,
                    learned_at = NULL,
                    last_seen = excluded.last_seen,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, word_id),
            )
        return {"streak": 0, "status": "learning", "learned_now": False}

    def mark_learned(self, user_id: int, word_id: int) -> None:
        already = self.progress_for(user_id, word_id)
        learned_now = not already or already["status"] != "learned"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO word_progress (
                    user_id, word_id, status, correct_streak,
                    learned_at, last_seen, updated_at
                )
                VALUES (?, ?, 'learned', ?, date('now'), date('now'), CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, word_id)
                DO UPDATE SET
                    status = 'learned',
                    correct_streak = ?,
                    learned_at = COALESCE(learned_at, date('now')),
                    last_seen = date('now'),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, word_id, LEARNED_STREAK_TARGET, LEARNED_STREAK_TARGET),
            )
            if learned_now:
                self._increment_daily(connection, user_id)

    def hide_word(self, user_id: int, word_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO word_progress (user_id, word_id, status, correct_streak, learned_at, updated_at)
                VALUES (?, ?, 'hidden', 0, NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, word_id)
                DO UPDATE SET
                    status = 'hidden',
                    correct_streak = 0,
                    learned_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, word_id),
            )

    def move_to_learning(self, user_id: int, word_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO word_progress (user_id, word_id, status, correct_streak, learned_at, updated_at)
                VALUES (?, ?, 'learning', 0, NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, word_id)
                DO UPDATE SET
                    status = 'learning',
                    correct_streak = 0,
                    learned_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, word_id),
            )

    def reset_all(self, user_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM word_progress WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM daily_stats WHERE user_id = ?", (user_id,))
            connection.execute(
                """
                INSERT INTO user_settings (user_id)
                VALUES (?)
                ON CONFLICT(user_id)
                DO UPDATE SET
                    min_level = 'A1',
                    placement_offered = 0,
                    placement_done = 0,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id,),
            )

    def stats(self, user_id: int) -> dict:
        today = date.today().isoformat()
        with self.connect() as connection:
            status_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM word_progress
                WHERE user_id = ?
                GROUP BY status
                """,
                (user_id,),
            ).fetchall()
            today_row = connection.execute(
                "SELECT learned_count FROM daily_stats WHERE user_id = ? AND day = ?",
                (user_id, today),
            ).fetchone()
        totals = {"learning": 0, "learned": 0, "hidden": 0}
        for row in status_rows:
            totals[row["status"]] = row["total"]
        totals["today_learned"] = today_row["learned_count"] if today_row else 0
        hidden_trainable = len(self.hidden_ids(user_id) & TRAINABLE_WORD_IDS)
        totals["available"] = len(TRAINABLE_WORDS) - hidden_trainable
        return totals

    def admin_stats(self) -> dict:
        today = date.today().isoformat()
        with self.connect() as connection:
            user_count = connection.execute(
                "SELECT COUNT(*) AS total FROM user_settings"
            ).fetchone()["total"]
            active_today = connection.execute(
                """
                SELECT COUNT(DISTINCT user_id) AS total
                FROM word_progress
                WHERE last_seen = ?
                """,
                (today,),
            ).fetchone()["total"]
            progress_rows = connection.execute(
                "SELECT COUNT(*) AS total FROM word_progress"
            ).fetchone()["total"]
            status_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM word_progress
                GROUP BY status
                """
            ).fetchall()
            learned_today = connection.execute(
                """
                SELECT COALESCE(SUM(learned_count), 0) AS total
                FROM daily_stats
                WHERE day = ?
                """,
                (today,),
            ).fetchone()["total"]
            learned_all = connection.execute(
                "SELECT COALESCE(SUM(learned_count), 0) AS total FROM daily_stats"
            ).fetchone()["total"]
            placement_done = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM user_settings
                WHERE placement_done = 1
                """
            ).fetchone()["total"]
            level_rows = connection.execute(
                """
                SELECT min_level, COUNT(*) AS total
                FROM user_settings
                GROUP BY min_level
                ORDER BY min_level
                """
            ).fetchall()

        statuses = {"learning": 0, "learned": 0, "hidden": 0}
        for row in status_rows:
            statuses[row["status"]] = row["total"]
        levels = {row["min_level"]: row["total"] for row in level_rows}
        return {
            "user_count": user_count,
            "active_today": active_today,
            "progress_rows": progress_rows,
            "learned_today": learned_today,
            "learned_all": learned_all,
            "placement_done": placement_done,
            "statuses": statuses,
            "levels": levels,
        }

    def word_ids_by_status(self, user_id: int, status: str) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT word_id
                FROM word_progress
                WHERE user_id = ? AND status = ?
                ORDER BY updated_at DESC, word_id ASC
                """,
                (user_id, status),
            ).fetchall()
        return [row["word_id"] for row in rows]

    def hidden_ids(self, user_id: int) -> set[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT word_id FROM word_progress WHERE user_id = ? AND status = 'hidden'",
                (user_id,),
            ).fetchall()
        return {row["word_id"] for row in rows}

    def learned_ids(self, user_id: int) -> set[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT word_id FROM word_progress WHERE user_id = ? AND status = 'learned'",
                (user_id,),
            ).fetchall()
        return {row["word_id"] for row in rows}

    def _increment_daily(self, connection: sqlite3.Connection, user_id: int) -> None:
        connection.execute(
            """
            INSERT INTO daily_stats (user_id, day, learned_count)
            VALUES (?, date('now'), 1)
            ON CONFLICT(user_id, day)
            DO UPDATE SET learned_count = learned_count + 1
            """,
            (user_id,),
        )


STORE = ProgressStore()


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["شروع تمرین", "آزمون تعیین سطح"],
            ["لیست‌ها", "آمار"],
            ["تنظیمات", "راهنما"],
        ],
        resize_keyboard=True,
    )


def word_answer(entry: dict) -> str:
    if entry.get("meaning_fa"):
        return entry["meaning_fa"]
    return "معنی ثبت نشده"


def format_card(entry: dict, progress: sqlite3.Row | None = None) -> str:
    streak = progress["correct_streak"] if progress else 0
    return (
        "معنی درست این کلمه چیست؟\n\n"
        f"{entry['word']}\n"
        f"پیشرفت این کلمه: {streak}/{LEARNED_STREAK_TARGET}"
    )


def format_word(entry: dict) -> str:
    return (
        f"{entry['word']}\n"
        f"{word_answer(entry)}\n"
        f"نمونه: {entry.get('example') or make_example(entry['word'])}"
    )


def level_allowed(word: dict, min_level: str) -> bool:
    return LEVEL_RANK[word["primary_level"]] >= LEVEL_RANK[min_level]


def get_candidate_words(user_id: int) -> tuple[list[dict], list[dict]]:
    settings = STORE.ensure_user(user_id)
    min_level = settings["min_level"]
    hidden = STORE.hidden_ids(user_id)
    learned = STORE.learned_ids(user_id)
    allowed = [
        word
        for word in TRAINABLE_WORDS
        if word["id"] not in hidden and level_allowed(word, min_level)
    ]
    learned_words = [word for word in allowed if word["id"] in learned]
    learning_words = [word for word in allowed if word["id"] not in learned]
    return learning_words, learned_words


def pick_study_word(user_id: int) -> dict | None:
    learning_words, learned_words = get_candidate_words(user_id)
    if learned_words and (not learning_words or random.random() < LEARNED_REVIEW_CHANCE):
        return random.choice(learned_words)
    if learning_words:
        return random.choice(learning_words)
    if learned_words:
        return random.choice(learned_words)
    return None


def build_options(entry: dict) -> list[dict]:
    correct_answer = word_answer(entry)
    pool = unique_meaning_pool(entry, correct_answer)
    same_level = [word for word in pool if word["primary_level"] == entry["primary_level"]]
    distractors = pick_distinct_distractors(correct_answer, same_level, 2)
    if len(distractors) < 2:
        used_ids = {word["id"] for word in distractors}
        fallback_pool = [word for word in pool if word["id"] not in used_ids]
        distractors.extend(pick_distinct_distractors(correct_answer, fallback_pool, 2 - len(distractors)))
    options = [entry, *distractors]
    random.shuffle(options)
    return options


@lru_cache(maxsize=None)
def normalize_meaning(value: str) -> str:
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[،,.!?؟؛:()\\[\\]{}\"'‌ـ-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


@lru_cache(maxsize=None)
def meaning_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in normalize_meaning(value).split()
        if token and token not in MEANING_STOPWORDS
    )


def meanings_too_similar(first: str, second: str) -> bool:
    first_normalized = normalize_meaning(first)
    second_normalized = normalize_meaning(second)
    if not first_normalized or not second_normalized:
        return True
    if first_normalized == second_normalized:
        return True
    if first_normalized in second_normalized or second_normalized in first_normalized:
        return True

    first_tokens = meaning_tokens(first)
    second_tokens = meaning_tokens(second)
    if not first_tokens or not second_tokens:
        return False
    overlap = first_tokens & second_tokens
    return len(overlap) / min(len(first_tokens), len(second_tokens)) >= 0.5


def unique_meaning_pool(entry: dict, correct_answer: str) -> list[dict]:
    pool = []
    used = {normalize_meaning(correct_answer)}
    for word in TRAINABLE_WORDS:
        answer = word_answer(word)
        key = normalize_meaning(answer)
        if word["id"] == entry["id"] or key in used or meanings_too_similar(correct_answer, answer):
            continue
        used.add(key)
        pool.append(word)
    return pool


def pick_distinct_distractors(correct_answer: str, candidates: list[dict], count: int) -> list[dict]:
    candidates = candidates[:]
    random.shuffle(candidates)
    selected = []
    for candidate in candidates:
        answer = word_answer(candidate)
        if meanings_too_similar(correct_answer, answer):
            continue
        if any(meanings_too_similar(answer, word_answer(item)) for item in selected):
            continue
        selected.append(candidate)
        if len(selected) == count:
            break
    return selected


def study_keyboard(entry: dict, options: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(word_answer(option), callback_data=f"answer:{entry['id']}:{option['id']}")]
        for option in options
    ]
    rows.extend(
        [
            [InlineKeyboardButton("این کلمه را می‌دانم", callback_data=f"known:{entry['id']}")],
            [InlineKeyboardButton("دیگر این کلمه را نشان نده", callback_data=f"hide:{entry['id']}")],
            [InlineKeyboardButton("نمایش نمونه جمله", callback_data=f"example:{entry['id']}")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def placement_keyboard(entry: dict) -> InlineKeyboardMarkup:
    choices = build_options(entry)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(word_answer(choice), callback_data=f"placement_answer:{entry['id']}:{choice['id']}")]
            for choice in choices
        ]
    )


async def send_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    stats = STORE.stats(user_id)
    settings = STORE.ensure_user(user_id)
    await update.message.reply_text(
        "صفحه اصلی\n\n"
        f"کلمات آموخته‌شده امروز: {stats['today_learned']}\n"
        f"کل کلمات آموخته‌شده: {stats['learned']}\n"
        f"کلمات در حال یادگیری: {stats['learning']}\n"
        f"سطح فعلی تمرین: {settings['min_level']} به بالا",
        reply_markup=main_keyboard(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    settings = STORE.ensure_user(user_id)
    await send_home(update, context)
    if not settings["placement_offered"]:
        STORE.set_placement_offered(user_id)
        await update.message.reply_text(
            "می‌خواهی اول آزمون تعیین سطح بدهی؟ اجباری نیست.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("شروع آزمون تعیین سطح", callback_data="placement_start")],
                    [InlineKeyboardButton("فعلا رد می‌کنم", callback_data="placement_skip")],
                ]
            ),
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/study - شروع تمرین سه‌گزینه‌ای\n"
        "/placement - آزمون تعیین سطح\n"
        "/lists - نمایش لیست‌ها\n"
        "/stats - آمار امروز و کل\n"
        "/settings - تنظیمات و بازنشانی\n"
        "/search word - جست‌وجوی کلمه\n"
        "/myid - نمایش شناسه تلگرام برای ادمین شدن",
        reply_markup=main_keyboard(),
    )


async def send_study_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    target = update.message or update.callback_query.message
    entry = pick_study_word(user_id)
    if not entry:
        await target.reply_text(
            "فعلا کلمه‌ای برای نمایش باقی نمانده. از تنظیمات می‌توانی لیست‌ها را ریست کنی.",
            reply_markup=main_keyboard(),
        )
        return
    STORE.seen(user_id, entry["id"])
    progress = STORE.progress_for(user_id, entry["id"])
    options = build_options(entry)
    await target.reply_text(
        format_card(entry, progress),
        reply_markup=study_keyboard(entry, options),
    )


async def study(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_study_card(update, context)


async def placement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_placement(update.effective_user.id, context)
    await update.message.reply_text(
        "آزمون تعیین سطح شروع شد. سطح درست هر کلمه را انتخاب کن.",
        reply_markup=main_keyboard(),
    )
    await send_placement_question(update, context)


async def start_placement(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    questions = []
    for level in LEVELS:
        candidates = TRAINABLE_WORDS_BY_LEVEL[level]
        questions.extend(random.sample(candidates, min(2, len(candidates))))
    random.shuffle(questions)
    context.user_data["placement"] = {
        "ids": [word["id"] for word in questions],
        "index": 0,
        "correct": 0,
    }


async def send_placement_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("placement")
    if not state:
        await placement(update, context)
        return
    if state["index"] >= len(state["ids"]):
        await finish_placement(update, context)
        return
    entry = WORDS_BY_ID[state["ids"][state["index"]]]
    target = update.message or update.callback_query.message
    await target.reply_text(
        f"سوال {state['index'] + 1}/{len(state['ids'])}\n\nمعنی این کلمه چیست؟\n{entry['word']}",
        reply_markup=placement_keyboard(entry),
    )


def placement_level(score: int) -> str:
    if score <= 2:
        return "A1"
    if score <= 4:
        return "A2"
    if score <= 6:
        return "B1"
    if score <= 8:
        return "B2"
    return "C1"


async def finish_placement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.pop("placement", None)
    score = state["correct"] if state else 0
    level = placement_level(score)
    STORE.set_min_level(update.effective_user.id, level, placement_done=True)
    target = update.message or update.callback_query.message
    await target.reply_text(
        f"نتیجه آزمون تعیین سطح: {level}\n"
        f"از این به بعد کلمات سطح {level} به بالا بیشتر نمایش داده می‌شوند.",
        reply_markup=main_keyboard(),
    )


async def lists(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "کدام لیست را می‌خواهی ببینی؟",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("آموخته‌شده‌ها", callback_data="list:learned:0")],
                [InlineKeyboardButton("آموخته‌نشده‌ها", callback_data="list:learning:0")],
                [InlineKeyboardButton("نمایش‌داده‌نشوند", callback_data="list:hidden:0")],
            ]
        ),
    )


def list_word_ids(user_id: int, list_name: str) -> list[int]:
    if list_name == "learned":
        return STORE.word_ids_by_status(user_id, "learned")
    if list_name == "hidden":
        return STORE.word_ids_by_status(user_id, "hidden")

    settings = STORE.ensure_user(user_id)
    hidden = STORE.hidden_ids(user_id)
    learned = STORE.learned_ids(user_id)
    explicit_learning = set(STORE.word_ids_by_status(user_id, "learning"))
    ids = [
        word["id"]
        for word in TRAINABLE_WORDS
        if word["id"] not in hidden
        and word["id"] not in learned
        and level_allowed(word, settings["min_level"])
    ]
    ids.sort(key=lambda word_id: (word_id not in explicit_learning, word_id))
    return ids


def render_list(user_id: int, list_name: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    ids = list_word_ids(user_id, list_name)
    title = {
        "learned": "آموخته‌شده‌ها",
        "learning": "آموخته‌نشده‌ها",
        "hidden": "نمایش‌داده‌نشوند",
    }[list_name]
    total_pages = max(1, (len(ids) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_ids = ids[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    lines = [f"{title} - صفحه {page + 1}/{total_pages}", f"تعداد: {len(ids)}"]
    rows = []
    for word_id in page_ids:
        entry = WORDS_BY_ID[word_id]
        lines.append(f"{entry['id']}. {entry['word']} - {word_answer(entry)}")
        action = "learn_again" if list_name == "learned" else "hide_from_list"
        label = "یادگیری دوباره" if list_name == "learned" else "دیگر نشان نده"
        if list_name == "hidden":
            action = "restore"
            label = "برگردان"
        rows.append([InlineKeyboardButton(f"{label}: {entry['word']}", callback_data=f"{action}:{word_id}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("قبلی", callback_data=f"list:{list_name}:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("بعدی", callback_data=f"list:{list_name}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("بازگشت به لیست‌ها", callback_data="lists_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    totals = STORE.stats(update.effective_user.id)
    await update.message.reply_text(
        "آمار یادگیری\n\n"
        f"آموخته‌شده امروز: {totals['today_learned']}\n"
        f"کل آموخته‌شده‌ها: {totals['learned']}\n"
        f"آموخته‌نشده‌ها: {totals['learning']}\n"
        f"کلمات حذف‌شده از نمایش: {totals['hidden']}\n"
        f"قابل تمرین: {totals['available']}",
        reply_markup=main_keyboard(),
    )


def admin_user_ids() -> set[int]:
    raw = os.getenv("ADMIN_USER_IDS", "")
    ids = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if item.isdigit():
            ids.add(int(item))
    return ids


def is_admin(user_id: int) -> bool:
    return user_id in admin_user_ids()


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"Telegram user id:\n{user.id}\n\n"
        "برای ادمین شدن این مقدار را در فایل .env بگذار:\n"
        f"ADMIN_USER_IDS={user.id}"
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(
            "تو هنوز ادمین ثبت نشده‌ای.\n"
            "اول /myid را بزن، بعد عدد را در .env با کلید ADMIN_USER_IDS قرار بده و ربات را ری‌استارت کن."
        )
        return

    totals = STORE.admin_stats()
    level_lines = ", ".join(
        f"{level}: {totals['levels'].get(level, 0)}" for level in LEVELS
    )
    await update.message.reply_text(
        "پنل ادمین\n\n"
        f"تعداد کاربران: {totals['user_count']}\n"
        f"کاربران فعال امروز: {totals['active_today']}\n"
        f"کاربران تعیین سطح داده: {totals['placement_done']}\n"
        f"کلمات آموخته‌شده امروز: {totals['learned_today']}\n"
        f"کل ثبت‌های آموخته‌شده: {totals['learned_all']}\n"
        f"رکوردهای پیشرفت: {totals['progress_rows']}\n"
        f"در حال یادگیری: {totals['statuses']['learning']}\n"
        f"آموخته‌شده: {totals['statuses']['learned']}\n"
        f"حذف‌شده از نمایش: {totals['statuses']['hidden']}\n"
        f"توزیع سطح کاربران: {level_lines}\n"
        f"تعداد کل کلمات دیتابیس: {len(WORDS)}\n"
        f"کلمات قابل تمرین: {len(TRAINABLE_WORDS)}"
    )


async def delete_message_after(message, seconds: int = 2) -> None:
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except Exception as exc:
        logger.debug("Could not delete temporary message: %s", exc)


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = STORE.ensure_user(update.effective_user.id)
    await update.message.reply_text(
        f"تنظیمات\n\nسطح فعلی: {current['min_level']}\nبرای بازنشانی کامل، دکمه زیر را بزن.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("بازنشانی همه چیز", callback_data="reset_confirm")],
                [InlineKeyboardButton("تعیین سطح دوباره", callback_data="placement_start")],
            ]
        ),
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip().casefold()
    if not query:
        await update.message.reply_text("بعد از /search کلمه را بنویس. مثلا:\n/search apple")
        return
    results = [
        word
        for word in WORDS
        if word["word"].casefold().startswith(query) or query in word["word"].casefold()
    ][:10]
    if not results:
        await update.message.reply_text("چیزی پیدا نشد.")
        return
    await update.message.reply_text("\n\n".join(format_word(word) for word in results))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if text == "شروع تمرین":
        await study(update, context)
    elif text == "آزمون تعیین سطح":
        await placement(update, context)
    elif text == "لیست‌ها":
        await lists(update, context)
    elif text == "آمار":
        await stats(update, context)
    elif text == "تنظیمات":
        await settings(update, context)
    elif text == "راهنما":
        await help_command(update, context)
    else:
        context.args = [text]
        await search(update, context)


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    parts = query.data.split(":")
    action = parts[0]

    if action == "answer":
        word_id = int(parts[1])
        selected_id = int(parts[2])
        is_correct = word_id == selected_id
        result = STORE.answer(user_id, word_id, is_correct)
        entry = WORDS_BY_ID[word_id]
        selected = WORDS_BY_ID[selected_id]
        if is_correct:
            prefix = "✅ درست بود."
            if result["learned_now"]:
                prefix += "\nاین کلمه وارد لیست آموخته‌شده‌ها شد."
            else:
                prefix += f"\nپیشرفت: {result['streak']}/{LEARNED_STREAK_TARGET}"
        else:
            prefix = (
                "❌ اشتباه بود.\n"
                "پیشرفت این کلمه ریست شد و دوباره وارد لیست یادگیری شد.\n"
                f"انتخاب تو: {word_answer(selected)}"
            )
        await query.message.edit_text(f"{prefix}\n\n{format_word(entry)}")
        asyncio.create_task(delete_message_after(query.message))
        await send_study_card(update, context)
        return

    if action == "known":
        word_id = int(parts[1])
        STORE.mark_learned(user_id, word_id)
        await query.edit_message_text("این کلمه به لیست آموخته‌شده‌ها اضافه شد.")
        await send_study_card(update, context)
        return

    if action == "hide":
        word_id = int(parts[1])
        STORE.hide_word(user_id, word_id)
        await query.edit_message_text("این کلمه دیگر به تو نمایش داده نمی‌شود.")
        await send_study_card(update, context)
        return

    if action == "hide_from_list":
        word_id = int(parts[1])
        STORE.hide_word(user_id, word_id)
        await query.message.reply_text("کلمه از لیست یادگیری حذف شد و دیگر نمایش داده نمی‌شود.")
        return

    if action == "example":
        entry = WORDS_BY_ID[int(parts[1])]
        await query.message.reply_text(
            f"نمونه جمله:\n{entry.get('example') or make_example(entry['word'])}"
        )
        return

    if action == "placement_start":
        await start_placement(user_id, context)
        await query.message.reply_text("آزمون تعیین سطح شروع شد.")
        await send_placement_question(update, context)
        return

    if action == "placement_skip":
        STORE.set_placement_offered(user_id)
        await query.edit_message_text("باشه، فعلا از سطح A1 شروع می‌کنیم.")
        return

    if action == "placement_answer":
        word_id = int(parts[1])
        selected_id = int(parts[2])
        entry = WORDS_BY_ID[word_id]
        selected = WORDS_BY_ID[selected_id]
        state = context.user_data.get("placement")
        if not state:
            await query.message.reply_text("آزمون فعال نیست. دوباره /placement را بزن.")
            return
        is_correct = selected_id == word_id
        if is_correct:
            state["correct"] += 1
        state["index"] += 1
        await query.message.edit_text(
            f"{'✅' if is_correct else '❌'} {entry['word']}\n"
            f"معنی درست: {word_answer(entry)}\n"
            f"انتخاب تو: {word_answer(selected)}"
        )
        asyncio.create_task(delete_message_after(query.message))
        await send_placement_question(update, context)
        return

    if action == "list":
        list_name = parts[1]
        page = int(parts[2])
        text, keyboard = render_list(user_id, list_name, page)
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if action == "lists_menu":
        await query.edit_message_text(
            "کدام لیست را می‌خواهی ببینی؟",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("آموخته‌شده‌ها", callback_data="list:learned:0")],
                    [InlineKeyboardButton("آموخته‌نشده‌ها", callback_data="list:learning:0")],
                    [InlineKeyboardButton("نمایش‌داده‌نشوند", callback_data="list:hidden:0")],
                ]
            ),
        )
        return

    if action == "learn_again":
        word_id = int(parts[1])
        STORE.move_to_learning(user_id, word_id)
        await query.message.reply_text("کلمه دوباره به لیست یادگیری برگشت.")
        return

    if action == "restore":
        word_id = int(parts[1])
        STORE.move_to_learning(user_id, word_id)
        await query.message.reply_text("کلمه دوباره قابل نمایش شد.")
        return

    if action == "reset_confirm":
        await query.edit_message_text(
            "مطمئنی همه پیشرفت‌ها، لیست آموخته‌شده‌ها و کلمات حذف‌شده پاک شوند؟",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("بله، همه چیز ریست شود", callback_data="reset_all")],
                    [InlineKeyboardButton("لغو", callback_data="settings_cancel")],
                ]
            ),
        )
        return

    if action == "reset_all":
        STORE.reset_all(user_id)
        await query.edit_message_text("همه چیز به حالت اولیه برگشت.")
        return

    if action == "settings_cancel":
        await query.edit_message_text("لغو شد.")
        return


def self_check() -> None:
    counts = Counter(word["primary_level"] for word in WORDS)
    missing_examples = [word["word"] for word in WORDS if len((word.get("example") or "").split()) < 3]
    words_without_persian_meaning = [
        word["word"]
        for word in WORDS
        if not has_persian_meaning(word.get("meaning_fa", ""))
    ]
    examples_without_translation = [
        word["word"]
        for word in WORDS
        if not example_has_persian_translation(word.get("example") or "")
    ]
    print(f"Loaded {len(WORDS)} Oxford entries from {WORDS_PATH}")
    for level in LEVELS:
        print(f"{level}: {counts[level]}")
    print(f"Trainable words: {len(TRAINABLE_WORDS)}")
    print(f"Words without Persian meaning: {len(words_without_persian_meaning)}")
    print(f"Examples shorter than 3 words: {len(missing_examples)}")
    print(f"Examples without Persian translation: {len(examples_without_translation)}")


def build_application(token: str) -> Application:
    if Application is None:
        raise SystemExit("Install dependencies first: pip install -r requirements.txt")

    builder = Application.builder().token(token)
    proxy_url = os.getenv("TELEGRAM_PROXY_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if proxy_url:
        logger.info("Using proxy for Telegram requests.")
        builder = builder.proxy_url(proxy_url).get_updates_proxy_url(proxy_url)

    application = builder.build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("study", study))
    application.add_handler(CommandHandler("placement", placement))
    application.add_handler(CommandHandler("lists", lists))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram vocabulary bot for Oxford word lists.")
    parser.add_argument("--check", action="store_true", help="Validate local vocabulary data and exit.")
    return parser.parse_args()


def load_env(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def is_placeholder_token(token: str) -> bool:
    return "replace_with_your_telegram_bot_token" in token or token.startswith("123456789:")


def main() -> None:
    args = parse_args()
    if args.check:
        self_check()
        return

    load_env()
    token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set BOT_TOKEN or TELEGRAM_BOT_TOKEN before running the bot.")
    if is_placeholder_token(token):
        raise SystemExit("Put your real BotFather token in .env instead of the example BOT_TOKEN.")

    logger.info("Starting Telegram bot with %s vocabulary entries.", len(WORDS))
    build_application(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
