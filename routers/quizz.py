"""
routers/quizz.py — Quiz (AI generate + SRS + progress)
Endpoints:
  POST   /quiz/generate              — Tạo quiz từ file (AI Groq)
  GET    /quiz/deck/{deck_id}        — Lấy quiz theo deck
  GET    /quiz/practice/{deck_id}    — Lấy quiz để luyện tập
  POST   /quiz/submit                — Nộp đáp án (cơ bản)
  POST   /quiz/submit-srs            — Nộp đáp án + SRS
  GET    /quiz/due-review/{deck_id}  — Quiz cần ôn lại hôm nay
  GET    /quiz/progress/{deck_id}    — Tiến độ quiz
  GET    /quiz/srs-stats/{deck_id}   — Thống kê SRS
  DELETE /quiz/{quiz_id}             — Xoá quiz
"""

import os
import re
import json
from io import BytesIO
from datetime import datetime, timedelta

import httpx
import fitz
from docx import Document
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends

from database import get_connection
from auth_utils import get_current_user

router = APIRouter(tags=["Quiz"])

# ===== Config =====
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.1-8b-instant"

# SRS Constants
INITIAL_EASE_FACTOR  = 2.5
EASE_FACTOR_BONUS    = 0.1
EASE_FACTOR_PENALTY  = 0.2
MIN_EASE_FACTOR      = 1.3
INTERVAL_WRONG       = 0
INTERVAL_FIRST_CORRECT  = 1
INTERVAL_SECOND_CORRECT = 3

BANNED_WORDS = ["sex", "porn", "rape", "terrorist", "fuck", "shit", "bitch", "nigger", "whore", "suicide", "weapon"]


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

async def extract_text(file: UploadFile) -> str:
    filename = file.filename.lower()
    if filename.endswith(".docx"):
        data = await file.read()
        return "\n".join(p.text for p in Document(BytesIO(data)).paragraphs).strip()
    if filename.endswith(".pdf"):
        data = await file.read()
        pdf  = fitz.open(stream=data, filetype="pdf")
        text = "\n".join(p.get_text("text") for p in pdf)
        pdf.close()
        return text.strip()
    if filename.endswith(".txt"):
        data = await file.read()
        return data.decode("utf-8-sig", errors="ignore").strip()
    raise HTTPException(status_code=400, detail="Chỉ hỗ trợ .docx, .pdf, .txt!")


def has_banned_words(text: str) -> bool:
    pattern = r"\b(" + "|".join(re.escape(w) for w in BANNED_WORDS) + r")\b"
    return bool(re.search(pattern, text, re.IGNORECASE))


def calculate_next_review(is_correct: bool, ease: float, interval: int, review_count: int):
    now = datetime.now()
    if not is_correct:
        new_ease     = max(ease - EASE_FACTOR_PENALTY, MIN_EASE_FACTOR)
        new_interval = INTERVAL_WRONG
        next_review  = now
    else:
        new_ease = min(ease + EASE_FACTOR_BONUS, 3.0)
        if review_count == 0:
            new_interval = INTERVAL_FIRST_CORRECT
        elif review_count == 1:
            new_interval = INTERVAL_SECOND_CORRECT
        else:
            new_interval = int(interval * new_ease)
        next_review = now + timedelta(days=new_interval)
    return new_ease, new_interval, next_review


