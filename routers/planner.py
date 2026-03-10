"""
routers/planner.py — Planner học tập theo tuần
Endpoints:
  POST  /planner/create        — Tạo planner tuần hiện tại
  GET   /planner/current       — Lấy planner tuần này
  GET   /planner/history       — Lịch sử planner
  GET   /planner/today         — Nhiệm vụ hôm nay
"""

from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timedelta

from database import get_connection
from auth_utils import get_current_user

router = APIRouter(tags=["Planner"])

DAY_MAP = {0: "MON", 1: "TUE", 2: "WED", 3: "THU", 4: "FRI", 5: "SAT", 6: "SUN"}


def generate_tasks_for_level(study_level: str) -> list[tuple]:
    """Trả về list (task_type, title, description, total_required)."""
    counts = {"easy": 5, "medium": 10, "hard": 15}.get(study_level.lower(), 5)
    return [
        ("flashcard", "Flashcards",           f"Ôn {counts} thẻ flashcard", counts),
        ("quiz",      "Multiple Choice Quiz",  f"Làm {counts} câu trắc nghiệm", counts),
        ("quiz",      "True/False Quiz",       f"Làm {counts} câu đúng/sai", counts),
        ("quiz",      "Fill-in-blank Quiz",    f"Làm {counts} câu điền chỗ trống", counts),
    ]


def get_user_profile(user_id: int) -> tuple:
    """Trả về (study_days, study_level). Raise 400 nếu chưa thiết lập."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT study_days, study_level FROM user_profile WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if not row or not row[0]:
        raise HTTPException(
            status_code=400,
            detail="Bạn chưa thiết lập study_days hoặc study_level! Hãy cập nhật profile trước.",
        )
    return row[0], row[1]


# ─────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────

@router.post("/planner/create")
def create_planner(current_user: dict = Depends(get_current_user)):
    """Tạo planner cho tuần hiện tại dựa trên study_days và study_level của user."""
    user_id = current_user["id"]

    study_days_str, study_level = get_user_profile(user_id)
    study_days = [d.strip().upper() for d in study_days_str.split(",")]

    today  = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Không tạo 2 planner cho cùng tuần
        cur.execute(
            "SELECT id FROM planner WHERE user_id = %s AND week_start = %s",
            (user_id, monday),
        )
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Planner tuần này đã tồn tại!")

        # Tạo planner
        cur.execute(
            "INSERT INTO planner (user_id, week_start, week_end) VALUES (%s, %s, %s) RETURNING id",
            (user_id, monday, sunday),
        )
        planner_id = cur.fetchone()[0]

        tasks_template = generate_tasks_for_level(study_level)
        days_added     = []

        for i in range(7):
            day_date = monday + timedelta(days=i)
            day_code = DAY_MAP[day_date.weekday()]

            if day_code not in study_days:
                continue

            cur.execute(
                "INSERT INTO planner_day (planner_id, study_date, day_of_week) VALUES (%s, %s, %s) RETURNING id",
                (planner_id, day_date, day_code),
            )
            day_id = cur.fetchone()[0]

            for task_type, title, desc, total in tasks_template:
                cur.execute(
                    "INSERT INTO task (planner_day_id, task_type, title, description, total_required) VALUES (%s,%s,%s,%s,%s)",
                    (day_id, task_type, title, desc, total),
                )

            days_added.append({"date": str(day_date), "day": day_code})

        conn.commit()
        cur.close()

        return {
            "message": "Tạo planner thành công!",
            "planner_id": planner_id,
            "week": f"{monday} → {sunday}",
            "study_days": days_added,
            "study_level": study_level,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/planner/current")
def get_current_planner(current_user: dict = Depends(get_current_user)):
    """Lấy toàn bộ planner tuần hiện tại kèm tasks."""
    user_id = current_user["id"]
    today   = datetime.now().date()
    monday  = today - timedelta(days=today.weekday())

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, week_start, week_end FROM planner WHERE user_id = %s AND week_start = %s",
            (user_id, monday),
        )
        planner = cur.fetchone()
        if not planner:
            return {"message": "Bạn chưa tạo planner tuần này!", "data": None}

        planner_id, week_start, week_end = planner

        cur.execute(
            "SELECT id, study_date, day_of_week, status FROM planner_day WHERE planner_id = %s ORDER BY study_date ASC",
            (planner_id,),
        )
        days = cur.fetchall()

        result_days = []
        for day in days:
            day_id, study_date, day_code, day_status = day
            cur.execute(
                "SELECT id, task_type, title, description, total_required, progress_count, status FROM task WHERE planner_day_id = %s ORDER BY id ASC",
                (day_id,),
            )
            tasks = [
                {"id": t[0], "task_type": t[1], "title": t[2], "description": t[3],
                 "total_required": t[4], "progress_count": t[5], "status": t[6]}
                for t in cur.fetchall()
            ]
            result_days.append({
                "id": day_id,
                "date": str(study_date),
                "day": day_code,
                "status": day_status,
                "tasks": tasks,
            })

        cur.close()
        return {
            "planner_id": planner_id,
            "week_start": str(week_start),
            "week_end": str(week_end),
            "days": result_days,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/planner/today")
def get_today_tasks(current_user: dict = Depends(get_current_user)):
    """Lấy nhiệm vụ học hôm nay."""
    user_id = current_user["id"]
    today   = datetime.now().date()
    monday  = today - timedelta(days=today.weekday())

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM planner WHERE user_id = %s AND week_start = %s",
            (user_id, monday),
        )
        planner = cur.fetchone()
        if not planner:
            return {"message": "Bạn chưa tạo planner tuần này!", "tasks": []}

        cur.execute(
            "SELECT id, status FROM planner_day WHERE planner_id = %s AND study_date = %s",
            (planner[0], today),
        )
        day = cur.fetchone()
        if not day:
            return {"message": "Hôm nay không có lịch học!", "tasks": []}

        cur.execute(
            "SELECT id, task_type, title, description, total_required, progress_count, status FROM task WHERE planner_day_id = %s ORDER BY id ASC",
            (day[0],),
        )
        tasks = [
            {"id": t[0], "task_type": t[1], "title": t[2], "description": t[3],
             "total_required": t[4], "progress_count": t[5], "status": t[6]}
            for t in cur.fetchall()
        ]
        cur.close()

        return {
            "date": str(today),
            "day_status": day[1],
            "tasks": tasks,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/planner/history")
def get_planner_history(current_user: dict = Depends(get_current_user)):
    """Lịch sử tất cả các tuần đã học."""
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, week_start, week_end, created_at FROM planner WHERE user_id = %s ORDER BY week_start DESC",
            (user_id,),
        )
        planners = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    return {
        "total": len(planners),
        "data": [{"id": p[0], "week_start": str(p[1]), "week_end": str(p[2]),
                  "created_at": p[3].strftime("%Y-%m-%d %H:%M:%S")} for p in planners],
    }