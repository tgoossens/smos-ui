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

    active = [t for t in tasks if t["state"] in SMOS_ACTIVE_STATES]
    waiting = [t for t in tasks if t["state"] == "WAITING"]
    done = [t for t in tasks if t["state"] in SMOS_TERMINAL_STATES]

    return {
        "workflow_dir": str(WORKFLOW_DIR),
        "inbox_file": str(INBOX_FILE),
        "files": rel_files,
        "folders": folders,
        "tasks": tasks,
        "stats": {
            "total": len(tasks),
            "active": len(active),
            "waiting": len(waiting),
            "done": len(done),
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
    return JSONResponse({"ok": True, **data, "count": len(data.get("inbox") or [])})


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


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    html = """
<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>smos-ui</title>
  <style>
    body { font-family: system-ui, Arial, sans-serif; margin: 20px; background:#0f1115; color:#e7eaf0; }
    h1 { margin-top:0; }
    .muted { color:#a5adba; font-size:14px; }
    .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:10px 0; }
    input, select, button, textarea {
      background:#171b23; color:#e7eaf0; border:1px solid #2b3342; border-radius:8px; padding:8px;
    }
    button { cursor:pointer; }
    .card { border:1px solid #2b3342; border-radius:12px; padding:12px; margin:12px 0; background:#121720; }
    .task { border-bottom:1px solid #20293a; padding:8px 0; }
    .pill { padding:2px 8px; border-radius:999px; font-size:12px; border:1px solid #2b3342; }
    .state-NEXT,.state-READY,.state-STARTED { color:#8cd8ff; }
    .state-WAITING { color:#ffd38c; }
    .state-DONE,.state-CANCELLED,.state-FAILED { color:#89d185; }
    .caps pre { white-space:pre-wrap; font-size:12px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    textarea { width:100%; min-height:130px; }
  </style>
</head>
<body>
  <h1>smos-ui</h1>
  <div class='muted'>Generic GTD UI over .smos files with native fallbacks.</div>

  <div class='card caps'>
    <div><strong>Capabilities</strong></div>
    <pre id='caps'>loading...</pre>
  </div>

  <div class='card'>
    <div class='row'>
      <select id='file'></select>
      <input id='newTask' placeholder='New task header'>
      <button onclick='addTask()'>Add task</button>
      <button onclick='refresh()'>Refresh</button>
      <button onclick='openReport("next")'>Report: next</button>
      <button onclick='openReport("work")'>Report: work</button>
      <button onclick='openReport("waiting")'>Report: waiting</button>
      <button onclick='openReport("projects")'>Report: projects</button>
      <button onclick='window.open("/gtd/terminal", "_blank")'>Terminal</button>
    </div>
    <div id='stats' class='muted'></div>
    <div id='tasks'></div>
  </div>

  <div class='card'>
    <div><strong>Inbox</strong></div>
    <div class='row'>
      <input id='inboxText' placeholder='Add inbox item'>
      <button onclick='addInbox()'>Add</button>
    </div>
    <div id='inbox'></div>
  </div>

  <div class='card'>
    <div><strong>Report output</strong></div>
    <textarea id='reportOut' class='mono' readonly></textarea>
  </div>

<script>
let DATA = null;
let INBOX = null;

async function api(url, opts) {
  const r = await fetch(url, opts || {});
  const ct = r.headers.get('content-type') || '';
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || (r.status + ' ' + r.statusText));
  }
  if (ct.includes('application/json')) return r.json();
  return r.text();
}

function esc(s) {
  return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function refresh() {
  DATA = await api('/gtd/data');
  document.getElementById('caps').textContent = JSON.stringify(DATA.capabilities || {}, null, 2);

  const fileSel = document.getElementById('file');
  const old = fileSel.value;
  fileSel.innerHTML = (DATA.files || []).map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join('');
  if (old && [...fileSel.options].some(o => o.value === old)) fileSel.value = old;

  const st = DATA.stats || {};
  document.getElementById('stats').textContent =
    `files=${st.files||0}, total=${st.total||0}, active=${st.active||0}, waiting=${st.waiting||0}, done=${st.done||0}`;

  const tasks = (DATA.tasks || []).map(t => {
    const indent = '&nbsp;'.repeat((t.level || 0) * 4);
    const state = t.state || '-';
    return `<div class='task'>
      <div>${indent}<span class='pill state-${esc(state)}'>${esc(state)}</span> <strong>${esc(t.header)}</strong>
      <span class='muted'>(${esc(t.file)})</span></div>
      <div class='row'>
        <select onchange='setState("${esc(t.id)}", this.value)'>
          ${['','TODO','NEXT','READY','STARTED','WAITING','DONE','CANCELLED','FAILED'].map(s =>
            `<option value="${s}" ${s===state?'selected':''}>${s||'(no-state)'}</option>`).join('')}
        </select>
        <button onclick='delTask("${esc(t.id)}")'>Delete</button>
      </div>
    </div>`;
  }).join('');

  document.getElementById('tasks').innerHTML = tasks || '<div class="muted">No tasks.</div>';

  INBOX = await api('/gtd/inbox');
  renderInbox();
}

function renderInbox() {
  const items = (INBOX && INBOX.inbox) || [];
  document.getElementById('inbox').innerHTML = items.map(x =>
    `<div class='row'><span>${esc(x.text)}</span>
      <button onclick='resolveInbox("${esc(x.id)}","done")'>Done</button>
      <button onclick='resolveInbox("${esc(x.id)}","remove")'>Remove</button>
    </div>`
  ).join('') || '<div class="muted">Inbox empty.</div>';
}

async function addTask() {
  const file = document.getElementById('file').value;
  const header = document.getElementById('newTask').value.trim();
  if (!file || !header) return;
  await api('/gtd/task/create', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({file, header})
  });
  document.getElementById('newTask').value = '';
  await refresh();
}

async function setState(id, state) {
  await api('/gtd/task/state', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({id, state})
  });
  await refresh();
}

async function delTask(id) {
  await api('/gtd/task/delete', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({id})
  });
  await refresh();
}

async function addInbox() {
  const text = document.getElementById('inboxText').value.trim();
  if (!text) return;
  await api('/gtd/inbox/add', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({text})
  });
  document.getElementById('inboxText').value = '';
  INBOX = await api('/gtd/inbox');
  renderInbox();
}

async function resolveInbox(id, action) {
  await api('/gtd/inbox/resolve', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({id, action})
  });
  INBOX = await api('/gtd/inbox');
  renderInbox();
}

async function openReport(kind) {
  const txt = await api('/gtd/report/' + encodeURIComponent(kind));
  document.getElementById('reportOut').value = txt;
}

refresh().catch(e => {
  document.getElementById('caps').textContent = String(e);
});
</script>
</body>
</html>
"""
    return HTMLResponse(html)
