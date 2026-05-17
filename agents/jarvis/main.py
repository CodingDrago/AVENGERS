import os
import json
import logging
import asyncio
import httpx
import aiosqlite
import re
from datetime import datetime
from typing import List, Dict, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# --- CONFIGURATION ---
CONFIG_PATH = "../../config.json"
if not os.path.exists(CONFIG_PATH):
    CONFIG_PATH = "config.json" # Fallback

try:
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
except Exception:
    config = {}

GEMINI_KEYS = config.get("gemini_keys", [])
MODELS = config.get("models", ["gemini-2.5-flash", "gemini-3-flash-preview", "gemini-3.1-flash-lite"])
LIMITS = config.get("daily_limits", [500, 1000, 1500])
TELEGRAM_TOKEN = config.get("telegram_token", "")
TELEGRAM_CHAT_ID = config.get("telegram_chat_id", "")
USER_CALLSIGN = config.get("user_callsign", "Sir")

# Logging
LOG_DIR = "../../outputs/jarvis/logs"
if not os.path.exists("../../outputs"):
    LOG_DIR = "outputs/jarvis/logs"
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f"jarvis_{datetime.now().strftime('%Y%m%d')}.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("JARVIS")

logger.info(f"Config loaded. Keys found: {len(GEMINI_KEYS)}, Models: {MODELS}")
logger.info(f"Key 1 present: {bool(GEMINI_KEYS[0]) if GEMINI_KEYS else False}")

# DB Path
_default_db = os.path.join(os.path.dirname(__file__), "..", "..", "db", "core.db")
DB_PATH = config.get("paths", {}).get("db", _default_db)
DB_PATH = os.path.normpath(DB_PATH)
db_dir = os.path.dirname(DB_PATH)
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)

# --- SYSTEM PROMPT ---
SYSTEM_PROMPT = f"""CRITICAL OUTPUT FORMAT RULES — THESE OVERRIDE EVERYTHING:
1. NEVER output JSON, XML, or any structured data format in your reply
2. NEVER wrap your response in ```json or any code fence unless the
   user explicitly asks you to show code
3. NEVER include keys like "agent", "port", "request", "response"
   as part of your conversational reply
4. You output plain natural English sentences ONLY
5. If you catch yourself writing {{ or [ to start a response, STOP
   and rewrite as plain English
6. Code examples are the ONLY exception — wrap code in ```language fences

You are JARVIS — not a butler, not an assistant, not a chatbot.
You are a highly intelligent AI who happens to run a personal
operations system. You are the user's most capable tool and
you know it — without being arrogant about it.

Your default is to answer everything yourself. You are a
generalist with deep knowledge across technology, science,
finance, design, health, learning, and strategy. You answer
like a brilliant friend who has expertise in everything —
direct, useful, occasionally dry, never performative.

You only mention other agents when they can do something you
genuinely cannot — execute a real task, build something,
generate an asset, automate a workflow. Even then, you answer
the informational part first, and mention the agent casually
at the end as an option, not a handoff.

You never say:
- "Forwarding to X, Sir" out of nowhere
- "Certainly!" / "Of course!" / "Great question!"
- "As your AI assistant..."
- "I'd be happy to help with that"
- "Shall I route this to..." before answering anything yourself
- Any variation of butler-speak or sycophantic openers

You address the user as "{USER_CALLSIGN}" — but sparingly. Once per
response maximum, usually at the end if at all. Not every
sentence. Not as a verbal tic.

Your tone: think less Alfred-the-butler, more Tony-Stark-talking-
to-himself. Confident, efficient, slightly dry, occasionally
with an edge of wit. You form opinions. You push back when
something is a bad idea. You notice things the user didn't ask
about but probably should know.

Response length: match the request exactly.
- Quick question → 1-3 sentences
- Technical explanation → structured paragraphs, no bullet
  points unless listing 4+ distinct items
- Complex strategy → thorough, but never padded

When you don't know something: "I don't have that data."
When something fails: state what failed and what the options are.
When the user is wrong about something: say so, briefly, without
lecturing.

You have a memory of this conversation. You reference earlier
things the user said when relevant. You don't re-introduce
yourself unless directly asked.

The agents in the system and what they actually do:
- ALFRED: reads Google Tasks + Calendar, builds daily/weekly
  schedules, tracks velocity, sends morning briefings
- VISION: takes a vague design idea and returns 3 structured
  concepts with layout, palette, fonts, mood
- BANNER: converts plain-English into working n8n automation
  workflows, deploys them automatically
- STARK: builds websites via Firebase Studio / Bolt / v0,
  pushes to GitHub, manages build queue
- NEBULA: generates images via Gemini Imagen, handles visual
  assets, notifies STARK when ready
- COULSON: verifies and installs all system dependencies,
  health-checks every agent before it runs
- PEPPER: tracks skincare, haircare, supplements, fitness
  inventory, sends low-stock alerts, maintains shopping list
- XAVIER: analyzes decisions and arguments, finds logical gaps,
  identifies cognitive biases, never just agrees
- FRIDAY: searches YouTube for top tutorials on a dev topic,
  extracts transcripts, summarizes into structured notes,
  saves to NotebookLM
- SHURI: identical to FRIDAY but for electronics, robotics,
  embedded systems, ROS, Arduino, ESP32, career roadmapping
- RHODEY: pulls Google Fit data, tracks steps/sleep/heart rate,
  finds weekly patterns, feeds context to ALFRED
- WONG: reads/writes Google Sheets income tracker, categorizes
  transactions, sends monthly summaries, tracks unpaid invoices

When mentioning an agent, use only their codename. Never say
"the learning agent" or "the health tracker". Say FRIDAY.
Say RHODEY. Always the codename.
"""

