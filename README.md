# AVENGERS SYSTEM

A personal multi-agent AI system running locally on PC, fully free, exportable via Docker.

## Architecture

The AVENGERS SYSTEM is designed as a distributed network of specialized agents.
- **JARVIS (Orchestrator):** The central entry point and UI provider. It handles intent classification and transparently routes requests to specialized agents.
- **Agent Mesh:** Agents operate as independent HTTP microservices listening on specific ports. Communication is handled via standard REST protocols.
- **Shared Data Layer:** A unified SQLite database (`core.db`) manages state, Gemini API quota rotation, and shared caches across the entire system.
- **Security:** All sensitive credentials, API keys, and OAuth tokens are centralized in `config.json` (ignored by Git).
- **Communication:** Integrated with Telegram Bot API for asynchronous briefings and priority alerts.

## Agents

| Agent | Port | Status | Description |
| :--- | :--- | :--- | :--- |
| **JARVIS** | 8000 | ✅ Working | System Orchestrator & UI Entry Point |
| **ALFRED** | 8001 | ✅ Working | Work Routine + Roadmap Intelligence |
| **VISION** | 8002 | 🔧 Coming Soon | Design Ideation & Visualization |
| **BANNER** | 8003 | 🔧 Coming Soon | n8n Automation & Logic Engine |
| **STARK** | 8004 | 🔧 Coming Soon | Web Builder & GitHub Integration |
| **NEBULA** | 8005 | 🔧 Coming Soon | Image & Asset Creation |
| **COULSON** | 8006 | 🔧 Coming Soon | Dependency & System Manager |
| **PEPPER** | 8007 | 🔧 Coming Soon | Routine & Inventory Management |
| **XAVIER** | 8008 | 🔧 Coming Soon | Decision Critic & Logic Auditor |
| **FRIDAY** | 8009 | 🔧 Coming Soon | Software Learning & Research |
| **SHURI** | 8010 | 🔧 Coming Soon | ECE & Robotics Knowledge Base |
| **RHODEY** | 8011 | 🔧 Coming Soon | Health & Fitness Tracking |
| **WONG** | 8012 | 🔧 Coming Soon | Finance & Budget Tracking |

## Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/CodingDrago/AVENGERS.git
   cd AVENGERS
   ```

2. **Configure Environment:**
   - Copy `config.example.json` to `config.json`.
   - Fill in your Gemini API keys, Telegram credentials, and Google API paths.

3. **Install Dependencies:**
   ```bash
   # For JARVIS
   pip install -r agents/jarvis/requirements.txt
   # For ALFRED
   pip install -r agents/alfred/requirements.txt
   ```

4. **Execution:**
   - Start JARVIS: `uvicorn agents.jarvis.main:app --port 8000`
   - Start ALFRED: `uvicorn agents.alfred.main:app --port 8001`
   - Alternatively, use `docker-compose up --build` (Experimental).

## Stack

- **Core:** Python 3.11, FastAPI
- **LLM:** Gemini API (2.0 Flash / 1.5 Flash)
- **APIs:** Google Tasks API, Google Calendar API, Telegram Bot API
- **Storage:** SQLite (aiosqlite)
- **Automation:** APScheduler, Playwright
- **Deployment:** Docker, Docker Compose

## Project Structure

```text
AVENGERS/
├── agents/
│   ├── alfred/
│   ├── jarvis/
│   └── [other agents]/
├── db/
│   └── core.db
├── outputs/
│   ├── alfred/
│   └── jarvis/
├── config.json
├── docker-compose.yml
└── README.md
```
