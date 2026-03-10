"""
routers/social.py — Mạng xã hội (Posts, Likes, Comments)
Endpoints:
  POST   /social/posts/create          — Tạo bài viết
  GET    /social/posts                 — Xem tất cả bài viết
  POST   /social/posts/{id}/like       — Like / Unlike bài viết
  POST   /social/posts/{id}/comment    — Bình luận
  GET    /social/posts/{id}/comments   — Xem bình luận
  DELETE /social/posts/{id}            — Xoá bài viết (chủ sở hữu)
"""

from fastapi import APIRouter, Form, HTTPException, Depends, Query
from typing import Optional
from datetime import datetime

from database import get_connection
from auth_utils import get_current_user

router = APIRouter(tags=["Social"])


# ─────────────────────────────────────────
# POSTS
# ─────────────────────────────────────────

@router.post("/social/posts/create")
def create_post(
    content:    str = Form(..., description="Nội dung bài viết"),
    post_type:  str = Form("general"),
    visibility: str = Form("public"),
    image_url:  Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
):
    if not content.strip():
        raise HTTPException(status_code=400, detail="Nội dung không được để trống!")
    if len(content) > 5000:
        raise HTTPException(status_code=400, detail="Nội dung quá dài (tối đa 5000 ký tự)!")

    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO posts (user_id, content, post_type, visibility, image_url) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (user_id, content.strip(), post_type, visibility, image_url),
        )
        post_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return {"message": "Đăng bài thành công!", "post_id": post_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/social/posts")
def get_posts(
    page:  int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    offset  = (page - 1) * limit
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.id, p.content, p.post_type, p.image_url, p.created_at,
                   u.id AS author_id, u.username,
                   (SELECT COUNT(*) FROM post_likes   WHERE post_id = p.id) AS likes,
                   (SELECT COUNT(*) FROM post_comments WHERE post_id = p.id) AS comments,
                   EXISTS(SELECT 1 FROM post_likes WHERE post_id = p.id AND user_id = %s) AS liked
            FROM posts p
            JOIN users u ON p.user_id = u.id
            WHERE p.visibility = 'public'
            ORDER BY p.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (user_id, limit, offset),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    data = [
        {"id": r[0], "content": r[1], "post_type": r[2], "image_url": r[3],
         "created_at": r[4].strftime("%Y-%m-%d %H:%M:%S"),
         "author": {"id": r[5], "username": r[6]},
         "likes": r[7], "comments": r[8], "liked_by_me": r[9]}
        for r in rows
    ]
    return {"page": page, "limit": limit, "data": data}


@router.post("/social/posts/{post_id}/like")
def toggle_like(post_id: int, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM posts WHERE id = %s", (post_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Bài viết không tồn tại!")

        cur.execute("SELECT id FROM post_likes WHERE post_id = %s AND user_id = %s", (post_id, user_id))
        existing = cur.fetchone()

        if existing:
            cur.execute("DELETE FROM post_likes WHERE id = %s", (existing[0],))
            action = "unliked"
        else:
            cur.execute("INSERT INTO post_likes (post_id, user_id) VALUES (%s,%s)", (post_id, user_id))
            action = "liked"

        conn.commit()
        cur.execute("SELECT COUNT(*) FROM post_likes WHERE post_id = %s", (post_id,))
        total_likes = cur.fetchone()[0]
        cur.close()

        return {"action": action, "total_likes": total_likes}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.post("/social/posts/{post_id}/comment")
def add_comment(
    post_id: int,
    content: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    if not content.strip():
        raise HTTPException(status_code=400, detail="Bình luận không được để trống!")
    if len(content) > 1000:
        raise HTTPException(status_code=400, detail="Bình luận quá dài (tối đa 1000 ký tự)!")

    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM posts WHERE id = %s", (post_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Bài viết không tồn tại!")

        cur.execute(
            "INSERT INTO post_comments (post_id, user_id, content) VALUES (%s,%s,%s) RETURNING id",
            (post_id, user_id, content.strip()),
        )
        comment_id = cur.fetchone()[0]
        conn.commit()
        cur.close()

        return {"message": "Đã bình luận!", "comment_id": comment_id}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/social/posts/{post_id}/comments")
def get_comments(post_id: int, current_user: dict = Depends(get_current_user)):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT c.id, c.content, c.created_at, u.id, u.username
            FROM post_comments c
            JOIN users u ON c.user_id = u.id
            WHERE c.post_id = %s
            ORDER BY c.created_at ASC
            """,
            (post_id,),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    data = [
        {"id": r[0], "content": r[1], "created_at": r[2].strftime("%Y-%m-%d %H:%M:%S"),
         "author": {"id": r[3], "username": r[4]}}
        for r in rows
    ]
    return {"post_id": post_id, "total": len(data), "data": data}


@router.delete("/social/posts/{post_id}")
def delete_post(post_id: int, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM posts WHERE id = %s", (post_id,))
        post = cur.fetchone()
        if not post:
            raise HTTPException(status_code=404, detail="Bài viết không tồn tại!")
        if post[0] != user_id:
            raise HTTPException(status_code=403, detail="Bạn không có quyền xoá bài này!")

        cur.execute("DELETE FROM post_likes    WHERE post_id = %s", (post_id,))
        cur.execute("DELETE FROM post_comments WHERE post_id = %s", (post_id,))
        cur.execute("DELETE FROM posts         WHERE id = %s",      (post_id,))
        conn.commit()
        cur.close()

        return {"message": f"Đã xoá bài viết #{post_id}!"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()