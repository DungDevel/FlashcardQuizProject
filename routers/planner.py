"""
routers/planner.py
Planner học tập theo tuần
"""

from fastapi import (
    APIRouter,
    HTTPException,
    Depends
)

from datetime import (
    datetime,
    timedelta
)

from pydantic import BaseModel

from database import get_connection
from auth_utils import get_current_user

router = APIRouter(tags=["Planner"])

DAY_MAP = {
    0: "MON",
    1: "TUE",
    2: "WED",
    3: "THU",
    4: "FRI",
    5: "SAT",
    6: "SUN"
}


# =========================================================
# REQUEST MODEL
# =========================================================

class UpdateTaskRequest(BaseModel):
    task_id: int
    progress_count: int


# =========================================================
# TASK TEMPLATE
# =========================================================

def generate_tasks_for_level(study_level: str):

    config = {
        "easy": {
            "card": 10,
            "quiz": 5
        },

        "medium": {
            "card": 20,
            "quiz": 10
        },

        "hard": {
            "card": 30,
            "quiz": 15
        }
    }.get(
        study_level.lower(),
        {
            "card": 10,
            "quiz": 5
        }
    )

    card_count = config["card"]
    quiz_count = config["quiz"]

    return [

        (
            "flashcard",
            "Flashcards",
            f"Ôn {card_count} thẻ flashcard",
            card_count
        ),

        (
            "quiz",
            "Multiple Choice Quiz",
            f"Làm {quiz_count} câu trắc nghiệm",
            quiz_count
        ),

        (
            "quiz",
            "True/False Quiz",
            f"Làm {quiz_count} câu đúng/sai",
            quiz_count
        ),

        (
            "quiz",
            "Fill-in-blank Quiz",
            f"Làm {quiz_count} câu điền chỗ trống",
            quiz_count
        )
    ]


# =========================================================
# USER PROFILE
# =========================================================

