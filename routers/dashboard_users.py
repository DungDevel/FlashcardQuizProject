"""
routers/dashboard_users.py
"""

import os
import json
import httpx
from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timedelta

from database import get_connection
from auth_utils import get_current_user

router = APIRouter(tags=["Dashboard"])

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.1-8b-instant"


def _collect_user_learning_data(user_id: int) -> dict:
    conn = get_connection()
    try:
        cur   = conn.cursor()
        today = datetime.now().date()

        # ── Profile ──
        cur.execute(
            "SELECT study_level, study_days FROM user_profile WHERE user_id = %s",
            (user_id,),
        )
        profile = cur.fetchone()

        # ── Flashcard: 30 ngày gần nhất ──
        cur.execute(
            """
            SELECT DATE(last_reviewed) AS d,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done
            FROM user_flashcard_progress
            WHERE user_id = %s AND last_reviewed >= NOW() - INTERVAL '30 days'
            GROUP BY DATE(last_reviewed)
            ORDER BY d ASC
            """,
            (user_id,),
        )
        fc_daily = [{"date": str(r[0]), "total": r[1], "done": r[2]} for r in cur.fetchall()]

        # ── Flashcard: tổng theo trạng thái ──
        cur.execute(
            """
            SELECT status, COUNT(*)
            FROM user_flashcard_progress
            WHERE user_id = %s GROUP BY status
            """,
            (user_id,),
        )
        fc_status = {r[0]: r[1] for r in cur.fetchall()}

        # ── Flashcard SRS (dùng cột mới sau migration) ──
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE interval_days = 0)              AS due_today,
                COUNT(*) FILTER (WHERE interval_days BETWEEN 1 AND 3)  AS short_term,
                COUNT(*) FILTER (WHERE interval_days BETWEEN 4 AND 14) AS mid_term,
                COUNT(*) FILTER (WHERE interval_days > 14)             AS long_term,
                ROUND(AVG(ease_factor)::numeric, 2)                    AS avg_ease,
                ROUND(AVG(interval_days)::numeric, 1)                  AS avg_interval
            FROM user_flashcard_progress
            WHERE user_id = %s
            """,
            (user_id,),
        )
        fc_srs = cur.fetchone()

        # ── Flashcard: cards due hôm nay ──
        cur.execute(
            """
            SELECT COUNT(*) FROM user_flashcard_progress
            WHERE user_id = %s AND next_review_date <= NOW()
            """,
            (user_id,),
        )
        fc_due_now = cur.fetchone()[0]

        # ── Quiz: 30 ngày gần nhất
        # Dùng last_review_date (tên cột thực tế trong DB của bạn)
        cur.execute(
            """
            SELECT DATE(last_review_date) AS d,
                   COUNT(*) AS total,
                   SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) AS correct
            FROM user_quiz_progress
            WHERE user_id = %s
              AND last_review_date >= NOW() - INTERVAL '30 days'
            GROUP BY DATE(last_review_date)
            ORDER BY d ASC
            """,
            (user_id,),
        )
        quiz_daily = [
            {
                "date":     str(r[0]),
                "total":    r[1],
                "correct":  r[2],
                "accuracy": round(r[2] / r[1] * 100, 1) if r[1] else 0,
            }
            for r in cur.fetchall()
        ]

        # ── Quiz: accuracy theo quiz_type ──
        cur.execute(
            """
            SELECT quiz_type,
                   COUNT(*) AS total,
                   SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) AS correct
            FROM user_quiz_progress
            WHERE user_id = %s
            GROUP BY quiz_type
            """,
            (user_id,),
        )
        quiz_by_type = [
            {
                "type":     r[0],
                "total":    r[1],
                "correct":  r[2],
                "accuracy": round(r[2] / r[1] * 100, 1) if r[1] else 0,
            }
            for r in cur.fetchall()
        ]

        # ── Quiz SRS (ease_factor, interval_days, next_review_date có sẵn trong DB) ──
        cur.execute(
            """
            SELECT
                ROUND(AVG(ease_factor)::numeric, 2)    AS avg_ease,
                ROUND(AVG(interval_days)::numeric, 1)  AS avg_interval,
                COUNT(*) FILTER (WHERE next_review_date <= NOW()) AS due_now,
                COUNT(*) FILTER (WHERE interval_days = 0)              AS short_0,
                COUNT(*) FILTER (WHERE interval_days BETWEEN 1 AND 3)  AS short_term,
                COUNT(*) FILTER (WHERE interval_days BETWEEN 4 AND 14) AS mid_term,
                COUNT(*) FILTER (WHERE interval_days > 14)             AS long_term
            FROM user_quiz_progress
            WHERE user_id = %s
            """,
            (user_id,),
        )
        quiz_srs = cur.fetchone()

        # ── Planner: task completion 4 tuần ──
        cur.execute(
            """
            SELECT pd.study_date,
                   COUNT(*) AS total,
                   SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS done
            FROM task t
            JOIN planner_day pd ON t.planner_day_id = pd.id
            JOIN planner p      ON pd.planner_id = p.id
            WHERE p.user_id = %s
              AND pd.study_date >= NOW() - INTERVAL '28 days'
            GROUP BY pd.study_date
            ORDER BY pd.study_date ASC
            """,
            (user_id,),
        )
        task_daily = [
            {
                "date":  str(r[0]),
                "total": r[1],
                "done":  r[2],
                "rate":  round(r[2] / r[1] * 100, 1) if r[1] else 0,
            }
            for r in cur.fetchall()
        ]

        # ── Streak ──
        cur.execute(
            """
            SELECT DISTINCT DATE(last_reviewed)
            FROM user_flashcard_progress
            WHERE user_id = %s AND status = 'done'
            ORDER BY DATE(last_reviewed) DESC
            """,
            (user_id,),
        )
        active_dates = sorted({r[0] for r in cur.fetchall()})

        current_streak = 0
        if active_dates:
            check = today
            for d in reversed(active_dates):
                if d == check or d == check - timedelta(days=1):
                    current_streak += 1
                    check = d
                else:
                    break

        # ── Active decks ──
        cur.execute(
            "SELECT COUNT(DISTINCT deck_id) FROM user_flashcard_progress WHERE user_id = %s",
            (user_id,),
        )
        active_decks = cur.fetchone()[0]

        cur.close()
    finally:
        conn.close()

    return {
        "profile": {
            "study_level": profile[0] if profile else "unknown",
            "study_days":  profile[1] if profile else 0,
        },
        "streak": {
            "current_streak":    current_streak,
            "total_active_days": len(active_dates),
        },
        "active_decks": active_decks,
        "flashcard": {
            "status_breakdown": fc_status,
            "due_now": fc_due_now,
            "srs": {
                "due_today":         int(fc_srs[0] or 0),
                "short_term":        int(fc_srs[1] or 0),
                "mid_term":          int(fc_srs[2] or 0),
                "long_term":         int(fc_srs[3] or 0),
                "avg_ease":          float(fc_srs[4] or 2.5),
                "avg_interval_days": float(fc_srs[5] or 0),
            },
            "daily_30d": fc_daily,
        },
        "quiz": {
            "by_type": quiz_by_type,
            "srs": {
                "avg_ease":          float(quiz_srs[0] or 2.5),
                "avg_interval_days": float(quiz_srs[1] or 0),
                "due_now":           int(quiz_srs[2] or 0),
                "short_term":        int(quiz_srs[4] or 0),
                "mid_term":          int(quiz_srs[5] or 0),
                "long_term":         int(quiz_srs[6] or 0),
            },
            "daily_30d": quiz_daily,
        },
        "planner": {
            "task_daily_28d": task_daily,
        },
        "today": str(today),
    }


@router.get("/dashboard/overview")
def get_dashboard_overview(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT COUNT(*) FROM user_flashcard_progress WHERE user_id = %s AND status = 'done'",
            (user_id,),
        )
        total_flashcards_done = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(DISTINCT deck_id) FROM user_flashcard_progress WHERE user_id = %s",
            (user_id,),
        )
        active_decks = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN is_correct THEN 1 ELSE 0 END)
            FROM user_quiz_progress WHERE user_id = %s
            """,
            (user_id,),
        )
        quiz_row        = cur.fetchone()
        total_quizzes   = quiz_row[0] or 0
        correct_quizzes = quiz_row[1] or 0

        cur.execute(
            """
            SELECT COUNT(*) FROM user_flashcard_progress
            WHERE user_id = %s AND next_review_date <= NOW()
            """,
            (user_id,),
        )
        fc_due_today = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*) FROM user_quiz_progress
            WHERE user_id = %s AND next_review_date <= NOW()
            """,
            (user_id,),
        )
        quiz_due_today = cur.fetchone()[0]

        today = datetime.now().date()
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE t.status = 'completed'), COUNT(*)
            FROM task t
            JOIN planner_day pd ON t.planner_day_id = pd.id
            JOIN planner p      ON pd.planner_id = p.id
            WHERE p.user_id = %s AND pd.study_date = %s
            """,
            (user_id, today),
        )
        task_row          = cur.fetchone()
        tasks_done_today  = task_row[0] or 0
        tasks_total_today = task_row[1] or 0

        cur.execute(
            "SELECT study_level, study_days FROM user_profile WHERE user_id = %s",
            (user_id,),
        )
        profile = cur.fetchone()
        cur.close()

        return {
            "user_id":              user_id,
            "study_level":          profile[0] if profile else None,
            "study_days":           profile[1] if profile else None,
            "active_decks":         active_decks,
            "flashcards_mastered":  total_flashcards_done,
            "flashcards_due_today": fc_due_today,
            "quizzes_completed":    total_quizzes,
            "quizzes_due_today":    quiz_due_today,
            "quiz_accuracy":        round(correct_quizzes / total_quizzes * 100, 1) if total_quizzes else 0,
            "today": {
                "tasks_done":  tasks_done_today,
                "tasks_total": tasks_total_today,
                "completion":  round(tasks_done_today / tasks_total_today * 100, 1) if tasks_total_today else 0,
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/dashboard/weekly")
def get_weekly_stats(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    today   = datetime.now().date()
    monday  = today - timedelta(days=today.weekday())

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT DATE(last_reviewed), COUNT(*)
            FROM user_flashcard_progress
            WHERE user_id = %s AND status = 'done' AND last_reviewed >= %s
            GROUP BY DATE(last_reviewed)
            ORDER BY DATE(last_reviewed) ASC
            """,
            (user_id, monday),
        )
        fc_rows = {str(r[0]): r[1] for r in cur.fetchall()}

        # Dùng last_review_date (tên thực tế trong user_quiz_progress)
        cur.execute(
            """
            SELECT DATE(last_review_date), COUNT(*)
            FROM user_quiz_progress
            WHERE user_id = %s AND last_review_date >= %s
            GROUP BY DATE(last_review_date)
            ORDER BY DATE(last_review_date) ASC
            """,
            (user_id, monday),
        )
        quiz_rows = {str(r[0]): r[1] for r in cur.fetchall()}
        cur.close()
    finally:
        conn.close()

    days = []
    for i in range(7):
        d = str(monday + timedelta(days=i))
        days.append({
            "date":            d,
            "flashcards_done": fc_rows.get(d, 0),
            "quizzes_done":    quiz_rows.get(d, 0),
        })

    return {"week_start": str(monday), "days": days}


@router.get("/dashboard/streaks")
def get_study_streaks(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT DATE(last_reviewed)
            FROM user_flashcard_progress
            WHERE user_id = %s AND status = 'done'
            ORDER BY DATE(last_reviewed) DESC
            """,
            (user_id,),
        )
        active_dates = [r[0] for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()

    if not active_dates:
        return {"current_streak": 0, "longest_streak": 0, "total_active_days": 0}

    today = datetime.now().date()

    current_streak = 0
    check_date     = today
    for d in active_dates:
        if d == check_date or d == check_date - timedelta(days=1):
            current_streak += 1
            check_date = d
        else:
            break

    sorted_dates   = sorted(set(active_dates))
    longest_streak = 1
    temp_streak    = 1
    for i in range(1, len(sorted_dates)):
        if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
            temp_streak    += 1
            longest_streak  = max(longest_streak, temp_streak)
        else:
            temp_streak = 1

    return {
        "current_streak":    current_streak,
        "longest_streak":    longest_streak,
        "total_active_days": len(set(active_dates)),
    }


@router.get("/dashboard/ai-prediction")
async def get_ai_prediction(current_user: dict = Depends(get_current_user)):
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY chưa được cấu hình!")

    user_id = current_user["id"]

    try:
        learning_data = _collect_user_learning_data(user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi thu thập dữ liệu: {e}")

    has_flashcard_data = bool(learning_data["flashcard"]["daily_30d"])
    has_quiz_data      = bool(learning_data["quiz"]["daily_30d"])
    if not has_flashcard_data and not has_quiz_data:
        return {
            "generated_at":  datetime.now().isoformat(),
            "data_snapshot": learning_data,
            "prediction":    None,
            "message":       "Chưa có đủ dữ liệu học tập để dự đoán. Hãy học thêm vài ngày nhé!",
        }

    prompt = f"""
You are an expert educational data analyst. Analyze this learner's real study data and predict their learning outcomes for the next 7 days and 30 days.

Today: {learning_data["today"]}

=== LEARNER DATA ===
{json.dumps(learning_data, ensure_ascii=False, indent=2)}

=== INSTRUCTIONS ===
Based on the data above, produce a JSON prediction. Use ONLY the data provided.

Rules:
- "flashcards_expected": estimated flashcards reviewed in the period, based on daily_30d trend
- "quiz_accuracy_expected": estimated accuracy % based on recent quiz trend
- "tasks_completion_expected": estimated task completion rate % based on planner history
- "mastery_rate_expected" (monthly only): % of cards likely to reach long_term SRS interval (>14 days)
- "highlights": 2-3 positive trends (cite actual numbers)
- "risks": 2-3 risks or weak spots (be specific)
- "recommendations": 3-5 actionable tips tailored to this learner's SRS data
- "confidence": "low" if <7 active days, "medium" if 7-20 days, "high" if >20 days
- "summary": 2-3 sentence assessment in Vietnamese

Output ONLY valid JSON, no extra text, no markdown:
{{
  "summary": "...",
  "weekly": {{
    "flashcards_expected": 0,
    "quiz_accuracy_expected": 0.0,
    "tasks_completion_expected": 0.0,
    "highlights": [],
    "risks": []
  }},
  "monthly": {{
    "flashcards_expected": 0,
    "quiz_accuracy_expected": 0.0,
    "mastery_rate_expected": 0.0,
    "highlights": [],
    "risks": []
  }},
  "recommendations": [],
  "confidence": "medium"
}}
"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":       GROQ_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens":  1200,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(GROQ_API_URL, headers=headers, json=payload)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Groq API lỗi: {resp.text}")

    ai_text = resp.json()["choices"][0]["message"]["content"].strip()

    prediction  = None
    parse_error = None
    try:
        prediction = json.loads(ai_text)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\{.*\}", ai_text, re.S)
        if match:
            try:
                prediction = json.loads(match.group(0))
            except json.JSONDecodeError as e:
                parse_error = str(e)
        else:
            parse_error = "Không tìm thấy JSON trong response của AI"

    if prediction is None:
        raise HTTPException(
            status_code=500,
            detail=f"AI trả về dữ liệu không hợp lệ: {parse_error}. Raw: {ai_text[:300]}",
        )

    return {
        "generated_at":  datetime.now().isoformat(),
        "data_snapshot": learning_data,
        "prediction":    prediction,
    }