def update_task_progress_for_quiz(conn, cur, user_id: int, quiz_type: str, deck_id: int) -> bool:
    """Cập nhật task planner khi hoàn thành quiz."""
    task_title_map = {
        "multiple":  "Multiple Choice Quiz",
        "truefalse": "True/False Quiz",
        "fillblank": "Fill-in-blank Quiz",
    }
    task_title = task_title_map.get(quiz_type, "Vocabulary Quiz")

    try:
        today  = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)

        cur.execute(
            """
            SELECT t.id, t.total_required, pd.id
            FROM task t
            JOIN planner_day pd ON t.planner_day_id = pd.id
            JOIN planner p ON pd.planner_id = p.id
            WHERE p.user_id = %s
              AND t.task_type = 'quiz'
              AND t.title = %s
              AND t.status = 'pending'
              AND pd.study_date BETWEEN %s AND %s
              AND pd.study_date <= %s
            ORDER BY pd.study_date ASC LIMIT 1
            """,
            (user_id, task_title, monday, sunday, today),
        )
        task = cur.fetchone()
        if not task:
            return False

        task_id, total_required, planner_day_id = task

        cur.execute(
            """
            SELECT COUNT(*) FROM user_quiz_progress
            WHERE user_id = %s AND deck_id = %s AND quiz_type = %s AND status = 'completed'
            """,
            (user_id, deck_id, quiz_type),
        )
        completed = cur.fetchone()[0]

        new_status = "completed" if completed >= total_required else "pending"
        cur.execute(
            "UPDATE task SET progress_count = %s, status = %s, updated_at = NOW() WHERE id = %s",
            (completed, new_status, task_id),
        )

        if new_status == "completed":
            cur.execute(
                "SELECT COUNT(*) FROM task WHERE planner_day_id = %s AND status = 'pending'",
                (planner_day_id,),
            )
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "UPDATE planner_day SET status = 'completed', updated_at = NOW() WHERE id = %s",
                    (planner_day_id,),
                )
        return True
    except Exception as e:
        print(f"⚠️ Lỗi update task quiz: {e}")
        return False


# ─────────────────────────────────────────
# QUIZ TABLE (thêm vào init_tables nếu cần)
# ─────────────────────────────────────────
QUIZ_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS quiz (
    id              SERIAL PRIMARY KEY,
    deck_id         INTEGER REFERENCES deck(id) ON DELETE CASCADE,
    question        TEXT NOT NULL,
    question_type   VARCHAR(20) NOT NULL,   -- 'multiple' | 'truefalse' | 'fillblank'
    options         JSONB,
    correct_answer  TEXT NOT NULL,
    context         TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_quiz_progress (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
    quiz_id         INTEGER REFERENCES quiz(id) ON DELETE CASCADE,
    deck_id         INTEGER REFERENCES deck(id) ON DELETE CASCADE,
    quiz_type       VARCHAR(20),
    user_answer     TEXT,
    is_correct      BOOLEAN,
    ease_factor     NUMERIC(4,2) DEFAULT 2.5,
    interval_days   INTEGER DEFAULT 0,
    next_review_date TIMESTAMP,
    review_count    INTEGER DEFAULT 0,
    last_review_date TIMESTAMP,
    attempt_count   INTEGER DEFAULT 0,
    status          VARCHAR(20) DEFAULT 'new',   -- 'new' | 'reviewing' | 'completed' | 'mastered'
    updated_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, quiz_id)
);
"""


def ensure_quiz_tables():
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(QUIZ_TABLE_SQL)
        conn.commit()
        cur.close()
    finally:
        conn.close()


# ─────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────

@router.post("/quiz/generate")
async def generate_quiz(
    deck_id:    int = Form(...),
    file:       UploadFile = File(...),
    quiz_type:  str = Form(..., description="multiple | truefalse | fillblank"),
    current_user: dict = Depends(get_current_user),
):
    """Đọc file → gọi Groq AI → lưu quiz vào DB."""
    if quiz_type not in ("multiple", "truefalse", "fillblank"):
        raise HTTPException(status_code=400, detail="quiz_type phải là: multiple | truefalse | fillblank")

    text = await extract_text(file)
    if not text or len(text.split()) < 10:
        raise HTTPException(status_code=400, detail="File quá ngắn hoặc trống!")
    if has_banned_words(text):
        raise HTTPException(status_code=400, detail="File chứa nội dung không phù hợp!")

    instruction_map = {
        "multiple":  (
            "Create as many multiple-choice questions as possible. "
            "Each item: question, options (4 choices as array), correct_answer, "
            "context (1-3 sentence hint, do NOT directly reveal the answer)."
        ),
        "truefalse": (
            "Create as many true/false questions as possible. "
            "Each item: question, correct_answer (true or false), "
            "context (1-3 sentence hint, do NOT directly reveal the answer)."
        ),
        "fillblank": (
            "Create as many fill-in-the-blank questions as possible. "
            "Each item: question (with ____), correct_answer, "
            "context (1-3 sentence hint, do NOT directly reveal the answer)."
        ),
    }

    prompt = (
        f"You are an educational quiz creator. {instruction_map[quiz_type]}\n"
        f"Output ONLY a valid JSON array. No extra text.\n\n"
        f"Source document:\n{text[:8000]}"
    )

    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY chưa được cấu hình!")

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(GROQ_API_URL, headers=headers, json=payload)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Groq API lỗi: {resp.text}")

    ai_text = resp.json()["choices"][0]["message"]["content"].strip()
    try:
        quiz_data = json.loads(ai_text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", ai_text, re.S)
        quiz_data = json.loads(match.group(0)) if match else []

    if not quiz_data:
        raise HTTPException(status_code=400, detail="AI không tạo được quiz nào!")

    ensure_quiz_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        for q in quiz_data:
            question = q.get("question") or q.get("statement", "")
            options  = q.get("options", [])
            answer   = q.get("correct_answer", "")
            context  = q.get("context", "")
            cur.execute(
                """
                INSERT INTO quiz (deck_id, question, question_type, options, correct_answer, context)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (deck_id, question, quiz_type, json.dumps(options) if options else None, answer, context),
            )
        conn.commit()
        cur.close()
    finally:
        conn.close()

    return {"message": f"Tạo {len(quiz_data)} câu quiz thành công!", "deck_id": deck_id, "quiz_type": quiz_type, "count": len(quiz_data)}


