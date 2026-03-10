"""
routers/deck.py — Quản lý Deck (bộ thẻ)
Endpoints:
  POST   /deck/create          — Tạo deck riêng
  GET    /deck/list            — Lấy danh sách deck (public + của user)
  DELETE /deck/delete/{id}     — Xoá deck riêng
"""

from fastapi import APIRouter, Form, HTTPException, Depends
from datetime import datetime

from database import get_connection
from auth_utils import get_current_user

router = APIRouter(tags=["Deck"])


# ============================================================
# 1. Tạo deck
# ============================================================
@router.post("/deck/create")
def create_deck(
    name: str = Form(..., description="Tên deck"),
    description: str = Form("", description="Mô tả"),
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user["id"]

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Kiểm tra trùng tên (trong public hoặc của chính user)
        cur.execute(
            """
            SELECT id FROM deck
            WHERE name = %s AND (user_id IS NULL OR user_id = %s)
            """,
            (name, user_id),
        )
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Deck này đã tồn tại!")

        cur.execute(
            "INSERT INTO deck (name, description, created_at, user_id) VALUES (%s, %s, %s, %s) RETURNING id",
            (name, description, datetime.now(), user_id),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()

        return {"message": "Tạo deck thành công!", "deck_id": new_id, "user_id": user_id}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ============================================================
# 2. Lấy danh sách deck
# ============================================================
@router.get("/deck/list")
def get_deck_list(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, description, created_at, user_id
            FROM deck
            WHERE user_id IS NULL OR user_id = %s
            ORDER BY created_at ASC
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        cur.close()

        decks = [
            {
                "id": r[0],
                "name": r[1],
                "description": r[2],
                "created_at": r[3].strftime("%Y-%m-%d %H:%M:%S") if r[3] else None,
                "type": "public" if r[4] is None else "private",
                "user_id": r[4],
            }
            for r in rows
        ]

        return {"message": f"Lấy {len(decks)} deck thành công!", "data": decks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ============================================================
# 3. Xoá deck riêng
# ============================================================
@router.delete("/deck/delete/{deck_id}")
def delete_deck(deck_id: int, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, user_id FROM deck WHERE id = %s", (deck_id,))
        deck = cur.fetchone()

        if not deck:
            raise HTTPException(status_code=404, detail="Không tìm thấy deck!")
        if deck[1] is None:
            raise HTTPException(status_code=403, detail="Không thể xoá deck công khai!")
        if deck[1] != user_id:
            raise HTTPException(status_code=403, detail="Bạn không có quyền xoá deck này!")

        cur.execute("DELETE FROM deck WHERE id = %s", (deck_id,))
        conn.commit()
        cur.close()

        return {"message": f"Xoá deck ID {deck_id} thành công!"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ============================================================
# 4. Khởi tạo 10 deck công khai mặc định
# ============================================================
def init_public_decks():
    """Gọi từ startup event trong main.py."""
    public_decks = [
        ("English Basics", "Từ vựng cơ bản tiếng Anh"),
        ("Animals", "Động vật"),
        ("Technology", "Công nghệ"),
        ("Food & Drinks", "Ẩm thực"),
        ("Travel", "Du lịch"),
        ("Health", "Sức khoẻ"),
        ("Jobs", "Nghề nghiệp"),
        ("Nature", "Thiên nhiên"),
        ("Daily Activities", "Sinh hoạt hằng ngày"),
        ("Sports", "Thể thao"),
    ]

    conn = get_connection()
    try:
        cur = conn.cursor()
        for name, desc in public_decks:
            cur.execute("SELECT id FROM deck WHERE name = %s AND user_id IS NULL", (name,))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO deck (name, description, created_at, user_id) VALUES (%s, %s, NOW(), NULL)",
                    (name, desc),
                )
        conn.commit()
        cur.close()
        print("✅ Deck công khai đã được khởi tạo.")
    except Exception as e:
        conn.rollback()
        print(f"⚠️ Lỗi khởi tạo deck công khai: {e}")
    finally:
        conn.close()