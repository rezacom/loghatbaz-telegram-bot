from __future__ import annotations

import random
import re
from functools import lru_cache

from config import CANCEL_PLACEMENT_TEXT, LEARNED_STREAK_TARGET
from vocabulary import TRAINABLE_WORDS, make_example

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
except ModuleNotFoundError:
    InlineKeyboardButton = InlineKeyboardMarkup = ReplyKeyboardMarkup = None


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


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["شروع تمرین", "آزمون تعیین سطح"],
            ["لیست‌ها", "آمار"],
            ["تنظیمات", "راهنما"],
        ],
        resize_keyboard=True,
    )


def placement_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[CANCEL_PLACEMENT_TEXT]], resize_keyboard=True)


def word_answer(entry: dict) -> str:
    if entry.get("meaning_fa"):
        return entry["meaning_fa"]
    return "معنی ثبت نشده"


def format_card(entry: dict, progress=None) -> str:
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
    value = re.sub(r"[،,.!?؟؛:()\[\]{}\"'‌ـ-]+", " ", value)
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


def placement_keyboard(entry: dict, question_no: int | None = None) -> InlineKeyboardMarkup:
    choices = build_options(entry)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    word_answer(choice),
                    callback_data=(
                        f"placement_answer:{entry['id']}:{choice['id']}:{question_no}"
                        if question_no is not None
                        else f"placement_answer:{entry['id']}:{choice['id']}"
                    ),
                )
            ]
            for choice in choices
        ]
    )