def get_user_profile(user_id: int):

    conn = get_connection()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT study_days, study_level
            FROM user_profile
            WHERE user_id = %s
        """, (user_id,))

        row = cur.fetchone()

        cur.close()

    finally:

        conn.close()

    if not row or not row[0]:

        raise HTTPException(
            status_code=400,
            detail="Bạn chưa thiết lập study_days hoặc study_level!"
        )

    return row[0], row[1]


# =========================================================
# CREATE PLANNER
# =========================================================

@router.post("/planner/create")
def create_planner(
    current_user: dict = Depends(get_current_user)
):

    user_id = current_user["id"]

    study_days_str, study_level = get_user_profile(user_id)

    study_days = [
        d.strip().upper()
        for d in study_days_str.split(",")
    ]

    today = datetime.now().date()

    monday = today - timedelta(
        days=today.weekday()
    )

    sunday = monday + timedelta(days=6)

    conn = get_connection()

    try:

        cur = conn.cursor()

        # =================================================
        # CHECK EXIST
        # =================================================

        cur.execute("""
            SELECT id
            FROM planner
            WHERE user_id = %s
            AND week_start = %s
        """, (
            user_id,
            monday
        ))

        if cur.fetchone():

            raise HTTPException(
                status_code=400,
                detail="Planner tuần này đã tồn tại!"
            )

        # =================================================
        # CREATE PLANNER
        # =================================================

        cur.execute("""
            INSERT INTO planner (
                user_id,
                week_start,
                week_end
            )
            VALUES (%s, %s, %s)
            RETURNING id
        """, (
            user_id,
            monday,
            sunday
        ))

        planner_id = cur.fetchone()[0]

        tasks_template = generate_tasks_for_level(
            study_level
        )

        days_added = []

        for i in range(7):

            day_date = monday + timedelta(days=i)

            day_code = DAY_MAP[
                day_date.weekday()
            ]

            if day_code not in study_days:
                continue

            cur.execute("""
                INSERT INTO planner_day (
                    planner_id,
                    study_date,
                    day_of_week,
                    status
                )
                VALUES (%s, %s, %s, 'pending')
                RETURNING id
            """, (
                planner_id,
                day_date,
                day_code
            ))

            planner_day_id = cur.fetchone()[0]

            for task_type, title, desc, total in tasks_template:

                cur.execute("""
                    INSERT INTO task (
                        planner_day_id,
                        task_type,
                        title,
                        description,
                        total_required,
                        progress_count,
                        status
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, 0, 'pending'
                    )
                """, (
                    planner_day_id,
                    task_type,
                    title,
                    desc,
                    total
                ))

            days_added.append({
                "date": str(day_date),
                "day": day_code
            })

        conn.commit()

        cur.close()

        return {
            "message": "Tạo planner thành công!",
            "planner_id": planner_id,
            "week": f"{monday} → {sunday}",
            "study_days": days_added,
            "study_level": study_level
        }

    except HTTPException:
        raise

    except Exception as e:

        conn.rollback()

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    finally:

        conn.close()


# =========================================================
# UPDATE TASK
# =========================================================

@router.post("/planner/task/update")
def update_task_progress(
    payload: UpdateTaskRequest,
    current_user: dict = Depends(get_current_user)
):

    conn = get_connection()

    try:

        cur = conn.cursor()

        # =================================================
        # GET TASK
        # =================================================

        cur.execute("""
            SELECT
                id,
                planner_day_id,
                total_required
            FROM task
            WHERE id = %s
        """, (payload.task_id,))

        task = cur.fetchone()

        if not task:

            raise HTTPException(
                status_code=404,
                detail="Task not found"
            )

        task_id = task[0]
        planner_day_id = task[1]
        total_required = task[2]

        # =================================================
        # STATUS
        # =================================================

        status = (
            "completed"
            if payload.progress_count >= total_required
            else "pending"
        )

        # =================================================
        # UPDATE TASK
        # =================================================

        cur.execute("""
            UPDATE task
            SET
                progress_count = %s,
                status = %s
            WHERE id = %s
        """, (
            payload.progress_count,
            status,
            task_id
        ))

        # =================================================
        # CHECK REMAINING TASK
        # =================================================

        cur.execute("""
            SELECT COUNT(*)
            FROM task
            WHERE planner_day_id = %s
            AND status != 'completed'
        """, (planner_day_id,))

        remaining = cur.fetchone()[0]

        # =================================================
        # UPDATE PLANNER DAY
        # =================================================

        planner_day_status = (
            "completed"
            if remaining == 0
            else "pending"
        )

        cur.execute("""
            UPDATE planner_day
            SET status = %s
            WHERE id = %s
        """, (
            planner_day_status,
            planner_day_id
        ))

        conn.commit()

        cur.close()

        return {
            "message": "Task updated successfully",
            "task_status": status,
            "planner_day_status": planner_day_status
        }

    except HTTPException:
        raise

    except Exception as e:

        conn.rollback()

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    finally:

        conn.close()


# =========================================================
# TODAY TASKS
# =========================================================

@router.get("/planner/today")
def get_today_tasks(
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
            SELECT id
            FROM planner
            WHERE user_id = %s
            AND week_start = %s
        """, (
            user_id,
            monday
        ))

        planner = cur.fetchone()

        if not planner:

            return {
                "message": "Bạn chưa tạo planner tuần này!",
                "tasks": []
            }

        cur.execute("""
            SELECT
                id,
                status
            FROM planner_day
            WHERE planner_id = %s
            AND study_date = %s
        """, (
            planner[0],
            today
        ))

        day = cur.fetchone()

        if not day:

            return {
                "message": "Hôm nay không có lịch học!",
                "tasks": []
            }

        planner_day_id = day[0]

        cur.execute("""
            SELECT
                id,
                task_type,
                title,
                description,
                total_required,
                progress_count,
                status

            FROM task

            WHERE planner_day_id = %s

            ORDER BY id ASC
        """, (planner_day_id,))

        tasks = [

            {
                "id": t[0],
                "task_type": t[1],
                "title": t[2],
                "description": t[3],
                "total_required": t[4],
                "progress_count": t[5],
                "status": t[6]
            }

            for t in cur.fetchall()
        ]

        cur.close()

        return {
            "date": str(today),
            "day_status": day[1],
            "tasks": tasks
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    finally:

        conn.close()