# --- AGENT DATA ---
AGENT_ROSTER = {
    "JARVIS": "Orchestrator", "ALFRED": "Work Routine + Roadmap", "VISION": "Design Ideation",
    "BANNER": "n8n Automation", "STARK": "Web Builder + GitHub", "NEBULA": "Image + Asset Creator",
    "COULSON": "Dependency Manager", "PEPPER": "Routine + Inventory", "XAVIER": "Decision Critic",
    "FRIDAY": "Software Learning", "SHURI": "ECE + Robotics", "RHODEY": "Health Tracking", "WONG": "Finance Tracking"
}

AGENT_PORTS = {
    "ALFRED": "http://localhost:8001",
    "VISION": "http://localhost:8002",
    "BANNER": "http://localhost:8003",
    "STARK":  "http://localhost:8004",
    "NEBULA": "http://localhost:8005",
    "PEPPER": "http://localhost:8007",
    "XAVIER": "http://localhost:8008",
    "FRIDAY": "http://localhost:8009",
    "SHURI":  "http://localhost:8010",
    "RHODEY": "http://localhost:8011",
    "WONG":   "http://localhost:8012",
}

async def route_to_agent(agent_name: str, user_input: str,
                          context: str = "") -> str | None:
    """
    POSTs to the agent's /run endpoint.
    Returns plain text reply or None if agent is offline/unreachable.
    Timeout: 30s (agents may call Gemini themselves).
    """
    url = AGENT_PORTS.get(agent_name.upper())
    if not url:
        return None
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"{url}/run",
                json={
                    "task": agent_name.lower(),
                    "context": context,
                    "user_input": user_input
                },
                timeout=30.0
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("reply", "")
            else:
                logger.warning(f"Agent {agent_name} returned {res.status_code}")
                return None
    except Exception as e:
        logger.warning(f"Agent {agent_name} unreachable: {e}")
        return None

