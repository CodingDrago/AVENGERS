import os
import json
import logging
import asyncio
import httpx
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from fastapi import FastAPI, Request
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from google import genai
from google.genai import types

# --- PATHS & CONSTANTS ---
CONFIG_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "config.json"))
if not os.path.exists(CONFIG_PATH):
    CONFIG_PATH = "config.json" # Fallback

def get_db_path():
    # Priority 1: config.json paths.db
    config_path = CONFIG.get("paths", {}).get("db")
    if config_path:
        return os.path.normpath(config_path)
    # Priority 2: Absolute /avengers/db
    if os.path.exists("/avengers/db"):
        return "/avengers/db/core.db"
    # Priority 3: Relative fallback
    base = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../"))
    return os.path.join(base, "db", "core.db")

DB_PATH = get_db_path()
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def get_log_dir():
    if os.path.exists("/avengers/outputs"):
        return "/avengers/outputs/alfred/logs"
    base = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../"))
    return os.path.join(base, "outputs", "alfred", "logs")

LOG_DIR = get_log_dir()
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"alfred_{datetime.now().strftime('%Y%m%d')}.log")

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] ALFRED: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("alfred")

# --- CONFIG & SECRETS ---
try:
    with open(CONFIG_PATH, "r") as f:
        CONFIG = json.load(f)
except Exception as e:
    logger.warning(f"Could not load config from {CONFIG_PATH}: {e}")
    CONFIG = {}

GEMINI_KEYS = CONFIG.get("gemini_keys", [])
MODELS = CONFIG.get("models", ["gemini-2.5-flash"])
LIMITS = CONFIG.get("daily_limits", [500])
TELEGRAM_TOKEN = CONFIG.get("telegram_token", "")
TELEGRAM_CHAT_ID = CONFIG.get("telegram_chat_id", "")
GOOGLE_CREDENTIALS_PATH = CONFIG.get("google_credentials_path", "credentials.json")
GOOGLE_TOKEN_PATH = CONFIG.get("google_token_path", "token.json")

SCOPES = [
    'https://www.googleapis.com/auth/tasks',
    'https://www.googleapis.com/auth/calendar'
]

def calculate_velocity_score(completed: int, total: int) -> int:
    """velocity_score = percentage of tasks completed, 0 if no tasks."""
    if total == 0:
        return 0
    return round((completed / total) * 100)

