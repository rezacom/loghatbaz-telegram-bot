from __future__ import annotations

import random

from config import (
    LEARNING_POOL_TARGET,
    LEVEL_RANK,
    LOWER_LEVEL_REVIEW_CHANCE,
)
from vocabulary import TRAINABLE_WORDS, WORDS_BY_ID


def level_allowed(word: dict, min_level: str) -> bool:
    return word["primary_level"] == min_level


def ensure_learning_pool(
    store,
    user_id: int,
    current_level: str,
    hidden: set[int],
    learned: set[int],
    explicit_learning: set[int],
) -> None:
    current_learning = [
        word_id
        for word_id in explicit_learning
        if word_id in WORDS_BY_ID
        and WORDS_BY_ID[word_id]["primary_level"] == current_level
        and word_id not in hidden
        and word_id not in learned
    ]
    missing_count = LEARNING_POOL_TARGET - len(current_learning)
    if missing_count <= 0:
        return

    candidates = [
        word
        for word in TRAINABLE_WORDS
        if word["primary_level"] == current_level
        and word["id"] not in hidden
        and word["id"] not in learned
        and word["id"] not in explicit_learning
    ]
    random.shuffle(candidates)
    store.add_words_to_learning(user_id, [word["id"] for word in candidates[:missing_count]])


def get_candidate_words(store, user_id: int) -> tuple[list[dict], list[dict]]:
    settings = store.ensure_user(user_id)
    current_level = settings["min_level"]
    hidden = store.hidden_ids(user_id)
    learned = store.learned_ids(user_id)
    explicit_learning = set(store.word_ids_by_status(user_id, "learning"))
    ensure_learning_pool(store, user_id, current_level, hidden, learned, explicit_learning)
    explicit_learning = set(store.word_ids_by_status(user_id, "learning"))
    lower_review_words = [
        word
        for word in TRAINABLE_WORDS
        if word["id"] not in hidden
        and (word["id"] in learned or word["id"] in explicit_learning)
        and LEVEL_RANK[word["primary_level"]] < LEVEL_RANK[current_level]
    ]
    learning_words = [
        word
        for word in TRAINABLE_WORDS
        if word["id"] in explicit_learning
        and word["id"] not in hidden
        and word["id"] not in learned
        and LEVEL_RANK[word["primary_level"]] <= LEVEL_RANK[current_level]
    ]
    return learning_words, lower_review_words


def pick_study_word(store, user_id: int) -> dict | None:
    store.advance_if_level_complete(user_id)
    learning_words, lower_review_words = get_candidate_words(store, user_id)
    if lower_review_words and random.random() < LOWER_LEVEL_REVIEW_CHANCE:
        return random.choice(lower_review_words)
    if learning_words:
        return random.choice(learning_words)
    if lower_review_words:
        return random.choice(lower_review_words)
    return None