@router.get("/quiz/deck/{deck_id}")
def get_quizzes_by_deck(
    deck_id:   int,
    quiz_type: str = None,
    current_user: dict = Depends(get_current_user),
):
    conn = get_connection()
    try:
        cur = conn.cursor()
        if quiz_type:
            cur.execute(
                "SELECT id, question, question_type, options, correct_answer, context FROM quiz WHERE deck_id = %s AND question_type = %s ORDER BY id ASC",
                (deck_id, quiz_type),
            )
        else:
            cur.execute(
                "SELECT id, question, question_type, options, correct_answer, context FROM quiz WHERE deck_id = %s ORDER BY id ASC",
                (deck_id,),
            )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    data = [
        {"id": r[0], "question": r[1], "type": r[2],
         "options": json.loads(r[3]) if r[3] else None,
         "correct_answer": r[4], "context": r[5]}
        for r in rows
    ]
    return {"total": len(data), "deck_id": deck_id, "quiz_type": quiz_type, "data": data}


@router.get("/quiz/practice/{deck_id}")
def get_practice_quiz(
    deck_id:   int,
    quiz_type: str = None,
    limit:     int = 10,
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        filter_sql = "AND q.question_type = %s" if quiz_type else ""
        params     = [user_id, deck_id] + ([quiz_type] if quiz_type else []) + [limit]
        cur.execute(
            f"""
            SELECT q.id, q.question, q.question_type, q.options, q.context,
                   COALESCE(p.is_correct, NULL), COALESCE(p.attempt_count, 0)
            FROM quiz q
            LEFT JOIN user_quiz_progress p ON q.id = p.quiz_id AND p.user_id = %s
            WHERE q.deck_id = %s {filter_sql}
            ORDER BY CASE WHEN p.id IS NULL THEN 1 WHEN p.is_correct = FALSE THEN 2 ELSE 3 END, RANDOM()
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    data = [
        {"id": r[0], "question": r[1], "type": r[2],
         "options": json.loads(r[3]) if r[3] else None,
         "context": r[4], "was_correct": r[5], "attempts": r[6]}
        for r in rows
    ]
    return {"deck_id": deck_id, "data": data}


@router.post("/quiz/submit")
def submit_quiz(
    quiz_id:     int  = Form(...),
    user_answer: str  = Form(...),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT deck_id, question_type, correct_answer FROM quiz WHERE id = %s", (quiz_id,))
        quiz = cur.fetchone()
        if not quiz:
            raise HTTPException(status_code=404, detail="Quiz không tồn tại!")

        deck_id, quiz_type, correct_answer = quiz
        is_correct = user_answer.strip().lower() == str(correct_answer).strip().lower()

        cur.execute("SELECT id, status FROM user_quiz_progress WHERE user_id = %s AND quiz_id = %s", (user_id, quiz_id))
        existing = cur.fetchone()

        if existing:
            cur.execute(
                "UPDATE user_quiz_progress SET user_answer=%s, is_correct=%s, status='completed', attempt_count=attempt_count+1, updated_at=NOW() WHERE id=%s",
                (user_answer, is_correct, existing[0]),
            )
            was_completed = existing[1] == "completed"
        else:
            cur.execute(
                "INSERT INTO user_quiz_progress (user_id, quiz_id, deck_id, quiz_type, user_answer, is_correct, status, attempt_count) VALUES (%s,%s,%s,%s,%s,%s,'completed',1)",
                (user_id, quiz_id, deck_id, quiz_type, user_answer, is_correct),
            )
            was_completed = False

        task_updated = False
        if not was_completed:
            task_updated = update_task_progress_for_quiz(conn, cur, user_id, quiz_type, deck_id)

        conn.commit()
        cur.close()

        return {
            "message": "Chính xác!" if is_correct else "Chưa chính xác!",
            "is_correct": is_correct,
            "correct_answer": correct_answer,
            "task_updated": task_updated,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.post("/quiz/submit-srs")
def submit_quiz_srs(
    quiz_id:     int  = Form(...),
    user_answer: str  = Form(...),
    current_user: dict = Depends(get_current_user),
):
    """Nộp đáp án với SRS — tự động điều chỉnh lịch ôn tập."""
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT deck_id, question_type, correct_answer, context, question FROM quiz WHERE id = %s", (quiz_id,))
        quiz = cur.fetchone()
        if not quiz:
            raise HTTPException(status_code=404, detail="Quiz không tồn tại!")

        deck_id, quiz_type, correct_answer, context, question = quiz
        is_correct = user_answer.strip().lower() == str(correct_answer).strip().lower()

        cur.execute(
            "SELECT id, ease_factor, interval_days, review_count, status FROM user_quiz_progress WHERE user_id=%s AND quiz_id=%s",
            (user_id, quiz_id),
        )
        existing = cur.fetchone()

        if existing:
            prog_id, ease, interval, review_count, old_status = existing
            ease = float(ease or INITIAL_EASE_FACTOR)
            new_ease, new_interval, next_review = calculate_next_review(is_correct, ease, interval or 0, review_count or 0)
            new_count = (review_count or 0) + (1 if is_correct else 0)
            cur.execute(
                """
                UPDATE user_quiz_progress
                SET user_answer=%s, is_correct=%s, ease_factor=%s, interval_days=%s,
                    next_review_date=%s, review_count=%s, last_review_date=NOW(),
                    attempt_count=attempt_count+1,
                    status=CASE WHEN %s THEN 'completed' ELSE 'reviewing' END, updated_at=NOW()
                WHERE id=%s
                """,
                (user_answer, is_correct, new_ease, new_interval, next_review, new_count, is_correct, prog_id),
            )
            was_completed = old_status == "completed"
        else:
            new_ease = INITIAL_EASE_FACTOR
            if is_correct:
                new_interval = INTERVAL_FIRST_CORRECT
                next_review  = datetime.now() + timedelta(days=new_interval)
                new_count    = 1
                status       = "completed"
            else:
                new_interval = INTERVAL_WRONG
                next_review  = datetime.now()
                new_count    = 0
                status       = "reviewing"
            cur.execute(
                """
                INSERT INTO user_quiz_progress
                (user_id, quiz_id, deck_id, quiz_type, user_answer, is_correct, ease_factor,
                 interval_days, next_review_date, review_count, last_review_date, attempt_count, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),1,%s)
                """,
                (user_id, quiz_id, deck_id, quiz_type, user_answer, is_correct,
                 new_ease, new_interval, next_review, new_count, status),
            )
            was_completed = False

        task_updated = False
        if not was_completed and is_correct:
            task_updated = update_task_progress_for_quiz(conn, cur, user_id, quiz_type, deck_id)

        conn.commit()
        cur.close()

        return {
            "is_correct": is_correct,
            "correct_answer": correct_answer,
            "explanation": context or "Không có giải thích.",
            "task_updated": task_updated,
            "srs": {"ease_factor": round(new_ease, 2), "interval_days": new_interval,
                    "next_review": next_review.strftime("%Y-%m-%d %H:%M:%S"), "review_count": new_count},
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/quiz/due-review/{deck_id}")
def get_due_review(deck_id: int, limit: int = 20, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT q.id, q.question, q.question_type, q.options, q.context,
                   p.ease_factor, p.interval_days, p.next_review_date, p.review_count
            FROM quiz q
            JOIN user_quiz_progress p ON q.id = p.quiz_id AND p.user_id = %s
            WHERE q.deck_id = %s AND p.next_review_date <= NOW() AND p.status != 'mastered'
            ORDER BY p.is_correct ASC, p.next_review_date ASC
            LIMIT %s
            """,
            (user_id, deck_id, limit),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    data = [
        {"id": r[0], "question": r[1], "type": r[2],
         "options": json.loads(r[3]) if r[3] else None, "context": r[4],
         "srs": {"ease_factor": float(r[5] or 2.5), "interval_days": r[6],
                 "next_review": r[7].strftime("%Y-%m-%d") if r[7] else None, "review_count": r[8]}}
        for r in rows
    ]
    return {"deck_id": deck_id, "total_due": len(data), "data": data}


@router.get("/quiz/progress/{deck_id}")
def get_quiz_progress(deck_id: int, quiz_type: str = None, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        filter_sql = "AND q.question_type = %s" if quiz_type else ""
        params     = [user_id, deck_id] + ([quiz_type] if quiz_type else [])
        cur.execute(
            f"""
            SELECT q.id, q.question, q.question_type, q.correct_answer, q.context,
                   p.user_answer, p.is_correct, COALESCE(p.status,'new'), p.attempt_count
            FROM quiz q
            LEFT JOIN user_quiz_progress p ON q.id = p.quiz_id AND p.user_id = %s
            WHERE q.deck_id = %s {filter_sql}
            ORDER BY q.id ASC
            """,
            params,
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    completed = sum(1 for r in rows if r[7] == "completed")
    correct   = sum(1 for r in rows if r[7] == "completed" and r[6])
    total     = len(rows)

    data = [
        {"id": r[0], "question": r[1][:100], "type": r[2],
         "status": r[7], "is_correct": r[6], "attempts": r[8] or 0}
        for r in rows
    ]
    return {
        "deck_id": deck_id, "total": total, "completed": completed, "correct": correct,
        "completion_rate": round(completed / total * 100, 1) if total else 0,
        "accuracy_rate":   round(correct / completed * 100, 1) if completed else 0,
        "data": data,
    }


@router.get("/quiz/srs-stats/{deck_id}")
def get_srs_stats(deck_id: int, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM quiz WHERE deck_id = %s", (deck_id,))
        total = cur.fetchone()[0]

        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE p.next_review_date <= NOW()),
                COUNT(*) FILTER (WHERE p.next_review_date BETWEEN NOW() AND NOW() + INTERVAL '1 day'),
                COUNT(*) FILTER (WHERE p.review_count >= 5 AND p.interval_days >= 30),
                AVG(p.ease_factor), AVG(p.interval_days)
            FROM quiz q
            LEFT JOIN user_quiz_progress p ON q.id = p.quiz_id AND p.user_id = %s
            WHERE q.deck_id = %s
            """,
            (user_id, deck_id),
        )
        s = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    return {
        "deck_id": deck_id, "total": total,
        "due_now": s[0] or 0, "due_tomorrow": s[1] or 0, "mastered": s[2] or 0,
        "avg_ease": round(float(s[3]), 2) if s[3] else 2.5,
        "avg_interval_days": round(float(s[4]), 1) if s[4] else 0,
        "mastery_rate": round((s[2] or 0) / total * 100, 1) if total else 0,
    }


@router.delete("/quiz/{quiz_id}")
def delete_quiz(quiz_id: int, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT d.user_id FROM quiz q JOIN deck d ON q.deck_id = d.id WHERE q.id = %s", (quiz_id,))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Quiz không tồn tại!")
        if result[0] != user_id:
            raise HTTPException(status_code=403, detail="Bạn không có quyền xoá quiz này!")

        cur.execute("DELETE FROM user_quiz_progress WHERE quiz_id = %s", (quiz_id,))
        cur.execute("DELETE FROM quiz WHERE id = %s", (quiz_id,))
        conn.commit()
        cur.close()

        return {"message": f"Đã xoá quiz #{quiz_id}!"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()