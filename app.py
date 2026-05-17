from __future__ import annotations

import json
import os
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

try:
    from dotenv import load_dotenv
except Exception:  # optional dependency
    def load_dotenv(*args, **kwargs):
        return False

load_dotenv()

APP_ROOT = Path(__file__).resolve().parent
WORKFLOW_DIR = Path(os.getenv("GTD_WORKFLOW_DIR", str(APP_ROOT / "sample-workflow"))).expanduser().resolve()
INBOX_FILE = Path(os.getenv("GTD_INBOX_FILE", str(APP_ROOT / "workflow-inbox.md"))).expanduser().resolve()
TERMINAL_PORT = int(os.getenv("GTD_TTYD_PORT", "7681"))
TERMINAL_BASE_PATH = os.getenv("GTD_TTYD_BASE_PATH", "/gtd-terminal")
TERMINAL_FONT_SIZE = int(os.getenv("GTD_TTYD_FONT_SIZE", "16"))
TERMINAL_LOG_FILE = Path(os.getenv("GTD_TTYD_LOG_FILE", str(APP_ROOT / "ttyd.log"))).expanduser().resolve()

GTD_MODE_REQUESTED = os.getenv("GTD_MODE", "auto").strip().lower() or "auto"
TERM_TOGGLE_REQUESTED = os.getenv("GTD_ENABLE_TERMINAL", "auto").strip().lower() or "auto"
DELEGATION_TOGGLE_REQUESTED = os.getenv("GTD_ENABLE_DELEGATION_EMAIL", "auto").strip().lower() or "auto"

SMTP_HOST = os.getenv("GTD_SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("GTD_SMTP_PORT", "587"))
SMTP_USER = os.getenv("GTD_SMTP_USER", "").strip()
SMTP_PASS = os.getenv("GTD_SMTP_PASS", "")
SMTP_FROM = os.getenv("GTD_SMTP_FROM", "").strip()
SMTP_STARTTLS = os.getenv("GTD_SMTP_STARTTLS", "1").strip() not in {"0", "false", "False"}

SMOS_BIN = shutil.which("smos")
SMOS_QUERY_BIN = shutil.which("smos-query")
TTYD_BIN = shutil.which("ttyd")

app = FastAPI(title="smos-ui", version="0.1.0")

_ttyd_lock = threading.Lock()
_ttyd_process: subprocess.Popen | None = None

SMOS_ACTIVE_STATES = {"TODO", "NEXT", "WAITING", "READY", "STARTED"}
SMOS_TERMINAL_STATES = {"DONE", "CANCELLED", "FAILED"}
SMOS_ALLOWED_STATES = SMOS_ACTIVE_STATES | SMOS_TERMINAL_STATES
SMOS_NO_STATE_SENTINELS = {"", "NONE", "NO_STATE", "__NONE__"}


def _parse_toggle(raw: str) -> str:
    v = (raw or "auto").strip().lower()
    if v in {"0", "false", "off", "no"}:
        return "0"
    if v in {"1", "true", "on", "yes"}:
        return "1"
    return "auto"


def _smtp_ready() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def _terminal_ready_binaries() -> tuple[bool, str | None]:
    if not TTYD_BIN:
        return False, "ttyd binary not found"
    if not SMOS_BIN:
        return False, "smos binary not found"
    return True, None


def _terminal_enabled() -> tuple[bool, str | None]:
    toggle = _parse_toggle(TERM_TOGGLE_REQUESTED)
    ready, reason = _terminal_ready_binaries()
    if toggle == "0":
        return False, "disabled by GTD_ENABLE_TERMINAL=0"
    if toggle == "1":
        return (ready, reason)
    if ready:
        return True, None
    return False, reason


def _delegation_enabled() -> tuple[bool, str | None]:
    toggle = _parse_toggle(DELEGATION_TOGGLE_REQUESTED)
    ready = _smtp_ready()
    if toggle == "0":
        return False, "disabled by GTD_ENABLE_DELEGATION_EMAIL=0"
    if toggle == "1":
        if ready:
            return True, None
        return False, "SMTP not configured"
    if ready:
        return True, None
    return False, "SMTP not configured"


def _effective_mode() -> tuple[str, str]:
    requested = GTD_MODE_REQUESTED if GTD_MODE_REQUESTED in {"auto", "native", "smos"} else "auto"
    if requested == "native":
        return "native", "forced native mode"
    if requested == "smos":
        if SMOS_QUERY_BIN:
            return "smos", "forced smos mode"
        return "native", "smos-query missing, using native fallback"
    if SMOS_QUERY_BIN:
        return "smos", "auto detected smos-query"
    return "native", "smos-query missing, using native mode"


def _ensure_defaults() -> None:
    WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    if not INBOX_FILE.exists():
        INBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
        INBOX_FILE.write_text(
            "# Workflow Inbox\n\n"
            "Purpose: shared inbox for captured work items and completion notes.\n\n"
            "## INBOX\n"
            "\n"
            "## DONE\n"
            "\n"
            "## REMOVED\n",
            encoding="utf-8",
        )


def _is_local_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def ensure_ttyd_running() -> tuple[bool, str | None]:
    global _ttyd_process

    enabled, reason = _terminal_enabled()
    if not enabled:
        return False, reason

    if _is_local_port_open(TERMINAL_PORT):
        return True, None

    with _ttyd_lock:
        if _is_local_port_open(TERMINAL_PORT):
            return True, None

        if _ttyd_process is not None and _ttyd_process.poll() is None:
            return True, None

        WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
        TERMINAL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            TTYD_BIN or "ttyd",
            "--writable",
            "--port",
            str(TERMINAL_PORT),
            "--base-path",
            TERMINAL_BASE_PATH,
            "--client-option",
            f"fontSize={TERMINAL_FONT_SIZE}",
            SMOS_BIN or "smos",
            "--workflow-dir",
            str(WORKFLOW_DIR),
            "--projects-dir",
            str(WORKFLOW_DIR),
            str(WORKFLOW_DIR),
        ]

        try:
            with TERMINAL_LOG_FILE.open("a", encoding="utf-8") as logf:
                _ttyd_process = subprocess.Popen(
                    cmd,
                    cwd=str(APP_ROOT),
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception as exc:
            return False, f"failed to start ttyd: {exc}"

    for _ in range(20):
        if _is_local_port_open(TERMINAL_PORT):
            return True, None
        time.sleep(0.1)

    return False, f"ttyd did not become ready on port {TERMINAL_PORT}"


def _smos_now_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_ymd(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    s = str(raw).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date().isoformat()
    except Exception:
        return None


def _normalize_smos_state(raw: str | None) -> str:
    s = (raw or "TODO").strip().upper()
    if s in SMOS_ALLOWED_STATES:
        return s
    return "TODO"


def _parse_smos_state_time(raw: Any) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _normalize_state_history(hist: Any) -> list[dict]:
    if not isinstance(hist, list):
        return []
    entries: list[dict] = [x for x in hist if isinstance(x, dict)]
    if len(entries) <= 1:
        return entries

    decorated: list[tuple[int, float, int, dict]] = []
    for idx, item in enumerate(entries):
        dt = _parse_smos_state_time(item.get("time"))
        if dt is None:
            decorated.append((1, 0.0, idx, item))
        else:
            decorated.append((0, -dt.timestamp(), idx, item))

    decorated.sort(key=lambda x: (x[0], x[1], x[2]))
    return [item for _, __, ___, item in decorated]


def _node_header(node: dict) -> str:
    if isinstance(node.get("entry"), dict):
        return str(node["entry"].get("header") or "").strip()
    if isinstance(node.get("entry"), str):
        return str(node.get("entry") or "").strip()
    return str(node.get("header") or "").strip()


def _node_contents(node: dict) -> str:
    if isinstance(node.get("entry"), dict):
        return str(node["entry"].get("contents") or "")
    return str(node.get("contents") or "")


def _node_forest(node: dict) -> list:
    forest = node.get("forest")
    if isinstance(forest, list):
        return forest
    node["forest"] = []
    return node["forest"]


def _node_timestamps(node: dict) -> dict:
    ts = node.get("timestamps")
    if isinstance(ts, dict):
        return ts
    node["timestamps"] = {}
    return node["timestamps"]


def _node_properties(node: dict) -> dict:
    props = node.get("properties")
    if isinstance(props, dict):
        return props
    node["properties"] = {}
    return node["properties"]


def _set_node_header(node: dict, value: str) -> None:
    if isinstance(node.get("entry"), dict):
        node["entry"]["header"] = value
    elif isinstance(node.get("entry"), str):
        node["entry"] = value
    else:
        node["header"] = value


def _set_node_contents(node: dict, value: str) -> None:
    if isinstance(node.get("entry"), dict):
        node["entry"]["contents"] = value
    elif isinstance(node.get("entry"), str):
        node["entry"] = {"header": node.get("entry") or "", "contents": value}
    else:
        node["contents"] = value


def _set_node_state(node: dict, state: str) -> None:
    raw_state = (state or "").strip().upper()
    if raw_state in SMOS_NO_STATE_SENTINELS:
        node.pop("state-history", None)
        return

    canonical = _normalize_smos_state(raw_state)
    hist = _normalize_state_history(node.get("state-history"))
    node["state-history"] = hist
    if hist and _normalize_smos_state(hist[0].get("state")) == canonical:
        return
    hist.insert(0, {"state": canonical, "time": _smos_now_timestamp()})


def _set_node_timestamp(node: dict, key: str, value: str | None) -> None:
    ts = _node_timestamps(node)
    if value:
        ts[key] = value
    else:
        ts.pop(key, None)
    if not ts:
        node.pop("timestamps", None)


def _safe_rel_folder_path(raw: str) -> str:
    s = str(raw or "").strip().replace("\\", "/")
    if not s:
        raise ValueError("folder is required")
    parts = [p for p in s.split("/") if p and p != "."]
    if not parts:
        raise ValueError("invalid folder path")
    if any(p == ".." for p in parts):
        raise ValueError("path traversal is not allowed")
    return "/".join(parts)


def _safe_rel_smos_file_path(raw: str) -> str:
    rel = _safe_rel_folder_path(raw)
    parts = rel.split("/")
    if not parts[-1].lower().endswith(".smos"):
        parts[-1] = parts[-1] + ".smos"
    return "/".join(parts)


def _write_yaml_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp") as tf:
        yaml.safe_dump(data, tf, sort_keys=False, allow_unicode=True)
        tmp_name = tf.name
    Path(tmp_name).replace(path)


def _scan_smos_files(workflow_dir: Path) -> list[Path]:
    if not workflow_dir.exists():
        return []
    out: list[Path] = []
    for p in workflow_dir.rglob("*.smos"):
        try:
            rel = p.relative_to(workflow_dir)
        except Exception:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        out.append(p)
    out.sort(key=lambda x: str(x).lower())
    return out


def _scan_smos_folders(workflow_dir: Path) -> list[str]:
    if not workflow_dir.exists():
        return []
    out: list[str] = []
    for p in workflow_dir.iterdir():
        if p.is_dir() and not p.name.startswith("."):
            out.append(p.name)
    out.sort(key=str.lower)
    return out


def _current_smos_state(node: dict) -> str:
    hist = _normalize_state_history(node.get("state-history"))
    if hist:
        return _normalize_smos_state(hist[0].get("state"))
    return ""


def _task_id(rel_file: str, path_parts: list[int]) -> str:
    return f"{rel_file}::{'.'.join(str(x) for x in path_parts)}"


def _decode_task_id(raw: str) -> tuple[str, list[int]]:
    if "::" not in raw:
        raise ValueError("invalid id")
    rel_file, raw_path = raw.split("::", 1)
    rel_file = _safe_rel_smos_file_path(rel_file)
    parts = [p for p in raw_path.split(".") if p != ""]
    if not parts:
        raise ValueError("invalid id path")
    try:
        idxs = [int(x) for x in parts]
    except Exception as exc:
        raise ValueError("invalid id path") from exc
    return rel_file, idxs


def _find_node_by_path(root_nodes: list, path_parts: list[int]) -> dict:
    if not path_parts:
        raise ValueError("empty path")
    cur_list = root_nodes
    node: dict | None = None
    for i, idx in enumerate(path_parts):
        if not isinstance(cur_list, list) or idx < 0 or idx >= len(cur_list):
            raise ValueError("task path out of range")
        node = cur_list[idx]
        if not isinstance(node, dict):
            raise ValueError("invalid smos node")
        if i < len(path_parts) - 1:
            cur_list = _node_forest(node)
    if node is None:
        raise ValueError("task not found")
    return node


def _load_smos_file_for_update(rel_file: str) -> tuple[Path, dict, list]:
    rel_file = _safe_rel_smos_file_path(rel_file)
    fp = (WORKFLOW_DIR / rel_file).resolve()
    fp.relative_to(WORKFLOW_DIR.resolve())
    if not fp.exists():
        raise ValueError("smos file not found")
    payload = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("invalid smos file")
    root = payload.get("value")
    if not isinstance(root, list):
        root = []
        payload["value"] = root
    if "version" not in payload:
        payload["version"] = "2.0.0"
    return fp, payload, root


def _new_smos_node(payload: dict) -> dict:
    header = str(payload.get("header") or "").strip()
    if not header:
        raise ValueError("header is required")
    contents = str(payload.get("contents") or "").strip()
    node: dict[str, Any] = {"entry": {"header": header}}
    if contents:
        node["entry"]["contents"] = contents

    raw_state = str(payload.get("state") or "TODO").strip().upper()
    if raw_state not in SMOS_NO_STATE_SENTINELS:
        node["state-history"] = [{"state": _normalize_smos_state(raw_state), "time": _smos_now_timestamp()}]

    due = _parse_ymd(payload.get("due"))
    scheduled = _parse_ymd(payload.get("scheduled"))
    if due or scheduled:
        node["timestamps"] = {}
        if due:
            node["timestamps"]["DEADLINE"] = due
        if scheduled:
            node["timestamps"]["SCHEDULED"] = scheduled

    owner = str(payload.get("owner") or "").strip()
    context = str(payload.get("context") or "").strip()
    if owner or context:
        node["properties"] = {}
        if owner:
            node["properties"]["owner"] = owner
        if context:
            node["properties"]["context"] = context

    tags = payload.get("tags")
    if isinstance(tags, str):
        tags = [x.strip() for x in tags.split(",") if x.strip()]
    if isinstance(tags, list) and tags:
        node["tags"] = [str(x).strip() for x in tags if str(x).strip()]

    return node


def _flatten_nodes(rel_file: str, nodes: list, parent: list[int] | None = None, level: int = 0) -> list[dict]:
    out: list[dict] = []
    parent = parent or []

    for idx, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        path = parent + [idx]
        task_id = _task_id(rel_file, path)
        ts = node.get("timestamps") if isinstance(node.get("timestamps"), dict) else {}
        props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        state = _current_smos_state(node)
        due = _parse_ymd(ts.get("DEADLINE"))
        scheduled = _parse_ymd(ts.get("SCHEDULED"))

        children = _node_forest(node)
        out.append(
            {
                "id": task_id,
                "file": rel_file,
                "path": path,
                "level": level,
                "header": _node_header(node),
                "contents": _node_contents(node),
                "state": state,
                "due": due,
                "scheduled": scheduled,
                "owner": str(props.get("owner") or "").strip(),
                "context": str(props.get("context") or "").strip(),
                "tags": node.get("tags") if isinstance(node.get("tags"), list) else [],
                "parent_id": _task_id(rel_file, parent) if parent else None,
                "has_children": bool(children),
                "terminal": state in SMOS_TERMINAL_STATES,
            }
        )

        if children:
            out.extend(_flatten_nodes(rel_file, children, parent=path, level=level + 1))

    return out


def _extract_smos_view_data() -> dict:
    _ensure_defaults()
    files = _scan_smos_files(WORKFLOW_DIR)
    folders = _scan_smos_folders(WORKFLOW_DIR)
    today = date.today()

    rel_files: list[str] = []
    tasks: list[dict] = []
    errors: list[str] = []

    for fp in files:
        rel = str(fp.relative_to(WORKFLOW_DIR)).replace("\\", "/")
        rel_files.append(rel)
        try:
            payload = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
            root = payload.get("value")
            if not isinstance(root, list):
                errors.append(f"{rel}: invalid smos structure")
                continue
            tasks.extend(_flatten_nodes(rel, root))
        except Exception as exc:
            errors.append(f"{rel}: {exc}")

    for t in tasks:
        due = t.get("due")
        overdue = False
        if due and t.get("state") not in SMOS_TERMINAL_STATES:
            try:
                overdue = datetime.fromisoformat(due).date() < today
            except Exception:
                overdue = False
        t["overdue"] = overdue

    # Shape expected by the rich UI (compatible with original dashboard GTD view)
    projects: list[dict] = []
    for rel in rel_files:
        items = [t for t in tasks if t.get("file") == rel]
        projects.append({"file": rel, "items": items})

    next_actions = [t for t in tasks if t.get("state") in {"NEXT", "READY", "STARTED"}]
    waiting = [t for t in tasks if t.get("state") == "WAITING"]
    overdue = [t for t in tasks if t.get("overdue")]
    quick_wins = sorted(
        [t for t in tasks if t.get("state") in SMOS_ACTIVE_STATES and len((t.get("header") or "")) <= 60],
        key=lambda x: (x.get("state") != "NEXT", x.get("due") or "9999-12-31", x.get("header") or ""),
    )[:8]

    board = {"TODO": [], "NEXT": [], "WAITING": [], "DONE": []}
    for t in tasks:
        s = (t.get("state") or "").upper()
        if s in {"DONE", "CANCELLED", "FAILED"}:
            board["DONE"].append(t)
        elif s in {"NEXT", "READY", "STARTED"}:
            board["NEXT"].append(t)
        elif s == "WAITING":
            board["WAITING"].append(t)
        else:
            board["TODO"].append(t)

    timeline = [t for t in tasks if t.get("due") or t.get("scheduled")]
    timeline.sort(key=lambda x: (x.get("due") or x.get("scheduled") or "9999-12-31", x.get("header") or ""))

    # Keep "tasks" for API convenience, but expose rich UI keys too.
    return {
        "workflow_dir": str(WORKFLOW_DIR),
        "inbox_file": str(INBOX_FILE),
        "files": rel_files,
        "folders": folders,
        "tasks": tasks,
        "projects": projects,
        "today": {
            "next_actions": next_actions,
            "overdue": overdue,
            "quick_wins": quick_wins,
            "waiting": waiting,
        },
        "board": board,
        "timeline": timeline,
        "stats": {
            "total": len(tasks),
            "next": len(next_actions),
            "waiting": len(waiting),
            "overdue": len(overdue),
            "files": len(rel_files),
        },
        "errors": errors,
    }


def _normalize_inbox_text(raw: str) -> str:
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    return text


def _inbox_item_id(text: str) -> str:
    norm = _normalize_inbox_text(text).lower()
    return str(abs(hash(norm)))


def _read_inbox_data() -> dict:
    _ensure_defaults()
    lines = INBOX_FILE.read_text(encoding="utf-8").splitlines()
    current = "INBOX"
    sections = {"INBOX": [], "DONE": [], "REMOVED": []}

    for raw in lines:
        line = raw.strip()
        if line.upper() == "## INBOX":
            current = "INBOX"
            continue
        if line.upper() == "## DONE":
            current = "DONE"
            continue
        if line.upper() == "## REMOVED":
            current = "REMOVED"
            continue
        m = re.match(r"^- \[[ xX]\] (.+)$", line)
        if m:
            text = _normalize_inbox_text(m.group(1))
            if text:
                sections[current].append({"id": _inbox_item_id(text), "text": text})

    return {
        "inbox": sections["INBOX"],
        "done": sections["DONE"],
        "removed": sections["REMOVED"],
        "path": str(INBOX_FILE),
    }


def _write_inbox_data(data: dict) -> None:
    _ensure_defaults()

    def lines(items: list[dict], checked: bool = False) -> list[str]:
        out: list[str] = []
        for item in items:
            text = _normalize_inbox_text((item or {}).get("text") or "")
            if not text:
                continue
            mark = "x" if checked else " "
            out.append(f"- [{mark}] {text}")
        return out

    content_lines = [
        "# Workflow Inbox",
        "",
        "Purpose: shared inbox for captured work items and completion notes.",
        "",
        "## INBOX",
        *(lines(data.get("inbox") or [], checked=False) or [""]),
        "",
        "## DONE",
        *(lines(data.get("done") or [], checked=True) or [""]),
        "",
        "## REMOVED",
        *(lines(data.get("removed") or [], checked=True) or [""]),
        "",
    ]

    INBOX_FILE.write_text("\n".join(content_lines), encoding="utf-8")


def _project_report_lines(tasks: list[dict]) -> list[str]:
    grouped: dict[str, list[dict]] = {}
    for t in tasks:
        grouped.setdefault(t["file"], []).append(t)

    lines: list[str] = []
    for file_name in sorted(grouped.keys(), key=str.lower):
        entries = grouped[file_name]
        active = [x for x in entries if x["state"] in SMOS_ACTIVE_STATES]
        has_next = any(x["state"] in {"NEXT", "READY", "STARTED"} for x in active)

        if not active:
            health = "DONE"
        elif has_next:
            health = "HEALTHY"
        elif all(x["state"] == "WAITING" for x in active):
            health = "WAITING-ONLY"
        else:
            health = "MISSING-NEXT"

        lines.append(
            f"[{health}] {file_name} | total={len(entries)} active={len(active)} waiting={sum(1 for x in active if x['state']=='WAITING')}"
        )

    if not lines:
        return ["No .smos files found."]
    return lines


def _native_report(kind: str) -> str:
    data = _extract_smos_view_data()
    tasks = data.get("tasks") or []

    if kind == "next":
        rows = [t for t in tasks if t["state"] in {"NEXT", "READY", "STARTED"}]
        title = "NEXT report"
    elif kind == "waiting":
        rows = [t for t in tasks if t["state"] == "WAITING"]
        title = "WAITING report"
    elif kind == "work":
        rows = [t for t in tasks if t["state"] in {"TODO", "NEXT", "READY", "STARTED"}]
        title = "WORK report"
    elif kind == "projects":
        title = "PROJECTS report"
        lines = _project_report_lines(tasks)
        return title + "\n\n" + "\n".join(lines)
    else:
        return "Unknown report kind"

    rows.sort(key=lambda x: (x.get("due") or "9999-12-31", x.get("file") or "", x.get("header") or ""))

    lines = [title, "", f"Total: {len(rows)}", ""]
    today = date.today().isoformat()
    for item in rows:
        due = item.get("due") or "-"
        overdue = " !OVERDUE" if due != "-" and due < today and item["state"] not in SMOS_TERMINAL_STATES else ""
        lines.append(f"[{item['state'] or '-'}] {item['file']} | due={due}{overdue} | {item['header']}")

    if len(lines) <= 4:
        lines.append("(no items)")
    return "\n".join(lines)


def _run_smos_report(kind: str) -> tuple[int, str]:
    if not SMOS_QUERY_BIN:
        return 127, "smos-query binary not found"

    cmd = [
        SMOS_QUERY_BIN,
        "--workflow-dir",
        str(WORKFLOW_DIR),
        "--projects-dir",
        str(WORKFLOW_DIR),
    ]

    if kind == "next":
        cmd.append("next")
    elif kind == "work":
        cmd.append("work")
    elif kind == "waiting":
        cmd.append("waiting")
    elif kind == "projects":
        cmd.append("projects")
    else:
        return 2, f"unknown report kind: {kind}"

    env = os.environ.copy()
    env["NO_COLOR"] = "1"

    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(APP_ROOT), env=env)
    text = (res.stdout or "").strip()
    if not text and res.stderr:
        text = (res.stderr or "").strip()
    return res.returncode, text


def _render_report(kind: str) -> str:
    mode, mode_reason = _effective_mode()
    if mode == "smos":
        rc, out = _run_smos_report(kind)
        if rc == 0:
            return out or "(empty report output)"
        native = _native_report(kind)
        return (
            f"smos-query failed (rc={rc}), using native fallback.\n"
            f"Reason: {out or 'unknown error'}\n\n"
            f"{native}"
        )
    return _native_report(kind)


def _send_delegate_email(target_email: str, target_name: str, task: dict) -> None:
    msg = EmailMessage()
    subject_task = re.sub(r"\s+", " ", str(task.get("header") or "").strip()) or "(no title)"
    msg["Subject"] = f"[GTD] Delegated task: {subject_task[:140]}"
    msg["From"] = SMTP_FROM
    msg["To"] = target_email

    due = task.get("due") or ""
    scheduled = task.get("scheduled") or ""

    body_lines = [
        f"Hi {target_name or 'there'},",
        "",
        "A GTD task was delegated to you and moved to WAITING state.",
        "",
        f"Task: {subject_task}",
    ]
    if due:
        body_lines.append(f"Deadline: {due}")
    if scheduled:
        body_lines.append(f"Scheduled: {scheduled}")
    if task.get("file"):
        body_lines.append(f"File: {task['file']}")
    if task.get("id"):
        body_lines.append(f"Task ID: {task['id']}")
    details = str(task.get("contents") or "").strip()
    if details:
        body_lines.extend(["", "Details:", details])

    msg.set_content("\n".join(body_lines))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=25) as smtp:
        if SMTP_STARTTLS:
            smtp.starttls(context=context)
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


