import json
import os
import re
import httpx

from datetime import datetime

from database import get_connection

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

GROQ_MODEL = "llama-3.3-70b-versatile"


async def generate_and_store_prediction(
    user_id: int,
    learning_data: dict
):

    if not GROQ_API_KEY:
        return

    prompt = f"""
You are an advanced AI learning coach.

Analyze this learner's study behavior.

Return ONLY valid JSON.

DATA:
{json.dumps(learning_data, ensure_ascii=False, indent=2)}

JSON FORMAT:
{{
  "summary": "...",
  "weekly": {{
    "flashcards_expected": 0,
    "quiz_accuracy_expected": 0,
    "tasks_completion_expected": 0,
    "highlights": [],
    "risks": []
  }},
  "monthly": {{
    "flashcards_expected": 0,
    "quiz_accuracy_expected": 0,
    "mastery_rate_expected": 0,
    "highlights": [],
    "risks": []
  }},
  "recommendations": [],
  "confidence": "low"
}}
"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
    "model": GROQ_MODEL,
    "messages": [
        {
            "role": "system",
            "content": """
        You are a JSON API.

        Return ONLY valid JSON.
        No markdown.
        """
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.2,
            "max_tokens": 1000,
            "response_format": {
                "type": "json_object"
            }
        }

    prediction = None

    # =========================
    # RETRY 3 LẦN
    # =========================

    for _ in range(3):

        try:

            async with httpx.AsyncClient(timeout=60) as client:

                response = await client.post(
                    GROQ_API_URL,
                    headers=headers,
                    json=payload
                )

            if response.status_code != 200:
                continue

            ai_text = (
                response.json()["choices"][0]["message"]["content"]
                .strip()
            )
            print("RAW AI RESPONSE:")
            print(ai_text)

            # =========================
            # SAFE JSON PARSE
            # =========================

            try:
                cleaned = (
                    ai_text
                    .replace("```json", "")
                    .replace("```", "")
                    .strip()
                )

                # Remove invalid control chars
                cleaned = cleaned.replace("\n", " ")
                cleaned = cleaned.replace("\r", " ")
                cleaned = cleaned.replace("\t", " ")

                prediction = json.loads(cleaned)

            except Exception as parse_error:

                print("JSON PARSE ERROR:", parse_error)
                print("CLEANED AI TEXT:", cleaned)

                match = re.search(r"\{.*\}", ai_text, re.S)

                if match:
                    try:
                        prediction = json.loads(match.group(0))
                    except Exception as second_error:
                        print("SECOND JSON ERROR:", second_error)
                        prediction = None

                match = re.search(r"\{.*\}", ai_text, re.S)

                if match:
                    try:
                        prediction = json.loads(
                            match.group(0)
                        )
                    except Exception:
                        prediction = None

            if prediction:
                required_keys = [
                    "summary",
                    "weekly",
                    "monthly",
                    "recommendations",
                    "confidence"
                ]

                if not all(k in prediction for k in required_keys):
                    prediction = None

        except Exception as e:
            print("AI generation error:", e)

    # =========================
    # FALLBACK
    # =========================

    if not prediction:

        prediction = {
            "summary": "AI analysis temporarily unavailable.",
            "weekly": {
                "flashcards_expected": 0,
                "quiz_accuracy_expected": 0,
                "tasks_completion_expected": 0,
                "highlights": [],
                "risks": []
            },
            "monthly": {
                "flashcards_expected": 0,
                "quiz_accuracy_expected": 0,
                "mastery_rate_expected": 0,
                "highlights": [],
                "risks": []
            },
            "recommendations": [
                "Continue learning consistently."
            ],
            "confidence": "low"
        }

    # =========================
    # SAVE TO DATABASE
    # =========================

    conn = get_connection()

    try:

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO ai_predictions (
                user_id,
                prediction,
                generated_at
            )

            VALUES (%s, %s, NOW())

            ON CONFLICT (user_id)

            DO UPDATE SET
                prediction = EXCLUDED.prediction,
                generated_at = NOW()
        """, (
            user_id,
            json.dumps(prediction)
        ))

        conn.commit()

        cur.close()

    except Exception as e:

        print("Save AI prediction error:", e)

    finally:

        conn.close()