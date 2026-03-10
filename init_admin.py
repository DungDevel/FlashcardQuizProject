"""
init_admin.py — Script tạo / nâng cấp admin user
Chạy: python init_admin.py
"""

import os
from datetime import datetime, timedelta
from urllib.parse import urlparse

import bcrypt
from jose import jwt
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY   = os.environ.get("SECRET_KEY", "mysecretkey")
ALGORITHM    = "HS256"


def get_connection():
    import psycopg2
    result = urlparse(DATABASE_URL)
    return psycopg2.connect(
        dbname=result.path.lstrip("/"), user=result.username,
        password=result.password, host=result.hostname, port=result.port,
    )


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def make_token(user_id: int, role: str) -> str:
    return jwt.encode(
        {"id": user_id, "role": role, "exp": datetime.utcnow() + timedelta(days=365)},
        SECRET_KEY, algorithm=ALGORITHM,
    )


def create_admin(username: str, email: str, password: str):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email=%s OR username=%s", (email, username))
    if cur.fetchone():
        print(f"❌ '{username}' hoặc '{email}' đã tồn tại!")
        return

    cur.execute(
        "INSERT INTO users (username, email, password_hash, role, created_at) VALUES (%s,%s,%s,'admin',NOW()) RETURNING id",
        (username, email, hash_password(password)),
    )
    uid = cur.fetchone()[0]
    cur.execute("INSERT INTO user_profile (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (uid,))
    conn.commit()

    print("=" * 55)
    print("✅ TẠO ADMIN THÀNH CÔNG!")
    print(f"   ID: {uid}  |  Username: {username}  |  Email: {email}")
    print(f"🔑 TOKEN:\n{make_token(uid, 'admin')}")
    print("=" * 55)
    cur.close(); conn.close()


def upgrade_to_admin(user_id: int):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT username, email FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    if not user:
        print(f"❌ User ID {user_id} không tồn tại!")
        return

    cur.execute("UPDATE users SET role='admin' WHERE id=%s", (user_id,))
    conn.commit()
    print(f"✅ Nâng cấp '{user[0]}' thành admin!\n🔑 TOKEN:\n{make_token(user_id, 'admin')}")
    cur.close(); conn.close()


def list_users():
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT id, username, email, role FROM users ORDER BY id")
    users = cur.fetchall()
    print(f"\n{'ID':<5} {'Username':<20} {'Email':<30} {'Role'}")
    print("-" * 65)
    for u in users:
        print(f"{u[0]:<5} {u[1]:<20} {u[2]:<30} {u[3]}")
    print(f"\nTổng: {len(users)} users\n")
    cur.close(); conn.close()


if __name__ == "__main__":
    print("🔧 QUẢN LÝ ADMIN\n")
    print("1. Tạo admin mới")
    print("2. Nâng cấp user thành admin")
    print("3. Xem danh sách users")
    choice = input("\nChọn (1/2/3): ").strip()

    if choice == "1":
        username = input("Username: ").strip()
        email    = input("Email: ").strip()
        password = input("Password: ").strip()
        if username and email and password:
            create_admin(username, email, password)
        else:
            print("❌ Nhập đầy đủ thông tin!")

    elif choice == "2":
        list_users()
        uid = input("Nhập User ID: ").strip()
        if uid.isdigit():
            upgrade_to_admin(int(uid))
        else:
            print("❌ ID không hợp lệ!")

    elif choice == "3":
        list_users()
    else:
        print("❌ Lựa chọn không hợp lệ!")