def _capabilities() -> dict:
    mode_effective, mode_reason = _effective_mode()
    terminal_enabled, terminal_reason = _terminal_enabled()
    delegation_enabled, delegation_reason = _delegation_enabled()

    return {
        "app": "smos-ui",
        "gtd_mode": {
            "requested": GTD_MODE_REQUESTED,
            "effective": mode_effective,
            "reason": mode_reason,
        },
        "binaries": {
            "smos": bool(SMOS_BIN),
            "smos_query": bool(SMOS_QUERY_BIN),
            "ttyd": bool(TTYD_BIN),
        },
        "features": {
            "terminal": {
                "requested": TERM_TOGGLE_REQUESTED,
                "enabled": terminal_enabled,
                "reason": terminal_reason,
                "route": "/gtd/terminal",
            },
            "delegation_email": {
                "requested": DELEGATION_TOGGLE_REQUESTED,
                "enabled": delegation_enabled,
                "reason": delegation_reason,
            },
        },
        "paths": {
            "workflow_dir": str(WORKFLOW_DIR),
            "inbox_file": str(INBOX_FILE),
        },
    }


@app.on_event("startup")
async def _startup() -> None:
    _ensure_defaults()


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "app": "smos-ui"}


@app.get("/capabilities")
async def capabilities() -> JSONResponse:
    return JSONResponse(_capabilities())


@app.get("/gtd/data")
async def gtd_data() -> JSONResponse:
    data = _extract_smos_view_data()
    data["capabilities"] = _capabilities()
    return JSONResponse(data)


@app.get("/gtd/inbox")
async def gtd_inbox() -> JSONResponse:
    data = _read_inbox_data()
    return JSONResponse({
        "ok": True,
        **data,
        "file": data.get("path") or str(INBOX_FILE),
        "count": len(data.get("inbox") or []),
    })


@app.post("/gtd/inbox/add")
async def gtd_inbox_add(request: Request) -> JSONResponse:
    payload = await request.json()
    text = _normalize_inbox_text(payload.get("text") or "")
    if not text:
        return JSONResponse({"ok": False, "error": "text is required"}, status_code=400)

    data = _read_inbox_data()
    item_id = _inbox_item_id(text)
    seen = {x.get("id") for x in (data.get("inbox") or []) + (data.get("done") or []) + (data.get("removed") or [])}
    if item_id in seen:
        return JSONResponse({"ok": False, "error": "duplicate inbox item"}, status_code=409)

    data["inbox"].append({"id": item_id, "text": text})
    _write_inbox_data(data)
    return JSONResponse({"ok": True, "id": item_id})


@app.post("/gtd/inbox/resolve")
async def gtd_inbox_resolve(request: Request) -> JSONResponse:
    payload = await request.json()
    item_id = str(payload.get("id") or "").strip()
    action = str(payload.get("action") or "done").strip().lower()

    if not item_id:
        return JSONResponse({"ok": False, "error": "id is required"}, status_code=400)
    if action not in {"done", "remove"}:
        return JSONResponse({"ok": False, "error": "action must be done or remove"}, status_code=400)

    data = _read_inbox_data()
    inbox = data.get("inbox") or []
    idx = next((i for i, item in enumerate(inbox) if item.get("id") == item_id), -1)
    if idx < 0:
        return JSONResponse({"ok": False, "error": "inbox item not found"}, status_code=404)

    item = inbox.pop(idx)
    bucket = "done" if action == "done" else "removed"
    data[bucket].append(item)
    _write_inbox_data(data)
    return JSONResponse({"ok": True})


@app.post("/gtd/folder/create")
async def gtd_folder_create(request: Request) -> JSONResponse:
    payload = await request.json()
    raw_name = payload.get("name") or payload.get("folder") or ""

    try:
        rel_folder = _safe_rel_folder_path(raw_name)
        folder_path = (WORKFLOW_DIR / rel_folder).resolve()
        folder_path.relative_to(WORKFLOW_DIR.resolve())

        if folder_path.exists():
            return JSONResponse({"ok": False, "error": "folder already exists"}, status_code=409)

        folder_path.mkdir(parents=True, exist_ok=False)
        return JSONResponse({"ok": True, "folder": rel_folder})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/gtd/folder/delete")
async def gtd_folder_delete(request: Request) -> JSONResponse:
    payload = await request.json()
    raw_name = payload.get("name") or payload.get("folder") or ""

    try:
        rel_folder = _safe_rel_folder_path(raw_name)
        folder_path = (WORKFLOW_DIR / rel_folder).resolve()
        folder_path.relative_to(WORKFLOW_DIR.resolve())

        if not folder_path.exists() or not folder_path.is_dir():
            return JSONResponse({"ok": False, "error": "folder not found"}, status_code=404)

        trash_root = WORKFLOW_DIR / ".trash-folders"
        trash_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = trash_root / f"{folder_path.name}-{stamp}"
        shutil.move(str(folder_path), str(target))

        return JSONResponse({"ok": True, "moved_to": str(target.relative_to(WORKFLOW_DIR))})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/gtd/file/create")
