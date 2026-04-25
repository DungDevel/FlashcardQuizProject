"""srs_sm2.py — Thuật toán SM-2"""

from datetime import datetime, timedelta
from dataclasses import dataclass


@dataclass
class SRSResult:
    ease_factor:      float
    interval_days:    int
    repetitions:      int
    next_review_date: datetime


def sm2_update(
    grade: int,
    ease_factor: float = 2.5,
    interval_days: int = 0,
    repetitions: int   = 0,
) -> SRSResult:
    grade = max(0, min(5, grade))

    if grade < 3:
        repetitions   = 0
        interval_days = 1
    else:
        if repetitions == 0:
            interval_days = 1
        elif repetitions == 1:
            interval_days = 6
        else:
            interval_days = round(interval_days * ease_factor)

        repetitions += 1
        ease_factor  = ease_factor + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
        ease_factor  = max(1.3, ease_factor)

    return SRSResult(
        ease_factor      = round(ease_factor, 4),
        interval_days    = interval_days,
        repetitions      = repetitions,
        next_review_date = datetime.now() + timedelta(days=interval_days),
    )


def grade_from_flashcard(is_correct: bool, difficulty: str = "normal") -> int:
    if not is_correct:
        return 1
    return {"easy": 5, "normal": 4, "hard": 3}.get(difficulty, 4)


def grade_from_quiz(is_correct: bool) -> int:
    return 4 if is_correct else 1