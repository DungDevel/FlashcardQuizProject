"""
routers/dashboard_users.py
"""

import os
import json

from fastapi import (
    APIRouter,
    Depends,
    BackgroundTasks
)

from datetime import (
    datetime,
    timedelta
)

from database import get_connection
from auth_utils import get_current_user

from services.ai_prediction_service import (
    generate_and_store_prediction
)

router = APIRouter(tags=["Dashboard"])

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")


# =========================================================
# COLLECT LEARNING DATA
# =========================================================

def _collect_user_learning_data(user_id: int):

    conn = get_connection()

    try:

        cur = conn.cursor()

        today = datetime.now().date()

        # =================================================
        # PROFILE
        # =================================================

        cur.execute("""
            SELECT study_level, study_days
            FROM user_profile
            WHERE user_id = %s
        """, (user_id,))

        profile = cur.fetchone()

        # =================================================
        # FLASHCARD DAILY
        # =================================================

        cur.execute("""
            SELECT
                DATE(last_reviewed) AS d,
                COUNT(*) AS total,
                SUM(
                    CASE
                        WHEN status = 'done'
                        THEN 1
                        ELSE 0
                    END
                ) AS done

            FROM user_flashcard_progress

            WHERE user_id = %s
            AND last_reviewed >= NOW() - INTERVAL '30 days'

            GROUP BY DATE(last_reviewed)

            ORDER BY d ASC
        """, (user_id,))

        fc_daily = [
            {
                "date": str(r[0]),
                "total": int(r[1] or 0),
                "done": int(r[2] or 0)
            }
            for r in cur.fetchall()
        ]

        # =================================================
        # FLASHCARD STATUS
        # =================================================

        cur.execute("""
            SELECT status, COUNT(*)

            FROM user_flashcard_progress

            WHERE user_id = %s

            GROUP BY status
        """, (user_id,))

        fc_status = {
            r[0]: int(r[1] or 0)
            for r in cur.fetchall()
        }

        # =================================================
        # FLASHCARD SRS
        # =================================================

        cur.execute("""
            SELECT
                COUNT(*) FILTER (
                    WHERE interval_days = 0
                ) AS due_today,

                COUNT(*) FILTER (
                    WHERE interval_days BETWEEN 1 AND 3
                ) AS short_term,

                COUNT(*) FILTER (
                    WHERE interval_days BETWEEN 4 AND 14
                ) AS mid_term,

                COUNT(*) FILTER (
                    WHERE interval_days > 14
                ) AS long_term,

                ROUND(AVG(ease_factor)::numeric, 2),

                ROUND(AVG(interval_days)::numeric, 1)

            FROM user_flashcard_progress

            WHERE user_id = %s
        """, (user_id,))

        fc_srs = cur.fetchone()

        # =================================================
        # FLASHCARD DUE NOW
        # =================================================

        cur.execute("""
            SELECT COUNT(*)

            FROM user_flashcard_progress

            WHERE user_id = %s
            AND next_review_date <= NOW()
        """, (user_id,))

        fc_due_now = int(cur.fetchone()[0] or 0)

        # =================================================
        # QUIZ DAILY
        # =================================================

        cur.execute("""
            SELECT
                DATE(last_review_date) AS d,
                COUNT(*) AS total,

                SUM(
                    CASE
                        WHEN is_correct
                        THEN 1
                        ELSE 0
                    END
                ) AS correct

            FROM user_quiz_progress

            WHERE user_id = %s
            AND last_review_date >= NOW() - INTERVAL '30 days'

            GROUP BY DATE(last_review_date)

            ORDER BY d ASC
        """, (user_id,))

        quiz_daily = []

        for r in cur.fetchall():

            total = int(r[1] or 0)
            correct = int(r[2] or 0)

            quiz_daily.append({
                "date": str(r[0]),
                "total": total,
                "correct": correct,
                "accuracy": round(
                    (correct / total) * 100,
                    1
                ) if total else 0
            })

        # =================================================
        # QUIZ TYPE
        # =================================================

        cur.execute("""
            SELECT
                quiz_type,
                COUNT(*) AS total,

                SUM(
                    CASE
                        WHEN is_correct
                        THEN 1
                        ELSE 0
                    END
                ) AS correct

            FROM user_quiz_progress

            WHERE user_id = %s

            GROUP BY quiz_type
        """, (user_id,))

        quiz_by_type = []

        for r in cur.fetchall():

            total = int(r[1] or 0)
            correct = int(r[2] or 0)

            quiz_by_type.append({
                "type": r[0],
                "total": total,
                "correct": correct,
                "accuracy": round(
                    (correct / total) * 100,
                    1
                ) if total else 0
            })

        # =================================================
        # QUIZ SRS
        # =================================================

        cur.execute("""
            SELECT
                ROUND(AVG(ease_factor)::numeric, 2),

                ROUND(AVG(interval_days)::numeric, 1),

                COUNT(*) FILTER (
                    WHERE next_review_date <= NOW()
                ),

                COUNT(*) FILTER (
                    WHERE interval_days BETWEEN 1 AND 3
                ),

                COUNT(*) FILTER (
                    WHERE interval_days BETWEEN 4 AND 14
                ),

                COUNT(*) FILTER (
                    WHERE interval_days > 14
                )

            FROM user_quiz_progress

            WHERE user_id = %s
        """, (user_id,))

        quiz_srs = cur.fetchone()

        # =================================================
        # TASK DAILY
        # =================================================

        cur.execute("""
            SELECT
                pd.study_date,

                COUNT(*) AS total,

                SUM(
                    CASE
                        WHEN t.status = 'completed'
                        THEN 1
                        ELSE 0
                    END
                ) AS done

            FROM task t

            JOIN planner_day pd
            ON t.planner_day_id = pd.id

            JOIN planner p
            ON pd.planner_id = p.id

            WHERE p.user_id = %s
            AND pd.study_date >= NOW() - INTERVAL '28 days'

            GROUP BY pd.study_date

            ORDER BY pd.study_date ASC
        """, (user_id,))

        task_daily = []

        for r in cur.fetchall():

            total = int(r[1] or 0)
            done = int(r[2] or 0)

            task_daily.append({
                "date": str(r[0]),
                "total": total,
                "done": done,
                "rate": round(
                    (done / total) * 100,
                    1
                ) if total else 0
            })

        # =================================================
        # STREAK
        # =================================================

        cur.execute("""
            SELECT DISTINCT DATE(last_reviewed)

            FROM user_flashcard_progress

            WHERE user_id = %s
            AND status = 'done'

            ORDER BY DATE(last_reviewed) DESC
        """, (user_id,))

        active_dates = [
            r[0]
            for r in cur.fetchall()
        ]

        current_streak = 0

        if active_dates:

            check_date = today

            for d in active_dates:

                if (
                    d == check_date or
                    d == check_date - timedelta(days=1)
                ):
                    current_streak += 1
                    check_date = d

                else:
                    break

        # =================================================
        # ACTIVE DECKS
        # =================================================

        cur.execute("""
            SELECT COUNT(DISTINCT deck_id)

            FROM user_flashcard_progress

            WHERE user_id = %s
        """, (user_id,))

        active_decks = int(cur.fetchone()[0] or 0)

        cur.close()

    finally:

        conn.close()

    return {
        "profile": {
            "study_level":
                profile[0] if profile else "unknown",

            "study_days":
                profile[1] if profile else 0,
        },

        "streak": {
            "current_streak": current_streak,
            "total_active_days": len(active_dates),
        },

        "active_decks": active_decks,

        "flashcard": {
            "status_breakdown": fc_status,

            "due_now": fc_due_now,

            "srs": {
                "due_today":
                    int(fc_srs[0] or 0),

                "short_term":
                    int(fc_srs[1] or 0),

                "mid_term":
                    int(fc_srs[2] or 0),

                "long_term":
                    int(fc_srs[3] or 0),

                "avg_ease":
                    float(fc_srs[4] or 2.5),

                "avg_interval_days":
                    float(fc_srs[5] or 0),
            },

            "daily_30d": fc_daily,
        },

        "quiz": {
            "by_type": quiz_by_type,

            "srs": {
                "avg_ease":
                    float(quiz_srs[0] or 2.5),

                "avg_interval_days":
                    float(quiz_srs[1] or 0),

                "due_now":
                    int(quiz_srs[2] or 0),

                "short_term":
                    int(quiz_srs[3] or 0),

                "mid_term":
                    int(quiz_srs[4] or 0),

                "long_term":
                    int(quiz_srs[5] or 0),
            },

            "daily_30d": quiz_daily,
        },

        "planner": {
            "task_daily_28d": task_daily,
        },

        "today": str(today),
    }