# --- ROUTING LOGIC (Keyword matching - No API call) ---
def should_auto_route(message: str, context: list) -> tuple[bool, str | None]:
    msg_lower = message.lower()
    
    # Check 1: Explicit agent naming
    for agent in AGENT_ROSTER.keys():
        if agent != "JARVIS" and re.search(rf'\b{agent.lower()}\b', msg_lower):
            return True, agent

    # Check 2: Explicit confirmation after an offer
    confirmations = ["yes", "go ahead", "do it", "route it", "hand it off", "sure", "ok", "please"]
    if any(c == msg_lower.strip() for c in confirmations) or any(msg_lower.startswith(c + " ") for c in confirmations):
        if context and len(context) > 0:
            last_msg = context[-1]
            if last_msg["role"] == "model":
                for agent in AGENT_ROSTER.keys():
                    if agent in last_msg["content"] and agent != "JARVIS":
                        return True, agent

    # Check 3: Pure execution commands
    exec_patterns = {
        "ALFRED": [
            r"tell alfred", r"ask alfred", r"alfred.*schedule",
            r"alfred.*tasks", r"alfred.*briefing", r"sync alfred",
            r"morning briefing", r"my schedule", r"build.*schedule",
            r"weekly roadmap", r"velocity report", r"how am i doing",
            r"tasks.*today", r"overdue tasks"
        ],
        "STARK": [r"build my site", r"deploy", r"push to github"],
        "VISION": [r"design concept", r"wireframe"],
        "BANNER": [r"create.*automation", r"n8n workflow"],
        "NEBULA": [r"generate.*image", r"create.*asset"],
        "WONG":   [r"log.*income", r"log.*expense", r"track.*transaction"]
    }
    for agent, patterns in exec_patterns.items():
        for pattern in patterns:
            if re.search(pattern, msg_lower):
                return True, agent

    return False, None

# --- DATABASE LOGIC ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                role TEXT,
                content TEXT,
                agent TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_key_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_index INTEGER,
                model_name TEXT,
                date TEXT,
                live_limit INTEGER DEFAULT 0,
                live_used INTEGER DEFAULT 0,
                live_remaining INTEGER DEFAULT 0,
                live_tokens_limit INTEGER DEFAULT 0,
                live_tokens_remaining INTEGER DEFAULT 0,
                reset_time TEXT,
                last_synced TEXT,
                notified_90 INTEGER DEFAULT 0,
                local_count INTEGER DEFAULT 0,
                source TEXT DEFAULT 'local',
                status TEXT DEFAULT 'VALID',
                UNIQUE(key_index, model_name, date)
            )
        """)
        await db.commit()

async def ensure_usage_record(key_index: int, model_name: str):
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM api_key_usage WHERE key_index=? AND model_name=? AND date=?",
            (key_index, model_name, today)
        ) as cursor:
            row = await cursor.fetchone()
        
        if not row:
            limit_idx = MODELS.index(model_name) if model_name in MODELS else 0
            limit = LIMITS[limit_idx] if limit_idx < len(LIMITS) else 500
            await db.execute("""
                INSERT OR IGNORE INTO api_key_usage
                (key_index, model_name, date, live_limit, live_used,
                 local_count, source, status)
                VALUES (?, ?, ?, ?, 0, 0, 'local', 'VALID')
            """, (key_index, model_name, today, limit))
            await db.commit()
            async with db.execute(
                "SELECT * FROM api_key_usage WHERE key_index=? AND model_name=? AND date=?",
                (key_index, model_name, today)
            ) as cursor:
                row = await cursor.fetchone()
    
    # Convert Row to plain dict so it never fails on key access
    return dict(row) if row else {
        "key_index": key_index,
        "model_name": model_name,
        "date": today,
        "live_limit": LIMITS[MODELS.index(model_name)] if model_name in MODELS else 500,
        "live_used": 0,
        "live_remaining": 0,
        "local_count": 0,
        "source": "local",
        "status": "VALID",
        "notified_90": 0,
        "reset_time": None,
        "last_synced": None
    }

async def increment_local_usage(key_index: int, model: str):
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE api_key_usage SET
                local_count = local_count + 1,
                last_synced = ?, source = 'local'
            WHERE key_index = ? AND model_name = ? AND date = ?
        """, (datetime.now().isoformat(), key_index, model, today))
        await db.commit()

