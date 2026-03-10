"""
routers/admin.py — Quản trị hệ thống (chỉ admin)
Endpoints:
  GET    /admin/users           — Danh sách tất cả users
  GET    /admin/users/{id}      — Chi tiết một user
  PUT    /admin/users/{id}/role — Thay đổi role
  DELETE /admin/users/{id}      — Xoá user
  GET    /admin/stats           — Thống kê toàn hệ thống
  GET    /admin/decks           — Tất cả decks
"""

from fastapi import APIRouter, HTTPException, Depends, Form, Query

from database import get_connection
from auth_utils import get_current_user, require_admin

router = APIRouter(prefix="/admin", tags=["Admin"])


# ─────────────────────────────────────────
# USERS
# ─────────────────────────────────────────

@router.get("/users")
def list_users(
    page:  int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    _: dict   = Depends(require_admin),
):
    offset = (page - 1) * limit
    conn   = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT u.id, u.username, u.email, u.role, u.created_at, u.last_login,
                   up.study_level, up.study_days
            FROM users u
            LEFT JOIN user_profile up ON u.id = up.user_id
            ORDER BY u.id DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]
        cur.close()
    finally:
        conn.close()

    data = [
        {
            "id": r[0], "username": r[1], "email": r[2], "role": r[3],
            "created_at":  r[4].strftime("%Y-%m-%d %H:%M:%S") if r[4] else None,
            "last_login":  r[5].strftime("%Y-%m-%d %H:%M:%S") if r[5] else None,
            "study_level": r[6], "study_days": r[7],
        }
        for r in rows
    ]
    return {"total": total, "page": page, "limit": limit, "data": data}


@router.get("/users/{user_id}")
def get_user_detail(user_id: int, _: dict = Depends(require_admin)):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT u.id, u.username, u.email, u.role, u.created_at, u.last_login,
                   up.full_name, up.bio, up.study_level, up.study_days
            FROM users u
            LEFT JOIN user_profile up ON u.id = up.user_id
            WHERE u.id = %s
            """,
            (user_id,),
        )
        user = cur.fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="User không tồn tại!")

        # Thống kê của user
        cur.execute("SELECT COUNT(*) FROM user_flashcard_progress WHERE user_id = %s AND status = 'done'", (user_id,))
        fc_done = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM user_quiz_progress WHERE user_id = %s", (user_id,))
        quiz_done = cur.fetchone()[0]
        cur.close()
    finally:
        conn.close()

    return {
        "id": user[0], "username": user[1], "email": user[2], "role": user[3],
        "created_at": user[4].strftime("%Y-%m-%d %H:%M:%S") if user[4] else None,
        "last_login":  user[5].strftime("%Y-%m-%d %H:%M:%S") if user[5] else None,
        "profile": {"full_name": user[6], "bio": user[7], "study_level": user[8], "study_days": user[9]},
        "stats": {"flashcards_done": fc_done, "quizzes_done": quiz_done},
    }


@router.put("/users/{user_id}/role")
def change_user_role(
    user_id:     int,
    role:        str = Form(..., description="user | admin"),
    admin_user:  dict = Depends(require_admin),
):
    if role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="Role chỉ có thể là 'user' hoặc 'admin'!")
    if admin_user["id"] == user_id and role != "admin":
        raise HTTPException(status_code=400, detail="Bạn không thể tự hạ quyền của chính mình!")

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="User không tồn tại!")

        cur.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
        conn.commit()
        cur.close()

        return {"message": f"Đã cập nhật role user #{user_id} thành '{role}'!"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin_user: dict = Depends(require_admin)):
    if admin_user["id"] == user_id:
        raise HTTPException(status_code=400, detail="Không thể xoá chính mình!")

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="User không tồn tại!")

        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
        cur.close()

        return {"message": f"Đã xoá user #{user_id}!"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ─────────────────────────────────────────
# SYSTEM STATS
# ─────────────────────────────────────────

@router.get("/stats")
def get_system_stats(_: dict = Depends(require_admin)):
    """Thống kê toàn hệ thống."""
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
        total_admins = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM deck WHERE user_id IS NULL")
        public_decks = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM deck WHERE user_id IS NOT NULL")
        private_decks = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM flashcards")
        total_flashcards = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM quiz")
        total_quizzes = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM posts")
        total_posts = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL '7 days'"
        )
        new_users_week = cur.fetchone()[0]

        cur.close()
    finally:
        conn.close()

    return {
        "users":        {"total": total_users, "admins": total_admins, "new_this_week": new_users_week},
        "decks":        {"public": public_decks, "private": private_decks},
        "content":      {"flashcards": total_flashcards, "quizzes": total_quizzes, "posts": total_posts},
    }


@router.get("/decks")
def list_all_decks(
    page:  int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    _:     dict = Depends(require_admin),
):
    offset = (page - 1) * limit
    conn   = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT d.id, d.name, d.description, d.created_at,
                   CASE WHEN d.user_id IS NULL THEN 'public' ELSE 'private' END,
                   u.username,
                   (SELECT COUNT(*) FROM flashcards WHERE deck_id = d.id) AS card_count
            FROM deck d
            LEFT JOIN users u ON d.user_id = u.id
            ORDER BY d.id DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM deck")
        total = cur.fetchone()[0]
        cur.close()
    finally:
        conn.close()

    data = [
        {"id": r[0], "name": r[1], "description": r[2],
         "created_at": r[3].strftime("%Y-%m-%d %H:%M:%S") if r[3] else None,
         "type": r[4], "owner": r[5], "card_count": r[6]}
        for r in rows
    ]
    return {"total": total, "page": page, "data": data}