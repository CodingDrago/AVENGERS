import os
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

# --- PATH CONFIGURATION (Windows / Ubuntu compatibility) ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.exists("/avengers/config.json"):
    CONFIG_PATH = "/avengers/config.json"
    DB_PATH = "/avengers/db/core.db"
    LOG_PATH = "/avengers/outputs/jarvis/logs/router.log"
else:
    CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
    DB_PATH = os.path.join(BASE_DIR, "db", "core.db")
    LOG_PATH = os.path.join(BASE_DIR, "outputs", "jarvis", "logs", "router.log")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

# --- LOAD CONFIG ---
try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
except Exception as e:
    cfg = {}
    print(f"Warning: Failed to load config at {CONFIG_PATH}: {e}")

GEMINI_KEYS = cfg.get("gemini_keys", ["", "", "", ""])
GROQ_API_KEY = cfg.get("groq_api_key", "")
OLLAMA_URL = cfg.get("ollama_base_url", "http://localhost:11434")

# --- CONSTANTS & STRUCTURES ---
KEY_MODEL_STRUCTURE = {
    1: {
        "key_env": "GEMINI_KEY_1",
        "models": [
            {"name": "gemini-2.5-flash",      "daily_limit": 500},
            {"name": "gemini-3-flash-preview", "daily_limit": 1000},
            {"name": "gemini-3.1-flash-lite",  "daily_limit": 1500}
        ]
    },
    2: {
        "key_env": "GEMINI_KEY_2",
        "models": [
            {"name": "gemini-2.5-flash",      "daily_limit": 500},
            {"name": "gemini-3-flash-preview", "daily_limit": 1000},
            {"name": "gemini-3.1-flash-lite",  "daily_limit": 1500}
        ]
    },
    3: {
        "key_env": "GEMINI_KEY_3",
        "models": [
            {"name": "gemini-2.5-flash",      "daily_limit": 500},
            {"name": "gemini-3-flash-preview", "daily_limit": 1000},
            {"name": "gemini-3.1-flash-lite",  "daily_limit": 1500}
        ]
    },
    4: {
        "key_env": "GEMINI_KEY_4",
        "models": [
            {"name": "gemini-2.5-flash",      "daily_limit": 500},
            {"name": "gemini-3-flash-preview", "daily_limit": 1000},
            {"name": "gemini-3.1-flash-lite",  "daily_limit": 1500}
        ]
    }
}

DAILY_LIMITS = {
    "gemini-2.5-flash":      500,
    "gemini-3-flash-preview": 1000,
    "gemini-3.1-flash-lite":  1500,
    "groq-llama-3.3-70b":    1000,
    "groq-deepseek-r1":      1000,
}

ROTATION_THRESHOLD = 0.90
COMPLEX_AGENTS = ["xavier", "friday", "shuri"]
FAST_AGENTS = ["jarvis", "alfred", "pepper", "wong", "banner", "vision", "stark", "nebula", "coulson", "rhodey"]

# --- LOGGING ---
def log(message: str):
    entry = f"[{datetime.utcnow().isoformat()}] {message}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass

# --- TELEGRAM NOTIFICATION ---
def notify_telegram(message: str):
    token = cfg.get("telegram_token", "")
    chat_id = cfg.get("telegram_chat_id", "")
    if not token or not chat_id:
        log("Telegram not configured — skipping notification")
        return
    try:
        import httpx
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5.0
        )
    except Exception as e:
        log(f"Telegram notification failed: {e}")

# --- TIME HELPERS ---
def next_gemini_reset() -> str:
    now = datetime.utcnow()
    reset = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now >= reset:
        reset += timedelta(days=1)
    return reset.isoformat()

def next_groq_reset() -> str:
    now = datetime.utcnow()
    reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return reset.isoformat()

