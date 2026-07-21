from __future__ import annotations

import json
import re
from pathlib import Path

from config import LEVELS, WORDS_PATH


PERSIAN_TEXT_RE = re.compile(r"[\u0600-\u06ff]")


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


def make_example(word: str) -> str:
    clean_word = word.split("(")[0].strip()
    return f"I learned the word {clean_word} today. / من امروز کلمه «{clean_word}» را یاد گرفتم."


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
