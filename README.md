# smos-ui

Standalone FastAPI app for GTD-style task management over `.smos` files, with a generic markdown inbox and optional CLI-powered enhancements.

## Features

- Project-agnostic `.smos` CRUD API and simple web UI
- Generic inbox markdown flow (`INBOX`, `DONE`, `REMOVED`)
- Reports endpoint with automatic fallback:
  - Uses `smos-query` when available (or forced)
  - Falls back to native Python report generation when unavailable
- Optional embedded terminal support with `ttyd + smos`
- Optional delegation email support via SMTP
- Capability endpoint with runtime detection and feature status

## Environment toggles

- `GTD_MODE=auto|native|smos`
- `GTD_ENABLE_TERMINAL=auto|0|1`
- `GTD_ENABLE_DELEGATION_EMAIL=auto|0|1`

Copy `.env.example` to `.env` and adjust values as needed.

## Quickstart

### Windows (PowerShell)

```powershell
cd projects/smos-ui
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Open: http://127.0.0.1:8000

### macOS / Linux

```bash
cd projects/smos-ui
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Open: http://127.0.0.1:8000

## Optional binaries

- `smos` and `smos-query` for CLI-backed mode and reports
- `ttyd` + `smos` for web terminal route (`/gtd/terminal`)

If missing, the app still starts and uses graceful fallback behavior.

## API overview

- `GET /capabilities`
- `GET /gtd/data`
- `GET /gtd/inbox`
- `POST /gtd/inbox/add`
- `POST /gtd/inbox/resolve`
- `POST /gtd/folder/create`
- `POST /gtd/folder/delete`
- `POST /gtd/file/create`
- `POST /gtd/file/rename`
- `POST /gtd/file/delete`
- `POST /gtd/task/create`
- `POST /gtd/task/update`
- `POST /gtd/task/state`
- `POST /gtd/task/delete`
- `POST /gtd/task/restore`
- `POST /gtd/task/delegate`
- `GET /gtd/report/{kind}`

## Sample workflow

`sample-workflow/` includes demo `.smos` files:
- `Inbox.smos`
- `Projects/Website-Refresh.smos`
- `Projects/Operations.smos`