async def gtd_file_create(request: Request) -> JSONResponse:
    payload = await request.json()
    raw_file = payload.get("file") or payload.get("name") or ""

    try:
        rel_file = _safe_rel_smos_file_path(raw_file)
        file_path = (WORKFLOW_DIR / rel_file).resolve()
        file_path.relative_to(WORKFLOW_DIR.resolve())

        if file_path.exists():
            return JSONResponse({"ok": False, "error": "file already exists"}, status_code=409)
        if not file_path.parent.exists():
            return JSONResponse({"ok": False, "error": "parent folder does not exist"}, status_code=404)

        payload_data = {"version": "2.0.0", "value": []}
        _write_yaml_atomic(file_path, payload_data)
        return JSONResponse({"ok": True, "file": rel_file})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/gtd/file/rename")
async def gtd_file_rename(request: Request) -> JSONResponse:
    payload = await request.json()
    old_raw = payload.get("old_file") or payload.get("from") or ""
    new_raw = payload.get("new_file") or payload.get("to") or ""

    try:
        old_rel = _safe_rel_smos_file_path(old_raw)
        new_rel = _safe_rel_smos_file_path(new_raw)
        old_path = (WORKFLOW_DIR / old_rel).resolve()
        new_path = (WORKFLOW_DIR / new_rel).resolve()
        old_path.relative_to(WORKFLOW_DIR.resolve())
        new_path.relative_to(WORKFLOW_DIR.resolve())

        if not old_path.exists() or not old_path.is_file():
            return JSONResponse({"ok": False, "error": "source file not found"}, status_code=404)
        if new_path.exists():
            return JSONResponse({"ok": False, "error": "target file already exists"}, status_code=409)
        if not new_path.parent.exists():
            return JSONResponse({"ok": False, "error": "target folder does not exist"}, status_code=404)

        shutil.move(str(old_path), str(new_path))
        return JSONResponse({"ok": True, "file": new_rel})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/gtd/file/delete")
async def gtd_file_delete(request: Request) -> JSONResponse:
    payload = await request.json()
    raw_file = payload.get("file") or payload.get("name") or ""

    try:
        rel_file = _safe_rel_smos_file_path(raw_file)
        file_path = (WORKFLOW_DIR / rel_file).resolve()
        file_path.relative_to(WORKFLOW_DIR.resolve())

        if not file_path.exists() or not file_path.is_file():
            return JSONResponse({"ok": False, "error": "file not found"}, status_code=404)

        payload_data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        root = payload_data.get("value")
        has_entries = isinstance(root, list) and len(root) > 0
        if has_entries and not bool(payload.get("force")):
            return JSONResponse({"ok": False, "error": "file is not empty"}, status_code=409)

        file_path.unlink()
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/gtd/task/create")
async def gtd_task_create(request: Request) -> JSONResponse:
    payload = await request.json()
    header = str(payload.get("header") or "").strip()
    if not header:
        return JSONResponse({"ok": False, "error": "header is required"}, status_code=400)

    rel_file = str(payload.get("file") or "").strip()
    if not rel_file:
        files = _extract_smos_view_data().get("files") or []
        if not files:
            return JSONResponse({"ok": False, "error": "no smos files found"}, status_code=400)
        rel_file = files[0]

    try:
        fp, data, root_nodes = _load_smos_file_for_update(rel_file)
        node = _new_smos_node(payload)
        parent_id = str(payload.get("parent_id") or "").strip()
        target_id = str(payload.get("target_id") or "").strip()
        mode = str(payload.get("mode") or "").strip().lower()

        created_path: list[int] | None = None

        if target_id:
            target_file, target_path = _decode_task_id(target_id)
            if target_file != _safe_rel_smos_file_path(rel_file):
                return JSONResponse({"ok": False, "error": "target task must be in same file"}, status_code=400)

            if mode == "child":
                target_node = _find_node_by_path(root_nodes, target_path)
                children = _node_forest(target_node)
                children.append(node)
                created_path = list(target_path) + [len(children) - 1]
            elif mode == "sibling":
                if len(target_path) == 1:
                    siblings = root_nodes
                    insert_at = target_path[-1] + 1
                else:
                    parent_node = _find_node_by_path(root_nodes, target_path[:-1])
                    siblings = _node_forest(parent_node)
                    insert_at = target_path[-1] + 1
                insert_at = max(0, min(insert_at, len(siblings)))
                siblings.insert(insert_at, node)
                created_path = list(target_path[:-1]) + [insert_at]
            else:
                return JSONResponse(
                    {"ok": False, "error": "mode must be child or sibling when target_id is set"},
                    status_code=400,
                )
        elif parent_id:
            parent_file, parent_path = _decode_task_id(parent_id)
            if parent_file != _safe_rel_smos_file_path(rel_file):
                return JSONResponse({"ok": False, "error": "parent task must be in same file"}, status_code=400)
            parent_node = _find_node_by_path(root_nodes, parent_path)
            children = _node_forest(parent_node)
            children.append(node)
            created_path = list(parent_path) + [len(children) - 1]
        else:
            root_nodes.append(node)
            created_path = [len(root_nodes) - 1]

        _write_yaml_atomic(fp, data)
        return JSONResponse({"ok": True, "id": _task_id(_safe_rel_smos_file_path(rel_file), created_path or [0])})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/gtd/task/update")
async def gtd_task_update(request: Request) -> JSONResponse:
    payload = await request.json()
    task_id = str(payload.get("id") or "").strip()
    if not task_id:
        return JSONResponse({"ok": False, "error": "id is required"}, status_code=400)

    try:
        rel_file, path_parts = _decode_task_id(task_id)
        fp, data, root_nodes = _load_smos_file_for_update(rel_file)
        node = _find_node_by_path(root_nodes, path_parts)

        if "header" in payload:
            header = str(payload.get("header") or "").strip()
            if not header:
                return JSONResponse({"ok": False, "error": "header cannot be empty"}, status_code=400)
            _set_node_header(node, header)

        if "contents" in payload:
            _set_node_contents(node, str(payload.get("contents") or "").strip())

        if "state" in payload:
            _set_node_state(node, str(payload.get("state") or ""))

        if "due" in payload:
            _set_node_timestamp(node, "DEADLINE", _parse_ymd(payload.get("due")))

        if "scheduled" in payload:
            _set_node_timestamp(node, "SCHEDULED", _parse_ymd(payload.get("scheduled")))

        if "owner" in payload or "context" in payload:
            props = _node_properties(node)
            if "owner" in payload:
                owner = str(payload.get("owner") or "").strip()
                if owner:
                    props["owner"] = owner
                else:
                    props.pop("owner", None)
            if "context" in payload:
                context = str(payload.get("context") or "").strip()
                if context:
                    props["context"] = context
                else:
                    props.pop("context", None)
            if not props:
                node.pop("properties", None)

        if "tags" in payload:
            tags = payload.get("tags")
            if isinstance(tags, str):
                tags = [x.strip() for x in tags.split(",") if x.strip()]
            if isinstance(tags, list) and tags:
                node["tags"] = [str(x).strip() for x in tags if str(x).strip()]
            else:
                node.pop("tags", None)

        _write_yaml_atomic(fp, data)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/gtd/task/state")
async def gtd_task_state(request: Request) -> JSONResponse:
    payload = await request.json()
    task_id = str(payload.get("id") or "").strip()
    state = payload.get("state")
    if not task_id or state is None:
        return JSONResponse({"ok": False, "error": "id and state are required"}, status_code=400)

    try:
        rel_file, path_parts = _decode_task_id(task_id)
        fp, data, root_nodes = _load_smos_file_for_update(rel_file)
        node = _find_node_by_path(root_nodes, path_parts)
        _set_node_state(node, str(state))
        _write_yaml_atomic(fp, data)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/gtd/task/delete")