async def sync_usage_from_headers(key_index: int, model: str, used: int, limit: int, reset_at: str):
    """Replace local estimate with Google's real numbers."""
    real_limit = max(limit, LIMITS[MODELS.index(model)] if model in MODELS else 500)
    real_used = max(used, 0)
    
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE api_key_usage 
            SET live_used = ?,
                source = 'headers',
                last_synced = ?,
                live_limit = ?,
                reset_time = ?
            WHERE key_index = ? AND model_name = ? AND date = ?
        """, (real_used, datetime.utcnow().isoformat(), 
              real_limit, reset_at, key_index, model, today))
        await db.commit()
    
    logger.info(f"Header sync: Key {key_index+1} {model} — {real_used}/{real_limit} ({round(real_used/real_limit*100)}%)")

async def mark_notified(key_index: int, model_name: str):
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE api_key_usage SET notified_90 = 1 WHERE key_index = ? AND model_name = ? AND date = ?", (key_index, model_name, today))
        await db.commit()

async def mark_slot_rate_limited(key_index: int, model_name: str):
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE api_key_usage SET status = 'RATE_LIMITED' WHERE key_index = ? AND model_name = ? AND date = ?", (key_index, model_name, today))
        await db.commit()

async def mark_slot_exhausted(key_index: int, model_name: str):
    today = datetime.now().strftime('%Y-%m-%d')
    limit_idx = MODELS.index(model_name) if model_name in MODELS else 0
    limit = LIMITS[limit_idx] if limit_idx < len(LIMITS) else 500
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE api_key_usage SET
              status = 'RATE_LIMITED',
              local_count = ?,
              live_used = ?,
              last_synced = ?
            WHERE key_index = ? AND model_name = ? AND date = ?
        """, (limit, limit, datetime.now().isoformat(),
              key_index, model_name, today))
        await db.commit()
    logger.warning(f"Slot exhausted: Key {key_index+1} {model_name} marked as RATE_LIMITED")

# --- UTILS ---
async def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception as e:
        logger.error(f"Telegram failed: {e}")