# =========================================================
# OVERVIEW
# =========================================================

@router.get("/dashboard/overview")
def get_dashboard_overview(
    current_user: dict = Depends(get_current_user)
):

    user_id = current_user["id"]

    data = _collect_user_learning_data(user_id)

    total_quiz = sum(
        q["total"]
        for q in data["quiz"]["by_type"]
    )

    total_correct = sum(
        q["correct"]
        for q in data["quiz"]["by_type"]
    )

    latest_task = (
        data["planner"]["task_daily_28d"][-1]
        if data["planner"]["task_daily_28d"]
        else {
            "done": 0,
            "total": 0,
            "rate": 0
        }
    )

    return {
        "user_id": user_id,

        "study_level":
            data["profile"]["study_level"],

        "study_days":
            data["profile"]["study_days"],

        "active_decks":
            data["active_decks"],

        "flashcards_mastered":
            data["flashcard"]["status_breakdown"]
            .get("done", 0),

        "flashcards_due_today":
            data["flashcard"]["due_now"],

        "quizzes_completed":
            total_quiz,

        "quiz_accuracy":
            round(
                (total_correct / total_quiz) * 100,
                1
            ) if total_quiz else 0,

        "today": {
            "tasks_done":
                latest_task["done"],

            "tasks_total":
                latest_task["total"],

            "completion":
                latest_task["rate"]
        }
    }