# --- DATABASE INIT ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_quota (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            provider      TEXT NOT NULL,
            key_index     INTEGER,
            model         TEXT NOT NULL,
            requests_used INTEGER DEFAULT 0,
            daily_limit   INTEGER NOT NULL,
            reset_at      TEXT NOT NULL,
            notified_90   INTEGER DEFAULT 0,
            last_updated  TEXT,
            UNIQUE(provider, key_index, model)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routing_state (
            id            INTEGER PRIMARY KEY,
            last_provider TEXT,
            last_key_index INTEGER,
            last_model    TEXT,
            updated_at    TEXT
        )
    """)
    conn.execute("INSERT OR IGNORE INTO routing_state (id) VALUES (1)")
    
    # Init 12 Gemini slots
    gem_reset = next_gemini_reset()
    for key_idx, config in KEY_MODEL_STRUCTURE.items():
        for model in config["models"]:
            conn.execute("""
                INSERT OR IGNORE INTO api_quota (provider, key_index, model, daily_limit, reset_at)
                VALUES (?, ?, ?, ?, ?)
            """, ("gemini", key_idx, model["name"], model["daily_limit"], gem_reset))
            
    # Init 2 Groq slots
    groq_reset = next_groq_reset()
    for model_name in ["groq-llama-3.3-70b", "groq-deepseek-r1"]:
        conn.execute("""
            INSERT OR IGNORE INTO api_quota (provider, key_index, model, daily_limit, reset_at)
            VALUES (?, ?, ?, ?, ?)
        """, ("groq", None, model_name, DAILY_LIMITS[model_name], groq_reset))
        
    conn.commit()
    conn.close()

# Initialize on import
init_db()

# --- QUOTA MANAGEMENT ---
def check_and_reset_quotas():
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, provider, key_index, model, reset_at, notified_90"
        " FROM api_quota WHERE reset_at <= ?", (now,)
    ).fetchall()
    
    for row in rows:
        id_, provider, key_index, model, reset_at, _ = row
        if provider == "gemini":
            next_reset = next_gemini_reset()
        else:
            next_reset = next_groq_reset()
            
        conn.execute(
            "UPDATE api_quota SET requests_used=0, notified_90=0,"
            " reset_at=? WHERE id=?", (next_reset, id_)
        )
        log(f"Quota reset: {provider} key{key_index} {model}")
        
    conn.commit()
    conn.close()

def get_usage(provider: str, key_index: Optional[int], model: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT requests_used FROM api_quota WHERE provider=? AND key_index IS ? AND model=?",
        (provider, key_index, model)
    ).fetchone()
    conn.close()
    return row[0] if row else 0

def increment_usage(provider: str, key_index: Optional[int], model: str) -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE api_quota SET requests_used = requests_used + 1,"
        " last_updated = ? WHERE provider=? AND key_index IS ? AND model=?",
        (datetime.utcnow().isoformat(), provider, key_index, model)
    )
    conn.commit()
    
    row = conn.execute(
        "SELECT requests_used, daily_limit, notified_90"
        " FROM api_quota WHERE provider=? AND key_index IS ? AND model=?",
        (provider, key_index, model)
    ).fetchone()
    conn.close()
    
    if not row:
        return {"requests_used": 0, "daily_limit": 1, "notified_90": 0}
        
    usage, limit, notified = row[0], row[1], row[2]
    
    if usage / limit >= ROTATION_THRESHOLD and not notified:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE api_quota SET notified_90 = 1 WHERE provider=? AND key_index IS ? AND model=?",
            (provider, key_index, model)
        )
        conn.commit()
        conn.close()
        
        notify_telegram(
            f"⚠️ JARVIS ALERT: Quota Threshold Reached\n"
            f"Provider: {provider.upper()}\n"
            f"Key Index: {key_index}\n"
            f"Model: {model}\n"
            f"Usage: {usage}/{limit} ({round((usage/limit)*100)}%)\n"
            f"Rotating to next available slot."
        )
        
    return {"requests_used": usage, "daily_limit": limit, "notified_90": notified}

def sync_from_headers(provider: str, key_index: int, model: str,
                      limit: int, remaining: int, reset_at: str):
    # Safety floor - use hardcoded limit if header value is suspect
    safe_limit = max(limit, DAILY_LIMITS.get(model, 500))
    used = safe_limit - remaining
    
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE api_quota SET
            requests_used = ?,
            daily_limit = ?,
            reset_at = ?,
            last_updated = ?
        WHERE provider=? AND key_index=? AND model=?
    """, (used, safe_limit, reset_at,
          datetime.utcnow().isoformat(),
          provider, key_index, model))
    conn.commit()
    conn.close()
    log(f"Header sync: {provider} key{key_index} {model}"
        f" — {used}/{safe_limit} used, resets {reset_at}")

