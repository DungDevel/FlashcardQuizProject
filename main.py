"""
main.py — Entry point của Flashcard API
Chứa: Auth (register, login, profile, forgot/reset password)
Tất cả routers khác được import từ thư mục routers/
"""

import os
import re
import random
import string
import smtplib
from typing import Optional
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from fastapi import FastAPI, HTTPException, Depends, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, constr
from jose import jwt
from passlib.context import CryptContext
from dotenv import load_dotenv

load_dotenv()

# ─── Routers ───
from routers.deck             import router as deck_router,      init_public_decks
from routers.flashcard        import router as flashcard_router
from routers.quizz            import router as quiz_router,      ensure_quiz_tables
from routers.planner          import router as planner_router
from routers.social           import router as social_router
from routers.dashboard_users  import router as dashboard_router
from routers.admin            import router as admin_router

# ─── Shared utils ───
from database    import get_connection, init_tables
from auth_utils  import get_current_user, validate_email, validate_username, validate_password

# ─────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────
app = FastAPI(
    title="Flashcard API",
    version="2.0",
    description=(
        "Backend API cho ứng dụng học flashcard tiếng Anh. "
        "Tính năng: Auth JWT, Deck, Flashcard (AI), Quiz (AI + SRS), Planner, Social, Dashboard, Admin."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "https://capstone1-ce77-api.onrender.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Gắn routers
app.include_router(deck_router)
app.include_router(flashcard_router)
app.include_router(quiz_router)
app.include_router(planner_router)
app.include_router(social_router)
app.include_router(dashboard_router)
app.include_router(admin_router)


# ─────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    try:
        init_tables()         # Tạo tất cả bảng
        ensure_quiz_tables()  # Thêm bảng quiz & user_quiz_progress
        init_public_decks()   # Seed 10 public decks
        print("✅ Khởi động thành công!")
    except Exception as e:
        print(f"⚠️ Lỗi khởi động: {e}")
        print("Service vẫn chạy, database sẽ kết nối khi cần.")


# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
SECRET_KEY                  = os.environ.get("SECRET_KEY", "mysecretkey")
ALGORITHM                   = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 1 ngày

SMTP_SERVER    = "smtp.gmail.com"
SMTP_PORT      = 587
EMAIL_USER     = os.environ.get("EMAIL_USER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def send_reset_email(email: str, reset_code: str) -> bool:
    try:
        msg            = MIMEMultipart()
        msg["From"]    = EMAIL_USER
        msg["To"]      = email
        msg["Subject"] = "Mã xác thực đặt lại mật khẩu - Flashcard App"
        msg.attach(MIMEText(
            f"""<html><body>
            <p>Xin chào,</p>
            <p> Chúng tôi đã nhận được yêu cầu đặt lại mật khẩu cho tài khoản của bạn. Vui lòng sử dụng mã xác thực bên dưới để tiếp tục: </p> <span style="font-size: 28px; font-weight: bold; color: #007bff;"> {reset_code} </span>
            <p> Mã xác thực này sẽ hết hạn sau <strong>10 phút</strong>. </p>
            <p> Nếu bạn không thực hiện yêu cầu này, vui lòng bỏ qua email. Tài khoản của bạn vẫn được bảo mật. </p>
            <p>Trân trọng,<br/>Đội ngũ hỗ trợ</p>
            </body></html>""",
            "html",
        ))
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"⚠️ Lỗi gửi email: {e}")
        return False


# ─────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────
class UserRegister(BaseModel):
    email:    str
    username: str
    password: constr(max_length=72)


class UserLogin(BaseModel):
    username: str
    password: constr(max_length=72)


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email:        str
    reset_code:   str
    new_password: constr(max_length=72)


# ─────────────────────────────────────────
# AUTH ENDPOINTS
# ─────────────────────────────────────────

@app.get("/", tags=["Health"])
def root():
    return {"message": "Flashcard API đang chạy!", "version": "2.0", "docs": "/docs"}


@app.post("/register", tags=["Authentication"])
def register(user: UserRegister):
    """Đăng ký tài khoản mới."""
    email    = validate_email(user.email)
    username = validate_username(user.username)
    validate_password(user.password)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = %s OR username = %s", (email, username))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Email hoặc Username đã tồn tại!")

        hashed_pw = hash_password(user.password)
        cur.execute(
            "INSERT INTO users (email, username, password_hash, role) VALUES (%s,%s,%s,'user') RETURNING id",
            (email, username, hashed_pw),
        )
        new_id = cur.fetchone()[0]

        # Tạo profile mặc định
        cur.execute(
            "INSERT INTO user_profile (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (new_id,),
        )
        conn.commit()
        cur.close()

        return {"message": "Đăng ký thành công!", "user_id": new_id}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@app.post("/login", tags=["Authentication"])
def login(user: UserLogin):
    """Đăng nhập, trả về JWT token."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username = %s",
            (user.username.strip(),),
        )
        db_user = cur.fetchone()

        if not db_user or not verify_password(user.password, db_user[2]):
            raise HTTPException(status_code=401, detail="Sai username hoặc mật khẩu!")

        user_id, username, _, role = db_user

        token = create_access_token({"sub": username, "id": user_id, "role": role})

        cur.execute("UPDATE users SET last_login = NOW() WHERE id = %s", (user_id,))
        conn.commit()
        cur.close()

        return {"access_token": token, "token_type": "bearer"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@app.get("/verify-token", tags=["Authentication"])
def verify_token(current_user: dict = Depends(get_current_user)):
    """Kiểm tra token còn hợp lệ không."""
    return {"message": "Token hợp lệ!", "user": current_user}


@app.get("/my-profile", tags=["Authentication"])
def get_profile(current_user: dict = Depends(get_current_user)):
    """Lấy hồ sơ cá nhân."""
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT u.id, u.username, u.email, u.role, u.created_at,
                   up.full_name, up.bio, up.study_level, up.study_days, up.study_time, up.reminders_enabled
            FROM users u
            LEFT JOIN user_profile up ON u.id = up.user_id
            WHERE u.id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="User không tồn tại!")

    return {
        "id": row[0], "username": row[1], "email": row[2], "role": row[3],
        "created_at": row[4].strftime("%Y-%m-%d %H:%M:%S") if row[4] else None,
        "profile": {
            "full_name":         row[5],
            "bio":               row[6],
            "study_level":       row[7],
            "study_days":        row[8],
            "study_time":        str(row[9]) if row[9] else None,
            "reminders_enabled": row[10],
        },
    }


@app.put("/edit-profile", tags=["Authentication"])
def edit_profile(
    full_name:         Optional[str]  = Form(None),
    bio:               Optional[str]  = Form(None),
    study_level:       Optional[str]  = Form(None, description="Easy | Medium | Hard"),
    study_days:        Optional[str]  = Form(None, description="MON,WED,FRI"),
    study_time:        Optional[str]  = Form(None, description="HH:MM"),
    reminders_enabled: Optional[bool] = Form(None),
    current_user: dict = Depends(get_current_user),
):
    """Cập nhật hồ sơ cá nhân."""
    if study_level and study_level not in ("Easy", "Medium", "Hard"):
        raise HTTPException(status_code=400, detail="study_level phải là Easy | Medium | Hard!")

    user_id = current_user["id"]

    # Chuẩn hoá study_days
    study_days_str = None
    if study_days:
        valid_days = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
        days = [d.strip().upper() for d in study_days.split(",")]
        invalid = [d for d in days if d not in valid_days]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Ngày không hợp lệ: {invalid}")
        study_days_str = ",".join(days)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM user_profile WHERE user_id = %s", (user_id,))
        exists = cur.fetchone()

        if exists:
            cur.execute(
                """
                UPDATE user_profile
                SET full_name         = COALESCE(%s, full_name),
                    bio               = COALESCE(%s, bio),
                    study_level       = COALESCE(%s, study_level),
                    study_days        = COALESCE(%s, study_days),
                    study_time        = COALESCE(%s::TIME, study_time),
                    reminders_enabled = COALESCE(%s, reminders_enabled),
                    updated_at        = NOW()
                WHERE user_id = %s
                """,
                (full_name, bio, study_level, study_days_str, study_time, reminders_enabled, user_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO user_profile (user_id, full_name, bio, study_level, study_days, study_time, reminders_enabled)
                VALUES (%s,%s,%s,%s,%s,%s::TIME,%s)
                """,
                (user_id, full_name, bio, study_level, study_days_str, study_time, reminders_enabled),
            )

        conn.commit()
        cur.close()

        return {"message": "Cập nhật hồ sơ thành công!"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ─────────────────────────────────────────
# PASSWORD RESET
# ─────────────────────────────────────────

@app.post("/forgot-password", tags=["Authentication"])
def forgot_password(request: ForgotPasswordRequest):
    """Gửi mã OTP về email để reset mật khẩu."""
    email = validate_email(request.email)
    conn  = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="Email không tồn tại trong hệ thống!")

        user_id    = user[0]
        reset_code = "".join(random.choices(string.digits, k=6))
        expires_at = datetime.utcnow() + timedelta(minutes=10)

        cur.execute("DELETE FROM password_reset_codes WHERE user_id = %s", (user_id,))
        cur.execute(
            "INSERT INTO password_reset_codes (user_id, email, reset_code, expires_at) VALUES (%s,%s,%s,%s)",
            (user_id, email, reset_code, expires_at),
        )
        conn.commit()
        cur.close()
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

    if not send_reset_email(email, reset_code):
        raise HTTPException(status_code=500, detail="Không thể gửi email! Kiểm tra cấu hình SMTP.")

    return {"message": f"Mã xác thực đã gửi đến {email}. Mã hết hạn sau 10 phút."}


@app.post("/reset-password", tags=["Authentication"])
def reset_password(request: ResetPasswordRequest):
    """Đặt lại mật khẩu bằng OTP."""
    email = validate_email(request.email)
    validate_password(request.new_password)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, expires_at FROM password_reset_codes WHERE email = %s AND reset_code = %s",
            (email, request.reset_code),
        )
        record = cur.fetchone()

        if not record:
            raise HTTPException(status_code=400, detail="Mã xác thực không đúng!")

        user_id, expires_at = record
        if datetime.utcnow() > expires_at:
            cur.execute("DELETE FROM password_reset_codes WHERE user_id = %s", (user_id,))
            conn.commit()
            raise HTTPException(status_code=400, detail="Mã xác thực đã hết hạn! Vui lòng yêu cầu mã mới.")

        new_hash = hash_password(request.new_password)
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (new_hash, user_id))
        cur.execute("DELETE FROM password_reset_codes WHERE user_id = %s", (user_id,))
        conn.commit()
        cur.close()

        return {"message": "Mật khẩu đã được đặt lại thành công!"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()