async def call_gemini_stream(k_idx, model_name, prompt, history=[], attachments=[]):
    """THE ONLY PLACE GEMINI API IS CALLED. Triggered only by real user messages."""
    key = GEMINI_KEYS[k_idx]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:streamGenerateContent?alt=sse"
    contents = []
    for h in history:
        contents.append({"role": "user" if h["role"] == "user" else "model", "parts": [{"text": h["content"]}]})
    
    user_parts = []
    for att in attachments:
        if att.get('type') == 'image' and att.get('data'):
            data_url = att['data']
            if ',' in data_url:
                header, b64data = data_url.split(',', 1)
                mime_type = header.split(':')[1].split(';')[0]
                user_parts.append({
                    "inlineData": {"mimeType": mime_type, "data": b64data}
                })
    if prompt:
        user_parts.append({"text": prompt})
    if not user_parts:
        user_parts.append({"text": " "})
    contents.append({"role": "user", "parts": user_parts})

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]}
    }
    
    if "preview" in model_name:
        payload["generationConfig"] = {"thinkingConfig": {"includeThoughts": False}}

    async def event_generator():
        full_reply = ""
        agent_name = "JARVIS"
        headers_dict = {}
        
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", url, headers={"x-goog-api-key": key, "Content-Type": "application/json"}, json=payload, timeout=60.0) as response:
                    if response.status_code == 429:
                        await mark_slot_exhausted(k_idx, model_name)
                        yield f"data: {json.dumps({'type': 'error', 'status': 'rate_limit', 'message': 'Rotating keys...'})}\n\n"
                        return
                    if response.status_code == 503:
                        logger.warning(f"503 ServiceUnavailable: Key {k_idx+1} {model_name}")
                        yield f"data: {json.dumps({'type': 'error', 'status': 'service_unavailable', 'text': 'Service temporarily unavailable. Retrying with next slot.'})}\n\n"
                        return
                    if response.status_code != 200:
                        err = await response.aread()
                        err_text = err.decode('utf-8', errors='ignore')
                        logger.error(f"Gemini API error {response.status_code}: {err_text[:500]}")
                        yield f"data: {json.dumps({'type': 'error', 'status': response.status_code, 'text': err_text[:200]})}\n\n"
                        return
                    
                    # Capture headers for free quota sync
                    headers_dict = dict(response.headers)
                    
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str.strip() == "": continue
                            try:
                                js = json.loads(data_str)
                                if "candidates" in js and js["candidates"]:
                                    part = js["candidates"][0].get("content", {}).get("parts", [{}])[0]
                                    text = part.get("text", "")
                                    if text:
                                        full_reply += text
                                        yield f"data: {json.dumps({'type': 'token', 'text': text})}\n\n"
                                
                                if "usageMetadata" in js:
                                    tokens = js["usageMetadata"].get("totalTokenCount", 0)
                                    yield f"data: {json.dumps({'type': 'context', 'tokens': tokens, 'limit': 1000000})}\n\n"
                            except Exception: pass
        except asyncio.CancelledError:
            yield f"data: {json.dumps({'type': 'system_internal', 'text': 'Command aborted.'})}\n\n"
            return
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
            return

        # Routing detection logic (Post-processing)
        should_route, target_agent = should_auto_route(prompt, history)
        if should_route and target_agent:
            agent_name = target_agent.upper()
            # Try routing to the actual agent container first
            agent_reply = await route_to_agent(agent_name, prompt, 
                                                context=full_reply)
            if agent_reply:
                # Agent responded — stream its reply as tokens
                yield f"data: {json.dumps({'type': 'system_internal', 'text': f'Routed to {agent_name}'})}\n\n"
                # Stream agent reply word by word so UI renders it properly
                words = agent_reply.split(' ')
                for i, word in enumerate(words):
                    token = word + (' ' if i < len(words)-1 else '')
                    yield f"data: {json.dumps({'type': 'token', 'text': token})}\n\n"
                    await asyncio.sleep(0.01)
                full_reply = agent_reply  # save agent reply to history
            else:
                # Agent offline — JARVIS handles it, already in full_reply
                yield f"data: {json.dumps({'type': 'system_internal', 'text': f'{agent_name} offline — handling directly'})}\n\n"
        else:
            match = re.search(r'(Routing to|Transferring to) ([A-Z]+)', 
                              full_reply, re.IGNORECASE)
            if match:
                agent_name = match.group(2).upper()
                yield f"data: {json.dumps({'type': 'system_internal', 'text': f'Routed to {agent_name}'})}\n\n"

        # Always increment local count for every message
        await increment_local_usage(k_idx, model_name)

        # Sync real headers from response if available
        limit_str = headers_dict.get('x-ratelimit-limit-requests')
        remaining_str = headers_dict.get('x-ratelimit-remaining-requests')
        reset_at = headers_dict.get('x-ratelimit-reset-requests', '')
        if limit_str and remaining_str:
            try:
                limit_int = int(limit_str)
                remaining_int = int(remaining_str)
                used = limit_int - remaining_int
                asyncio.create_task(sync_usage_from_headers(
                    k_idx, model_name, used, limit_int, str(reset_at)
                ))
            except Exception: pass
            
        await save_history("jarvis", full_reply, agent=agent_name)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# --- CORE STATE ---
STATE = {"active_key_index": 0, "active_model_index": 0}
SESSION_START = datetime.now().isoformat()

async def find_best_slot():
    """Find slot with lowest usage. Used on startup and after refresh."""
    today = datetime.now().strftime('%Y-%m-%d')
    best_slot = None
    best_used = float('inf')
    
    for k in range(len(GEMINI_KEYS)):
        if not GEMINI_KEYS[k] or not GEMINI_KEYS[k].strip():
            continue
        for m in range(len(MODELS)):
            rec = await ensure_usage_record(k, MODELS[m])
            if rec.get('status') in ['INVALID', 'RATE_LIMITED']:
                continue
            used = rec['live_used'] if rec.get('source') == 'live' else rec['local_count']
            limit = rec['live_limit'] if rec.get('live_limit', 0) > 0 else LIMITS[m]
            if used >= limit * 0.95:
                continue
            if used < best_used:
                best_used = used
                best_slot = (k, m)
    
    if best_slot:
        logger.info(f"Best slot: Key {best_slot[0]+1} Model {MODELS[best_slot[1]]} ({best_used} used)")
    return best_slot if best_slot else (None, None)

