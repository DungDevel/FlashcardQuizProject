"""
routers/flashcard.py — Quản lý Flashcard (AI generate + progress)
Endpoints:
  POST   /flashcards/generate          — Tạo flashcard từ file (AI Groq)
  GET    /flashcards/by-deck           — Lấy tất cả flashcard theo deck
  POST   /flashcards/progress/update   — Cập nhật trạng thái học
  GET    /flashcards/progress/by-deck  — Lấy tiến độ theo deck
  DELETE /flashcards/delete            — Xoá flashcard
"""

import os
import re
import json
import hashlib
from io import BytesIO
from datetime import datetime, timedelta

import httpx
import fitz  # PyMuPDF
from docx import Document
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from database import get_connection
from auth_utils import get_current_user
import base64
from PIL import Image
import io

router = APIRouter(tags=["Flashcard"])

# ===== Config =====
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.1-8b-instant"   # Model Groq ổn định

BANNED_WORDS = [
    "sex", "porn", "rape", "terrorist", "fuck", "shit", "bitch",
    "nigger", "whore", "suicide", "weapon",
]


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def extract_text(file_bytes: bytes, filename: str) -> str:
    """Trích xuất text từ .docx / .pdf / .txt."""
    name = filename.lower()

    if name.endswith(".docx"):
        doc = Document(BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs).strip()

    if name.endswith(".pdf"):
        pdf  = fitz.open(stream=file_bytes, filetype="pdf")
        text = "\n".join(page.get_text("text") for page in pdf)
        pdf.close()

        # PDF có text bình thường → trả về luôn
        if len(text.strip().split()) >= 20:
            return text.strip()

        # PDF dạng ảnh → trả về None để fallback sang Vision
        return None

    if name.endswith(".txt"):
        return file_bytes.decode("utf-8-sig", errors="ignore").strip()

    raise HTTPException(status_code=400, detail="Chỉ hỗ trợ file .docx, .pdf, .txt!")

async def extract_text_from_image_pdf(file_bytes: bytes, max_pages: int = 8) -> str:
    """
    Gộp tất cả trang PDF thành 1 ảnh duy nhất
    → gọi Groq Vision 1 lần → trả về text.
    """
    pdf    = fitz.open(stream=file_bytes, filetype="pdf")
    total  = min(len(pdf), max_pages)
    images = []

    # ── Bước A: Render từng trang thành ảnh ──
    for i in range(total):
        pix = pdf[i].get_pixmap(dpi=100)          # dpi=100 → ảnh nhỏ, đủ đọc
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        images.append(img)
    pdf.close()

    if not images:
        raise HTTPException(status_code=400, detail="Không render được trang PDF nào!")

    # ── Bước B: Ghép tất cả ảnh theo chiều dọc ──
    total_width  = max(img.width  for img in images)
    total_height = sum(img.height for img in images)
    combined     = Image.new("RGB", (total_width, total_height), "white")

    y_offset = 0
    for img in images:
        combined.paste(img, (0, y_offset))
        y_offset += img.height

    # ── Bước C: Convert ảnh gộp sang base64 ──
    buf = io.BytesIO()
    combined.save(buf, format="PNG", optimize=True)
    img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    # ── Bước D: Gọi Groq Vision 1 lần duy nhất ──
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{img_b64}",
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Extract ALL text from this image exactly as it appears. "
                            "Return only the raw text, no explanations, no markdown."
                        ),
                    },
                ],
            }
        ],
        "max_tokens": 2048,
        "temperature": 0.1,
    }

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(GROQ_API_URL, headers=headers, json=payload)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Groq Vision lỗi: {resp.text}")

    return resp.json()["choices"][0]["message"]["content"].strip()

def compute_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def has_banned_words(text: str) -> bool:
    pattern = r"\b(" + "|".join(re.escape(w) for w in BANNED_WORDS) + r")\b"
    return bool(re.search(pattern, text, re.IGNORECASE))