def get_google_credentials():
    """
    Loads saved OAuth credentials. Returns None if not yet authenticated.
    Call /auth/google then /auth/callback to generate token.json first.
    """
    token_path = CONFIG.get("google_token_path", "token.json")
    creds_path = CONFIG.get("google_credentials_path", "")

    if not os.path.exists(token_path):
        logger.warning(
            "token.json not found. "
            "Run: curl http://localhost:8001/auth/google and follow instructions."
        )
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GoogleRequest
        creds = Credentials.from_authorized_user_file(
            token_path,
            scopes=SCOPES
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            with open(token_path, "w") as f:
                f.write(creds.to_json())
            logger.info("Google credentials refreshed automatically")
        return creds
    except Exception as e:
        logger.error(f"Failed to load Google credentials: {e}")
        return None

SYSTEM_PROMPT = """You are ALFRED, the scheduling and productivity intelligence for Tony Stark's personal operations system. You produce clear, actionable schedules and roadmaps. You are direct and efficient. You address the user as Sir.

CRITICAL OUTPUT RULES:
- Output plain English only
- Never output JSON, XML, or code fences
- Never use markdown headers with # symbols
- Use simple line breaks and dashes for structure
- Keep responses concise — schedules should be scannable
- Time blocks format: HH:MM - HH:MM | [TYPE] Task name"""

# --- DATABASE INIT ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Check if api_key_usage exists, otherwise log warning
        try:
            await db.execute("SELECT 1 FROM api_key_usage LIMIT 1")
        except:
            logger.warning("api_key_usage table not found in shared DB.")
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS tasks_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT UNIQUE,
                title TEXT,
                notes TEXT,
                due_date TEXT,
                status TEXT,
                list_id TEXT,
                last_synced TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS calendar_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                title TEXT,
                start_datetime TEXT,
                end_datetime TEXT,
                description TEXT,
                location TEXT,
                last_synced TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS daily_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE,
                schedule_json TEXT,
                schedule_text TEXT,
                created_at TEXT,
                velocity_score REAL,
                approved INTEGER DEFAULT 0
            )
        ''')
        # Migration for existing columns
        try:
            await db.execute("ALTER TABLE daily_schedules ADD COLUMN approved INTEGER DEFAULT 0")
            await db.commit()
        except Exception: pass
        try:
            await db.execute("ALTER TABLE daily_schedules ADD COLUMN schedule_text TEXT")
            await db.commit()
        except Exception: pass

        await db.execute('''
            CREATE TABLE IF NOT EXISTS task_velocity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE,
                tasks_completed INTEGER,
                tasks_total INTEGER,
                velocity_score INTEGER,
                notes TEXT
            )
        ''')
        await db.commit()
    logger.info("Database initialized")

def parse_schedule_lines(schedule_text: str) -> list[dict]:
    """
    Parse schedule text into time blocks for Google Calendar.
    Format: HH:MM - HH:MM | [TYPE] Task name
    Skips malformed lines silently and logs them.
    Returns list of {start, end, title, type} dicts.
    """
    import re
    blocks = []
    pattern = re.compile(
        r'(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*\|\s*\[(\w+)\]\s*(.+)'
    )
    today = datetime.now().strftime('%Y-%m-%d')
    for line in schedule_text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        match = pattern.match(line)
        if not match:
            logger.warning(f"Schedule parse: skipping malformed line: {line}")
            continue
        start_str, end_str, block_type, title = match.groups()
        try:
            start_dt = datetime.strptime(f"{today} {start_str}", '%Y-%m-%d %H:%M')
            end_dt = datetime.strptime(f"{today} {end_str}", '%Y-%m-%d %H:%M')
            blocks.append({
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "title": f"[{block_type}] {title.strip()}",
                "type": block_type.upper()
            })
        except ValueError as e:
            logger.warning(f"Schedule parse: time parse failed for line '{line}': {e}")
    return blocks

async def get_tasks():
    creds = get_google_credentials()
    if not creds:
        return "Google APIs not configured. Visit http://localhost:8001/auth/google to authenticate, Sir."

    loop = asyncio.get_event_loop()
    try:
        tasks_service = build('tasks', 'v1', credentials=creds)
        
        all_tasks = []
        lists = await loop.run_in_executor(None, lambda: tasks_service.tasklists().list().execute())
        
        for task_list in lists.get('items', []):
            tasks_res = await loop.run_in_executor(None, lambda: tasks_service.tasks().list(
                tasklist=task_list['id'], showHidden=True).execute())
            
            for task in tasks_res.get('items', []):
                if task.get('status') == 'needsAction':
                    all_tasks.append({
                        'id': task.get('id'),
                        'title': task.get('title', 'Untitled'),
                        'notes': task.get('notes', ''),
                        'due_date': task.get('due', ''),
                        'status': task.get('status'),
                        'list_id': task_list['id'],
                        'list_name': task_list['title']
                    })

        all_tasks.sort(key=lambda x: (not bool(x['due_date']), x['due_date']))
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM tasks_cache")
            for t in all_tasks:
                await db.execute('''
                    INSERT INTO tasks_cache (task_id, title, notes, due_date, status, list_id, last_synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (t['id'], t['title'], t['notes'], t['due_date'], t['status'], t['list_id'], datetime.now().isoformat()))
            await db.commit()
            
        logger.info(f"Cached {len(all_tasks)} tasks")
        return all_tasks
    except Exception as e:
        logger.error(f"Error fetching tasks: {e}")
        return await get_cached_tasks()

async def get_cached_tasks():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT task_id, title, notes, due_date, status, list_id FROM tasks_cache WHERE status = 'needsAction'") as cursor:
            rows = await cursor.fetchall()
            return [{'id': r[0], 'title': r[1], 'notes': r[2], 'due_date': r[3], 'status': r[4], 'list_id': r[5]} for r in rows]