async def gtd_task_delete(request: Request) -> JSONResponse:
    payload = await request.json()
    task_id = str(payload.get("id") or "").strip()
    if not task_id:
        return JSONResponse({"ok": False, "error": "id is required"}, status_code=400)

    try:
        rel_file, path_parts = _decode_task_id(task_id)
        fp, data, root_nodes = _load_smos_file_for_update(rel_file)

        parent_path = path_parts[:-1]
        if len(path_parts) == 1:
            siblings = root_nodes
        else:
            parent = _find_node_by_path(root_nodes, parent_path)
            siblings = _node_forest(parent)

        idx = path_parts[-1]
        if idx < 0 or idx >= len(siblings):
            return JSONResponse({"ok": False, "error": "task path out of range"}, status_code=400)

        deleted_node = siblings.pop(idx)
        _write_yaml_atomic(fp, data)

        return JSONResponse(
            {
                "ok": True,
                "undo": {
                    "file": rel_file,
                    "parent_path": parent_path,
                    "index": idx,
                    "node": deleted_node,
                },
            }
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/gtd/task/restore")
async def gtd_task_restore(request: Request) -> JSONResponse:
    payload = await request.json()
    rel_file = str(payload.get("file") or "").strip()
    node = payload.get("node")
    parent_path = payload.get("parent_path")
    index = payload.get("index")

    if not rel_file:
        return JSONResponse({"ok": False, "error": "file is required"}, status_code=400)
    if not isinstance(node, dict):
        return JSONResponse({"ok": False, "error": "node is required"}, status_code=400)
    if not isinstance(parent_path, list) or not all(isinstance(x, int) for x in parent_path):
        return JSONResponse({"ok": False, "error": "parent_path must be a list of ints"}, status_code=400)

    try:
        fp, data, root_nodes = _load_smos_file_for_update(rel_file)
        if parent_path:
            parent = _find_node_by_path(root_nodes, parent_path)
            siblings = _node_forest(parent)
        else:
            siblings = root_nodes

        insert_at = int(index) if isinstance(index, int) or (isinstance(index, str) and str(index).strip()) else len(siblings)
        insert_at = max(0, min(insert_at, len(siblings)))
        siblings.insert(insert_at, node)

        restored_path = list(parent_path) + [insert_at]
        _write_yaml_atomic(fp, data)
        return JSONResponse({"ok": True, "id": _task_id(_safe_rel_smos_file_path(rel_file), restored_path)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/gtd/task/delegate")
async def gtd_task_delegate(request: Request) -> JSONResponse:
    payload = await request.json()
    task_id = str(payload.get("id") or "").strip()
    target_email = str(payload.get("target_email") or "").strip()
    target_name = str(payload.get("target_name") or "").strip() or "there"

    if not task_id:
        return JSONResponse({"ok": False, "error": "id is required"}, status_code=400)
    if not target_email:
        return JSONResponse({"ok": False, "error": "target_email is required"}, status_code=400)

    enabled, reason = _delegation_enabled()
    if not enabled:
        return JSONResponse({"ok": False, "error": f"delegation email unavailable: {reason}"}, status_code=503)

    try:
        rel_file, path_parts = _decode_task_id(task_id)
        fp, data, root_nodes = _load_smos_file_for_update(rel_file)
        node = _find_node_by_path(root_nodes, path_parts)

        task_snapshot = {
            "id": task_id,
            "file": rel_file,
            "header": _node_header(node),
            "contents": _node_contents(node),
            "state": _current_smos_state(node),
            "due": _parse_ymd(_node_timestamps(node).get("DEADLINE")),
            "scheduled": _parse_ymd(_node_timestamps(node).get("SCHEDULED")),
        }

        _send_delegate_email(target_email, target_name, task_snapshot)
        _set_node_state(node, "WAITING")
        _write_yaml_atomic(fp, data)

        return JSONResponse({"ok": True, "state": "WAITING", "target_email": target_email})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/gtd/report/{kind}")
async def gtd_report(kind: str) -> PlainTextResponse:
    text = _render_report(kind)
    return PlainTextResponse(text)


@app.get("/gtd/terminal")
async def gtd_terminal() -> HTMLResponse:
    ok, err = ensure_ttyd_running()
    if not ok:
        html = (
            "<html><body style='font-family:Arial,sans-serif;padding:20px;'>"
            "<h2>Terminal unavailable</h2>"
            f"<p>{err}</p>"
            "<p>Install smos + ttyd, or set GTD_ENABLE_TERMINAL=0.</p>"
            "</body></html>"
        )
        return HTMLResponse(html, status_code=503)

    src = f"http://127.0.0.1:{TERMINAL_PORT}{TERMINAL_BASE_PATH}/"
    html = (
        "<html><body style='margin:0;background:#111;'>"
        f"<iframe src='{src}' style='border:none;width:100vw;height:100vh;'></iframe>"
        "</body></html>"
    )
    return HTMLResponse(html)


@app.api_route("/gtd-terminal", methods=["GET"])
@app.api_route("/gtd-terminal/{path:path}", methods=["GET"])
async def gtd_terminal_redirect(path: str = ""):
    ok, err = ensure_ttyd_running()
    if not ok:
        return PlainTextResponse(f"GTD terminal unavailable: {err}", status_code=503)
    suffix = f"/{path}" if path else "/"
    target = f"http://127.0.0.1:{TERMINAL_PORT}{TERMINAL_BASE_PATH}{suffix}"
    return RedirectResponse(target)

def _shared_gtd_header_css() -> str:
    return """
      .header { background: var(--header-bg, var(--panel, #111)); border-bottom: 1px solid var(--border, var(--line, #333)); padding: 16px 24px; display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }
      .header h1 { font-size: 20px; font-weight: 700; color: var(--accent, #d4843a); margin: 0; }
      .countdown-pills { display: flex; gap: 12px; flex-wrap: wrap; }
      .pill { background: var(--card, var(--chip, #252525)); border: 1px solid var(--border, var(--line, #333)); border-radius: 20px; padding: 4px 14px; font-size: 12px; color: var(--muted, #888); }
      .pill strong { color: var(--text, #e8e0d4); }
      .header .theme-toggle { border: 0; background: transparent; color: var(--text, #e8e0d4); font-size: 12px; font-weight: 600; cursor: pointer; padding: 0; }
      @media (max-width: 860px) {
        .header { padding: 12px 14px; gap: 10px; }
        .header h1 { font-size: 18px; }
        .countdown-pills { gap: 8px; }
      }
    """


def _shared_gtd_header_html() -> str:
    return (
        "<div class='header'>"
        "<h1>GTD Dashboard</h1>"
        "<div class='countdown-pills'>"
        "<div class='pill'><strong>Project-agnostic smos workflow</strong></div>"
        "<a class='pill nav active' href='/gtd'><strong>GTD</strong></a>"
        "<a class='pill nav' href='/gtd/terminal'><strong>Terminal</strong></a>"
        "<div class='pill'><button type='button' id='themeToggle' class='theme-toggle'>☀️ Light mode</button></div>"
        "</div></div>"
    )


@app.get("/", response_class=HTMLResponse)
@app.get("/gtd", response_class=HTMLResponse)
async def gtd_view() -> HTMLResponse:
    html_page = r"""
    <!DOCTYPE html>
    <html lang='en' data-theme='dark'>
    <head>
      <meta charset='UTF-8'>
      <meta name='viewport' content='width=device-width, initial-scale=1.0'>
      <title>GTD</title>
      <style>
        :root {{ --bg:#14181f; --panel:#1d2532; --card:#1d2532; --line:#2f3a4d; --border:#2f3a4d; --text:#ecf1fa; --muted:#9fb0c9; --accent:#d4843a; --header-bg:#111722; }}
        html[data-theme='light'], body.theme-light {{ --bg:#f3f6fb; --panel:#fff; --card:#fff; --line:#d7deec; --border:#d7deec; --text:#122038; --muted:#64758e; --accent:#b35f1f; --header-bg:#eaf0f8; }}
        * {{ box-sizing:border-box; }}
        body {{ margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
        __SHARED_HEADER_CSS__
        .main {{ max-width:1500px; margin:0 auto; padding:12px; }}
        .top {{ display:grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap:8px; margin-bottom:10px; }}
        .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px; }}
        .n {{ font-size:26px; font-weight:700; }}
        .muted {{ color:var(--muted); font-size:12px; }}
        .actions {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; position:sticky; top:72px; z-index:15; background:var(--bg); padding:8px 0; }}
        button, input, select, textarea {{ font:inherit; }}
        .btn {{ border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:10px; padding:8px 10px; cursor:pointer; min-height:40px; }}
        .btn.primary {{ border-color:var(--accent); color:var(--accent); }}
        .btn.icon-btn {{ padding:6px 8px; min-width:34px; line-height:1; }}
        .grid {{ display:grid; grid-template-columns: 2fr 1fr; gap:10px; align-items:start; }}
        .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px; }}
        .title {{ font-size:12px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); margin-bottom:8px; }}
        .compact-row {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
        input,select,textarea {{ width:100%; background:transparent; border:1px solid var(--line); color:var(--text); border-radius:8px; padding:8px; }}
        .task-tree {{ border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
        .task-row {{ display:flex; align-items:center; gap:8px; padding:8px; border-bottom:1px solid rgba(255,255,255,.05); }}
        .task-row.todo-row {{ background:rgba(255,107,107,.08); border-left:3px solid #ff6b6b; }}
        .task-row.selected-row {{ outline:1px solid var(--accent); outline-offset:-1px; background:rgba(212,132,58,.10); }}
        .task-row:last-child {{ border-bottom:none; }}
        .task-main {{ min-width:0; flex:1; display:flex; flex-direction:column; align-items:flex-start; gap:6px; }}
        .task-line1 {{ width:100%; min-width:0; display:flex; align-items:center; gap:8px; }}
        .task-line2 {{ width:100%; display:flex; gap:6px; flex-wrap:wrap; align-items:center; }}
        .task-file {{ font-size:11px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%; }}
        .toggle-btn {{ width:24px; height:24px; border-radius:6px; border:1px solid var(--line); background:transparent; color:var(--muted); cursor:pointer; }}
        .toggle-btn.placeholder {{ visibility:hidden; }}
        .task-header {{ font-size:14px; cursor:pointer; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
        .task-header:hover {{ color:var(--accent); }}
        .task-edit-input {{ max-width:520px; font-size:14px; padding:6px 8px; }}
        .task-meta {{ font-size:11px; color:var(--muted); white-space:nowrap; }}
        .task-date-pill {{ font-size:11px; border:1px solid var(--line); border-radius:999px; padding:2px 7px; color:var(--muted); font-weight:700; }}
        .task-date-pill.deadline {{ border-color:#ef4444; color:#ef4444; }}
        .task-date-pill.scheduled {{ border-color:#f59e0b; color:#f59e0b; }}
        .task-actions {{ display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }}
        .mobile-tools {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:8px 0; }}
        .task-search {{ max-width:320px; }}
        .filter-chips {{ display:flex; gap:6px; flex-wrap:wrap; }}
        .filter-chip {{ border:1px solid var(--line); background:rgba(255,255,255,.04); color:var(--text); border-radius:999px; padding:6px 10px; font-size:12px; cursor:pointer; }}
        .filter-chip.active {{ border-color:var(--accent); color:var(--accent); background:rgba(212,132,58,.14); }}
        .state-current {{ box-shadow:0 0 0 1px rgba(255,255,255,.2) inset; }}
        .chip {{ font-size:11px; border:1px solid var(--line); border-radius:999px; padding:3px 9px; cursor:pointer; color:#111; font-weight:600; }}
        .chip:hover {{ filter:brightness(1.05); transform:translateY(-1px); }}
        .chip-todo {{ background:#ff6b6b !important; border-color:#e95757 !important; }}
        .chip-next {{ background:#ffb347 !important; border-color:#e39a38 !important; }}
        .chip-ready {{ background:#ffb347 !important; border-color:#e39a38 !important; }}
        .chip-waiting {{ background:#6fb8ff !important; border-color:#5ba5ec !important; }}
        .chip-done {{ background:#78d68b !important; border-color:#63bc75 !important; }}
        .chip-cancelled {{ background:#78d68b !important; border-color:#63bc75 !important; }}
        .chip-action {{ background:rgba(255,255,255,.08); color:var(--text); border-color:var(--line); }}
        .state-badge {{ font-size:11px; font-weight:700; border-radius:999px; padding:3px 8px; color:#111; border:1px solid rgba(0,0,0,.18); }}
        .state-TODO {{ background:#ff6b6b; }}
        .state-NEXT {{ background:#ffb347; }}
        .state-WAITING {{ background:#6fb8ff; }}
        .state-DONE, .state-CANCELLED, .state-FAILED {{ background:#78d68b; }}
        .state-READY, .state-STARTED {{ background:#ffb347; }}
        .state-EMPTY {{ background:#9aa8be; color:#101825; }}
        .indent-line {{ display:inline-block; width:12px; height:1px; }}
        .project-head {{ font-size:12px; color:var(--muted); padding:8px 10px; border-top:1px solid var(--line); background:rgba(0,0,0,.14); display:flex; align-items:center; gap:8px; }}
        .project-head:first-child {{ border-top:none; }}
        .keyboard-hint { font-size:11px; color:var(--muted); border:1px dashed var(--line); border-radius:8px; padding:8px; margin-top:8px; }
        .date-modal-backdrop { position:fixed; inset:0; background:rgba(0,0,0,.45); display:none; align-items:center; justify-content:center; z-index:60; }
        .date-modal-backdrop.open { display:flex; }
        .date-modal { width:min(460px, 94vw); background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px; }
        .date-modal-title { font-size:13px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); margin-bottom:8px; }
        .date-modal-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:8px 0; }
        .date-quick-btn { border:1px solid var(--line); background:rgba(255,255,255,.04); color:var(--text); border-radius:999px; padding:5px 10px; cursor:pointer; }
        .task-date-pill.clickable { cursor:pointer; }
        pre { background:#0f141d; color:#d9e7ff; border:1px solid var(--line); border-radius:10px; padding:10px; overflow:auto; max-height:280px; }
        .report-cards {{ border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
        .report-row {{ padding:8px 10px; border-bottom:1px solid rgba(255,255,255,.06); }}
        .report-row:last-child {{ border-bottom:none; }}
        .report-title {{ font-size:13px; color:var(--text); }}
        .report-meta {{ margin-top:4px; font-size:11px; color:var(--muted); display:flex; gap:6px; flex-wrap:wrap; align-items:center; }}
        .report-state-chip {{ font-size:11px; border-radius:999px; padding:2px 8px; color:#111; font-weight:700; border:1px solid rgba(0,0,0,.18); }}
        .report-text {{ border:1px solid var(--line); border-radius:10px; padding:8px 10px; background:#0f141d; max-height:300px; overflow:auto; }}
        .report-line {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:12px; line-height:1.45; color:#d9e7ff; padding:2px 0; white-space:pre-wrap; word-break:break-word; }}
        .report-gap {{ height:6px; }}
        .report-empty {{ color:var(--muted); padding:10px; font-size:12px; }}
        .overdue {{ color:#ff6b6b; }}
        .row {{ padding:8px 0; border-bottom:1px solid rgba(255,255,255,.06); }}
        .row:last-child {{ border-bottom:none; }}
        .task {{ font-size:14px; }}
        .chips {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; }}
        .inbox-wrap {{ display:flex; flex-direction:column; gap:8px; }}
        .inbox-head {{ display:flex; justify-content:space-between; align-items:center; gap:8px; }}
        .inbox-count {{ font-size:11px; border-radius:999px; padding:2px 8px; border:1px solid var(--line); color:var(--muted); }}
        .inbox-body {{ border:1px solid var(--line); border-radius:10px; padding:10px; min-height:160px; background:rgba(255,255,255,.02); }}
        .inbox-text {{ font-size:15px; line-height:1.45; white-space:pre-wrap; }}
        .inbox-empty {{ color:var(--muted); font-size:13px; }}
        .inbox-nav {{ display:flex; justify-content:space-between; align-items:center; gap:8px; }}
        .inbox-actions {{ display:flex; gap:8px; flex-wrap:wrap; }}
        .btn.danger {{ border-color:#e95757; color:#ff8f8f; }}
        .mobile-bottom-bar {{ display:none; }}
        @media (max-width: 1100px) {{
          .grid, .top {{ grid-template-columns:1fr; }}
          .main {{ padding:10px; padding-bottom:88px; }}
          .actions {{ top:110px; overflow:auto; flex-wrap:nowrap; white-space:nowrap; }}
          .btn {{ min-height:44px; }}
          .task-row {{ flex-direction:column; align-items:stretch; padding:10px; }}
          .task-actions {{ margin-top:6px; justify-content:flex-start; overflow:auto; white-space:nowrap; }}
          .task-actions {{ -webkit-overflow-scrolling:touch; scroll-snap-type:x proximity; touch-action:pan-x; }}
          .task-actions .chip {{ scroll-snap-align:start; }}
          .task-header {{ white-space:normal; overflow:visible; text-overflow:unset; }}
          .compact-row {{ flex-direction:column; align-items:stretch; }}
          .compact-row select, .compact-row input, .compact-row .btn {{ max-width:none !important; width:100%; }}
          .task-search {{ max-width:none; width:100%; }}
          .mobile-bottom-bar {{
            position:fixed; left:0; right:0; bottom:0; z-index:30; display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:6px;
            padding:8px; background:var(--bg); border-top:1px solid var(--line);
          }}
          .mobile-bottom-bar .btn {{ width:100%; min-height:44px; font-size:12px; padding:8px 6px; }}
        }}
      </style>
    </head>
    <body>
      __SHARED_HEADER_HTML__
      <div class='main'>
        <div style='margin-bottom:10px; padding:10px 12px; border-radius:10px; border:1px solid #6fb8ff; background:rgba(111,184,255,.14); color:#d9ecff; font-weight:600;'>
          GTD keyboard build active, arrows navigate tree, TAB fold/unfold, SPACE cycle state.
        </div>
        <div class='top'>
          <div class='card'><div class='n' id='st-total'>0</div><div class='muted'>Total tasks</div></div>
          <div class='card'><div class='n' id='st-next'>0</div><div class='muted'>Next actions</div></div>
          <div class='card'><div class='n' id='st-waiting'>0</div><div class='muted'>Waiting for</div></div>
          <div class='card'><div class='n overdue' id='st-overdue'>0</div><div class='muted'>Overdue</div></div>
        </div>

        <div class='actions'>
          <button class='btn primary' onclick='openReport("next")'>Next action report</button>
          <button class='btn primary' onclick='openReport("work")'>Work report</button>
          <button class='btn primary' onclick='openReport("waiting")'>Waiting for report</button>
          <button class='btn' onclick='undoDelete()'>Undo delete (Ctrl+Z)</button>
          <button class='btn' onclick='refreshData()'>Refresh</button>
        </div>

        <div class='panel' style='margin-bottom:10px;'>
          <div class='compact-row'>
            <span class='muted'>State colors:</span>
            <span class='chip chip-todo'>TODO</span>
            <span class='chip chip-next'>NEXT</span>
            <span class='chip chip-ready'>READY</span>
            <span class='chip chip-waiting'>WAITING</span>
            <span class='chip chip-done'>DONE</span>
            <span class='chip chip-cancelled'>CANCELLED</span>
          </div>
        </div>

        <div class='grid'>
          <div class='panel'>
            <div class='title'>Smos-style task list</div>
            <div class='muted' style='margin-bottom:8px;'>Build: __BUILD_STAMP__</div>
            <div class='title'>Report output</div>
            <div id='reportOut' class='report-empty' style='margin-bottom:10px;'>(click a report button)</div>
            <div class='mobile-tools'>
              <input id='taskSearch' class='task-search' placeholder='Search tasks, file, state, date...'>
              <div class='filter-chips'>
                <button type='button' class='filter-chip active' data-filter='ALL' onclick='setTaskFilter("ALL")'>All</button>
                <button type='button' class='filter-chip' data-filter='TODO' onclick='setTaskFilter("TODO")'>TODO</button>
                <button type='button' class='filter-chip' data-filter='NEXT' onclick='setTaskFilter("NEXT")'>NEXT</button>
                <button type='button' class='filter-chip' data-filter='WAITING' onclick='setTaskFilter("WAITING")'>WAITING</button>
                <button type='button' class='filter-chip' data-filter='DONE' onclick='setTaskFilter("DONE")'>DONE+</button>
              </div>
              <button class='btn' type='button' onclick='jumpToFile()'>Jump to file</button>
            </div>
            <div class='compact-row' style='margin-bottom:8px;'>
              <select id='q-file' style='max-width:340px;'></select>
              <select id='q-folder' style='max-width:240px;'></select>
              <button class='btn icon-btn' title='Delete selected top-level folder' onclick='deleteSelectedFolder()'>🗑️</button>
              <input id='q-header' placeholder='Add task to file root...' style='max-width:380px;'>
              <button class='btn primary' onclick='quickCreateRoot()'>Add</button>
              <button class='btn' onclick='collapseAll()'>Collapse all</button>
              <button class='btn' onclick='expandAll()'>Expand all</button>
            </div>

            <div class='task-tree' id='taskTree'></div>

            <div class='keyboard-hint'>
              Keyboard: ↑/↓ move, ←/→ collapse/expand or parent/child, TAB fold/unfold subtree, SPACE cycle state (incl. no-state), tw WAITING, td DONE, tn NEXT, tt TODO, tr READY, tc CANCELLED, sd DEADLINE date, ss SCHEDULED date (accept YYYY-MM-DD or +1d/+1w/+1m), e new sibling (or root on project row), E new child (or root on project row), n new .smos file (inline), N new folder, d/Del delete task (or delete selected .smos file when on project row, with confirmation), Esc cancels new inline creation, Ctrl+Z undo delete, Alt+↑/Alt+↓ jump between levels, Home/End go top/bottom.
            </div>
          </div>

          <div class='panel'>
            <div class='inbox-wrap'>
              <div class='inbox-head'>
                <div class='title' style='margin-bottom:0;'>Assistant inbox</div>
                <span id='inboxCount' class='inbox-count'>0 items</span>
              </div>
              <div id='inboxFile' class='muted'></div>
              <div id='inboxBody' class='inbox-body'>
                <div id='inboxText' class='inbox-text' style='display:none;'></div>
                <div id='inboxEmpty' class='inbox-empty'>Inbox is empty.</div>
              </div>
              <div class='inbox-nav'>
                <button class='btn' type='button' onclick='inboxPrev()'>Prev</button>
                <span id='inboxPos' class='muted'>0 / 0</span>
                <button class='btn' type='button' onclick='inboxNext()'>Next</button>
              </div>
              <div class='inbox-actions'>
                <button class='btn primary' type='button' onclick='inboxResolve("done")'>Check done</button>
                <button class='btn danger' type='button' onclick='inboxResolve("remove")'>Remove</button>
              </div>
            </div>
          </div>
        </div>

        <div class='mobile-bottom-bar'>
          <button class='btn primary' type='button' onclick='mobileQuickAdd()'>Add</button>
          <button class='btn' type='button' onclick='collapseAll()'>Collapse</button>
          <button class='btn' type='button' onclick='expandAll()'>Expand</button>
          <button class='btn' type='button' onclick='jumpToFile()'>Jump</button>
        </div>
      </div>

      <div id='dateModalBackdrop' class='date-modal-backdrop' onclick='if(event.target===this) closeDateEditor()'>
        <div class='date-modal' onclick='event.stopPropagation()'>
          <div id='dateModalTitle' class='date-modal-title'>Edit date</div>
          <div class='date-modal-row'>
            <input id='dateModalInput' placeholder='YYYY-MM-DD or +1d / +1w / +1m'>
          </div>
          <div class='date-modal-row'>
            <input id='dateModalPicker' type='date'>
          </div>
          <div class='date-modal-row'>
            <button type='button' class='date-quick-btn' onclick="applyDateQuick('+1d')">+1d</button>
            <button type='button' class='date-quick-btn' onclick="applyDateQuick('+1w')">+1w</button>
            <button type='button' class='date-quick-btn' onclick="applyDateQuick('+1m')">+1m</button>
            <button type='button' class='date-quick-btn' onclick='setDateToday()'>today</button>
          </div>
          <div class='date-modal-row' style='justify-content:flex-end;'>
            <button type='button' class='btn' onclick='clearDateEditorValue()'>Clear</button>
            <button type='button' class='btn' onclick='closeDateEditor()'>Cancel</button>
            <button type='button' class='btn primary' onclick='saveDateEditor()'>Save</button>
          </div>
          <div class='muted'>Tip: enter relative values like +2d, +2w, +2m.</div>
        </div>
      </div>

      <script>
        function esc(s) { return (s ?? '').toString().replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
        let DATA = null;
        let COLLAPSED = new Set();
        let COLLAPSE_INIT = false;
        let selectedTaskId = null;
        let visibleTaskOrder = [];
        let visibleTaskMeta = new Map();
        let shouldRestoreTaskFocus = true;
        let editingTaskId = null;
        let inlineEditSaving = false;
        let reportInitialized = false;
        let taskFilter = 'ALL';
        let taskSearch = '';
        let UNDO_DELETE_STACK = [];
        let INBOX_DATA = { inbox: [], count: 0, file: '' };
        let INBOX_INDEX = 0;
        let pendingStatePrefix = null;
        let pendingStateTimer = null;
        let pendingDatePrefix = null;
        let pendingDateTimer = null;
        let dateEditContext = null;
        let pendingNewTaskId = null;
        let pendingNewFile = null;
        let editingProjectFile = null;
        let inlineProjectEditSaving = false;

        async function api(path, opts={}) {
          const r = await fetch(path, {headers: {'Content-Type':'application/json'}, ...opts});
          if (!r.ok) throw new Error(await r.text());
          const ct = r.headers.get('content-type') || '';
          return ct.includes('application/json') ? r.json() : r.text();
        }

        function setTheme(theme) {
          const t = theme === 'light' ? 'light' : 'dark';
          document.documentElement.setAttribute('data-theme', t);
          if (document.body) {
            document.body.classList.remove('theme-light', 'theme-dark');
            document.body.classList.add(`theme-${t}`);
          }
          try {
            localStorage.setItem('gtd-theme', t);
            localStorage.setItem('dashboard-theme', t);
          } catch (e) {}
          const headerBtn = document.getElementById('themeToggle');
          if (headerBtn) headerBtn.textContent = t === 'light' ? '🌙 Dark mode' : '☀️ Light mode';
        }

        function toggleTheme() {
          const current = document.documentElement.getAttribute('data-theme') || 'dark';
          setTheme(current === 'dark' ? 'light' : 'dark');
        }

        function initTheme() {
          let saved = null;
          try {
            saved = localStorage.getItem('dashboard-theme') || localStorage.getItem('gtd-theme');
          } catch (e) {}
          setTheme(saved || 'dark');
          const headerBtn = document.getElementById('themeToggle');
          if (headerBtn && !headerBtn.dataset.boundThemeToggle) {
            headerBtn.addEventListener('click', toggleTheme);
            headerBtn.dataset.boundThemeToggle = '1';
          }
        }

        function idFrom(file, path) { return `${file}::${path.join('.')}`; }
        function projectInputId(file) { return `inline-project-edit-${encodeURIComponent(file).replace(/%/g, '_')}`; }

        function fmtDateLocal(d) {
          const y = d.getFullYear();
          const m = String(d.getMonth() + 1).padStart(2, '0');
          const day = String(d.getDate()).padStart(2, '0');
          return `${y}-${m}-${day}`;
        }

        function parseAbsoluteDate(raw) {
          const s = String(raw || '').trim();
          const m = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
          if (!m) return null;
          const y = Number(m[1]);
          const mo = Number(m[2]);
          const d = Number(m[3]);
          const dt = new Date(y, mo - 1, d);
          if (dt.getFullYear() !== y || dt.getMonth() !== (mo - 1) || dt.getDate() !== d) return null;
          return fmtDateLocal(dt);
        }

        function addMonthsClamped(base, months) {
          const y = base.getFullYear();
          const m0 = base.getMonth();
          const d0 = base.getDate();
          const total = m0 + months;
          const ny = y + Math.floor(total / 12);
          const nm = ((total % 12) + 12) % 12;
          const last = new Date(ny, nm + 1, 0).getDate();
          const nd = Math.min(d0, last);
          return new Date(ny, nm, nd);
        }

        function parseRelativeDate(raw, baseDate = new Date()) {
          const s = String(raw || '').trim().toLowerCase();
          const m = s.match(/^\+(\d+)\s*([dwm])$/);
          if (!m) return null;
          const n = Number(m[1]);
          const unit = m[2];
          if (!Number.isFinite(n) || n < 0) return null;
          const b = new Date(baseDate.getFullYear(), baseDate.getMonth(), baseDate.getDate());
          let out;
          if (unit === 'd') out = new Date(b.getFullYear(), b.getMonth(), b.getDate() + n);
          else if (unit === 'w') out = new Date(b.getFullYear(), b.getMonth(), b.getDate() + (n * 7));
          else out = addMonthsClamped(b, n);
          return fmtDateLocal(out);
        }

        function resolveDateInput(raw) {
          const s = String(raw || '').trim();
          if (!s) return { ok: true, value: null };
          const rel = parseRelativeDate(s);
          if (rel) return { ok: true, value: rel };
          const abs = parseAbsoluteDate(s);
          if (abs) return { ok: true, value: abs };
          return { ok: false, value: null };
        }

        function getTaskById(id) {
          const all = (DATA && DATA.projects ? DATA.projects.flatMap(p => p.items || []) : []);
          return all.find(x => x.id === id) || null;
        }

        function openDateEditor(id, field) {
          const t = getTaskById(id);
          if (!t) return;
          const key = field === 'scheduled' ? 'scheduled' : 'due';
          dateEditContext = { id, field: key };
          const current = key === 'scheduled' ? (t.scheduled || '') : (t.due || '');

          const backdrop = document.getElementById('dateModalBackdrop');
          const title = document.getElementById('dateModalTitle');
          const input = document.getElementById('dateModalInput');
          const picker = document.getElementById('dateModalPicker');
          if (!backdrop || !title || !input || !picker) return;

          title.textContent = `${key === 'scheduled' ? 'SCHEDULED' : 'DEADLINE'} · ${t.header || '(no title)'}`;
          input.value = current;
          picker.value = current;
          backdrop.classList.add('open');
          requestAnimationFrame(() => { input.focus(); input.select(); });
        }

        function closeDateEditor() {
          dateEditContext = null;
          const backdrop = document.getElementById('dateModalBackdrop');
          if (backdrop) backdrop.classList.remove('open');
        }

        function setDateToday() {
          const v = fmtDateLocal(new Date());
          const input = document.getElementById('dateModalInput');
          const picker = document.getElementById('dateModalPicker');
          if (input) input.value = v;
          if (picker) picker.value = v;
        }

        function applyDateQuick(spec) {
          const resolved = parseRelativeDate(spec);
          if (!resolved) return;
          const input = document.getElementById('dateModalInput');
          const picker = document.getElementById('dateModalPicker');
          if (input) input.value = spec;
          if (picker) picker.value = resolved;
        }

        function clearDateEditorValue() {
          const input = document.getElementById('dateModalInput');
          const picker = document.getElementById('dateModalPicker');
          if (input) input.value = '';
          if (picker) picker.value = '';
        }

        async function saveDateEditor() {
          if (!dateEditContext || !dateEditContext.id) return;
          const input = document.getElementById('dateModalInput');
          const picker = document.getElementById('dateModalPicker');
          const rawInput = String(input ? input.value : '').trim();
          const raw = rawInput || String(picker ? picker.value : '').trim();
          const parsed = resolveDateInput(raw);
          if (!parsed.ok) {
            alert('Invalid date. Use YYYY-MM-DD or +Nd/+Nw/+Nm (for example +2d, +1w, +1m).');
            return;
          }
          const payload = { id: dateEditContext.id };
          if (dateEditContext.field === 'scheduled') payload.scheduled = parsed.value;
          else payload.due = parsed.value;

          selectedTaskId = dateEditContext.id;
          shouldRestoreTaskFocus = true;
          await api('/gtd/task/update', {method:'POST', body: JSON.stringify(payload)});
          closeDateEditor();
          await refreshData();
        }

        function renderToday() {
          const box = document.getElementById('today-next');
          const items = DATA.today.next_actions.slice(0,8);
          const overdue = DATA.today.overdue.slice(0,4);
          const quick = DATA.today.quick_wins.slice(0,4);
          box.innerHTML = `
            <div class='muted' style='margin-bottom:8px;'>Next actions</div>
            ${items.map(t => taskRow(t)).join('') || "<div class='muted'>No next actions.</div>"}
            <div class='muted' style='margin:8px 0;'>Overdue</div>
            ${overdue.map(t => taskRow(t,true)).join('') || "<div class='muted'>No overdue tasks.</div>"}
            <div class='muted' style='margin:8px 0;'>Quick wins</div>
            ${quick.map(t => taskRow(t)).join('') || "<div class='muted'>No quick wins.</div>"}
          `;
        }

        function taskRow(t, red=false) {
          return `<div class='row'>
            <div class='task ${red?'overdue':''}>${esc(t.header)}${t.overdue?" <span class='overdue'>●</span>":''}</div>
            <div class='muted'>${esc(t.file)} · ${esc((t.state || '').toUpperCase() || '—')}${t.due?` · deadline ${esc(t.due)}`:''}${t.scheduled?` · scheduled ${esc(t.scheduled)}`:''}</div>
          </div>`;
        }

        function badgeClass(state) {
          const s = (state || '').toUpperCase();
          if (!s) return 'state-badge state-EMPTY';
          return `state-badge state-${s}`;
        }

        function chipClassForState(state) {
          const s = (state || '').toUpperCase();
          if (!s) return 'chip-action';
          if (s === 'NEXT') return 'chip-next';
          if (s === 'READY' || s === 'STARTED') return 'chip-ready';
          if (s === 'WAITING') return 'chip-waiting';
          if (s === 'DONE' || s === 'FAILED') return 'chip-done';
          if (s === 'CANCELLED') return 'chip-cancelled';
          return 'chip-todo';
        }

        function reportStateClass(state) {
          const s = (state || '').toUpperCase();
          if (!s) return 'state-EMPTY';
          if (s === 'NEXT' || s === 'READY' || s === 'STARTED') return 'state-NEXT';
          if (s === 'WAITING') return 'state-WAITING';
          if (s === 'DONE' || s === 'CANCELLED' || s === 'FAILED') return 'state-DONE';
          return 'state-TODO';
        }

        function buildChildrenMap(items) {
          const children = new Map();
          for (const t of items) children.set(t.id, []);
          for (const t of items) {
            if (!t.path || t.path.length <= 1) continue;
            const parentId = idFrom(t.file, t.path.slice(0, -1));
            if (children.has(parentId)) children.get(parentId).push(t);
          }
          return children;
        }

        function sortByPath(items) {
          const order = { NEXT: 0, WAITING: 1, TODO: 2, DONE: 3, CANCELLED: 4, FAILED: 5 };
          const normalized = (state) => {
            const s = (state || '').toUpperCase();
            if (s === 'READY' || s === 'STARTED') return 'NEXT';
            return s;
          };
          const pathCmp = (a,b) => {
            const pa = a.path || [];
            const pb = b.path || [];
            const n = Math.min(pa.length, pb.length);
            for (let i=0;i<n;i++) { if (pa[i] !== pb[i]) return pa[i]-pb[i]; }
            return pa.length - pb.length;
          };
          return items.slice().sort((a,b) => {
            const ra = order[normalized(a.state)] ?? 99;
            const rb = order[normalized(b.state)] ?? 99;
            if (ra !== rb) return ra - rb;
            return pathCmp(a,b);
          });
        }

        function projectKey(file) {
          return `project::${file}`;
        }

        function folderKey(folder) {
          return `folder::${folder}`;
        }

        function parentFolderOfFile(file) {
          const parts = String(file || '').split('/').filter(Boolean);
          return parts.length > 1 ? parts[0] : '';
        }

        function taskIdFromPath(file, path) {
          return `${file}::${(path || []).join('.')}`;
        }

        function expandFirstLevelSubtrees(file) {
          const project = (DATA.projects || []).find(p => p.file === file);
          if (!project) return;
          for (const t of (project.items || [])) {
            if (Array.isArray(t.path) && t.path.length === 1) {
              COLLAPSED.delete(t.id);
            }
          }
        }

        function normalizeStateForFilter(state) {
          const s = (state || '').toUpperCase();
          if (s === 'READY' || s === 'STARTED') return 'NEXT';
          if (s === 'CANCELLED' || s === 'FAILED') return 'DONE';
          return s || '';
        }

        function itemMatchesFilters(t) {
          const ns = normalizeStateForFilter(t.state);
          if (taskFilter !== 'ALL') {
            if (taskFilter === 'DONE') {
              if (!['DONE', 'CANCELLED', 'FAILED'].includes((t.state || '').toUpperCase())) return false;
            } else if (ns !== taskFilter) {
              return false;
            }
          }
          if (!taskSearch) return true;
          const hay = [
            t.header || '',
            t.file || '',
            (t.state || '').toUpperCase(),
            t.due || '',
            t.scheduled || '',
          ].join(' ').toLowerCase();
          return hay.includes(taskSearch);
        }

        function filterProjectItemsForView(items) {
          const src = items || [];
          const matched = src.filter(itemMatchesFilters);
          if (!matched.length) return [];
          const byId = new Map(src.map(t => [t.id, t]));
          const keep = new Set();
          for (const t of matched) {
            keep.add(t.id);
            let curPath = Array.isArray(t.path) ? t.path.slice(0, -1) : [];
            while (curPath.length) {
              const pid = taskIdFromPath(t.file, curPath);
              if (!byId.has(pid) || keep.has(pid)) break;
              keep.add(pid);
              curPath = curPath.slice(0, -1);
            }
          }
          return src.filter(t => keep.has(t.id));
        }

        function setTaskFilter(filter) {
          taskFilter = filter || 'ALL';
          for (const el of document.querySelectorAll('.filter-chip[data-filter]')) {
            el.classList.toggle('active', el.getAttribute('data-filter') === taskFilter);
          }
          selectedTaskId = null;
          renderTree();
        }

        function focusTaskById(id) {
          if (!id) return;
          const root = document.getElementById('taskTree');
          const row = root ? root.querySelector(`.task-row[data-task-id="${CSS.escape(id)}"]`) : null;
          if (!row) return;
          row.scrollIntoView({block:'nearest'});
          row.focus();
        }

        function ensureSelectedVisible() {
          if (selectedTaskId && visibleTaskOrder.includes(selectedTaskId)) return;
          selectedTaskId = visibleTaskOrder[0] || null;
        }

        function renderTaskNode(t, childrenMap, indent, acc) {
          const kid = sortByPath(childrenMap.get(t.id) || []);
          const hasChild = kid.length > 0;
          const open = !COLLAPSED.has(t.id);
          const isTodo = (t.state || '').toUpperCase() === 'TODO';
          const tabIndex = selectedTaskId === t.id ? 0 : -1;
          const parentId = (t.path && t.path.length > 1) ? taskIdFromPath(t.file, t.path.slice(0, -1)) : null;
          const firstChildId = hasChild ? kid[0].id : null;
          const isEditing = editingTaskId === t.id;
          const canDelegate = !['WAITING', 'DONE', 'CANCELLED', 'FAILED'].includes((t.state || '').toUpperCase());
          const line2PadPx = (Math.max(0, indent) * 12) + 40;

          visibleTaskOrder.push(t.id);
          visibleTaskMeta.set(t.id, {
            id: t.id,
            type: 'task',
            file: t.file,
            path: t.path || [],
            parentId,
            hasChild,
            open,
            firstChildId,
            state: (t.state || '').toUpperCase(),
          });

          acc.push(`<div class='task-row ${isTodo ? 'todo-row' : ''} ${selectedTaskId === t.id ? 'selected-row' : ''}' data-task-id='${esc(t.id)}' tabindex='${tabIndex}' onclick='selectTask("${esc(t.id)}")'>
            <div class='task-main'>
              <div class='task-line1'>
                <span>${'<span class="indent-line"></span>'.repeat(Math.max(0, indent))}</span>
                <button class='toggle-btn ${hasChild ? '' : 'placeholder'}' ${!hasChild ? 'tabindex="-1"' : ''} onclick='event.stopPropagation(); toggleNode("${esc(t.id)}")'>${hasChild ? (open ? '▾' : '▸') : '•'}</button>
                <span class='${badgeClass(t.state)}'>${esc((t.state || '').toUpperCase() || '—')}</span>
                ${isEditing
                  ? `<input id='inline-edit-${esc(t.id)}' class='task-edit-input' value='${esc(t.header || '')}' onclick='event.stopPropagation()' onkeydown='handleInlineEditKeydown(event,"${esc(t.id)}")' onblur='saveInlineEdit("${esc(t.id)}")' />`
                  : `<span class='task-header' onclick='event.stopPropagation(); startInlineEdit("${esc(t.id)}")'>${esc(t.header || '(no title)')}</span>`}
              </div>
              ${(t.due || t.scheduled) ? `<div class='task-line2' style='padding-left:${line2PadPx}px;'>
                ${t.due ? `<span class='task-date-pill clickable deadline ${t.overdue ? 'overdue' : ''}' onclick='event.stopPropagation(); openDateEditor("${esc(t.id)}","due")'>DEADLINE ${esc(t.due)}</span>` : ''}
                ${t.scheduled ? `<span class='task-date-pill clickable scheduled' onclick='event.stopPropagation(); openDateEditor("${esc(t.id)}","scheduled")'>SCHEDULED ${esc(t.scheduled)}</span>` : ''}
              </div>` : ''}
            </div>
            <div class='task-actions'>
              <span class='chip chip-action' onclick='event.stopPropagation(); openDateEditor("${esc(t.id)}","due")'>📅 due</span>
              <span class='chip chip-action' onclick='event.stopPropagation(); openDateEditor("${esc(t.id)}","scheduled")'>🗓 sched</span>
              ${canDelegate ? `<span class='chip chip-action' onclick='event.stopPropagation(); delegateTask("${esc(t.id)}")'>delegate</span>` : ''}
            </div>
          </div>`);

          if (hasChild && open) {
            for (const c of kid) {
              renderTaskNode(c, childrenMap, indent + 1, acc);
            }
          }
        }

        function renderTree() {
          const root = document.getElementById('taskTree');
          const blocks = [];
          visibleTaskOrder = [];
          visibleTaskMeta = new Map();
          const rawProjects = (DATA.projects || []).slice().sort((a,b) => String(a.file || '').localeCompare(String(b.file || '')));
          const topFiles = [];
          const folderMap = new Map();

          for (const p of rawProjects) {
            const folder = parentFolderOfFile(p.file);
            if (!folder) {
              topFiles.push(p);
              continue;
            }
            if (!folderMap.has(folder)) folderMap.set(folder, []);
            folderMap.get(folder).push(p);
          }

          const folderNames = Array.from(folderMap.keys()).sort((a,b) => a.localeCompare(b));

          function renderFileProject(p, baseIndent=0) {
            const items = filterProjectItemsForView(p.items || []);
            const topLevel = items.filter(t => t.path && t.path.length === 1);

            const projectOpen = !COLLAPSED.has(projectKey(p.file));
            const nextCount = (p.items || []).filter(t => itemMatchesFilters(t) && ['NEXT','READY','STARTED'].includes((t.state || '').toUpperCase())).length;
            const todoBadge = nextCount > 0 ? `<span class='state-badge state-NEXT'>${nextCount} NEXT</span>` : '';
            const pKey = projectKey(p.file);
            const projectEditing = editingProjectFile === p.file;
            const displayName = baseIndent > 0 ? String(p.file).split('/').slice(1).join('/') : p.file;
            const projectInput = projectEditing
              ? `<input id='${projectInputId(p.file)}' class='task-edit-input' value='${esc(p.file)}' onclick='event.stopPropagation()' onkeydown='handleInlineProjectEditKeydown(event,"${esc(p.file)}")' onblur='saveInlineProjectEdit("${esc(p.file)}")' />`
              : `<strong>${esc(displayName)}</strong>`;
            const firstChildId = topLevel.length ? sortByPath(topLevel)[0].id : null;

            visibleTaskOrder.push(pKey);
            visibleTaskMeta.set(pKey, {
              id: pKey,
              type: 'project',
              file: p.file,
              path: [],
              parentId: baseIndent > 0 ? folderKey(parentFolderOfFile(p.file)) : null,
              hasChild: topLevel.length > 0,
              open: projectOpen,
              firstChildId,
              state: '',
            });

            const pTabIndex = selectedTaskId === pKey ? 0 : -1;
            blocks.push(`<div class='project-head task-row ${selectedTaskId === pKey ? 'selected-row' : ''}' data-task-id='${esc(pKey)}' tabindex='${pTabIndex}' onclick='selectTask("${esc(pKey)}")'>
              <button class='toggle-btn' onclick='toggleProject("${esc(p.file)}")'>${projectOpen ? '▾' : '▸'}</button>
              <span>${'<span class="indent-line"></span>'.repeat(Math.max(0, baseIndent))}</span>
              ${projectInput}
              <span class='muted'>${items.length} tasks</span>
              ${todoBadge}
            </div>`);

            if (!projectOpen) return;
            const childrenMap = buildChildrenMap(items);
            for (const t of sortByPath(topLevel)) {
              renderTaskNode(t, childrenMap, baseIndent, blocks);
            }
          }

          for (const p of topFiles) renderFileProject(p, 0);

          for (const folder of folderNames) {
            const files = (folderMap.get(folder) || []).slice().sort((a,b) => String(a.file || '').localeCompare(String(b.file || '')));
            const fKey = folderKey(folder);
            const folderOpen = !COLLAPSED.has(fKey);
            const firstChildId = files.length ? projectKey(files[0].file) : null;

            visibleTaskOrder.push(fKey);
            visibleTaskMeta.set(fKey, {
              id: fKey,
              type: 'folder',
              folder,
              file: '',
              path: [],
              parentId: null,
              hasChild: files.length > 0,
              open: folderOpen,
              firstChildId,
              state: '',
            });

            const fTabIndex = selectedTaskId === fKey ? 0 : -1;
            blocks.push(`<div class='project-head task-row ${selectedTaskId === fKey ? 'selected-row' : ''}' data-task-id='${esc(fKey)}' tabindex='${fTabIndex}' onclick='selectTask("${esc(fKey)}")'>
              <button class='toggle-btn' onclick='toggleFolder("${esc(folder)}")'>${folderOpen ? '▾' : '▸'}</button>
              <strong>${esc(folder)}</strong>
              <span class='muted'>${files.length} files</span>
            </div>`);

            if (!folderOpen) continue;
            for (const p of files) renderFileProject(p, 1);
          }

          ensureSelectedVisible();

          // Rebuild row tab-index with selected id after the visibility pass.
          const html = blocks.join('');
          root.innerHTML = html || "<div class='muted' style='padding:10px;'>No tasks match current filter/search.</div>";

          for (const row of root.querySelectorAll('.task-row[data-task-id]')) {
            const tid = row.getAttribute('data-task-id');
            row.setAttribute('tabindex', tid === selectedTaskId ? '0' : '-1');
          }

          if (shouldRestoreTaskFocus && selectedTaskId) {
            requestAnimationFrame(() => focusTaskById(selectedTaskId));
            shouldRestoreTaskFocus = false;
          }

          if (editingProjectFile) {
            requestAnimationFrame(() => {
              const el = document.getElementById(projectInputId(editingProjectFile));
              if (el) { el.focus(); el.select(); }
            });
          }
        }

        function renderTimeline() {
          const box = document.getElementById('timeline');
          box.innerHTML = DATA.timeline.slice(0,28).map(t => `<div class='row'><div class='task'>${esc(t.header)}</div><div class='muted'>${t.due?('DEADLINE '+esc(t.due)):''} ${t.scheduled?(' · SCHEDULED '+esc(t.scheduled)):''} · ${esc((t.state || '').toUpperCase() || '—')} · ${esc(t.file)}</div></div>`).join('') || "<div class='muted'>No timeline items.</div>";
        }

        function fillFiles() {
          const sel = document.getElementById('q-file');
          sel.innerHTML = DATA.files.map(f => `<option value='${esc(f)}'>${esc(f)}</option>`).join('');
        }

        function jumpToFile() {
          if (!DATA || !Array.isArray(DATA.files) || !DATA.files.length) return;
          const current = document.getElementById('q-file')?.value || DATA.files[0];
          const query = prompt('Jump to file (type part of file name):', current || '');
          if (query === null) return;
          const q = query.trim().toLowerCase();
          const match = DATA.files.find(f => !q || f.toLowerCase().includes(q));
          if (!match) {
            alert('No file match found.');
            return;
          }
          const fileSel = document.getElementById('q-file');
          if (fileSel) fileSel.value = match;
          COLLAPSED.delete(projectKey(match));
          selectedTaskId = projectKey(match);
          shouldRestoreTaskFocus = true;
          renderTree();
        }

        async function mobileQuickAdd() {
          const file = document.getElementById('q-file')?.value || (DATA.files && DATA.files[0]) || '';
          if (!file) return;
          const header = prompt('Task title', '');
          if (header === null || !header.trim()) return;
          await api('/gtd/task/create', {
            method:'POST',
            body: JSON.stringify({ file, header: header.trim() })
          });
          await refreshData();
        }

        function fillFolders() {
          const sel = document.getElementById('q-folder');
          const folders = (DATA.folders || []);
          sel.innerHTML = folders.length
            ? folders.map(f => `<option value='${esc(f)}'>📁 ${esc(f)}</option>`).join('')
            : "<option value=''>No folders</option>";
        }

        async function deleteFolderByName(folder, askConfirm=true) {
          const clean = String(folder || '').trim();
          if (!clean) return;
          if (askConfirm && !confirm(`Move folder to trash?\n\n${clean}`)) return;
          await api('/gtd/folder/delete', {method:'POST', body: JSON.stringify({folder: clean})});
          await refreshData();
        }

        function nearestProjectKeyByFile(file) {
          const files = (DATA && DATA.projects ? DATA.projects.map(p => p.file) : []);
          if (!files.length) return null;
          const idx = files.indexOf(file);
          if (idx < 0) return projectKey(files[0]);
          for (let i = idx + 1; i < files.length; i++) {
            if (files[i] !== file) return projectKey(files[i]);
          }
          for (let i = idx - 1; i >= 0; i--) {
            if (files[i] !== file) return projectKey(files[i]);
          }
          return null;
        }

        function nearestVisibleRowId(fromId) {
          const order = visibleTaskOrder || [];
          if (!order.length) return null;
          const idx = order.indexOf(fromId);
          if (idx < 0) return order[0];
          for (let i = idx + 1; i < order.length; i++) {
            if (order[i] !== fromId) return order[i];
          }
          for (let i = idx - 1; i >= 0; i--) {
            if (order[i] !== fromId) return order[i];
          }
          return null;
        }

        async function deleteProjectFileByHotkey(file) {
          const relFile = String(file || '').trim();
          if (!relFile) return;
          if (!confirm(`Delete this .smos file?\n\n${relFile}\n\nThis cannot be undone from the UI.`)) return;
          selectedTaskId = nearestProjectKeyByFile(relFile);
          shouldRestoreTaskFocus = true;
          await api('/gtd/file/delete', {method:'POST', body: JSON.stringify({file: relFile, force: true})});
          await refreshData();
        }

        async function deleteSelectedFolder() {
          const sel = document.getElementById('q-folder');
          const folder = (sel && sel.value) ? sel.value.trim() : '';
          await deleteFolderByName(folder, true);
        }

        async function createFolderPrompt() {
          const name = prompt('New folder name (top-level)', '');
          if (name === null) return;
          const clean = String(name || '').trim();
          if (!clean) return;
          await api('/gtd/folder/create', {method:'POST', body: JSON.stringify({name: clean})});
          await refreshData();
          const sel = document.getElementById('q-folder');
          if (sel) sel.value = clean;
        }

        function suggestNewFilePath() {
          const folderSel = document.getElementById('q-folder');
          const folder = (folderSel && folderSel.value) ? String(folderSel.value).trim() : '';
          const prefix = folder ? `${folder}/` : '';
          const used = new Set((DATA && DATA.files) ? DATA.files : []);
          for (let i = 1; i < 999; i++) {
            const cand = `${prefix}new-file-${i}.smos`;
            if (!used.has(cand)) return cand;
          }
          return `${prefix}new-file-${Date.now()}.smos`;
        }

        async function createFileInline() {
          const res = await api('/gtd/file/create', {method:'POST', body: JSON.stringify({file: suggestNewFilePath()})});
          await refreshData();
          if (res && res.file) {
            const fileSel = document.getElementById('q-file');
            if (fileSel) fileSel.value = res.file;
            COLLAPSED.delete(projectKey(res.file));
            const pf = parentFolderOfFile(res.file);
            if (pf) COLLAPSED.delete(folderKey(pf));
            selectedTaskId = projectKey(res.file);
            startInlineProjectEdit(res.file, true);
          }
        }

        async function createFilePrompt() {
          await createFileInline();
        }

        function currentInboxItem() {
          const items = (INBOX_DATA && INBOX_DATA.inbox) || [];
          if (!items.length) return null;
          const idx = Math.max(0, Math.min(INBOX_INDEX, items.length - 1));
          INBOX_INDEX = idx;
          return items[idx];
        }

        function renderInbox() {
          const countEl = document.getElementById('inboxCount');
          const fileEl = document.getElementById('inboxFile');
          const textEl = document.getElementById('inboxText');
          const emptyEl = document.getElementById('inboxEmpty');
          const posEl = document.getElementById('inboxPos');
          if (!countEl || !fileEl || !textEl || !emptyEl || !posEl) return;

          const items = (INBOX_DATA && INBOX_DATA.inbox) || [];
          const count = items.length;
          countEl.textContent = `${count} item${count === 1 ? '' : 's'}`;
          fileEl.textContent = INBOX_DATA && INBOX_DATA.file ? `File: ${INBOX_DATA.file}` : '';

          if (!count) {
            textEl.style.display = 'none';
            emptyEl.style.display = 'block';
            posEl.textContent = '0 / 0';
            return;
          }

          const item = currentInboxItem();
          textEl.textContent = item ? item.text : '';
          textEl.style.display = 'block';
          emptyEl.style.display = 'none';
          posEl.textContent = `${INBOX_INDEX + 1} / ${count}`;
        }

        async function fetchInbox() {
          INBOX_DATA = await api('/gtd/inbox');
          const count = (INBOX_DATA && INBOX_DATA.inbox ? INBOX_DATA.inbox.length : 0);
          if (INBOX_INDEX >= count) INBOX_INDEX = Math.max(0, count - 1);
          renderInbox();
        }

        function inboxPrev() {
          const items = (INBOX_DATA && INBOX_DATA.inbox) || [];
          if (!items.length) return;
          INBOX_INDEX = Math.max(0, INBOX_INDEX - 1);
          renderInbox();
        }

        function inboxNext() {
          const items = (INBOX_DATA && INBOX_DATA.inbox) || [];
          if (!items.length) return;
          INBOX_INDEX = Math.min(items.length - 1, INBOX_INDEX + 1);
          renderInbox();
        }

        async function inboxResolve(action) {
          const item = currentInboxItem();
          if (!item) return;
          await api('/gtd/inbox/resolve', {method:'POST', body: JSON.stringify({id: item.id, action})});
          const currentLen = ((INBOX_DATA && INBOX_DATA.inbox) || []).length;
          if (INBOX_INDEX >= currentLen - 1) INBOX_INDEX = Math.max(0, INBOX_INDEX - 1);
          await fetchInbox();
        }

        async function refreshData() {
          DATA = await api('/gtd/data');
          if (!COLLAPSE_INIT) {
            for (const p of DATA.projects) {
              COLLAPSED.add(projectKey(p.file));
              const pf = parentFolderOfFile(p.file);
              if (pf) COLLAPSED.add(folderKey(pf));
            }
            const all = DATA.projects.flatMap(p => p.items || []);
            for (const t of all) COLLAPSED.add(t.id);
            COLLAPSE_INIT = true;
          }
          document.getElementById('st-total').textContent = DATA.stats.total;
          document.getElementById('st-next').textContent = DATA.stats.next;
          document.getElementById('st-waiting').textContent = DATA.stats.waiting;
          document.getElementById('st-overdue').textContent = DATA.stats.overdue;
          fillFiles(); fillFolders(); renderTree();
          await fetchInbox();
          if (!reportInitialized) {
            reportInitialized = true;
            openReport('next');
          }
        }

        async function quickCreateRoot() {
          const payload = {
            header: document.getElementById('q-header').value,
            file: document.getElementById('q-file').value,
          };
          if (!payload.file) return alert('Please select a file');
          if (!payload.header.trim()) return alert('Please enter a task');
          await api('/gtd/task/create', {method:'POST', body: JSON.stringify(payload)});
          document.getElementById('q-header').value='';
          await refreshData();
        }

        async function setState(id, state, preserveFocus=false) {
          if (preserveFocus) {
            selectedTaskId = id;
            shouldRestoreTaskFocus = true;
          }
          const sendState = (state === '' || state === null || state === undefined) ? '__NONE__' : state;
          await api('/gtd/task/state', {method:'POST', body: JSON.stringify({id, state: sendState})});
          await refreshData();
        }

        async function delegateTask(id) {
          if (!id) return;
          const targetEmail = String(prompt('Delegate to email address') || '').trim();
          if (!targetEmail) return;
          const targetName = String(prompt('Delegate to name (optional)') || '').trim();
          selectedTaskId = id;
          shouldRestoreTaskFocus = true;
          try {
            await api('/gtd/task/delegate', {
              method:'POST',
              body: JSON.stringify({id, target_email: targetEmail, target_name: targetName})
            });
            await refreshData();
          } catch (e) {
            alert('Delegation failed: ' + e.message);
          }
        }

        async function deleteTask(id, pushUndo=true) {
          if (!id) return;
          selectedTaskId = nearestVisibleRowId(id);
          shouldRestoreTaskFocus = true;
          const res = await api('/gtd/task/delete', {method:'POST', body: JSON.stringify({id})});
          if (pushUndo && res && res.undo) {
            UNDO_DELETE_STACK.push(res.undo);
            if (UNDO_DELETE_STACK.length > 20) UNDO_DELETE_STACK.shift();
          }
          await refreshData();
        }

        async function deleteFile(relFile) {
          if (!relFile) return;
          await api('/gtd/file/delete', {method:'POST', body: JSON.stringify({file: relFile})});
          await refreshData();
        }

        async function undoDelete() {
          const snap = UNDO_DELETE_STACK.pop();
          if (!snap) return;
          const res = await api('/gtd/task/restore', {method:'POST', body: JSON.stringify(snap)});
          if (res && res.id) {
            selectedTaskId = res.id;
            shouldRestoreTaskFocus = true;
          }
          await refreshData();
        }

        async function quickEdit(id) {
          const all = DATA.projects.flatMap(p => p.items || []);
          const t = all.find(x => x.id === id);
          if (!t) return;
          const header = prompt('Task title', t.header || '');
          if (header === null) return;
          const due = prompt('Due date YYYY-MM-DD (empty to clear)', t.due || '');
          if (due === null) return;
          await api('/gtd/task/update', {method:'POST', body: JSON.stringify({id, header, due})});
          await refreshData();
        }

        function startInlineEdit(id, asNew=false) {
          editingTaskId = id;
          if (asNew) pendingNewTaskId = id;
          shouldRestoreTaskFocus = false;
          renderTree();
          requestAnimationFrame(() => {
            const el = document.getElementById(`inline-edit-${id}`);
            if (el) { el.focus(); el.select(); }
          });
        }

        function cancelInlineEdit() {
          editingTaskId = null;
          pendingNewTaskId = null;
          inlineEditSaving = false;
          shouldRestoreTaskFocus = true;
          renderTree();
        }

        async function cancelNewTaskInline(id) {
          editingTaskId = null;
          pendingNewTaskId = null;
          inlineEditSaving = false;
          await deleteTask(id, false);
        }

        async function saveInlineEdit(id) {
          if (inlineEditSaving || editingTaskId !== id) return;
          const el = document.getElementById(`inline-edit-${id}`);
          if (!el) return cancelInlineEdit();
          const header = (el.value || '').trim();
          if (!header) {
            if (id === pendingNewTaskId) {
              await cancelNewTaskInline(id);
              return;
            }
            return cancelInlineEdit();
          }
          inlineEditSaving = true;
          try {
            await api('/gtd/task/update', {method:'POST', body: JSON.stringify({id, header})});
            editingTaskId = null;
            if (pendingNewTaskId === id) pendingNewTaskId = null;
            selectedTaskId = id;
            shouldRestoreTaskFocus = true;
            await refreshData();
          } finally {
            inlineEditSaving = false;
          }
        }

        function handleInlineEditKeydown(ev, id) {
          if (ev.key === 'Enter') {
            ev.preventDefault();
            saveInlineEdit(id);
            return;
          }
          if (ev.key === 'Escape') {
            ev.preventDefault();
            if (id === pendingNewTaskId) {
              cancelNewTaskInline(id);
            } else {
              cancelInlineEdit();
            }
            return;
          }
        }

        function startInlineProjectEdit(file, isNew=false) {
          editingProjectFile = file;
          if (isNew) pendingNewFile = file;
          shouldRestoreTaskFocus = false;
          renderTree();
        }

        async function cancelInlineProjectEdit(file) {
          const oldFile = file || editingProjectFile;
          editingProjectFile = null;
          inlineProjectEditSaving = false;
          if (oldFile && pendingNewFile === oldFile) {
            pendingNewFile = null;
            await deleteFile(oldFile);
            return;
          }
          pendingNewFile = null;
          shouldRestoreTaskFocus = true;
          renderTree();
        }

        async function saveInlineProjectEdit(oldFile) {
          if (!oldFile || inlineProjectEditSaving || editingProjectFile !== oldFile) return;
          const el = document.getElementById(projectInputId(oldFile));
          if (!el) return cancelInlineProjectEdit(oldFile);
          const next = String(el.value || '').trim();
          if (!next) {
            await cancelInlineProjectEdit(oldFile);
            return;
          }

          if (next === oldFile || next === oldFile.replace(/\.smos$/i, '')) {
            editingProjectFile = null;
            if (pendingNewFile === oldFile) pendingNewFile = null;
            selectedTaskId = projectKey(oldFile);
            shouldRestoreTaskFocus = true;
            renderTree();
            return;
          }

          inlineProjectEditSaving = true;
          try {
            const res = await api('/gtd/file/rename', {
              method:'POST',
              body: JSON.stringify({old_file: oldFile, new_file: next}),
            });
            editingProjectFile = null;
            pendingNewFile = null;
            if (res && res.file) {
              selectedTaskId = projectKey(res.file);
              COLLAPSED.delete(projectKey(res.file));
            }
            shouldRestoreTaskFocus = true;
            await refreshData();
          } finally {
            inlineProjectEditSaving = false;
          }
        }

        function handleInlineProjectEditKeydown(ev, oldFile) {
          if (ev.key === 'Enter') {
            ev.preventDefault();
            saveInlineProjectEdit(oldFile);
            return;
          }
          if (ev.key === 'Escape') {
            ev.preventDefault();
            cancelInlineProjectEdit(oldFile);
            return;
          }
        }

        function selectTask(id, focus=false) {
          if (!id) return;
          selectedTaskId = id;
          const meta = visibleTaskMeta.get(id);
          const fileSel = document.getElementById('q-file');
          if (meta && meta.file && fileSel) fileSel.value = meta.file;
          const root = document.getElementById('taskTree');
          if (root) {
            for (const row of root.querySelectorAll('.task-row[data-task-id]')) {
              const tid = row.getAttribute('data-task-id');
              row.setAttribute('tabindex', tid === selectedTaskId ? '0' : '-1');
              if (tid === selectedTaskId) row.classList.add('selected-row');
              else row.classList.remove('selected-row');
            }
          }
          if (focus) focusTaskById(id);
        }

        function cycleState(state, backwards=false) {
          const order = ['', 'TODO', 'NEXT', 'READY', 'WAITING', 'DONE', 'CANCELLED'];
          const current = (state || '').toUpperCase();
          const idx = order.indexOf(current);
          const i = idx >= 0 ? idx : 0;
          const nextIndex = backwards
            ? (i - 1 + order.length) % order.length
            : (i + 1) % order.length;
          return order[nextIndex];
        }

        function clearPendingStatePrefix() {
          pendingStatePrefix = null;
          if (pendingStateTimer) {
            clearTimeout(pendingStateTimer);
            pendingStateTimer = null;
          }
        }

        function armStatePrefix(prefix) {
          pendingStatePrefix = prefix;
          if (pendingStateTimer) clearTimeout(pendingStateTimer);
          pendingStateTimer = setTimeout(() => {
            pendingStatePrefix = null;
            pendingStateTimer = null;
          }, 1200);
        }

        function clearPendingDatePrefix() {
          pendingDatePrefix = null;
          if (pendingDateTimer) {
            clearTimeout(pendingDateTimer);
            pendingDateTimer = null;
          }
        }

        function armDatePrefix(prefix) {
          pendingDatePrefix = prefix;
          if (pendingDateTimer) clearTimeout(pendingDateTimer);
          pendingDateTimer = setTimeout(() => {
            pendingDatePrefix = null;
            pendingDateTimer = null;
          }, 1200);
        }

        function moveSelection(delta) {
          if (!visibleTaskOrder.length) return;
          const current = selectedTaskId && visibleTaskOrder.includes(selectedTaskId)
            ? visibleTaskOrder.indexOf(selectedTaskId)
            : 0;
          const next = Math.max(0, Math.min(visibleTaskOrder.length - 1, current + delta));
          if (next === current && ((delta < 0 && current === 0) || (delta > 0 && current === visibleTaskOrder.length - 1))) {
            if (switchFileByDelta(delta)) return;
          }
          selectedTaskId = visibleTaskOrder[next];
          selectTask(selectedTaskId, true);
        }

        function firstTaskIdInFile(file) {
          const project = (DATA.projects || []).find(p => p.file === file);
          if (!project) return null;
          const items = sortByPath(filterProjectItemsForView(project.items || []));
          return items.length ? items[0].id : null;
        }

        function lastTaskIdInFile(file) {
          const project = (DATA.projects || []).find(p => p.file === file);
          if (!project) return null;
          const items = sortByPath(filterProjectItemsForView(project.items || []));
          return items.length ? items[items.length - 1].id : null;
        }

        function switchFileByDelta(delta) {
          if (!DATA || !Array.isArray(DATA.projects) || !DATA.projects.length) return false;
          const files = DATA.projects.map(p => p.file);
          if (!files.length) return false;

          let currentFile = null;
          const selectedMeta = selectedTaskId ? visibleTaskMeta.get(selectedTaskId) : null;
          if (selectedMeta) currentFile = selectedMeta.file;
          if (!currentFile) {
            const fileSel = document.getElementById('q-file');
            currentFile = (fileSel && fileSel.value) ? fileSel.value : files[0];
          }

          let idx = files.indexOf(currentFile);
          if (idx < 0) idx = 0;
          const step = delta > 0 ? 1 : -1;
          for (let i = idx + step; i >= 0 && i < files.length; i += step) {
            const file = files[i];
            const targetId = step > 0 ? firstTaskIdInFile(file) : lastTaskIdInFile(file);
            if (!targetId) continue;
            COLLAPSED.delete(projectKey(file));
            selectedTaskId = targetId;
            shouldRestoreTaskFocus = true;
            renderTree();
            return true;
          }
          return false;
        }

        function expandChainToTask(id) {
          const meta = visibleTaskMeta.get(id);
          if (!meta) return;
          let p = meta.parentId;
          while (p) {
            COLLAPSED.delete(p);
            const pm = visibleTaskMeta.get(p);
            p = pm ? pm.parentId : null;
          }
          COLLAPSED.delete(projectKey(meta.file));
          const pf = parentFolderOfFile(meta.file);
          if (pf) COLLAPSED.delete(folderKey(pf));
        }

        function expandToTaskId(taskId) {
          const [file, rawPath] = String(taskId || '').split('::');
          if (!file || !rawPath) return;
          COLLAPSED.delete(projectKey(file));
          const pf = parentFolderOfFile(file);
          if (pf) COLLAPSED.delete(folderKey(pf));
          const parts = rawPath.split('.').map(x => Number(x)).filter(n => Number.isInteger(n) && n >= 0);
          if (!parts.length) return;
          for (let i = 1; i < parts.length; i++) {
            const parentId = `${file}::${parts.slice(0, i).join('.')}`;
            COLLAPSED.delete(parentId);
          }
        }

        function jumpLevel(delta) {
          if (!selectedTaskId || !visibleTaskOrder.length) return;
          const curMeta = visibleTaskMeta.get(selectedTaskId);
          if (!curMeta) return;
          const targetDepth = Math.max(0, (curMeta.path || []).length - 1 + delta);
          const curIdx = visibleTaskOrder.indexOf(selectedTaskId);
          let bestId = null;

          for (let i = curIdx + 1; i < visibleTaskOrder.length; i++) {
            const id = visibleTaskOrder[i];
            const m = visibleTaskMeta.get(id);
            if (!m) continue;
            const d = Math.max(0, (m.path || []).length - 1);
            if (d === targetDepth) { bestId = id; break; }
          }
          if (!bestId) {
            for (let i = curIdx - 1; i >= 0; i--) {
              const id = visibleTaskOrder[i];
              const m = visibleTaskMeta.get(id);
              if (!m) continue;
              const d = Math.max(0, (m.path || []).length - 1);
              if (d === targetDepth) { bestId = id; break; }
            }
          }
          if (!bestId) return;
          selectedTaskId = bestId;
          expandChainToTask(bestId);
          shouldRestoreTaskFocus = true;
          renderTree();
        }

        function toggleNode(id) {
          selectedTaskId = id;
          shouldRestoreTaskFocus = true;
          if (COLLAPSED.has(id)) COLLAPSED.delete(id); else COLLAPSED.add(id);
          renderTree();
        }

        function toggleProject(file) {
          const key = projectKey(file);
          shouldRestoreTaskFocus = true;
          if (COLLAPSED.has(key)) {
            COLLAPSED.delete(key);
            expandFirstLevelSubtrees(file);
          } else {
            COLLAPSED.add(key);
          }
          renderTree();
        }

        function toggleFolder(folder) {
          const key = folderKey(folder);
          shouldRestoreTaskFocus = true;
          if (COLLAPSED.has(key)) COLLAPSED.delete(key); else COLLAPSED.add(key);
          renderTree();
        }

        function collapseAll() {
          for (const p of DATA.projects) {
            COLLAPSED.add(projectKey(p.file));
            const pf = parentFolderOfFile(p.file);
            if (pf) COLLAPSED.add(folderKey(pf));
          }
          const all = DATA.projects.flatMap(p => p.items || []);
          for (const t of all) COLLAPSED.add(t.id);
          renderTree();
        }

        function expandAll() {
          COLLAPSED.clear();
          renderTree();
        }

        async function addNear(targetId, mode) {
          const header = prompt(mode === 'child' ? 'Subtask title' : 'Sibling task title', '');
          if (header === null || !header.trim()) return;
          const relFile = targetId.split('::')[0];
          await api('/gtd/task/create', {
            method:'POST',
            body: JSON.stringify({ file: relFile, header: header.trim(), target_id: targetId, mode })
          });
          await refreshData();
        }

        async function addNearInline(targetId, mode) {
          const relFile = targetId.split('::')[0];
          const res = await api('/gtd/task/create', {
            method:'POST',
            body: JSON.stringify({ file: relFile, header: 'new task', target_id: targetId, mode })
          });
          await refreshData();
          if (res && res.id) {
            expandToTaskId(res.id);
            selectedTaskId = res.id;
            startInlineEdit(res.id, true);
          }
        }

        async function addRootInline(file) {
          if (!file) return;
          const res = await api('/gtd/task/create', {
            method:'POST',
            body: JSON.stringify({ file, header: 'new task' })
          });
          await refreshData();
          if (res && res.id) {
            COLLAPSED.delete(projectKey(file));
            const pf = parentFolderOfFile(file);
            if (pf) COLLAPSED.delete(folderKey(pf));
            selectedTaskId = res.id;
            startInlineEdit(res.id, true);
          }
        }

        function formatReportText(txt) {
          const lines = String(txt || '').split('\\n');
          return lines.map((raw) => {
            if (!raw.trim()) return "<div class='report-gap'></div>";
            let line = esc(raw);
            line = line.replace(/\\b(TODO|NEXT|WAITING|DONE|CANCELLED|FAILED|READY|STARTED)\\b/g, (m) => {
              return `<span class='report-state-chip ${reportStateClass(m)}'>${m}</span>`;
            });
            line = line.replace(/\\bDEADLINE\\b(?:\\s+\\d{4}-\\d{2}-\\d{2})?/g, (m) => `<span class='task-date-pill deadline'>${m}</span>`);
            line = line.replace(/\\bSCHEDULED\\b(?:\\s+\\d{4}-\\d{2}-\\d{2})?/g, (m) => `<span class='task-date-pill scheduled'>${m}</span>`);
            return `<div class='report-line'>${line}</div>`;
          }).join('');
        }

        async function openReport(kind) {
          const out = document.getElementById('reportOut');
          out.className = 'report-empty';
          out.textContent = 'Loading report...';
          try {
            const txt = await api(`/gtd/report/${kind}`);
            if (!String(txt || '').trim()) {
              out.className = 'report-empty';
              out.textContent = 'No items.';
              return;
            }
            out.className = 'report-text';
            out.innerHTML = formatReportText(txt);
          } catch (e) {
            out.className = 'report-empty';
            out.textContent = 'Error: ' + e;
          }
        }

        function handleTreeKeydown(ev) {
          if (editingTaskId || editingProjectFile) return;
          const row = ev.target.closest('.task-row[data-task-id]');
          if (!row) return;
          const id = row.getAttribute('data-task-id');
          if (!id) return;
          selectTask(id, false);

          const meta = visibleTaskMeta.get(id);
          if (!meta) return;

          if (!ev.ctrlKey && !ev.metaKey && !ev.altKey && meta.type === 'task') {
            const k = String(ev.key || '');
            const kl = k.toLowerCase();

            if (pendingStatePrefix === 't') {
              const map = { w: 'WAITING', d: 'DONE', n: 'NEXT', t: 'TODO', r: 'READY', c: 'CANCELLED' };
              if (map[kl]) {
                ev.preventDefault();
                clearPendingStatePrefix();
                clearPendingDatePrefix();
                setState(id, map[kl], true);
                return;
              }
              clearPendingStatePrefix();
            }

            if (pendingDatePrefix === 's') {
              if (kl === 'd' || kl === 's') {
                ev.preventDefault();
                clearPendingDatePrefix();
                clearPendingStatePrefix();
                openDateEditor(id, kl === 'd' ? 'due' : 'scheduled');
                return;
              }
              clearPendingDatePrefix();
            }

            if (k === 't' || k === 'T') {
              ev.preventDefault();
              clearPendingDatePrefix();
              armStatePrefix('t');
              return;
            }
            if (k === 's' || k === 'S') {
              ev.preventDefault();
              clearPendingStatePrefix();
              armDatePrefix('s');
              return;
            }
          }

          if (!ev.ctrlKey && !ev.metaKey && !ev.altKey) {
            if (ev.key === 'n') {
              ev.preventDefault();
              createFileInline();
              return;
            }
            if (ev.key === 'N') {
              ev.preventDefault();
              createFolderPrompt();
              return;
            }
            if (ev.key === 'e') {
              ev.preventDefault();
              if (meta.type === 'project') addRootInline(meta.file);
              else if (meta.type === 'task') addNearInline(id, 'sibling');
              return;
            }
            if (ev.key === 'E') {
              ev.preventDefault();
              if (meta.type === 'project') addRootInline(meta.file);
              else if (meta.type === 'task') addNearInline(id, 'child');
              return;
            }
            if (ev.key === 'd' || ev.key === 'D') {
              ev.preventDefault();
              if (meta.type === 'project') {
                deleteProjectFileByHotkey(meta.file);
              } else if (meta.type === 'task') {
                deleteTask(id);
              }
              return;
            }
          }

          if (ev.key === 'ArrowUp') {
            ev.preventDefault();
            moveSelection(-1);
            return;
          }
          if (ev.key === 'ArrowDown') {
            ev.preventDefault();
            moveSelection(1);
            return;
          }
          if (ev.key === 'ArrowLeft') {
            ev.preventDefault();
            if (meta.type === 'project') {
              if (meta.open) toggleProject(meta.file);
              else if (meta.parentId && visibleTaskMeta.has(meta.parentId)) selectTask(meta.parentId, true);
              return;
            }
            if (meta.type === 'folder') {
              if (meta.open) toggleFolder(meta.folder);
              return;
            }
            if (meta.hasChild && meta.open) {
              toggleNode(id);
            } else if (meta.parentId && visibleTaskMeta.has(meta.parentId)) {
              selectTask(meta.parentId, true);
            }
            return;
          }
          if (ev.key === 'ArrowRight') {
            ev.preventDefault();
            if (meta.type === 'project') {
              if (!meta.open) {
                toggleProject(meta.file);
              } else if (meta.firstChildId && visibleTaskMeta.has(meta.firstChildId)) {
                selectTask(meta.firstChildId, true);
              }
              return;
            }
            if (meta.type === 'folder') {
              if (!meta.open) {
                toggleFolder(meta.folder);
              } else if (meta.firstChildId && visibleTaskMeta.has(meta.firstChildId)) {
                selectTask(meta.firstChildId, true);
              }
              return;
            }
            if (meta.hasChild && !meta.open) {
              toggleNode(id);
            } else if (meta.firstChildId && visibleTaskMeta.has(meta.firstChildId)) {
              selectTask(meta.firstChildId, true);
            }
            return;
          }
          if (ev.altKey && ev.key === 'ArrowUp') {
            ev.preventDefault();
            jumpLevel(-1);
            return;
          }
          if (ev.altKey && ev.key === 'ArrowDown') {
            ev.preventDefault();
            jumpLevel(1);
            return;
          }
          if (ev.key === 'Home') {
            ev.preventDefault();
            if (visibleTaskOrder.length) selectTask(visibleTaskOrder[0], true);
            return;
          }
          if (ev.key === 'End') {
            ev.preventDefault();
            if (visibleTaskOrder.length) selectTask(visibleTaskOrder[visibleTaskOrder.length - 1], true);
            return;
          }
          if (ev.key === 'Tab') {
            ev.preventDefault();
            if (meta.type === 'project') {
              if (meta.hasChild) toggleProject(meta.file);
            } else if (meta.type === 'folder') {
              if (meta.hasChild) toggleFolder(meta.folder);
            } else if (meta.hasChild) {
              toggleNode(id);
            }
            return;
          }
          if (ev.key === 'Enter') {
            ev.preventDefault();
            if (meta.type === 'project') {
              if (meta.hasChild) toggleProject(meta.file);
            } else if (meta.type === 'folder') {
              if (meta.hasChild) toggleFolder(meta.folder);
            } else {
              startInlineEdit(id);
            }
            return;
          }
          if (ev.key === ' ' || ev.code === 'Space' || ev.key === 'Spacebar') {
            ev.preventDefault();
            if (meta.type !== 'task') return;
            const nextState = cycleState(meta.state, ev.shiftKey);
            setState(id, nextState, true);
            return;
          }
          if (ev.key === 'Delete') {
            ev.preventDefault();
            if (meta.type === 'project') {
              deleteProjectFileByHotkey(meta.file);
              return;
            }
            if (meta.type !== 'task') return;
            deleteTask(id);
            return;
          }
          if ((ev.ctrlKey || ev.metaKey) && !ev.shiftKey && String(ev.key || '').toLowerCase() === 'z') {
            ev.preventDefault();
            undoDelete();
            return;
          }
        }

        document.getElementById('taskTree').addEventListener('keydown', handleTreeKeydown);
        document.getElementById('taskTree').addEventListener('focusin', (ev) => {
          const row = ev.target.closest('.task-row[data-task-id]');
          if (!row) return;
          const id = row.getAttribute('data-task-id');
          if (id) selectTask(id, false);
        });

        const taskSearchInput = document.getElementById('taskSearch');
        if (taskSearchInput) {
          taskSearchInput.addEventListener('input', (ev) => {
            taskSearch = String(ev.target.value || '').trim().toLowerCase();
            selectedTaskId = null;
            renderTree();
          });
        }

        const qFileSel = document.getElementById('q-file');
        if (qFileSel) {
          qFileSel.addEventListener('change', () => {
            const file = qFileSel.value;
            if (!file) return;
            COLLAPSED.delete(projectKey(file));
            const pf = parentFolderOfFile(file);
            if (pf) COLLAPSED.delete(folderKey(pf));
            selectedTaskId = projectKey(file);
            shouldRestoreTaskFocus = true;
            renderTree();
          });
        }

        const dateModalInput = document.getElementById('dateModalInput');
        const dateModalPicker = document.getElementById('dateModalPicker');
        if (dateModalPicker && dateModalInput) {
          dateModalPicker.addEventListener('change', () => {
            if (dateModalPicker.value) dateModalInput.value = dateModalPicker.value;
          });
        }
        if (dateModalInput) {
          dateModalInput.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter') {
              ev.preventDefault();
              saveDateEditor();
              return;
            }
            if (ev.key === 'Escape') {
              ev.preventDefault();
              closeDateEditor();
              return;
            }
          });
        }

        initTheme();
        refreshData().catch(e => { const el = document.getElementById('reportOut'); el.className='report-empty'; el.textContent = 'Error: ' + e; });
      </script>
    </body>
    </html>"""
    build_stamp = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    html_page = (
        html_page.replace("{{", "{")
        .replace("}}", "}")
        .replace("__SHARED_HEADER_CSS__", _shared_gtd_header_css())
        .replace("__SHARED_HEADER_HTML__", _shared_gtd_header_html())
        .replace("__BUILD_STAMP__", build_stamp)
    )
    return HTMLResponse(html_page)

