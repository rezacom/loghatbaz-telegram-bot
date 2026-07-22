from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
WORDS_PATH = BASE_DIR / "data" / "words.jsonl"
DB_PATH = BASE_DIR / "bot.db"
ENV_PATH = BASE_DIR / ".env"

LEVELS = ("A1", "A2", "B1", "B2", "C1")
LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}

LEARNED_STREAK_TARGET = 7
PAGE_SIZE = 8
LEARNING_POOL_TARGET = 10

DAILY_REMINDER_INTERVAL_SECONDS = 60
DAILY_REMINDER_TEXT = "سلام امروز تمرین یادت رفته لغت باز منتظرته"

LOWER_LEVEL_REVIEW_CHANCE = 0.12
LOWER_LEVEL_WRONG_LIMIT = 5

PLACEMENT_LEVELS = ("A2", "B1", "B2", "C1")
PLACEMENT_QUESTIONS_PER_LEVEL = 5
PLACEMENT_TIMEOUT_SECONDS = 10
TEMP_RESULT_SECONDS = 3
CANCEL_PLACEMENT_TEXT = "لغو آزمون"
RANDOM_CHAT_SEARCH_SECONDS = 60
RANDOM_CHAT_BUTTON_TEXT = "مکالمه انگلیسی با کاربر رندوم"
RANDOM_CHAT_CANCEL_TEXT = "لغو مکالمه"
