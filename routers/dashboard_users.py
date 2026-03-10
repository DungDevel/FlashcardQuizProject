"""
routers/dashboard_users.py — Dashboard thống kê cá nhân
Endpoints:
  GET /dashboard/overview     — Tổng quan học tập
  GET /dashboard/streaks      — Chuỗi ngày học liên tiếp
  GET /dashboard/weekly       — Thống kê tuần này
"""

from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timedelta

from database import get_connection
from auth_utils import get_current_user

router = APIRouter(tags=["Dashboard"])


@router.get("/dashboard/overview")
def get_dashboard_overview(current_user: dict = Depends(get_current_user)):
    """Tổng quan học tập: tổng flashcard, quiz, progress."""
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()

        # Tổng flashcard đã học
        cur.execute(
            "SELECT COUNT(*) FROM user_flashcard_progress WHERE user_id = %s AND status = 'done'",
            (user_id,),
        )
        total_flashcards_done = cur.fetchone()[0]

        # Tổng số deck đang học
        cur.execute(
            "SELECT COUNT(DISTINCT deck_id) FROM user_flashcard_progress WHERE user_id = %s",
            (user_id,),
        )
        active_decks = cur.fetchone()[0]

        # Tổng quiz đã làm
        cur.execute(
            "SELECT COUNT(*), SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) FROM user_quiz_progress WHERE user_id = %s",
            (user_id,),
        )
        quiz_row = cur.fetchone()
        total_quizzes  = quiz_row[0] or 0
        correct_quizzes = quiz_row[1] or 0

        # Task hôm nay
        today  = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE t.status = 'completed'),
                   COUNT(*)
            FROM task t
            JOIN planner_day pd ON t.planner_day_id = pd.id
            JOIN planner p ON pd.planner_id = p.id
            WHERE p.user_id = %s AND pd.study_date = %s
            """,
            (user_id, today),
        )
        task_row            = cur.fetchone()
        tasks_done_today    = task_row[0] or 0
        tasks_total_today   = task_row[1] or 0

        # Profile
        cur.execute("SELECT study_level, study_days FROM user_profile WHERE user_id = %s", (user_id,))
        profile = cur.fetchone()
        cur.close()

        return {
            "user_id": user_id,
            "study_level":        profile[0] if profile else None,
            "study_days":         profile[1] if profile else None,
            "active_decks":       active_decks,
            "flashcards_mastered": total_flashcards_done,
            "quizzes_completed":  total_quizzes,
            "quiz_accuracy":      round(correct_quizzes / total_quizzes * 100, 1) if total_quizzes else 0,
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
    """Thống kê 7 ngày gần nhất."""
    user_id = current_user["id"]
    today   = datetime.now().date()
    monday  = today - timedelta(days=today.weekday())

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Flashcard done theo từng ngày trong tuần
        cur.execute(
            """
            SELECT DATE(last_reviewed), COUNT(*)
            FROM user_flashcard_progress
            WHERE user_id = %s AND status = 'done'
              AND last_reviewed >= %s
            GROUP BY DATE(last_reviewed)
            ORDER BY DATE(last_reviewed) ASC
            """,
            (user_id, monday),
        )
        fc_rows = {str(r[0]): r[1] for r in cur.fetchall()}

        # Quiz done theo từng ngày
        cur.execute(
            """
            SELECT DATE(updated_at), COUNT(*)
            FROM user_quiz_progress
            WHERE user_id = %s AND status = 'completed'
              AND updated_at >= %s
            GROUP BY DATE(updated_at)
            ORDER BY DATE(updated_at) ASC
            """,
            (user_id, monday),
        )
        quiz_rows = {str(r[0]): r[1] for r in cur.fetchall()}

        cur.close()
    finally:
        conn.close()

    # Build response cho 7 ngày
    days = []
    for i in range(7):
        d = str(monday + timedelta(days=i))
        days.append({
            "date":             d,
            "flashcards_done":  fc_rows.get(d, 0),
            "quizzes_done":     quiz_rows.get(d, 0),
        })

    return {"week_start": str(monday), "days": days}


@router.get("/dashboard/streaks")
def get_study_streaks(current_user: dict = Depends(get_current_user)):
    """Chuỗi ngày học liên tiếp (streak)."""
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

    # Current streak
    current_streak = 0
    check_date = today
    for d in active_dates:
        if d == check_date or d == check_date - timedelta(days=1):
            current_streak += 1
            check_date = d
        else:
            break

    # Longest streak
    sorted_dates   = sorted(set(active_dates))
    longest_streak = 1
    temp_streak    = 1
    for i in range(1, len(sorted_dates)):
        if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
            temp_streak += 1
            longest_streak = max(longest_streak, temp_streak)
        else:
            temp_streak = 1

    return {
        "current_streak":   current_streak,
        "longest_streak":   longest_streak,
        "total_active_days": len(set(active_dates)),
    }