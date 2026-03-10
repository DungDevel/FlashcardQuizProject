"""
database.py — Kết nối PostgreSQL tập trung
Tất cả routers đều import từ file này, không ai được tự định nghĩa get_connection() riêng.
"""

import psycopg2
import os
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    """
    Tạo và trả về kết nối đến PostgreSQL từ DATABASE_URL trong .env.
    Sử dụng context manager (with get_connection() as conn) để tự đóng kết nối.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("❌ DATABASE_URL chưa được cấu hình trong file .env!")

    result = urlparse(database_url)
    return psycopg2.connect(
        dbname=result.path.lstrip("/"),
        user=result.username,
        password=result.password,
        host=result.hostname,
        port=result.port,
        sslmode="require",
        connect_timeout=10,
    )


def init_tables():
    """
    Tạo tất cả bảng cần thiết nếu chưa tồn tại.
    Gọi một lần khi khởi động app trong main.py.
    """
    sql = """
    -- =====================
    -- USERS
    -- =====================
    CREATE TABLE IF NOT EXISTS users (
        id              SERIAL PRIMARY KEY,
        email           VARCHAR(255) UNIQUE NOT NULL,
        username        VARCHAR(100) UNIQUE NOT NULL,
        password_hash   TEXT NOT NULL,
        role            VARCHAR(20) DEFAULT 'user',
        last_login      TIMESTAMP,
        created_at      TIMESTAMP DEFAULT NOW()
    );

    -- =====================
    -- USER PROFILE
    -- =====================
    CREATE TABLE IF NOT EXISTS user_profile (
        id                  SERIAL PRIMARY KEY,
        user_id             INTEGER UNIQUE REFERENCES users(id) ON DELETE CASCADE,
        full_name           VARCHAR(255),
        bio                 TEXT,
        study_level         VARCHAR(20) DEFAULT 'Easy',
        study_days          VARCHAR(100),   -- ví dụ: "MON,WED,FRI"
        study_time          TIME,
        reminders_enabled   BOOLEAN DEFAULT FALSE,
        created_at          TIMESTAMP DEFAULT NOW(),
        updated_at          TIMESTAMP DEFAULT NOW()
    );

    -- =====================
    -- DECK
    -- =====================
    CREATE TABLE IF NOT EXISTS deck (
        id          SERIAL PRIMARY KEY,
        name        VARCHAR(255) NOT NULL,
        description TEXT,
        user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,  -- NULL = public deck
        created_at  TIMESTAMP DEFAULT NOW()
    );

    -- =====================
    -- FLASHCARDS
    -- =====================
    CREATE TABLE IF NOT EXISTS flashcards (
        id          SERIAL PRIMARY KEY,
        deck_id     INTEGER REFERENCES deck(id) ON DELETE CASCADE,
        front       VARCHAR(500) NOT NULL,
        back        TEXT NOT NULL,
        verb        VARCHAR(200),   -- phiên âm IPA
        example     TEXT,
        created_at  TIMESTAMP DEFAULT NOW()
    );

    -- =====================
    -- USER FLASHCARD PROGRESS
    -- =====================
    CREATE TABLE IF NOT EXISTS user_flashcard_progress (
        id              SERIAL PRIMARY KEY,
        user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
        flashcard_id    INTEGER REFERENCES flashcards(id) ON DELETE CASCADE,
        deck_id         INTEGER REFERENCES deck(id) ON DELETE CASCADE,
        status          VARCHAR(20) DEFAULT 'new',  -- 'new' | 'done'
        review_count    INTEGER DEFAULT 0,
        last_reviewed   TIMESTAMP,
        updated_at      TIMESTAMP DEFAULT NOW(),
        UNIQUE(user_id, flashcard_id)
    );

    -- =====================
    -- FILE UPLOAD TRACKING (chống trùng lặp)
    -- =====================
    CREATE TABLE IF NOT EXISTS user_uploaded_files (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
        file_hash   VARCHAR(64) NOT NULL,
        created_at  TIMESTAMP DEFAULT NOW(),
        UNIQUE(user_id, file_hash)
    );

    -- =====================
    -- PLANNER
    -- =====================
    CREATE TABLE IF NOT EXISTS planner (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
        week_start  DATE NOT NULL,
        week_end    DATE NOT NULL,
        created_at  TIMESTAMP DEFAULT NOW()
    );

    -- =====================
    -- PLANNER DAY
    -- =====================
    CREATE TABLE IF NOT EXISTS planner_day (
        id          SERIAL PRIMARY KEY,
        planner_id  INTEGER REFERENCES planner(id) ON DELETE CASCADE,
        study_date  DATE NOT NULL,
        day_of_week VARCHAR(10),
        status      VARCHAR(20) DEFAULT 'pending',  -- 'pending' | 'completed'
        updated_at  TIMESTAMP DEFAULT NOW()
    );

    -- =====================
    -- TASK
    -- =====================
    CREATE TABLE IF NOT EXISTS task (
        id              SERIAL PRIMARY KEY,
        planner_day_id  INTEGER REFERENCES planner_day(id) ON DELETE CASCADE,
        task_type       VARCHAR(50),    -- 'flashcard' | 'quiz'
        title           VARCHAR(255),
        description     TEXT,
        total_required  INTEGER DEFAULT 10,
        progress_count  INTEGER DEFAULT 0,
        status          VARCHAR(20) DEFAULT 'pending',  -- 'pending' | 'completed'
        updated_at      TIMESTAMP DEFAULT NOW()
    );

    -- =====================
    -- PASSWORD RESET CODES
    -- =====================
    CREATE TABLE IF NOT EXISTS password_reset_codes (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
        email       VARCHAR(255) NOT NULL,
        reset_code  VARCHAR(6) NOT NULL,
        expires_at  TIMESTAMP NOT NULL,
        created_at  TIMESTAMP DEFAULT NOW()
    );

    -- =====================
    -- SOCIAL — POSTS
    -- =====================
    CREATE TABLE IF NOT EXISTS posts (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
        content     TEXT NOT NULL,
        post_type   VARCHAR(50) DEFAULT 'general',
        visibility  VARCHAR(20) DEFAULT 'public',
        image_url   TEXT,
        created_at  TIMESTAMP DEFAULT NOW(),
        updated_at  TIMESTAMP DEFAULT NOW()
    );

    -- =====================
    -- SOCIAL — LIKES
    -- =====================
    CREATE TABLE IF NOT EXISTS post_likes (
        id          SERIAL PRIMARY KEY,
        post_id     INTEGER REFERENCES posts(id) ON DELETE CASCADE,
        user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
        created_at  TIMESTAMP DEFAULT NOW(),
        UNIQUE(post_id, user_id)
    );

    -- =====================
    -- SOCIAL — COMMENTS
    -- =====================
    CREATE TABLE IF NOT EXISTS post_comments (
        id          SERIAL PRIMARY KEY,
        post_id     INTEGER REFERENCES posts(id) ON DELETE CASCADE,
        user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
        content     TEXT NOT NULL,
        created_at  TIMESTAMP DEFAULT NOW()
    );

    -- =====================
    -- INDEXES (tăng tốc truy vấn)
    -- =====================
    CREATE INDEX IF NOT EXISTS idx_flashcards_deck_id         ON flashcards(deck_id);
    CREATE INDEX IF NOT EXISTS idx_progress_user_deck         ON user_flashcard_progress(user_id, deck_id);
    CREATE INDEX IF NOT EXISTS idx_planner_user_week          ON planner(user_id, week_start);
    CREATE INDEX IF NOT EXISTS idx_planner_day_planner        ON planner_day(planner_id);
    CREATE INDEX IF NOT EXISTS idx_task_planner_day           ON task(planner_day_id);
    CREATE INDEX IF NOT EXISTS idx_reset_code_email           ON password_reset_codes(email, reset_code);
    CREATE INDEX IF NOT EXISTS idx_posts_user                 ON posts(user_id);
    """

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
        cur.close()
        print("✅ Tất cả bảng đã được khởi tạo thành công!")
    except Exception as e:
        conn.rollback()
        print(f"❌ Lỗi khởi tạo bảng: {e}")
        raise
    finally:
        conn.close()