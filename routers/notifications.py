"""
routers/notifications.py — Push Notification
"""

import os
import json
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime
from pywebpush import webpush, WebPushException

from database import get_connection
from auth_utils import get_current_user

router = APIRouter(tags=["Notifications"])

VAPID_PRIVATE_KEY   = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY    = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIM_EMAIL   = os.environ.get("VAPID_CLAIM_EMAIL", "mailto:admin@example.com")


# ─────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────

class PushSubscription(BaseModel):
    endpoint: str
    p256dh:   str
    auth:     str


# ─────────────────────────────────────────
# HELPER — Gửi push đến 1 subscription
# ─────────────────────────────────────────

def _send_push(endpoint: str, p256dh: str, auth: str, title: str, body: str) -> bool:
    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth},
            },
            data=json.dumps({"title": title, "body": body}),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_CLAIM_EMAIL},
        )
        return True
    except WebPushException as e:
        print(f"Push thất bại: {e}")
        return False


# ─────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────

@router.get("/notifications/vapid-public-key")
def get_vapid_public_key():
    """Frontend lấy VAPID public key để đăng ký subscription."""
    return {"vapid_public_key": VAPID_PUBLIC_KEY}


@router.post("/notifications/subscribe")
def subscribe(
    sub: PushSubscription,
    current_user: dict = Depends(get_current_user),
):
    """Lưu push subscription của user vào DB."""
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, endpoint) DO UPDATE
                SET p256dh = EXCLUDED.p256dh,
                    auth   = EXCLUDED.auth
            """,
            (user_id, sub.endpoint, sub.p256dh, sub.auth),
        )
        conn.commit()
        cur.close()
        return {"message": "Đăng ký nhận thông báo thành công!"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.delete("/notifications/unsubscribe")
def unsubscribe(current_user: dict = Depends(get_current_user)):
    """Hủy đăng ký nhận thông báo."""
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM push_subscriptions WHERE user_id = %s",
            (user_id,),
        )
        conn.commit()
        cur.close()
        return {"message": "Đã hủy đăng ký thông báo!"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/notifications/check-tasks")
def check_pending_tasks(current_user: dict = Depends(get_current_user)):
    """
    Kiểm tra xem user có task chưa hoàn thành hôm nay không.
    Frontend gọi endpoint này khi user vào app.
    """
    user_id = current_user["id"]
    today   = datetime.now().date()
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) FROM task t
            JOIN planner_day pd ON t.planner_day_id = pd.id
            JOIN planner p      ON pd.planner_id = p.id
            WHERE p.user_id = %s
              AND pd.study_date = %s
              AND t.status = 'pending'
            """,
            (user_id, today),
        )
        pending_count = cur.fetchone()[0]
        cur.close()

        return {
            "has_pending":   pending_count > 0,
            "pending_count": pending_count,
            "date":          str(today),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.post("/notifications/send-reminder")
def send_reminder(current_user: dict = Depends(get_current_user)):
    """
    Gửi push notification nhắc nhở user hoàn thành task.
    Frontend trigger endpoint này khi phát hiện có pending tasks.
    """
    user_id = current_user["id"]
    today   = datetime.now().date()
    conn    = get_connection()
    try:
        cur = conn.cursor()

        # Lấy số task pending
        cur.execute(
            """
            SELECT COUNT(*) FROM task t
            JOIN planner_day pd ON t.planner_day_id = pd.id
            JOIN planner p      ON pd.planner_id = p.id
            WHERE p.user_id = %s
              AND pd.study_date = %s
              AND t.status = 'pending'
            """,
            (user_id, today),
        )
        pending_count = cur.fetchone()[0]

        if pending_count == 0:
            return {"message": "Không có task nào cần nhắc!", "sent": False}

        # Lấy tất cả subscription của user
        cur.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s",
            (user_id,),
        )
        subscriptions = cur.fetchall()
        cur.close()

        if not subscriptions:
            return {"message": "User chưa đăng ký nhận thông báo!", "sent": False}

        # Gửi push đến tất cả thiết bị của user
        title = "📚 Nhắc nhở học tập!"
        body  = f"Bạn còn {pending_count} task chưa hoàn thành hôm nay. Cố lên nhé!"

        sent_count  = 0
        failed_subs = []

        for endpoint, p256dh, auth in subscriptions:
            success = _send_push(endpoint, p256dh, auth, title, body)
            if success:
                sent_count += 1
            else:
                failed_subs.append(endpoint)

        # Xóa subscription hết hạn (endpoint không còn hợp lệ)
        if failed_subs:
            conn2 = get_connection()
            try:
                cur2 = conn2.cursor()
                cur2.execute(
                    "DELETE FROM push_subscriptions WHERE user_id = %s AND endpoint = ANY(%s)",
                    (user_id, failed_subs),
                )
                conn2.commit()
                cur2.close()
            finally:
                conn2.close()

        return {
            "sent":          sent_count > 0,
            "sent_count":    sent_count,
            "pending_tasks": pending_count,
            "message":       f"Đã gửi thông báo đến {sent_count} thiết bị!",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()