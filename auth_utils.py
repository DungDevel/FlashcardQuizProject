"""
auth_utils.py — JWT Authentication dùng chung
Tất cả routers đều import get_current_user từ file này.
"""

import os
import re
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError

SECRET_KEY = os.environ.get("SECRET_KEY", "mysecretkey")
ALGORITHM  = "HS256"

security = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Xác thực JWT token từ header Authorization: Bearer <token>.
    Trả về payload: {"sub": username, "id": user_id, "role": role}
    """
    try:
        payload = jwt.decode(
            credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM]
        )
        if payload.get("id") is None:
            raise HTTPException(status_code=401, detail="Token không hợp lệ!")
        return payload
    except JWTError:
        raise HTTPException(
            status_code=401,
            detail="Token không hợp lệ hoặc đã hết hạn!",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """
    Dependency dùng cho các endpoint chỉ admin mới truy cập được.
    Dùng: current_user: dict = Depends(require_admin)
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Chỉ admin mới có quyền truy cập!")
    return current_user


# =====================
# VALIDATION HELPERS
# =====================

def validate_password(password: str) -> bool:
    """Mật khẩu >= 6 ký tự, có chữ hoa, chữ thường và số."""
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu phải có ít nhất 6 ký tự!")
    if not re.search(r"[A-Z]", password):
        raise HTTPException(status_code=400, detail="Mật khẩu phải có ít nhất 1 chữ hoa!")
    if not re.search(r"[a-z]", password):
        raise HTTPException(status_code=400, detail="Mật khẩu phải có ít nhất 1 chữ thường!")
    if not re.search(r"\d", password):
        raise HTTPException(status_code=400, detail="Mật khẩu phải có ít nhất 1 chữ số!")
    return True


def validate_username(username: str) -> str:
    """Username: 3–50 ký tự, chỉ a-z, A-Z, 0-9, _ và -."""
    username = username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username không được để trống!")
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username phải có ít nhất 3 ký tự!")
    if " " in username:
        raise HTTPException(status_code=400, detail="Username không được chứa khoảng trắng!")
    if not re.match(r"^[a-zA-Z0-9_-]+$", username):
        raise HTTPException(
            status_code=400,
            detail="Username chỉ được chứa chữ cái, số, gạch dưới (_) và gạch ngang (-)!",
        )
    return username


def validate_email(email: str) -> str:
    """Email hợp lệ theo định dạng chuẩn."""
    email = email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email không được để trống!")
    if len(email) > 254:
        raise HTTPException(status_code=400, detail="Email quá dài!")
    if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email):
        raise HTTPException(
            status_code=400,
            detail="Email không hợp lệ! Ví dụ đúng: example@domain.com",
        )
    return email