async def get_calendar_events(days_ahead=7):
    creds = get_google_credentials()
    if not creds:
        return "Google APIs not configured. Visit http://localhost:8001/auth/google to authenticate, Sir."

    loop = asyncio.get_event_loop()
    try:
        calendar_service = build('calendar', 'v3', credentials=creds)
        now = datetime.utcnow().isoformat() + 'Z'
        future = (datetime.utcnow() + timedelta(days=days_ahead)).isoformat() + 'Z'
        
        events_res = await loop.run_in_executor(None, lambda: calendar_service.events().list(
            calendarId='primary', timeMin=now, timeMax=future,
            singleEvents=True, orderBy='startTime').execute())
            
        events = events_res.get('items', [])
        processed = []
        for e in events:
            start = e['start'].get('dateTime', e['start'].get('date'))
            end = e['end'].get('dateTime', e['end'].get('date'))
            processed.append({
                'id': e.get('id'),
                'title': e.get('summary', 'Busy'),
                'start': start,
                'end': end,
                'description': e.get('description', ''),
                'location': e.get('location', '')
            })

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM calendar_cache")
            for p in processed:
                await db.execute('''
                    INSERT INTO calendar_cache (event_id, title, start_datetime, end_datetime, description, location, last_synced)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (p['id'], p['title'], p['start'], p['end'], p['description'], p['location'], datetime.now().isoformat()))
            await db.commit()
            
        logger.info(f"Cached {len(processed)} events")
        return processed
    except Exception as e:
        logger.error(f"Error fetching calendar: {e}")
        return await get_cached_calendar()

async def get_cached_calendar():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT event_id, title, start_datetime, end_datetime, description, location FROM calendar_cache") as cursor:
            rows = await cursor.fetchall()
            return [{'id': r[0], 'title': r[1], 'start': r[2], 'end': r[3], 'description': r[4], 'location': r[5]} for r in rows]

# --- TELEGRAM ---
async def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured. Skipping notification.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
            if res.status_code == 200:
                logger.info("Telegram notification sent successfully")
            else:
                logger.error(f"Telegram failed: {res.text}")
    except Exception as e:
        logger.error(f"Telegram failed: {e}")

# --- GEMINI USAGE (SHARED LOGIC) ---
async def find_available_slot():
    """Finds best key/model from shared DB, similar to JARVIS."""
    if not GEMINI_KEYS:
        return None, None
        
    today = datetime.now().strftime('%Y-%m-%d')
    best_key_idx = -1
    best_model = None
    min_usage = float('inf')

    async with aiosqlite.connect(DB_PATH) as db:
        for ki, key in enumerate(GEMINI_KEYS):
            for m_name in MODELS:
                limit_idx = MODELS.index(m_name) if m_name in MODELS else 0
                limit = LIMITS[limit_idx] if limit_idx < len(LIMITS) else 500
                
                async with db.execute(
                    "SELECT status, live_used FROM api_key_usage WHERE key_index = ? AND model_name = ? AND date = ?",
                    (ki, m_name, today)
                ) as cursor:
                    row = await cursor.fetchone()
                    
                if row:
                    status, used = row[0], row[1]
                else:
                    status, used = "VALID", 0
                    
                if status == "VALID" and used < (limit * 0.95):
                    if used < min_usage:
                        min_usage = used
                        best_key_idx = ki
                        best_model = m_name

    if best_key_idx != -1 and best_model:
        return best_key_idx, best_model
        
    return 0, MODELS[0] # Fallback

async def increment_local_usage(key_index: int, model_name: str):
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO api_key_usage (key_index, model_name, date, status, local_count, live_used)
            VALUES (?, ?, ?, 'VALID', 1, 1)
            ON CONFLICT(key_index, model_name, date) DO UPDATE SET
            local_count = local_count + 1,
            live_used = live_used + 1
        ''', (key_index, model_name, today))
        await db.commit()

async def call_gemini(prompt: str):
    logger.info("Calling Gemini")
    k_idx, model_name = await find_available_slot()
    if k_idx is None:
         return "Error: No Gemini API keys configured."
         
    key = GEMINI_KEYS[k_idx]
    client = genai.Client(api_key=key)
    
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.2
            )
        )
        await increment_local_usage(k_idx, model_name)
        return response.text
    except Exception as e:
        logger.error(f"Gemini call failed: {e}")
        return "I apologize Sir, I am unable to connect to the mainframe to process that request."

# --- ALFRED LOGIC ---
def is_google_configured():
    try:
        creds_path = CONFIG.get('google_credentials_path', '')
        token_path = CONFIG.get('google_token_path', '')
        # Basic check: does credentials file exist?
        if not creds_path or not os.path.exists(creds_path):
            return False
        return True
    except:
        return False

def get_google_config_error():
    return (
        "Google APIs are not yet configured, Sir. "
        "To connect your Tasks and Calendar, please:\n"
        "1. Add google_credentials_path to config.json\n"
        "2. Add google_token_path to config.json\n"
        "3. Place your credentials.json file at the specified path\n"
        "4. Restart ALFRED to complete OAuth authorization\n\n"
        "I can still answer general scheduling questions without "
        "your live data."
    )

