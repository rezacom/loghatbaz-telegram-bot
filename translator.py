from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass

from config import DB_PATH


GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TRANSLATION_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class TranslationResult:
    source_language: str
    target_language: str
    translated_text: str
    cached: bool = False


@contextmanager
def connect_cache():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS translation_cache (
                source_text TEXT NOT NULL,
                source_language TEXT NOT NULL,
                target_language TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_text, source_language, target_language)
            )
            """
        )
        yield connection
        connection.commit()
    finally:
        connection.close()


def looks_persian(text: str) -> bool:
    return any("\u0600" <= character <= "\u06ff" for character in text)


def choose_target_language(text: str) -> str:
    return "en" if looks_persian(text) else "fa"


def normalize_source_text(text: str) -> str:
    return " ".join(text.strip().split())


def get_cached_translation(source_text: str, source_language: str, target_language: str) -> str | None:
    with connect_cache() as connection:
        row = connection.execute(
            """
            SELECT translated_text
            FROM translation_cache
            WHERE source_text = ? AND source_language = ? AND target_language = ?
            """,
            (source_text, source_language, target_language),
        ).fetchone()
    return row["translated_text"] if row else None


def save_translation(source_text: str, source_language: str, target_language: str, translated_text: str) -> None:
    with connect_cache() as connection:
        connection.execute(
            """
            INSERT INTO translation_cache (
                source_text, source_language, target_language, translated_text
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_text, source_language, target_language)
            DO UPDATE SET translated_text = excluded.translated_text
            """,
            (source_text, source_language, target_language, translated_text),
        )


def translate_text(text: str, target_language: str | None = None) -> TranslationResult:
    source_text = normalize_source_text(text)
    if not source_text:
        raise ValueError("Text is empty.")

    source_language = "auto"
    target_language = target_language or choose_target_language(source_text)
    cached = get_cached_translation(source_text, source_language, target_language)
    if cached:
        return TranslationResult(source_language, target_language, cached, cached=True)

    query = urllib.parse.urlencode(
        {
            "client": "gtx",
            "sl": source_language,
            "tl": target_language,
            "dt": "t",
            "q": source_text,
        }
    )
    request = urllib.request.Request(
        f"{GOOGLE_TRANSLATE_URL}?{query}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=TRANSLATION_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))

    translated_text = "".join(part[0] for part in payload[0] if part and part[0]).strip()
    if not translated_text:
        raise RuntimeError("Translation response was empty.")

    save_translation(source_text, source_language, target_language, translated_text)
    detected_language = payload[2] if len(payload) > 2 and payload[2] else source_language
    return TranslationResult(detected_language, target_language, translated_text)