def get_all_slots_needing_sync() -> list:
    # Returns slots not synced in the last 55 minutes
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.utcnow() -
              timedelta(minutes=55)).isoformat()
    rows = conn.execute("""
        SELECT provider, key_index, model
        FROM api_quota
        WHERE last_updated IS NULL OR last_updated < ?
    """, (cutoff,)).fetchall()
    conn.close()
    return [{"provider": r[0], "key_index": r[1],
             "model": r[2]} for r in rows]

# --- ROUTING STATE ---
def update_routing_state(provider: str, key_index: Optional[int], model: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE routing_state SET last_provider=?, last_key_index=?, last_model=?, updated_at=? WHERE id=1",
        (provider, key_index, model, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def detect_switch(new_provider: str, new_key_index: Optional[int], new_model: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT last_provider, last_key_index, last_model FROM routing_state WHERE id=1"
    ).fetchone()
    conn.close()
    
    if not row or not row[0]:
        update_routing_state(new_provider, new_key_index, new_model)
        return False
        
    switched = (row[0] != new_provider or row[1] != new_key_index or row[2] != new_model)
    if switched:
        log(f"Provider switch: {row[0]}/key{row[1]}/{row[2]}"
            f" → {new_provider}/key{new_key_index}/{new_model}"
            f" — JARVIS must re-inject conversation history")
    
    update_routing_state(new_provider, new_key_index, new_model)
    return switched

# --- SLOT FINDERS ---
def find_available_gemini_slot() -> Optional[Dict[str, Any]]:
    for key_index in range(1, 5):
        for model_cfg in KEY_MODEL_STRUCTURE[key_index]["models"]:
            usage = get_usage("gemini", key_index, model_cfg["name"])
            threshold = model_cfg["daily_limit"] * ROTATION_THRESHOLD
            if usage < threshold:
                return {
                    "key_index": key_index,
                    "model": model_cfg["name"],
                    "daily_limit": model_cfg["daily_limit"],
                    "requests_used": usage
                }
    return None

def find_available_groq_slot() -> Optional[Dict[str, Any]]:
    for model in ["groq-llama-3.3-70b", "groq-deepseek-r1"]:
        usage = get_usage("groq", None, model)
        limit = DAILY_LIMITS[model]
        threshold = limit * ROTATION_THRESHOLD
        if usage < threshold:
            return {"model": model, "daily_limit": limit, "requests_used": usage}
    return None

# --- RESULT BUILDERS & WARNINGS ---
def check_pending_warning(provider: str, key_index: Optional[int], model: str, usage: int, limit: int) -> Optional[Dict[str, Any]]:
    percent = usage / limit
    if 0.80 <= percent < 0.90:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT notified_90 FROM api_quota WHERE provider=? AND key_index IS ? AND model=?",
            (provider, key_index, model)
        ).fetchone()
        conn.close()
        
        # In this specific context, notified_90 acts as a proxy for "already warned at 80/90%"
        # But per specs, we just return the warning if between 80 and 90, frontend handles it being non-blocking.
        return {
            "level": "approaching",
            "provider": provider,
            "key_index": key_index,
            "model": model,
            "percent": round(percent * 100),
            "requests_used": usage,
            "daily_limit": limit,
            "message": f"Key {key_index} · {model} is at {round(percent*100)}% — approaching limit"
        }
    elif percent >= 0.90:
         return {
            "level": "threshold",
            "provider": provider,
            "key_index": key_index,
            "model": model,
            "percent": round(percent * 100),
            "requests_used": usage,
            "daily_limit": limit,
            "next_model": None, # Will be filled by router if needed, or by frontend requesting new route
            "next_key_num": None,
            "message": f"Key {key_index} · {model} reached 90% ({usage}/{limit})"
        }
    return None

def get_gemini_api_key(key_index: int) -> str:
    # key_index is 1-based, GEMINI_KEYS list is 0-based
    if 1 <= key_index <= len(GEMINI_KEYS):
        return GEMINI_KEYS[key_index - 1]
    return ""

def build_gemini_result(slot: Dict[str, Any], conversation_history: Optional[List] = None) -> Dict[str, Any]:
    key_idx = slot["key_index"]
    model = slot["model"]
    switched = detect_switch("gemini", key_idx, model)
    warning = check_pending_warning("gemini", key_idx, model, slot["requests_used"], slot["daily_limit"])
    
    return {
        "provider": "gemini",
        "model": model,
        "key_index": key_idx,
        "api_key": get_gemini_api_key(key_idx),
        "switch_detected": switched,
        "pending_warning": warning
    }

def build_groq_result(slot: Dict[str, Any], conversation_history: Optional[List] = None) -> Dict[str, Any]:
    model = slot["model"]
    switched = detect_switch("groq", None, model)
    warning = check_pending_warning("groq", None, model, slot["requests_used"], slot["daily_limit"])
    
    return {
        "provider": "groq",
        "model": model,
        "key_index": None,
        "api_key": GROQ_API_KEY,
        "switch_detected": switched,
        "pending_warning": warning
    }

def fallback_to_ollama(reason: str) -> Dict[str, Any]:
    log(f"OLLAMA FALLBACK: {reason}")
    notify_telegram(
        f"⚠️ JARVIS: All cloud providers exhausted or unreachable.\n"
        f"Reason: {reason}\n"
        f"Falling back to local Ollama (phi3:latest).\n"
        f"Time: {datetime.utcnow().isoformat()}"
    )
    return {
        "provider": "ollama",
        "model": "phi3:latest",
        "key_index": None,
        "api_key": None,
        "base_url": OLLAMA_URL,
        "switch_detected": True,
        "pending_warning": None
    }

# --- MAIN ROUTING LOGIC ---
def route(task_type: str, agent_name: str, conversation_history: Optional[List] = None) -> Dict[str, Any]:
    check_and_reset_quotas()
    
    if task_type.lower() == "complex" or agent_name.lower() in COMPLEX_AGENTS:
        tier = "complex"
    else:
        tier = "fast"
        
    if tier == "complex":
        slot = find_available_gemini_slot()
        if slot:
            return build_gemini_result(slot, conversation_history)
            
        slot = find_available_groq_slot()
        if slot:
            return build_groq_result(slot, conversation_history)
            
        return fallback_to_ollama("All providers exhausted for complex task")
        
    if tier == "fast":
        slot = find_available_groq_slot()
        if slot:
            return build_groq_result(slot, conversation_history)
            
        slot = find_available_gemini_slot()
        if slot:
            return build_gemini_result(slot, conversation_history)
            
        return fallback_to_ollama("All providers exhausted for fast task")
        
    return fallback_to_ollama("Unknown tier")

def report_transient_failure(provider: str, key_index: Optional[int], model: str, error: str):
    log(f"TRANSIENT FAILURE — {provider} key{key_index} {model}: {error}")

# --- STATUS ENDPOINT SUPPORT ---
def get_routing_status() -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT provider, key_index, model, requests_used,"
        " daily_limit, reset_at, notified_90, last_updated"
        " FROM api_quota ORDER BY provider, key_index, model"
    ).fetchall()
    conn.close()

    gemini_keys = {}
    groq_models = []
    
    for row in rows:
        provider, key_idx, model, used, limit, reset_at, n90, upd = row
        entry = {
            "model": model,
            "requests_used": used,
            "daily_limit": limit,
            "percent": round((used / limit) * 100, 1) if limit else 0,
            "reset_at": reset_at,
            "notified_90": bool(n90),
            "last_updated": upd,
            "status": "EXHAUSTED" if used >= limit * ROTATION_THRESHOLD
                      else "WARNING" if used >= limit * 0.80
                      else "OK"
        }
        
        if provider == "gemini":
            if key_idx not in gemini_keys:
                gemini_keys[key_idx] = []
            gemini_keys[key_idx].append(entry)
        else:
            groq_models.append(entry)

    active = find_available_gemini_slot() or find_available_groq_slot()
    exhausted_gemini_count = sum(1 for rows in gemini_keys.values() for r in rows if r["status"] == "EXHAUSTED")
    
    return {
        "gemini_keys": gemini_keys,
        "groq_models": groq_models,
        "active_slot": active,
        "total_gemini_slots": 12,
        "exhausted_gemini_slots": exhausted_gemini_count,
        "timestamp": datetime.utcnow().isoformat()
    }