# =========================================================
# WEEKLY
# =========================================================

@router.get("/dashboard/weekly")
def get_weekly_stats(
    current_user: dict = Depends(get_current_user)
):

    user_id = current_user["id"]

    today = datetime.now().date()

    monday = today - timedelta(
        days=today.weekday()
    )

    conn = get_connection()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT
                DATE(last_reviewed),
                COUNT(*)

            FROM user_flashcard_progress

            WHERE user_id = %s
            AND status = 'done'
            AND last_reviewed >= %s

            GROUP BY DATE(last_reviewed)
        """, (user_id, monday))

        fc_rows = {
            str(r[0]): int(r[1] or 0)
            for r in cur.fetchall()
        }

        cur.execute("""
            SELECT
                DATE(last_review_date),
                COUNT(*)

            FROM user_quiz_progress

            WHERE user_id = %s
            AND last_review_date >= %s

            GROUP BY DATE(last_review_date)
        """, (user_id, monday))

        quiz_rows = {
            str(r[0]): int(r[1] or 0)
            for r in cur.fetchall()
        }

        days = []

        for i in range(7):

            d = monday + timedelta(days=i)

            key = str(d)

            days.append({
                "date": d.strftime("%a"),

                "flashcards_done":
                    fc_rows.get(key, 0),

                "quizzes_done":
                    quiz_rows.get(key, 0),
            })

        return {
            "week_start": str(monday),
            "days": days
        }

    finally:

        conn.close()


# =========================================================
# STREAKS
# =========================================================

@router.get("/dashboard/streaks")
def get_study_streaks(
    current_user: dict = Depends(get_current_user)
):

    user_id = current_user["id"]

    data = _collect_user_learning_data(user_id)

    active_days = data["streak"]["total_active_days"]

    return {
        "current_streak":
            data["streak"]["current_streak"],

        "longest_streak":
            active_days,

        "total_active_days":
            active_days
    }


# =========================================================
# AI PREDICTION
# =========================================================

@router.get("/dashboard/ai-prediction")
async def get_ai_prediction(
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):

    user_id = current_user["id"]

    if not GROQ_API_KEY:

        return {
            "generated_at":
                datetime.now().isoformat(),

            "prediction": None,

            "message":
                "AI service unavailable"
        }

    conn = get_connection()

    try:

        cur = conn.cursor()

        # =================================================
        # CHECK CACHE
        # =================================================

        cur.execute("""
            SELECT
                prediction,
                generated_at

            FROM ai_predictions

            WHERE user_id = %s
        """, (user_id,))

        cache_row = cur.fetchone()

        # =================================================
        # CACHE EXISTS
        # =================================================

        if cache_row:

            cached_prediction = cache_row[0]

            # Nếu PostgreSQL trả về string JSON
            if isinstance(cached_prediction, str):
                try:
                    cached_prediction = json.loads(cached_prediction)
                except Exception:
                    cached_prediction = None

            generated_at = cache_row[1]

            age_seconds = (
                datetime.now() - generated_at
            ).total_seconds()

            # AUTO REFRESH 6 HOURS

            if age_seconds > 21600:

                try:

                    learning_data = (
                        _collect_user_learning_data(
                            user_id
                        )
                    )

                    background_tasks.add_task(
                        generate_and_store_prediction,
                        user_id,
                        learning_data
                    )

                except Exception as e:
                    print("Refresh error:", e)

            if cached_prediction:

                return {
                    "generated_at": generated_at.isoformat(),
                    "prediction": cached_prediction
                }

            # Nếu cache lỗi -> regenerate
            learning_data = _collect_user_learning_data(user_id)

            background_tasks.add_task(
                generate_and_store_prediction,
                user_id,
                learning_data
            )

            return {
                "generated_at": datetime.now().isoformat(),
                "prediction": None,
                "message": "Regenerating AI prediction..."
            }

        # =================================================
        # NO CACHE
        # =================================================

        learning_data = _collect_user_learning_data(
            user_id
        )

        has_flashcard = bool(
            learning_data["flashcard"]["daily_30d"]
        )

        has_quiz = bool(
            learning_data["quiz"]["daily_30d"]
        )

        # =================================================
        # NOT ENOUGH DATA
        # =================================================

        if not has_flashcard and not has_quiz:

            return {
                "generated_at":
                    datetime.now().isoformat(),

                "prediction": {
                    "summary":
                        "Chưa có đủ dữ liệu học tập.",

                    "weekly": {
                        "flashcards_expected": 0,
                        "quiz_accuracy_expected": 0,
                        "tasks_completion_expected": 0,
                        "highlights": [],
                        "risks": []
                    },

                    "monthly": {
                        "flashcards_expected": 0,
                        "quiz_accuracy_expected": 0,
                        "mastery_rate_expected": 0,
                        "highlights": [],
                        "risks": []
                    },

                    "recommendations": [
                        "Hãy học thêm để AI phân tích."
                    ],

                    "confidence": "low"
                }
            }

        # =================================================
        # GENERATE BACKGROUND
        # =================================================

        background_tasks.add_task(
            generate_and_store_prediction,
            user_id,
            learning_data
        )

        return {
            "generated_at":
                datetime.now().isoformat(),

            "prediction": None,

            "message":
                "Generating AI insights..."
        }

    finally:

        conn.close()