def get_existing_fronts(deck_id: int) -> list[str]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT front FROM flashcards WHERE deck_id = %s", (deck_id,))
        return [r[0].lower() for r in cur.fetchall()]
    finally:
        conn.close()


def update_task_progress(conn, cur, user_id: int, deck_id: int):
    """Tự động cập nhật task planner khi flashcard được đánh dấu done."""
    try:
        today   = datetime.now().date()
        monday  = today - timedelta(days=today.weekday())
        sunday  = monday + timedelta(days=6)

        cur.execute(
            """
            SELECT t.id, t.total_required, pd.id
            FROM task t
            JOIN planner_day pd ON t.planner_day_id = pd.id
            JOIN planner p ON pd.planner_id = p.id
            WHERE p.user_id = %s
              AND t.task_type = 'flashcard'
              AND t.status = 'pending'
              AND pd.study_date BETWEEN %s AND %s
              AND pd.study_date <= %s
            ORDER BY pd.study_date ASC
            LIMIT 1
            """,
            (user_id, monday, sunday, today),
        )
        task = cur.fetchone()
        if not task:
            return

        task_id, total_required, planner_day_id = task

        cur.execute(
            """
            SELECT COUNT(*) FROM user_flashcard_progress
            WHERE user_id = %s AND deck_id = %s AND status = 'done'
            """,
            (user_id, deck_id),
        )
        done_count = cur.fetchone()[0]

        new_status = "completed" if done_count >= total_required else "pending"
        cur.execute(
            "UPDATE task SET progress_count = %s, status = %s, updated_at = NOW() WHERE id = %s",
            (done_count, new_status, task_id),
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
    except Exception as e:
        print(f"⚠️ Lỗi cập nhật task progress: {e}")


# ─────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────

@router.post("/flashcards/generate")
async def generate_flashcards(
    deck_id: int = Form(...),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Đọc file → gọi Groq AI → lưu flashcard vào deck."""
    user_id = current_user["id"]

    # 1. Kiểm tra deck tồn tại
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM deck WHERE id = %s", (deck_id,))
        deck = cur.fetchone()
    finally:
        conn.close()

    if not deck:
        raise HTTPException(status_code=404, detail="Deck không tồn tại!")
    deck_name = deck[1]

    # 2. Đọc file
    file_bytes = await file.read()
    file_hash  = compute_hash(file_bytes)

    # 3. Kiểm tra trùng lặp
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM user_uploaded_files WHERE user_id = %s AND file_hash = %s",
            (user_id, file_hash),
        )
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="File này bạn đã upload trước đó!")

        cur.execute(
            "INSERT INTO user_uploaded_files (user_id, file_hash) VALUES (%s, %s)",
            (user_id, file_hash),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()

    # 4. Trích xuất text
    text = extract_text(file_bytes, file.filename)

    # PDF dạng ảnh → dùng Groq Vision
    if text is None:
        text = await extract_text_from_image_pdf(file_bytes, max_pages=8)

    if not text or len(text.strip()) == 0:
        raise HTTPException(
            status_code=400,
            detail="Không đọc được nội dung trong file!"
        )
    if len(text.split()) < 10:
        raise HTTPException(
            status_code=400,
            detail="File quá ngắn (cần ít nhất 10 từ)!"
        )
    if has_banned_words(text):
        raise HTTPException(
            status_code=400,
            detail="File chứa nội dung không phù hợp!"
        )

    # 5. Build prompt
    existing = ", ".join(get_existing_fronts(deck_id)[:100])
    prompt = f"""
        You are an expert English vocabulary flashcard generator.
        Your output must be ONLY a valid JSON array. No steps, no explanations, no markdown.

        Deck topic: {deck_name}

        Existing words (DO NOT include these words again):
        {existing}

        TASK:
        Extract English vocabulary from the document that is strongly related to the deck topic.

        RULES:
        - ONLY use words that appear in the document
        - DO NOT invent new words
        - Prefer academic, technical, formal, or topic-related vocabulary
        - Skip very common/basic English words (the, is, and, a, of, to, in, for...)
        - Skip proper nouns (Google, Microsoft, Chrome...)
        - Skip programming tool names (ReactJS, FastAPI, PostgreSQL...)

        STRICT DUPLICATE RULES — before outputting, ensure:
        1. No two cards have the same "front" value (even different forms: analyze/analysis)
        2. No two cards have the same "back" value (even synonyms with same Vietnamese meaning)
        3. No word from Existing words list appears in "front"

        FLASHCARD FORMAT — each object must have exactly these 4 keys:
        - "front": English word (base form)
        - "back": Vietnamese meaning (must be unique across all cards)
        - "ipa": IPA pronunciation
        - "example": one natural English sentence using the word

        CRITICAL — OUTPUT FORMAT:
        - Start your response with [ and end with ]
        - Do NOT write any steps, reasoning, or explanation
        - Do NOT write "Step 1", "Step 2" or any similar text
        - Do NOT use markdown code blocks
        - Return ONLY the raw JSON array

        EXAMPLE OUTPUT:
        [
        {{
            "front": "Algorithm",
            "back": "Thuật toán",
            "ipa": "/ˈælɡərɪðəm/",
            "example": "The algorithm sorts data efficiently."
        }},
        {{
            "front": "Retention",
            "back": "Khả năng ghi nhớ",
            "ipa": "/rɪˈtenʃən/",
            "example": "Spaced repetition improves long-term retention."
        }}
        ]

        DOCUMENT:
        {text[:8000]}
        """

    # 6. Gọi Groq API
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY chưa được cấu hình!")

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(GROQ_API_URL, headers=headers, json=payload)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Groq API lỗi: {resp.text}")

    ai_text = resp.json()["choices"][0]["message"]["content"].strip()

    # 7. Parse JSON từ AI
    cleaned = ai_text.strip()

    # fix lỗi back=" thành back":
    cleaned = re.sub(r'("back")=', r'\1:', cleaned)

    # remove markdown
    cleaned = cleaned.replace("```json", "").replace("```", "")

    flashcards = []

    try:
        flashcards = json.loads(cleaned)
    except Exception:
        match = re.search(r"\[.*\]", cleaned, re.S)
        if match:
            try:
                flashcards = json.loads(match.group(0))
            except:
                flashcards = []

    if not flashcards:
        print("AI RAW RESPONSE:\n", ai_text)
        raise HTTPException(status_code=400, detail="AI không tạo được flashcard nào!")
    
    # Sau khi parse JSON từ AI, thêm đoạn này trước khi lưu DB

    # Loại bỏ duplicate front (từ vựng)
    seen_fronts   = set()
    seen_backs    = set()
    unique_cards  = []

    for card in flashcards:
        front = card.get("front", "").strip().lower()
        back  = card.get("back",  "").strip()

        # Bỏ qua nếu từ hoặc nghĩa đã xuất hiện
        if front in seen_fronts:
            continue
        if back in seen_backs:
            continue

        seen_fronts.add(front)
        seen_backs.add(back)
        unique_cards.append(card)

    flashcards = unique_cards

    if not flashcards:
        raise HTTPException(status_code=400, detail="AI không tạo được flashcard nào!")

    # 8. Lưu vào DB
    conn = get_connection()
    try:
        cur = conn.cursor()
        for card in flashcards:
            cur.execute(
                "INSERT INTO flashcards (deck_id, front, back, verb, example) VALUES (%s, %s, %s, %s, %s)",
                (
                    deck_id,
                    card.get("front", ""),
                    card.get("back", ""),
                    card.get("ipa", card.get("verb", "")),  # ← fix key ipa
                    card.get("example", ""),
                ),
            )
        conn.commit()
        cur.close()
    finally:
        conn.close()

    return {"message": f"Tạo {len(flashcards)} flashcard thành công!", "deck_id": deck_id, "count": len(flashcards)}


# ─────────────────────────────────────────

@router.get("/flashcards/by-deck")
def get_flashcards_by_deck(deck_id: int, current_user: dict = Depends(get_current_user)):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, front, back, verb, example FROM flashcards WHERE deck_id = %s ORDER BY id ASC",
            (deck_id,),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    data = [{"id": r[0], "front": r[1], "back": r[2], "verb": r[3], "example": r[4]} for r in rows]
    return {"message": f"Lấy {len(data)} flashcard thành công!", "deck_id": deck_id, "data": data}


# ─────────────────────────────────────────

@router.post("/flashcards/progress/update")
def update_progress(
    flashcard_id: int = Form(...),
    deck_id: int = Form(...),
    status: str = Form(..., description="'new' hoặc 'done'"),
    current_user: dict = Depends(get_current_user),
):
    if status not in ("new", "done"):
        raise HTTPException(status_code=400, detail="Status chỉ chấp nhận 'new' hoặc 'done'!")

    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()

        # Kiểm tra flashcard thuộc deck
        cur.execute("SELECT id FROM flashcards WHERE id = %s AND deck_id = %s", (flashcard_id, deck_id))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Flashcard không tồn tại trong deck này!")

        # Lấy trạng thái cũ
        cur.execute(
            "SELECT id, status FROM user_flashcard_progress WHERE user_id = %s AND flashcard_id = %s",
            (user_id, flashcard_id),
        )
        existing = cur.fetchone()
        old_status = existing[1] if existing else "new"

        if existing:
            cur.execute(
                """
                UPDATE user_flashcard_progress
                SET status = %s, review_count = review_count + 1, last_reviewed = NOW(), updated_at = NOW()
                WHERE id = %s
                """,
                (status, existing[0]),
            )
        else:
            cur.execute(
                """
                INSERT INTO user_flashcard_progress (user_id, flashcard_id, deck_id, status, review_count, last_reviewed)
                VALUES (%s, %s, %s, %s, 1, NOW())
                """,
                (user_id, flashcard_id, deck_id, status),
            )

        # Cập nhật task planner nếu chuyển new → done
        task_updated = False
        if old_status == "new" and status == "done":
            update_task_progress(conn, cur, user_id, deck_id)
            task_updated = True

        conn.commit()
        cur.close()

        return {
            "message": f"Flashcard #{flashcard_id} đã cập nhật thành '{status}'!",
            "task_updated": task_updated,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ─────────────────────────────────────────

@router.get("/flashcards/progress/by-deck")
def get_progress_by_deck(deck_id: int, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT f.id, f.front, f.back, f.verb, f.example,
                   COALESCE(p.status, 'new') AS status
            FROM flashcards f
            LEFT JOIN user_flashcard_progress p
                   ON f.id = p.flashcard_id AND p.user_id = %s
            WHERE f.deck_id = %s
            ORDER BY f.id ASC
            """,
            (user_id, deck_id),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    done  = sum(1 for r in rows if r[5] == "done")
    total = len(rows)
    data  = [{"id": r[0], "front": r[1], "back": r[2], "verb": r[3], "example": r[4], "status": r[5]} for r in rows]

    return {
        "deck_id": deck_id,
        "total": total,
        "done": done,
        "progress_percent": round(done / total * 100, 2) if total else 0,
        "data": data,
    }


# ─────────────────────────────────────────

@router.delete("/flashcards/delete")
def delete_flashcard(flashcard_id: int, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn    = get_connection()
    try:
        cur = conn.cursor()

        # Chỉ owner của deck mới xoá được
        cur.execute(
            """
            SELECT f.id, d.user_id
            FROM flashcards f
            JOIN deck d ON f.deck_id = d.id
            WHERE f.id = %s
            """,
            (flashcard_id,),
        )
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Flashcard không tồn tại!")
        if result[1] != user_id:
            raise HTTPException(status_code=403, detail="Bạn không có quyền xoá flashcard này!")

        cur.execute("DELETE FROM user_flashcard_progress WHERE flashcard_id = %s", (flashcard_id,))
        cur.execute("DELETE FROM flashcards WHERE id = %s", (flashcard_id,))
        conn.commit()
        cur.close()

        return {"message": f"Đã xoá flashcard ID {flashcard_id} thành công!"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()