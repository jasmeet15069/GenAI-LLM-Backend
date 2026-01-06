import os
import re
import io
import base64
import json
import mysql.connector

from flask import Flask, send_from_directory
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
from gtts import gTTS
from groq import Groq

# ================= ENV =================
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

if not GROQ_API_KEY:
    raise RuntimeError("Missing GROQ_API_KEY")

# ================= APP =================
app = Flask(__name__, static_folder="static")

# IMPORTANT: eventlet REMOVED
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

llm = Groq(api_key=GROQ_API_KEY)

# ================= PROMPTS =================
SYSTEM_PROMPT = """
You are JARVIS.
Short. Direct. Precise.
No emojis. No filler.
"""

MEMORY_EXTRACT_PROMPT = """
Extract long-term user memory from the text below.
Only extract stable preferences or facts.

Return STRICT JSON:
{
  "key": "...",
  "value": "..."
}

If nothing should be saved, return:
null
"""

# ================= MYSQL =================
def db():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        ssl_verify_cert=False,
        ssl_verify_identity=False,
    )

def init_db():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS jarvis_memory (
                id INT AUTO_INCREMENT PRIMARY KEY,
                memory_key TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS jarvis_chat_history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_message TEXT NOT NULL,
                assistant_reply TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

init_db()

# ================= STATE =================
last_user_message = None

# ================= HELPERS =================
def detect_lang(text):
    return "hi" if re.search(r"[\u0900-\u097F]", text) else "en"

def tts_bytes(text, lang):
    tts = gTTS(text=text, lang="hi" if lang == "hi" else "en")
    buf = io.BytesIO()
    tts.write_to_fp(buf)
    return buf.getvalue()

def is_save_command(t):
    t = t.lower()
    return any(x in t for x in ["save it", "remember this", "store it", "save in memory"])

def is_forget_command(t):
    return t.lower().startswith("forget ")

def extract_forget_key(t):
    return t.lower().replace("forget", "", 1).strip()

# ================= MEMORY =================
def extract_memory_llm(text):
    r = llm.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": MEMORY_EXTRACT_PROMPT},
            {"role": "user", "content": text}
        ],
        temperature=0
    )

    raw = r.choices[0].message.content.strip()
    if raw == "null":
        return None

    try:
        return json.loads(raw)
    except:
        return None

def save_memory(key, value):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO jarvis_memory (memory_key, memory_value) VALUES (%s,%s)",
            (key.lower(), value)
        )
        conn.commit()

def delete_memory_by_key(keyword):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM jarvis_memory WHERE memory_key LIKE %s",
            (f"%{keyword.lower()}%",)
        )
        conn.commit()

def find_memory(query):
    with db() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT memory_value
            FROM jarvis_memory
            WHERE %s LIKE CONCAT('%', memory_key, '%')
            ORDER BY created_at DESC
            LIMIT 1
        """, (query.lower(),))
        row = cur.fetchone()
        return row["memory_value"] if row else None

def list_memories():
    with db() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT memory_key, memory_value, created_at
            FROM jarvis_memory
            ORDER BY created_at DESC
        """)
        return cur.fetchall()

# ================= CHAT HISTORY =================
def save_chat(user, assistant):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO jarvis_chat_history (user_message, assistant_reply) VALUES (%s,%s)",
            (user, assistant)
        )
        conn.commit()

def load_history(limit=50):
    with db() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT user_message, assistant_reply, created_at
            FROM jarvis_chat_history
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()

# ================= ROUTES =================
@app.route("/")
def ui():
    return send_from_directory("static", "jarvis.html")

# ================= SOCKET =================
@socketio.on("start_stream")
def start_stream(data):
    global last_user_message

    msg = (data.get("message") or "").strip()
    if not msg:
        emit("llm_token", {"token": "Say something."})
        emit("stream_done")
        return

    lang = detect_lang(msg)

    if is_forget_command(msg):
        key = extract_forget_key(msg)
        if key:
            delete_memory_by_key(key)
            reply = f"memory '{key}' forgotten"
        else:
            reply = "specify what to forget"

    elif is_save_command(msg) and last_user_message:
        mem = extract_memory_llm(last_user_message)
        if mem:
            save_memory(mem["key"], mem["value"])
            reply = "updated memory"
        else:
            reply = "nothing to save"

    else:
        remembered = find_memory(msg)
        if remembered:
            reply = remembered
        else:
            r = llm.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": msg}
                ],
                temperature=0.3
            )
            reply = r.choices[0].message.content.strip()

    save_chat(msg, reply)
    last_user_message = msg

    emit("llm_token", {"token": reply})
    emit("audio_chunk", {
        "audio_b64": base64.b64encode(tts_bytes(reply, lang)).decode()
    })
    emit("stream_done")

@socketio.on("load_history")
def socket_history():
    emit("history", load_history())

@socketio.on("list_memory")
def socket_memory():
    emit("memory_list", list_memories())

# ================= LOCAL RUN =================
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
# 1.1