async def find_available_slot():
    total_keys = len(GEMINI_KEYS)
    total_models = len(MODELS)
    
    # Start from current active or wrap around
    start_k = STATE.get("active_key_index", 0)
    start_m = STATE.get("active_model_index", 0)
    
    for i in range(total_keys * total_models):
        idx = (start_k * total_models + start_m + i) % (total_keys * total_models)
        k = idx // total_models
        m = idx % total_models
        
        if not GEMINI_KEYS[k] or GEMINI_KEYS[k].strip() == "":
            continue
            
        rec = await ensure_usage_record(k, MODELS[m])
        if rec.get('status') in ['INVALID', 'RATE_LIMITED']:
            continue
            
        used = rec['live_used'] if rec.get('source') == 'live' else rec['local_count']
        limit = rec['live_limit'] if rec.get('live_limit', 0) > 0 else LIMITS[m]
        
        if used < limit * 0.95:
            logger.info(f"Slot search: selected Key {k+1} Model {MODELS[m]}")
            return k, m
            
    return None, None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Only initialize local resources. NO API CALLS ON STARTUP.
    await init_db()
    yield

app = FastAPI(title="JARVIS Orchestrator", lifespan=lifespan)

@app.post("/reset-rate-limits")
async def reset_rate_limits():
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE api_key_usage SET
              status = 'VALID',
              local_count = 0,
              live_used = 0,
              live_remaining = 0,
              notified_90 = 0
            WHERE date = ?
        """, (today,))
        await db.commit()
    STATE["active_key_index"] = 0
    STATE["active_model_index"] = 0
    logger.info("Rate limits manually reset")
    return {"status": "ok", "message": "All slots reset to VALID"}

class UserInput(BaseModel):
    text: str
    attachments: list = []  # [{type, name, data}] where data is base64 data URL

class DecisionRequest(BaseModel):
    decision: str
    pending_prompt: str

class SwitchModelRequest(BaseModel):
    key_index: int
    model_index: int

async def save_history(role: str, content: str, agent: str = "JARVIS"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO history (role, content, agent) VALUES (?, ?, ?)", (role, content, agent))
        await db.commit()

async def get_history(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT role, content, agent FROM history ORDER BY timestamp DESC LIMIT ?", (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [{"role": r["role"], "content": r["content"], "agent": r["agent"]} for r in reversed(rows)]

@app.get("/history")
async def get_chat_history():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Only return messages from BEFORE this session started
        # This correctly separates previous sessions from current
        async with db.execute(
            "SELECT role, content, agent, timestamp FROM history"
            " WHERE content IS NOT NULL AND content != ''"
            " AND timestamp < ?"
            " ORDER BY id DESC LIMIT 10",
            (SESSION_START,)
        ) as cursor:
            rows = await cursor.fetchall()
    rows = list(reversed(rows))
    return JSONResponse({"history": [
        {"role": r["role"], "content": r["content"],
         "agent": r["agent"], "timestamp": r["timestamp"]}
        for r in rows
    ]})

@app.get("/startup-check")
async def startup_check():
    """Called by frontend on page load. Finds best slot. No API calls."""
    k, m = await find_best_slot()
    if k is not None:
        STATE["active_key_index"] = k
        STATE["active_model_index"] = m
        logger.info(f"Startup: selected Key {k+1} Model {MODELS[m]}")
        return {
            "status": "ok",
            "active_key": k + 1,
            "active_model": MODELS[m],
            "active_key_model_index": m,
            "message": f"Using Key {k+1} · {MODELS[m]}"
        }
    return {"status": "exhausted", "active_key": -1, "active_model": "none"}

@app.post("/switch-model")
async def switch_model(req: SwitchModelRequest):
    global STATE
    if 0 <= req.key_index < len(GEMINI_KEYS) and 0 <= req.model_index < len(MODELS):
        STATE["active_key_index"] = req.key_index
        STATE["active_model_index"] = req.model_index
        logger.info(f"Manual switch: Key {req.key_index+1} Model {MODELS[req.model_index]}")
        return {"status": "ok", "key": req.key_index+1, "model": MODELS[req.model_index]}
    return {"status": "error"}

@app.delete("/conversation/pending")
async def clear_pending():
    return JSONResponse({"status": "ok"})

@app.post("/chat")
async def chat(user_input: UserInput):
    k, m = await find_available_slot()
    if k is None:
        return {"reply": f"Local systems only, {USER_CALLSIGN}. Cloud providers are offline.", "status": "fully_exhausted"}
    
    STATE["active_key_index"] = k
    STATE["active_model_index"] = m
    
    rec = await ensure_usage_record(k, MODELS[m])
    used = rec['live_used'] if rec['source'] == 'live' else rec['local_count']
    limit = rec['live_limit'] if rec['live_limit'] > 0 else LIMITS[m]
    
    # 90% Threshold Check (Purely local check)
    if used >= (limit * 0.9) and rec['notified_90'] == 0:
        await mark_notified(k, MODELS[m])
        msg = f"⚠️ JARVIS: Key {k+1} · {MODELS[m]} is at 90% usage ({used}/{limit}). Awaiting decision."
        await send_telegram(msg)
        return {
            "status": "threshold_warning",
            "warning": {
                "key_num": k + 1, "model_name": MODELS[m], "used": used, "limit": limit,
                "percent": int((used/limit)*100) if limit > 0 else 0,
                "next_model": MODELS[m+1] if m+1 < len(MODELS) else "Exhausted",
                "next_key_num": k + 2 if k + 1 < len(GEMINI_KEYS) else "None"
            },
            "pending_prompt": user_input.text
        }
    
    await save_history("user", user_input.text)
    history = await get_history(5)
    
    # The only place the API is called
    return await call_gemini_stream(k, MODELS[m], user_input.text, history, attachments=user_input.attachments)

@app.post("/decide")
async def decide(req: DecisionRequest):
    global STATE
    total_keys = len(GEMINI_KEYS)
    total_models = len(MODELS)
    
    if req.decision == "switch_model":
        if STATE["active_model_index"] + 1 < total_models:
            STATE["active_model_index"] += 1
        else:
            STATE["active_key_index"] = (STATE["active_key_index"] + 1) % total_keys
            STATE["active_model_index"] = 0
    elif req.decision == "switch_key":
        STATE["active_key_index"] = (STATE["active_key_index"] + 1) % total_keys
        STATE["active_model_index"] = 0
    
    return {"status": "ok"}

@app.get("/api-key-status")
async def get_api_status():
    """Returns local usage snapshot. No API calls."""
    today = datetime.now().strftime('%Y-%m-%d')
    keys_data = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for k_idx, key in enumerate(GEMINI_KEYS):
            if not key or not key.strip():
                continue
            models_data = []
            for m_idx, m_name in enumerate(MODELS):
                async with db.execute("SELECT live_used, local_count, live_limit, source FROM api_key_usage WHERE key_index = ? AND model_name = ? AND date = ?", (k_idx, m_name, today)) as cursor:
                    row = await cursor.fetchone()
                
                source = row['source'] if row else 'local'
                used = row['live_used'] if (row and source == 'headers') else (row['local_count'] if row else 0)
                limit = (row['live_limit'] if row and row['live_limit'] and row['live_limit'] > 0 else LIMITS[m_idx])
                percent = round((used / limit) * 100, 1) if limit > 0 else 0
                
                models_data.append({
                    "name": m_name,
                    "used": used,
                    "limit": limit,
                    "percent": percent,
                    "source": source,
                    "status": ("EXHAUSTED" if percent >= 90 else "WARNING" if percent >= 70 else "OK")
                })
            keys_data.append({"key_num": k_idx + 1, "models": models_data})
    
    active_key = STATE.get("active_key_index", 0)
    active_model = STATE.get("active_model_index", 0)
    
    return {
        "keys": keys_data,
        "active_key": active_key + 1,
        "active_model": MODELS[active_model] if active_model < len(MODELS) else "unknown"
    }

@app.get("/health-check")
async def health_check():
    """Health check tests local file/DB existence only. No Gemini API calls."""
    db_ok = os.path.exists(DB_PATH)
    config_ok = os.path.exists(CONFIG_PATH)
    return {"status": "ok", "db": db_ok, "config": config_ok, "api_test": "skipped (quota preservation)"}

@app.get("/", response_class=HTMLResponse)
async def get_ui():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