async def build_daily_schedule():
    if not is_google_configured():
        return get_google_config_error()
        
    tasks = await get_tasks()
    events = await get_calendar_events(days_ahead=1)
    
    prompt = f"Today is {datetime.now().strftime('%A, %B %d, %Y')}.\n\n"
    prompt += "Tasks:\n"
    for t in tasks:
        prompt += f"- {t['title']} (Due: {t['due_date']})\n"
        
    prompt += "\nCalendar Events Today:\n"
    for e in events:
        prompt += f"- {e['title']} ({e['start']} to {e['end']})\n"
        
    prompt += "\nPlease produce a time-blocked daily schedule based on these constraints."
    
    schedule_text = await call_gemini(prompt)
    
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO daily_schedules (date, schedule_text, schedule_json, created_at, velocity_score, approved)
            VALUES (?, ?, ?, ?, 0, 0)
        ''', (today, schedule_text, json.dumps({"text": schedule_text}), datetime.now().isoformat()))
        await db.commit()
        
    # Update velocity metrics
    await update_velocity_for_today()
        
    return schedule_text

async def update_velocity_for_today():
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        # Sync first
        await get_tasks()
        
        # Count from tasks_cache for today
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT COUNT(*) as total FROM tasks_cache"
            ) as cursor:
                total_row = await cursor.fetchone()
                total = total_row['total'] if total_row else 0

            async with db.execute(
                "SELECT COUNT(*) as done FROM tasks_cache "
                "WHERE status = 'completed'"
            ) as cursor:
                done_row = await cursor.fetchone()
                completed = done_row['done'] if done_row else 0

            score = calculate_velocity_score(completed, total)

            await db.execute("""
                INSERT OR REPLACE INTO task_velocity
                    (date, tasks_completed, tasks_total, velocity_score)
                VALUES (?, ?, ?, ?)
            """, (today, completed, total, score))
            await db.commit()
            logger.info(f"Velocity updated: {completed}/{total} ({score}%)")
    except Exception as e:
        logger.error(f"Velocity update error: {e}")

async def record_daily_velocity():
    """
    Runs at 23:55. Counts today's completed vs total tasks from cache
    and writes a velocity record to task_velocity table.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        # Sync first
        await get_tasks()
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Count from tasks_cache — completed today
            async with db.execute(
                "SELECT COUNT(*) FROM tasks_cache WHERE status='completed' "
                "AND date(last_synced)=?", (today,)
            ) as cur:
                completed = (await cur.fetchone())[0]
            # Total tasks seen today
            async with db.execute(
                "SELECT COUNT(*) FROM tasks_cache "
                "WHERE date(last_synced)=?", (today,)
            ) as cur:
                total = (await cur.fetchone())[0]

        score = calculate_velocity_score(completed, total)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO task_velocity
                (date, tasks_completed, tasks_total, velocity_score)
                VALUES (?, ?, ?, ?)
            """, (today, completed, total, score))
            await db.commit()

        logger.info(
            f"Velocity snapshot: {completed}/{total} tasks "
            f"({score}%) on {today}"
        )
    except Exception as e:
        logger.error(f"Velocity snapshot failed: {e}")

async def build_weekly_roadmap():
    if not is_google_configured():
        return get_google_config_error()
        
    tasks = await get_tasks()
    events = await get_calendar_events(days_ahead=7)
    
    prompt = f"Today is {datetime.now().strftime('%A, %B %d, %Y')}.\n\n"
    prompt += "Tasks for the week:\n"
    for t in tasks:
        prompt += f"- {t['title']} (Due: {t['due_date']})\n"
        
    prompt += "\nCalendar Events Next 7 Days:\n"
    for e in events:
        prompt += f"- {e['title']} ({e['start']} to {e['end']})\n"
        
    prompt += "\nPlease produce a day-by-day roadmap for the next 7 days."
    
    return await call_gemini(prompt)

async def get_velocity_report() -> str:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT date, tasks_completed, tasks_total,
                       velocity_score, notes
                FROM task_velocity
                ORDER BY date DESC
                LIMIT 14
            """) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return ("No velocity data available yet, Sir. "
                    "Complete some tasks and check back tomorrow "
                    "once ALFRED has tracked your first day.")

        # Build data summary for Gemini
        data_lines = []
        total_completed = 0
        total_tasks = 0
        for row in rows:
            completed = row['tasks_completed'] or 0
            total = row['tasks_total'] or 0
            score = row['velocity_score'] or 0
            total_completed += completed
            total_tasks += total
            data_lines.append(
                f"{row['date']}: {completed}/{total} tasks completed "
                f"(score: {score:.1f})"
                + (f" — {row['notes']}" if row['notes'] else "")
            )

        # Calculate basic stats
        days_tracked = len(rows)
        avg_completion = (total_completed / total_tasks * 100
                         if total_tasks > 0 else 0)

        # Build scores list for trend
        scores = [row['velocity_score'] or 0 for row in rows]
        scores.reverse()  # chronological order
        recent_avg = sum(scores[-3:]) / len(scores[-3:]) if scores else 0
        older_avg = sum(scores[:3]) / len(scores[:3]) if len(scores) >= 3 else recent_avg
        trend = "improving" if recent_avg > older_avg else (
                "declining" if recent_avg < older_avg else "stable")

        data_summary = "\n".join(data_lines)

        prompt = f"""Analyze this productivity data for the last {days_tracked} days:

{data_summary}

Summary statistics:
- Average completion rate: {avg_completion:.1f}%
- Recent trend: {trend}
- Total tasks completed: {total_completed} out of {total_tasks}

Based on this REAL data, provide:
1. A clear assessment of current productivity trend
2. The best and worst performing days with specific dates
3. 3 concrete actionable recommendations to improve
4. One honest observation about a pattern you notice

Be direct and specific. Reference the actual dates and numbers."""

        return await call_gemini(prompt)

    except Exception as e:
        logger.error(f"Velocity report error: {e}")
        return f"Unable to generate velocity report, Sir. Error: {str(e)}"

async def list_tasks():
    tasks = await get_tasks()
    if not tasks:
        return "You have no active tasks, Sir."
    res = "Active Tasks:\n"
    for t in tasks[:10]:
        res += f"- {t['title']}\n"
    if len(tasks) > 10:
        res += f"...and {len(tasks)-10} more."
    return res

async def morning_briefing():
    logger.info("Generating morning briefing")
    if not is_google_configured():
        # Don't return error text for cron job, just log it
        logger.warning("Morning briefing skipped: Google not configured")
        return "Google APIs not configured, Sir."
        
    schedule = await build_daily_schedule()
    tasks = await get_cached_tasks()
    
    overdue = [t for t in tasks if t['due_date'] and t['due_date'] < datetime.now().isoformat() and t['status'] != 'completed']
    
    briefing = f"Good morning, Sir.\n\n"
    if overdue:
        briefing += f"You have {len(overdue)} overdue tasks that require attention.\n\n"
        
    briefing += "Today's Schedule:\n"
    briefing += schedule
    
    await send_telegram(briefing)
    return briefing

from fastapi.responses import JSONResponse

# --- FASTAPI APP ---
app = FastAPI(title="ALFRED Agent")
scheduler = AsyncIOScheduler()

@app.get("/auth/google")
async def google_auth_start():
    """
    Step 1 of OAuth. Call this to get the URL to open in your browser.
    Returns plain text with the URL.
    Usage: curl http://localhost:8001/auth/google
    Then open the URL in your browser, complete auth, copy the code.
    Then call /auth/callback?code=YOUR_CODE
    """
    creds_path = CONFIG.get("google_credentials_path", "")
    if not creds_path or not os.path.exists(creds_path):
        return JSONResponse(
            {"error": "credentials.json not found. Set google_credentials_path in config.json"},
            status_code=400
        )
    try:
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_secrets_file(
            creds_path,
            scopes=SCOPES,
            redirect_uri="urn:ietf:wg:oauth:2.0:oob"
        )
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true"
        )
        # Store flow state temporarily
        app.state.oauth_flow = flow
        return JSONResponse({
            "auth_url": auth_url,
            "instructions": (
                "1. Open the auth_url in your browser\n"
                "2. Sign in and allow access\n"
                "3. Copy the authorization code shown\n"
                "4. POST to /auth/callback with body: {\"code\": \"YOUR_CODE\"}"
            )
        })
    except Exception as e:
        logger.error(f"OAuth start failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/auth/callback")
async def google_auth_callback(request: Request):
    """
    Step 2 of OAuth. Send the code from your browser here.
    Body: {"code": "4/0AX..."}
    Saves token.json to google_token_path from config.json.
    """
    body = await request.json()
    code = body.get("code", "").strip()
    if not code:
        return JSONResponse({"error": "code is required"}, status_code=400)

    flow = getattr(app.state, "oauth_flow", None)
    if not flow:
        return JSONResponse(
            {"error": "No active OAuth flow. Call /auth/google first."},
            status_code=400
        )
    try:
        flow.fetch_token(code=code)
        token_path = CONFIG.get("google_token_path", "token.json")
        creds = flow.credentials
        with open(token_path, "w") as f:
            f.write(creds.to_json())
        app.state.oauth_flow = None
        logger.info(f"Google OAuth token saved to {token_path}")
        return JSONResponse({
            "status": "ok",
            "message": "Authentication successful. Token saved. ALFRED can now access Google APIs.",
            "token_path": token_path
        })
    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.on_event("startup")
async def startup_event():
    await init_db()
    
    # Schedule morning briefing at 8am
    scheduler.add_job(morning_briefing, 'cron', hour=8, minute=0)
    
    # End-of-day velocity snapshot — runs at 23:55 every night
    scheduler.add_job(
        record_daily_velocity,
        trigger="cron",
        hour=23,
        minute=55,
        id="daily_velocity_snapshot",
        replace_existing=True
    )
    
    scheduler.start()
    logger.info("ALFRED started and scheduler running")
    logger.warning(
        "SCOPE UPGRADE: If you get 'insufficient_permission' errors, "
        "delete token.json and re-authenticate via GET /auth/google"
    )

@app.on_event("shutdown")
async def shutdown_event():
    scheduler.shutdown()
    logger.info("ALFRED shut down")

class RunRequest(BaseModel):
    task: str = ""
    context: str = ""
    user_input: str = ""

@app.post("/run")
async def run_agent(req: RunRequest):
    inp = req.user_input.lower()
    
    logger.info(f"Received request: {inp}")
    
    if any(w in inp for w in ["schedule", "today", "plan my day"]):
        reply = await build_daily_schedule()
    elif any(w in inp for w in ["roadmap", "this week", "weekly"]):
        reply = await build_weekly_roadmap()
    elif any(w in inp for w in ["velocity", "how am i doing", "progress"]):
        reply = await get_velocity_report()
    elif any(w in inp for w in ["tasks", "what do i have", "list"]):
        reply = await list_tasks()
    elif any(w in inp for w in ["briefing", "morning", "summary"]):
        reply = await morning_briefing()
    else:
        # Fallback to general Gemini call with context
        prompt = f"User request: {req.user_input}\nContext: {req.context}\nPlease answer the user directly and concisely."
        reply = await call_gemini(prompt)
        
    return {"reply": reply, "agent": "ALFRED", "status": "ok"}

@app.post("/schedule/regenerate")
async def regenerate_schedule():
    """Delete today's schedule and rebuild fresh."""
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM daily_schedules WHERE date = ?", (today,)
        )
        await db.commit()
    text = await build_daily_schedule()
    return {"reply": text, "agent": "ALFRED", "status": "ok"}


@app.post("/schedule/approve")
async def approve_schedule():
    """
    Parse today's schedule and write each time block
    to Google Calendar as individual events.
    """
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT schedule_text FROM daily_schedules WHERE date = ?",
            (today,)
        ) as cur:
            row = await cur.fetchone()

    if not row or not row['schedule_text']:
        return JSONResponse(
            {"error": "No schedule found for today. Generate one first."},
            status_code=404
        )

    creds = get_google_credentials()
    if not creds:
        return JSONResponse(
            {"error": "Google not authenticated. Visit /auth/google"},
            status_code=401
        )

    blocks = parse_schedule_lines(row['schedule_text'])
    if not blocks:
        return JSONResponse(
            {"error": "Schedule format not recognized. No valid HH:MM - HH:MM | [TYPE] lines found."},
            status_code=400
        )

    loop = asyncio.get_event_loop()
    created = []
    failed = []

    tz = CONFIG.get("user_timezone", "UTC")

    try:
        calendar_service = build('calendar', 'v3', credentials=creds)
        for block in blocks:
            try:
                event = {
                    'summary': block['title'],
                    'start': {'dateTime': block['start'],
                              'timeZone': tz},
                    'end': {'dateTime': block['end'],
                            'timeZone': tz},
                    'colorId': {
                        'TASK': '9', 'MEETING': '11',
                        'BREAK': '2', 'BUFFER': '8'
                    }.get(block['type'], '1')
                }
                created_event = await loop.run_in_executor(
                    None,
                    lambda e=event: calendar_service.events()
                    .insert(calendarId='primary', body=e).execute()
                )
                created.append(block['title'])
                logger.info(f"Calendar event created: {block['title']}")
            except Exception as e:
                failed.append(block['title'])
                logger.error(f"Failed to create event '{block['title']}': {e}")

        # Mark as approved in DB
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE daily_schedules SET approved = 1 WHERE date = ?",
                (today,)
            )
            await db.commit()

        msg = f"{len(created)} events added to Google Calendar."
        if failed:
            msg += f" {len(failed)} failed: {', '.join(failed)}"
        return {"reply": msg, "agent": "ALFRED",
                "status": "ok", "created": created, "failed": failed}

    except Exception as e:
        logger.error(f"Calendar write failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/schedule/delete")
async def delete_schedule():
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM daily_schedules WHERE date = ?", (today,)
        )
        await db.commit()
    return {"reply": "Today's schedule deleted.", "agent": "ALFRED", "status": "ok"}


@app.post("/schedule/edit")
async def edit_schedule(request: Request):
    """Edit a specific line in today's schedule by index."""
    body = await request.json()
    block_index = body.get("block_index")
    new_text = body.get("new_text", "").strip()
    if block_index is None or not new_text:
        return JSONResponse(
            {"error": "block_index and new_text required"},
            status_code=400
        )
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT schedule_text FROM daily_schedules WHERE date = ?",
            (today,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row['schedule_text']:
        return JSONResponse({"error": "No schedule found."}, status_code=404)

    lines = row['schedule_text'].strip().split('\n')
    if block_index < 0 or block_index >= len(lines):
        return JSONResponse(
            {"error": f"block_index {block_index} out of range (0-{len(lines)-1})"},
            status_code=400
        )
    lines[block_index] = new_text
    updated_text = '\n'.join(lines)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE daily_schedules SET schedule_text = ?, approved = 0 WHERE date = ?",
            (updated_text, today)
        )
        await db.commit()
    return {"reply": updated_text, "agent": "ALFRED", "status": "ok"}


@app.post("/tasks/add")
async def add_task(request: Request):
    body = await request.json()
    title = body.get("title", "").strip()
    due_date = body.get("due_date", "").strip()
    notes = body.get("notes", "").strip()
    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)

    creds = get_google_credentials()
    if not creds:
        return JSONResponse({"error": "Google not authenticated."}, status_code=401)

    loop = asyncio.get_event_loop()
    try:
        tasks_service = build('tasks', 'v1', credentials=creds)
        task_body = {"title": title}
        if notes:
            task_body["notes"] = notes
        if due_date:
            # Google Tasks due dates must be RFC 3339 UTC midnight
            task_body["due"] = f"{due_date}T00:00:00.000Z"

        created = await loop.run_in_executor(
            None,
            lambda: tasks_service.tasks()
            .insert(tasklist='@default', body=task_body).execute()
        )
        logger.info(f"Task created: {title}")
        return {
            "reply": f"Task '{title}' added successfully, Sir.",
            "agent": "ALFRED",
            "status": "ok",
            "task_id": created.get('id')
        }
    except Exception as e:
        logger.error(f"Add task failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/tasks/complete")
async def complete_task(request: Request):
    body = await request.json()
    task_id = body.get("task_id", "").strip()
    if not task_id:
        return JSONResponse({"error": "task_id required"}, status_code=400)

    creds = get_google_credentials()
    if not creds:
        return JSONResponse({"error": "Google not authenticated."}, status_code=401)

    loop = asyncio.get_event_loop()
    try:
        tasks_service = build('tasks', 'v1', credentials=creds)
        await loop.run_in_executor(
            None,
            lambda: tasks_service.tasks().patch(
                tasklist='@default',
                task=task_id,
                body={"status": "completed"}
            ).execute()
        )
        # Update local cache
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tasks_cache SET status='completed' WHERE task_id=?",
                (task_id,)
            )
            await db.commit()
        return {
            "reply": "Task marked complete, Sir.",
            "agent": "ALFRED",
            "status": "ok"
        }
    except Exception as e:
        logger.error(f"Complete task failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/tasks/overdue")
async def get_overdue_tasks():
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM tasks_cache
            WHERE status = 'needsAction'
            AND due_date IS NOT NULL
            AND due_date < ?
            ORDER BY due_date ASC
        """, (today,)) as cur:
            rows = await cursor.fetchall()
    tasks = [dict(r) for r in rows]
    if not tasks:
        return {"reply": "No overdue tasks, Sir.", "tasks": [],
                "agent": "ALFRED", "status": "ok"}
    lines = [f"- {t['title']} (due {t['due_date']})" for t in tasks]
    return {
        "reply": f"Overdue tasks ({len(tasks)}):\n" + "\n".join(lines),
        "tasks": tasks,
        "agent": "ALFRED",
        "status": "ok"
    }


@app.get("/tasks/today")
async def get_tasks_today():
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM tasks_cache
            WHERE status = 'needsAction'
            AND due_date = ?
            ORDER BY title ASC
        """, (today,)) as cur:
            rows = await cur.fetchall()
    tasks = [dict(r) for r in rows]
    if not tasks:
        return {"reply": "No tasks due today, Sir.", "tasks": [],
                "agent": "ALFRED", "status": "ok"}
    lines = [f"- {t['title']}" for t in tasks]
    return {
        "reply": f"Tasks due today ({len(tasks)}):\n" + "\n".join(lines),
        "tasks": tasks,
        "agent": "ALFRED",
        "status": "ok"
    }


@app.get("/tasks/week")
async def get_tasks_week():
    today = datetime.now()
    week_end = (today + timedelta(days=7)).strftime('%Y-%m-%d')
    today_str = today.strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM tasks_cache
            WHERE status = 'needsAction'
            AND due_date BETWEEN ? AND ?
            ORDER BY due_date ASC
        """, (today_str, week_end)) as cur:
            rows = await cursor.fetchall()
    tasks = [dict(r) for r in rows]
    if not tasks:
        return {"reply": "No tasks this week, Sir.", "tasks": [],
                "agent": "ALFRED", "status": "ok"}
    lines = [f"- {t['title']} (due {t['due_date']})" for t in tasks]
    return {
        "reply": f"Tasks this week ({len(tasks)}):\n" + "\n".join(lines),
        "tasks": tasks,
        "agent": "ALFRED",
        "status": "ok"
    }


@app.post("/sync")
async def force_sync():
    """Force sync both Google Tasks and Calendar. Returns counts."""
    tasks = await get_tasks()
    events = await get_calendar_events(days_ahead=7)
    return {
        "reply": f"Sync complete, Sir. {len(tasks)} tasks and {len(events)} calendar events loaded.",
        "tasks_count": len(tasks),
        "events_count": len(events),
        "agent": "ALFRED",
        "status": "ok"
    }


@app.get("/velocity/today")
async def get_velocity_today():
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM task_velocity WHERE date = ?", (today,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {"reply": "No velocity data for today yet, Sir.",
                "score": None, "agent": "ALFRED", "status": "ok"}
    return {
        "reply": f"Today's velocity: {row['velocity_score']}% ({row['tasks_completed']}/{row['tasks_total']} tasks completed)",
        "score": row['velocity_score'],
        "completed": row['tasks_completed'],
        "total": row['tasks_total'],
        "agent": "ALFRED",
        "status": "ok"
    }


@app.post("/briefing/send")
async def send_briefing():
    """Regenerate morning briefing and send to Telegram."""
    text = await morning_briefing()
    token = CONFIG.get("telegram_token", "")
    chat_id = CONFIG.get("telegram_chat_id", "")
    sent = False
    if token and chat_id:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=10.0
                )
            sent = True
            logger.info("Briefing sent to Telegram via /briefing/send")
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
    return {
        "reply": text,
        "telegram_sent": sent,
        "agent": "ALFRED",
        "status": "ok"
    }

@app.get("/health")
async def health_check():
    has_tasks = False
    has_calendar = False
    try:
        creds = get_google_credentials()
        if creds:
            try:
                build('tasks', 'v1', credentials=creds)
                has_tasks = True
            except: pass
            try:
                build('calendar', 'v3', credentials=creds)
                has_calendar = True
            except: pass
    except:
        pass
        
    return {
        "status": "ok",
        "agent": "ALFRED",
        "port": 8001,
        "google_tasks": has_tasks,
        "google_calendar": has_calendar
    }

@app.get("/tasks")
async def list_tasks_endpoint():
    tasks = await get_cached_tasks()
    return {"tasks": tasks}

@app.get("/schedule/today")
async def get_today_schedule():
    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT schedule_json FROM daily_schedules WHERE date = ?", (today,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
            
    # Generate if not exists
    schedule = await build_daily_schedule()
    return {"text": schedule}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)