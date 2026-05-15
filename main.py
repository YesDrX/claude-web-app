import asyncio
import io
import json
import os
import tomllib
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, Form, Body, WebSocket, WebSocketDisconnect, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import database as db
from auth import (
    init_auth, get_auth, login_required,
    set_auth_cookie, clear_auth_cookie, COOKIE_NAME,
)
from claude_manager import ClaudeManager
from acp_manager import AcpManager
from models import (
    SessionCreate, SessionUpdate, TemplateCreate, TemplateUpdate, BulkAction,
)
from events import (
    REPLAY_START, HEARTBEAT, STATUS, PONG, USER_MSG,
    ASSISTANT_TEXT, THINKING_TEXT, TOOL_USE, TOOL_RESULT,
    ASK_USER_QUESTION, DONE, INTERRUPTED, ERROR, STDERR,
    SYSTEM_INIT, PERMISSION_REQUEST, CONFIRM_PERMISSION,
    MODE_UPDATE, MODEL_UPDATE, SET_MODE, SET_MODEL,
    EFFORT_UPDATE, SET_EFFORT,
    PROMPT, INTERRUPT, STOP, PING,
    CLIENT_ACTIONS, STREAM_EVENTS,
)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.toml"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Load config
with open(CONFIG_PATH, "rb") as f:
    config = tomllib.load(f)

# Initialize auth
auth_cfg = config["auth"]
init_auth(auth_cfg["username"], auth_cfg.get("password_hash", auth_cfg.get("password", "")))

# Claude config (utilities only)
claude_cfg = config.get("claude", {})
default_model = claude_cfg.get("default_model", "")
available_models = claude_cfg.get("models", ["", "claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"])

# ClaudeManager (read-only utilities)
claude_manager = ClaudeManager(config["claude"])

# ACP Manager (subprocess lifecycle)
acp_cfg = config.get("acp", {})
acp_manager = AcpManager(idle_timeout=acp_cfg.get("idle_timeout", 1800))

# Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _linkify(text: str) -> str:
    import re
    def replace(m):
        url = m.group(0)
        return f'<a href="{url}" target="_blank" rel="noopener" class="text-blue-400 hover:text-blue-300 underline">{url}</a>'
    return re.sub(r'https?://[^\s<>"{}|\\^`\[\]]+', replace, text)


templates.env.filters['linkify'] = _linkify


def template_context(request: Request, extra: dict = None) -> dict:
    ctx = {
        "request": request,
        "default_model": default_model,
        "models": available_models,
        "event_types": {k: v for k, v in vars().items() if k.isupper()},  # all EVENT_* constants
    }
    if extra:
        ctx.update(extra)
    return ctx


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    yield


app = FastAPI(lifespan=lifespan, title="Claude Code Web (ACP)")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── Auth Middleware ───────────────────────────────────────────────

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    auth = get_auth()
    return await auth.middleware(request, call_next)


# ─── Auth Routes ───────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    auth = get_auth()
    if username != auth.username or not auth.verify_password(password):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Invalid username or password."
        }, status_code=401)
    token = auth.create_session()
    resp = RedirectResponse(url="/", status_code=302)
    set_auth_cookie(resp, token)
    return resp


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        get_auth().revoke_token(token)
    resp = RedirectResponse(url="/login", status_code=302)
    clear_auth_cookie(resp)
    return resp


# ─── Dashboard / Session List ──────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, search: str = ""):
    database = await db.get_db()
    try:
        if search:
            rows = await db.execute_fetchall(database,
                "SELECT * FROM sessions WHERE title LIKE ? OR claude_session_id LIKE ? ORDER BY created_at DESC",
                (f"%{search}%", f"%{search}%")
            )
        else:
            rows = await db.execute_fetchall(database,
                "SELECT * FROM sessions ORDER BY created_at DESC"
            )
        sessions = rows

        template_rows = await db.execute_fetchall(database,
            "SELECT * FROM templates ORDER BY name ASC"
        )
        templates_list = template_rows
    finally:
        await database.close()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "sessions": sessions,
        "templates": templates_list,
        "default_model": default_model,
        "models": available_models,
        "search": search,
    })


# ─── Templates CRUD ────────────────────────────────────────────────

@app.get("/templates", response_class=HTMLResponse)
async def templates_page(request: Request):
    database = await db.get_db()
    try:
        rows = await db.execute_fetchall(database, "SELECT * FROM templates ORDER BY name ASC")
        templates_list = rows
    finally:
        await database.close()
    return templates.TemplateResponse("templates_manage.html", {
        "request": request,
        "templates": templates_list,
        "default_model": default_model,
        "models": available_models,
    })


@app.post("/api/templates")
async def create_template(tmpl: TemplateCreate):
    database = await db.get_db()
    try:
        cursor = await database.execute(
            "INSERT INTO templates (name, title, cwd, prompt, model, mode, command, env_vars) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tmpl.name, tmpl.title, tmpl.cwd, tmpl.prompt, tmpl.model, tmpl.mode, tmpl.command, tmpl.env_vars)
        )
        await database.commit()
        tid = cursor.lastrowid
    finally:
        await database.close()
    return JSONResponse({"id": tid, "status": "created"})


@app.put("/api/templates/{tid}")
async def update_template(tid: int, tmpl: TemplateUpdate):
    database = await db.get_db()
    try:
        updates = {}
        for field in ["name", "title", "cwd", "prompt", "model", "mode", "command", "env_vars"]:
            val = getattr(tmpl, field, None)
            if val is not None:
                updates[field] = val
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [tid]
            await database.execute(f"UPDATE templates SET {set_clause} WHERE id = ?", vals)
            await database.commit()
    finally:
        await database.close()
    return JSONResponse({"status": "updated"})


@app.delete("/api/templates/{tid}")
async def delete_template(tid: int):
    database = await db.get_db()
    try:
        await database.execute("DELETE FROM templates WHERE id = ?", (tid,))
        await database.commit()
    finally:
        await database.close()
    return JSONResponse({"status": "deleted"})


# ─── Sessions API ──────────────────────────────────────────────────

@app.post("/api/sessions")
async def create_session(sess: SessionCreate):
    import uuid
    cache_key = uuid.uuid4().hex
    database = await db.get_db()
    try:
        cursor = await database.execute(
            "INSERT INTO sessions (title, cwd, prompt, model, mode, command, env_vars, status, cache_key) VALUES (?, ?, ?, ?, ?, ?, ?, 'Idle', ?)",
            (sess.title, sess.cwd, sess.prompt, sess.model or default_model, sess.mode, sess.command, sess.env_vars, cache_key)
        )
        await database.commit()
        sid = cursor.lastrowid
    finally:
        await database.close()
    return JSONResponse({"id": sid, "status": "created"})


@app.post("/api/sessions/from-template/{tid}")
async def create_session_from_template(tid: int):
    import uuid
    cache_key = uuid.uuid4().hex
    database = await db.get_db()
    try:
        row = await db.execute_fetchall(database, "SELECT * FROM templates WHERE id = ?", (tid,))
        if not row:
            return JSONResponse({"error": "Template not found"}, status_code=404)
        tmpl = row[0]
        cursor = await database.execute(
            "INSERT INTO sessions (title, cwd, prompt, model, mode, command, env_vars, status, cache_key) VALUES (?, ?, ?, ?, ?, ?, ?, 'Idle', ?)",
            (tmpl["title"] or tmpl["name"], tmpl["cwd"], tmpl["prompt"], tmpl["model"] or default_model, tmpl.get("mode", "bypassPermissions"), tmpl.get("command", "claude"), tmpl.get("env_vars", ""), cache_key)
        )
        await database.commit()
        sid = cursor.lastrowid
    finally:
        await database.close()
    return JSONResponse({"id": sid, "status": "created"})


@app.get("/api/sessions/{sid}")
async def get_session(sid: int):
    database = await db.get_db()
    try:
        row = await db.execute_fetchall(database, "SELECT * FROM sessions WHERE id = ?", (sid,))
        if not row:
            return JSONResponse({"error": "Not found"}, status_code=404)
        session = row[0]
        msgs = await db.execute_fetchall(database,
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC, id ASC", (sid,)
        )
        session["messages"] = msgs
    finally:
        await database.close()
    return JSONResponse(session)


@app.put("/api/sessions/{sid}")
async def update_session(sid: int, sess: SessionUpdate):
    database = await db.get_db()
    try:
        updates = {}
        for field in ["title", "cwd", "prompt", "model", "mode", "status", "command", "env_vars"]:
            val = getattr(sess, field, None)
            if val is not None:
                updates[field] = val
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [sid]
            await database.execute(f"UPDATE sessions SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", vals)
            await database.commit()
    finally:
        await database.close()
    return JSONResponse({"status": "updated"})


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: int):
    database = await db.get_db()
    try:
        await database.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        await database.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        await database.commit()
    finally:
        await database.close()
    claude_manager.delete_session_events_from_disk(sid)
    return JSONResponse({"status": "deleted"})


@app.post("/api/sessions/bulk")
async def bulk_session_action(action: BulkAction):
    database = await db.get_db()
    try:
        for sid in action.session_ids:
            if action.action == "delete":
                await acp_manager.close_session(sid)
                await database.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                await database.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                claude_manager.delete_session_events_from_disk(sid)
            elif action.action == "interrupt":
                await acp_manager.interrupt_session(sid)
                await database.execute("UPDATE sessions SET status = 'Idle', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (sid,))
        await database.commit()
    finally:
        await database.close()
    return JSONResponse({"status": action.action + "d"})


# ─── Session Rename ─────────────────────────────────────────────────

@app.patch("/api/sessions/{sid}/rename")
async def rename_session(sid: int, body: dict = Body()):
    new_title = body.get("title", "")
    database = await db.get_db()
    try:
        await database.execute(
            "UPDATE sessions SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_title, sid)
        )
        await database.commit()
    finally:
        await database.close()
    return JSONResponse({"status": "renamed"})


# ─── Scan Existing Sessions ────────────────────────────────────────

@app.get("/api/scan-sessions")
async def scan_existing_sessions():
    existing = await claude_manager.scan_existing_sessions()
    database = await db.get_db()
    try:
        imported_rows = await db.execute_fetchall(database, "SELECT claude_session_id FROM sessions WHERE claude_session_id IS NOT NULL")
        imported_ids = {r["claude_session_id"] for r in imported_rows}
    finally:
        await database.close()
    unimported = [s for s in existing if s["claude_session_id"] not in imported_ids]
    return JSONResponse({"sessions": unimported, "total": len(unimported)})


@app.post("/api/import-sessions")
async def import_sessions(body: dict = Body()):
    sessions_data = body.get("sessions", [])
    database = await db.get_db()
    try:
        for s in sessions_data:
            await database.execute(
                "INSERT OR IGNORE INTO sessions (claude_session_id, title, cwd, model, mode, status) VALUES (?, ?, ?, ?, 'bypassPermissions', 'Idle')",
                (s["claude_session_id"], s.get("title", s["claude_session_id"][:8]), s.get("cwd", ""), s.get("model", default_model))
            )
        await database.commit()
    finally:
        await database.close()
    return JSONResponse({"status": "imported", "count": len(sessions_data)})


# ─── Settings / MCP / Skills / Plugins ─────────────────────────────

def _get_settings_data():
    return {
        "mcp_config": claude_manager.get_mcp_servers_from_settings(),
        "plugins": claude_manager.get_plugins(),
        "skills": claude_manager.get_skills(),
    }


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    data = _get_settings_data()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "mcp_config": data["mcp_config"],
        "plugins": data["plugins"],
        "skills": data["skills"],
    })


@app.get("/api/settings/refresh")
async def refresh_settings():
    return JSONResponse(_get_settings_data())


@app.get("/api/skills/{name}")
async def get_skill_content(name: str):
    content = claude_manager.get_skill_content(name)
    if content is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    return JSONResponse({"name": name, "content": content})


# ─── Chat View ─────────────────────────────────────────────────────

@app.get("/chat/{sid}", response_class=HTMLResponse)
async def chat_page(request: Request, sid: int):
    database = await db.get_db()
    try:
        row = await db.execute_fetchall(database, "SELECT * FROM sessions WHERE id = ?", (sid,))
        if not row:
            return RedirectResponse(url="/", status_code=302)
        session = row[0]
        msgs = await db.execute_fetchall(database,
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC, id ASC", (sid,)
        )
        grouped = []
        for m in msgs:
            m = dict(m)
            # Skip separator markers
            if m["role"] == "system" and m.get("content") == "__prompt__":
                continue
            # Reasoning messages: attach to preceding assistant
            if m["role"] == "assistant" and m.get("tool_name") == "_reasoning":
                if grouped and grouped[-1]["role"] == "assistant":
                    grouped[-1].setdefault("reasoning", []).append(m["content"])
                else:
                    m["reasoning"] = [m["content"]]
                    m["content"] = ""
                    m["tools"] = []
                    grouped.append(m)
            # Tool messages: attach to preceding assistant
            elif m["role"] == "tool" and grouped and grouped[-1]["role"] == "assistant":
                grouped[-1].setdefault("tools", []).append(m)
            else:
                m["tools"] = []
                grouped.append(m)
        session["messages"] = grouped
    finally:
        await database.close()
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "session": session,
        "default_model": default_model,
        "models": available_models,
        "acp_modes": ["default", "acceptEdits", "bypassPermissions", "plan"],
        "effort_levels": ["low", "medium", "high", "xhigh", "max"],
        "current_effort": acp_manager.get_effort(sid),
    })


# ─── File Upload for Chat ──────────────────────────────────────────

REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)


@app.post("/api/sessions/{sid}/upload")
async def upload_chat_files(sid: int, files: list[UploadFile] = File(...)):
    session_dir = REPORTS_DIR / "chat_files" / str(sid)
    session_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        if not f.filename:
            continue
        safe_name = Path(f.filename).name
        dest = session_dir / safe_name
        content = await f.read()
        dest.write_bytes(content)
        saved.append(str(dest))
    return JSONResponse({"files": saved, "count": len(saved)})


# ─── Debug: WS send logging ─────────────────────────────────────

import time as _time
_ws_send_log: dict[int, list[dict]] = {}  # sid -> list of {ts, type, seq, text_preview}

def _log_ws_send(sid: int, evt: dict):
    """Log every event sent to a WebSocket client for debugging."""
    t = evt.get("type", "?")
    seq = evt.get("seq", "?")
    preview = ""
    if t in ("user_msg", "assistant_text", "thinking_text"):
        preview = str(evt.get("text", ""))[:80]
    elif t == "tool_use":
        preview = f"name={evt.get('name', '?')}"
    elif t == "tool_result":
        preview = str(evt.get("data", ""))[:80]
    elif t == "replay_start":
        preview = f"count={evt.get('count', '?')} live={evt.get('live', '?')}"
    entry = {"ts": _time.time(), "type": t, "seq": seq, "preview": preview}
    if sid not in _ws_send_log:
        _ws_send_log[sid] = []
    _ws_send_log[sid].append(entry)
    # Keep last 500 per session
    if len(_ws_send_log[sid]) > 500:
        _ws_send_log[sid] = _ws_send_log[sid][-500:]
    print(f"[WS:{sid}] SEND -> {t} seq={seq} {preview}")

async def _ws_send(ws, sid: int, evt: dict):
    """Send JSON to WS client with logging."""
    _log_ws_send(sid, evt)
    await ws.send_json(evt)


# ─── WebSocket for Real-Time Chat ──────────────────────────────────

def _build_full_prompt(prompt_text: str, claude_sid: str | None, system_prompt: str) -> str:
    if claude_sid:
        return prompt_text
    if system_prompt.strip():
        return system_prompt.strip() + "\n\n---\n\n" + prompt_text
    return prompt_text


_PREAMBLE_MAX_CHARS = 16000
_PREAMBLE_MAX_TURNS = 20


async def _build_history_preamble(sid: int) -> str:
    """Synthesize a prior-conversation preamble from DB messages for the model
    when ACP resume produced a fresh sessionId (context not carried)."""
    import database as db
    database = await db.get_db()
    try:
        rows = await db.execute_fetchall(database,
            "SELECT role, content, tool_name FROM messages "
            "WHERE session_id = ? AND role IN ('user', 'assistant') "
            "ORDER BY id DESC LIMIT ?",
            (sid, _PREAMBLE_MAX_TURNS * 4))
    finally:
        await database.close()
    rows.reverse()  # oldest first
    lines = []
    total = 0
    for row in rows:
        if row["tool_name"] == "_reasoning":
            continue
        line = f"{row['role']}: {row['content']}"
        if total + len(line) > _PREAMBLE_MAX_CHARS:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return (
        "<prior-conversation>\n"
        "The following is a reconstructed history from the local database because\n"
        "the ACP backend did not carry forward the prior session context on resume.\n"
        "Use this to understand what has already been discussed and decided.\n\n"
        + "\n".join(lines) +
        "\n</prior-conversation>"
    )


# ─── Streaming: forward bus events to WebSocket ──────────────────

async def _stream_bus_to_ws(ws: WebSocket, sid: int, bus, my_queue: asyncio.Queue,
                           history: list[dict] | None = None):
    """Forward events from queue to WebSocket. Sends history first, then streams new events."""
    try:
        if history:
            for evt in history:
                await _ws_send(ws, sid, evt)
        while bus.running or not my_queue.empty():
            try:
                event = await asyncio.wait_for(my_queue.get(), timeout=0.3)
            except asyncio.TimeoutError:
                if not bus.running and my_queue.empty():
                    break
                continue
            await _ws_send(ws, sid, event)
    except WebSocketDisconnect:
        print(f"[WS:{sid}] Disconnected during streaming")
    finally:
        bus.unsubscribe(my_queue)


# ─── Consolidate chunked events in bus._history ─────────────────

def _consolidate_bus_history(history: list[dict], start_seq: int):
    """Mutate history in-place: collapse consecutive thinking_text/assistant_text
    events whose seq >= start_seq into single events keeping the first seq."""
    i = 0
    while i < len(history):
        evt = history[i]
        seq = evt.get("seq", 0)
        if seq < start_seq:
            i += 1
            continue
        t = evt.get("type", "")
        if t not in ("thinking_text", "assistant_text"):
            i += 1
            continue
        # Find consecutive same-type events and merge text
        base_seq = seq
        merged_text = evt.get("text", "") or ""
        j = i + 1
        while j < len(history) and history[j].get("type") == t:
            merged_text += history[j].get("text", "") or ""
            j += 1
        if j > i + 1:
            # Replace first event with consolidated, remove the rest
            evt["text"] = merged_text
            del history[i + 1:j]
        i += 1


# ─── Consolidated DB persistence (runs on prompt completion) ───

async def _update_session_claude_sid(sid: int, claude_sid: str):
    database = await db.get_db()
    try:
        await database.execute(
            "UPDATE sessions SET claude_session_id=? WHERE id=? AND (claude_session_id IS NULL OR claude_session_id!=?)",
            (claude_sid, sid, claude_sid))
        await database.commit()
    finally:
        await database.close()


async def _save_consolidated_prompt_to_db(sid: int, events: list[dict]):
    """Consolidate all events from one completed prompt into DB rows.
    thinking_text chunks → single row, assistant_text chunks → single row."""
    user_text = ""
    reasoning_text = ""
    assistant_text = ""
    tools: list[dict] = []  # {type, name, input, result}
    done_usage = None
    claude_sid = ""

    for evt in events:
        t = evt.get("type", "")
        if t == "user_msg":
            user_text = evt.get("text", "")
        elif t == "thinking_text":
            reasoning_text += evt.get("text", "")
        elif t == "assistant_text":
            assistant_text += evt.get("text", "")
        elif t == "tool_use":
            tools.append({"name": evt.get("name", ""), "input": evt.get("input", {}), "result": None})
        elif t == "tool_result":
            data = evt.get("data", "")
            result_str = data if isinstance(data, str) else json.dumps(data)
            if tools:
                tools[-1]["result"] = result_str[:2000]
        elif t == "done":
            done_usage = (evt.get("data") or {}).get("usage")
        elif t == "system_init":
            claude_sid = evt.get("session_id", "")

    if not user_text and not reasoning_text and not assistant_text and not tools:
        return

    database = await db.get_db()
    try:
        # Dedup: skip if this user_msg already exists
        if user_text:
            rows = await db.execute_fetchall(database,
                "SELECT id FROM messages WHERE session_id=? AND role='user' AND content=? ORDER BY id DESC LIMIT 1",
                (sid, user_text))
            if rows:
                return  # already persisted

        # Prompt separator
        await database.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, 'system', '__prompt__')",
            (sid,))

        # User message
        if user_text:
            await database.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)",
                (sid, user_text))

        # Reasoning (consolidated)
        if reasoning_text:
            await database.execute(
                "INSERT INTO messages (session_id, role, content, tool_name) VALUES (?, 'assistant', ?, '_reasoning')",
                (sid, reasoning_text))

        # Assistant text (consolidated)
        if assistant_text:
            await database.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                (sid, assistant_text))

        # Tool calls with results
        for tool in tools:
            await database.execute(
                "INSERT INTO messages (session_id, role, content, tool_name, tool_input, tool_result) VALUES (?, 'tool', ?, ?, ?, ?)",
                (sid, f"Tool: {tool['name']}", tool["name"],
                 json.dumps(tool["input"]), tool["result"] or ""))

        # Token usage
        if done_usage:
            tokens = done_usage.get("input_tokens", 0) + done_usage.get("output_tokens", 0)
            if tokens:
                await database.execute(
                    "UPDATE sessions SET tokens_used = tokens_used + ? WHERE id = ?",
                    (tokens, sid))

        # Claude session ID
        if claude_sid:
            await database.execute(
                "UPDATE sessions SET claude_session_id=? WHERE id=?",
                (claude_sid, sid))

        await database.commit()
    finally:
        await database.close()


async def _persist_prompt_on_done(sid: int, bus, prompt_start_seq: int):
    """Wait for bus to finish, then persist events to DB.

    Runs as a background task spawned right after send_prompt. Independent of
    any WebSocket connection - if the WS disconnects mid-prompt, this task
    keeps polling the bus and persists when the prompt completes."""
    try:
        await bus.until_done()
        bus.consolidate_history(lambda hist: _consolidate_bus_history(hist, prompt_start_seq))
        cur_events = bus.snapshot_events(from_seq=prompt_start_seq)
        if cur_events:
            await _save_consolidated_prompt_to_db(sid, cur_events)
        acp_s = acp_manager.get_session(sid)
        if acp_s and acp_s.session_id:
            await _update_session_claude_sid(sid, acp_s.session_id)
        await _update_session_status(sid, "Idle")
        print(f"[Persist:{sid}] prompt persisted (start_seq={prompt_start_seq})")
    except Exception as e:
        print(f"[Persist:{sid}] error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


# ─── Control message handler (shared by live stream + idle loop) ─

async def _handle_control(ws, sid, data, bus):
    """Handle a control message. Returns True if session was stopped."""
    t = data.get("type", "")
    if t == STOP:
        await acp_manager.close_session(sid)
        await _update_session_status(sid, "Idle")
        await _ws_send(ws, sid, {"type": INTERRUPTED, "text": "Session closed", "seq": 0})
        return True
    elif t == INTERRUPT:
        await acp_manager.interrupt_session(sid)
        return True
    elif t == CONFIRM_PERMISSION:
        await acp_manager.confirm_permission(sid, data.get("requestId", ""), data.get("optionId", "reject_once"))
    elif t == "answer_question":
        await acp_manager.confirm_permission(sid, data.get("requestId", ""), data.get("optionId", "reject_once"))
    elif t == SET_MODE:
        await acp_manager.set_mode(sid, data.get("mode", "bypassPermissions"))
        await _update_session_mode(sid, data.get("mode", "bypassPermissions"))
        if bus:
            bus.publish({"type": MODE_UPDATE, "mode": data.get("mode", "")}, transient=True)
    elif t == SET_MODEL:
        await acp_manager.set_model(sid, data.get("model", ""))
    elif t == SET_EFFORT:
        eff = data.get("effort", "max") or "max"
        acp_manager.set_effort(sid, eff)
        if bus:
            bus.publish({"type": EFFORT_UPDATE, "effort": eff}, transient=True)
    elif t == PING:
        await _ws_send(ws, sid, {"type": PONG})
    return False


# ─── Main WebSocket handler ──────────────────────────────────────

@app.websocket("/ws/{sid}")
async def websocket_chat(ws: WebSocket, sid: int):
    token = ws.cookies.get(COOKIE_NAME)
    auth = get_auth()
    if not token or not auth.validate_token(token):
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()
    from_seq = int(ws.query_params.get("from_seq", "0"))
    print(f"[WS:{sid}] Connected (from_seq={from_seq})")

    # Load session from DB
    database = await db.get_db()
    try:
        row = await db.execute_fetchall(database, "SELECT * FROM sessions WHERE id = ?", (sid,))
        if not row:
            await _ws_send(ws, sid, {"type": ERROR, "text": "Session not found"})
            await ws.close()
            return
        session = row[0]
    finally:
        await database.close()

    cache_key = session.get("cache_key", "")

    # Clear orphaned Working status (server restart during prompt)
    if session["status"] == "Working":
        await _update_session_status(sid, "Idle")

    my_queue = None
    stream_task = None
    bus = acp_manager.get_event_bus(sid)

    try:
        # ══════ PHASE 1: REPLAY ══════
        # Only replay from a running bus (in-flight prompt not yet in DB).
        # The Jinja-rendered page already has the full DB history in the DOM.
        last_replayed_seq = max(from_seq - 1, 0)
        if bus and bus.running and bus._history:
            replay_events = [e for e in bus._history if e.get("seq", 0) >= from_seq]
            if replay_events:
                await _ws_send(ws, sid, {"type": REPLAY_START, "count": len(replay_events), "live": True, "seq": 0})
                for evt in replay_events:
                    await _ws_send(ws, sid, evt)
                last_replayed_seq = max(e.get("seq", 0) for e in replay_events)
            else:
                await _ws_send(ws, sid, {"type": REPLAY_START, "count": 0, "live": False, "seq": 0})
        else:
            await _ws_send(ws, sid, {"type": REPLAY_START, "count": 0, "live": False, "seq": 0})

        # End replay with done marker so client enables input
        await _ws_send(ws, sid, {"type": DONE, "seq": 0})

        # ══════ PHASE 2: LIVE STREAM (if ACP is processing) ══════
        if bus and bus.running:
            print(f"[WS:{sid}] Joining live bus ({bus.history_length} buffered)")
            _history, my_queue = bus.subscribe(from_seq=last_replayed_seq + 1)
            stream_task = asyncio.create_task(
                _stream_bus_to_ws(ws, sid, bus, my_queue, history=_history)
            )
            while bus.running:
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                stopped = await _handle_control(ws, sid, data, bus)
                if stopped:
                    break
                if data.get("type") == INTERRUPT:
                    bus.publish({"type": INTERRUPTED, "text": "Interrupted by user"})
            if stream_task:
                try:
                    await asyncio.wait_for(stream_task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    stream_task.cancel()
            if my_queue:
                bus.unsubscribe(my_queue)
            my_queue = None
            return

        # ══════ PHASE 3: IDLE — wait for prompts ══════
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if data.get("type") == PROMPT:
                prompt_text = data.get("text", "")
                if not prompt_text.strip():
                    continue

                existing = acp_manager.get_event_bus(sid)
                if existing and existing.running:
                    await _ws_send(ws, sid, {"type": ERROR, "text": "Session is already processing a prompt"})
                    continue

                print(f"[WS:{sid}] Prompt: {prompt_text[:80]}")

                bus = await acp_manager.send_prompt(
                    db_id=sid,
                    prompt=_build_full_prompt(prompt_text, session.get("claude_session_id"), session.get("prompt", "")),
                    working_dir=session.get("cwd", ""),
                    env_vars=session.get("env_vars") or "",
                    claude_sid=session.get("claude_session_id"),
                    history_preamble_fn=lambda: _build_history_preamble(sid),
                )
                prompt_start_seq = bus.last_seq + 1
                bus.publish({"type": USER_MSG, "text": prompt_text})
                await _ws_send(ws, sid, {"type": STATUS, "text": "Thinking..."})

                # Spawn persistence task that survives WS disconnect.
                asyncio.create_task(_persist_prompt_on_done(sid, bus, prompt_start_seq))

                _history, my_queue = bus.subscribe(from_seq=bus.last_seq)
                stream_task = asyncio.create_task(
                    _stream_bus_to_ws(ws, sid, bus, my_queue, history=_history)
                )
                while bus.running:
                    try:
                        raw = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    stopped = await _handle_control(ws, sid, data, bus)
                    if stopped:
                        break
                    if data.get("type") == INTERRUPT:
                        bus.publish({"type": INTERRUPTED, "text": "Interrupted by user"})

                if stream_task:
                    try:
                        await asyncio.wait_for(stream_task, timeout=5)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        stream_task.cancel()

                my_queue = None

            else:
                await _handle_control(ws, sid, data, bus)

    except WebSocketDisconnect:
        print(f"[WS:{sid}] Disconnected")
    except Exception as exc:
        print(f"[WS:{sid}] Error: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        if my_queue and bus:
            bus.unsubscribe(my_queue)
        print(f"[WS:{sid}] Cleanup complete")




async def _update_session_status(sid: int, status: str):
    database = await db.get_db()
    try:
        await database.execute(
            "UPDATE sessions SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, sid)
        )
        await database.commit()
    finally:
        await database.close()


async def _update_session_mode(sid: int, mode: str):
    database = await db.get_db()
    try:
        await database.execute(
            "UPDATE sessions SET mode = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (mode, sid)
        )
        await database.commit()
    finally:
        await database.close()


# ─── Reports Space ─────────────────────────────────────────────────

import mimetypes
import markdown as md_lib

_allowed_raw = config.get("reports", {}).get("allowed_paths", [])
ALLOWED_REPORT_PATHS = [Path(p).expanduser().resolve() for p in _allowed_raw]


def _check_reports_path(target: Path) -> bool:
    r = target.resolve()
    s = str(r)
    root = str(REPORTS_DIR.resolve())
    if s.startswith(root):
        return True
    for allowed in ALLOWED_REPORT_PATHS:
        if s.startswith(str(allowed)) or s == str(allowed):
            return True
    return False


MIME_MAP = {
    ".html": "text/html", ".htm": "text/html",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/html; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".c": "text/plain; charset=utf-8", ".h": "text/plain; charset=utf-8",
    ".cpp": "text/plain; charset=utf-8",
    ".js": "text/plain; charset=utf-8", ".ts": "text/plain; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json", ".xml": "application/xml",
    ".csv": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8", ".yml": "text/plain; charset=utf-8",
    ".toml": "text/plain; charset=utf-8",
    ".sh": "text/plain; charset=utf-8", ".ps1": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".pdf": "application/pdf",
}


def _get_mime(path: str) -> str:
    ext = Path(path).suffix.lower()
    return MIME_MAP.get(ext, mimetypes.guess_type(path)[0] or "application/octet-stream")


def _is_text(mime: str) -> bool:
    return mime.startswith("text/") or mime in ("application/json", "application/xml")


@app.get("/reports", response_class=HTMLResponse)
async def reports_list(request: Request, path: str = ""):
    browse_dir = REPORTS_DIR
    breadcrumbs = [{"name": "reports", "path": ""}]
    if path:
        browse_dir = REPORTS_DIR / path
        if not _check_reports_path(browse_dir):
            return JSONResponse({"error": "not allowed"}, status_code=403)
        browse_dir = browse_dir.resolve()
        parts = path.replace("\\", "/").strip("/").split("/")
        for i, p in enumerate(parts):
            breadcrumbs.append({"name": p, "path": "/".join(parts[:i+1])})

    if not browse_dir.exists() or not browse_dir.is_dir():
        return JSONResponse({"error": "not found"}, status_code=404)

    files = []
    for entry in sorted(browse_dir.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        rel = (path + "/" + entry.name).replace("\\", "/") if path else entry.name.replace("\\", "/")
        if entry.is_dir():
            files.append({"name": entry.name + "/", "size": 0, "type": "dir", "url": "?path=" + rel, "rel": rel})
        else:
            try:
                st = entry.stat()
                files.append({
                    "name": entry.name, "size": st.st_size,
                    "type": "file", "url": "/reports/view/" + rel,
                    "mime": _get_mime(rel), "rel": rel,
                })
            except OSError:
                pass
    return templates.TemplateResponse("reports.html", {
        "request": request, "files": files, "breadcrumbs": breadcrumbs,
    })


@app.get("/reports/view/{file_path:path}")
async def serve_report(file_path: str):
    full = REPORTS_DIR / file_path
    if not _check_reports_path(full):
        return JSONResponse({"error": "not allowed"}, status_code=403)
    full = full.resolve()
    if not full.exists() or not full.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    mime = _get_mime(str(full))
    content = full.read_bytes()
    if full.suffix.lower() == ".md":
        try:
            text = content.decode("utf-8")
            html_body = md_lib.markdown(text, extensions=["fenced_code", "codehilite", "tables"])
            html_page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{file_path.split('/')[-1]}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; max-width: 900px; margin: 0 auto; padding: 2rem; background: #0f172a; color: #e2e8f0; line-height: 1.6; }}
a {{ color: #60a5fa; }} code {{ background: #1e293b; padding: .15em .4em; border-radius: .25em; color: #f472b6; }}
pre {{ background: #0f172a; border: 1px solid #334155; border-radius: .5rem; padding: 1rem; overflow-x: auto; }}
pre code {{ background: none; padding: 0; color: #e2e8f0; font-size: .85em; }}
</style>
</head>
<body>{html_body}</body>
</html>"""
            return Response(content=html_page, media_type="text/html; charset=utf-8")
        except UnicodeDecodeError:
            pass
    if _is_text(mime):
        try:
            text = content.decode("utf-8")
            return Response(content=text, media_type=mime)
        except UnicodeDecodeError:
            pass
    return Response(content=content, media_type=mime)


@app.delete("/api/reports/delete")
async def delete_report_item(body: dict = Body()):
    rel_path = body.get("path", "").replace("\\", "/").strip("/")
    if not rel_path:
        return JSONResponse({"error": "no path"}, status_code=400)
    target = REPORTS_DIR / rel_path
    if not _check_reports_path(target):
        return JSONResponse({"error": "not allowed"}, status_code=403)
    target = target.resolve()
    if not target.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        if target.is_dir():
            import shutil
            shutil.rmtree(target)
        else:
            target.unlink()
        return JSONResponse({"status": "deleted", "path": rel_path})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/reports/upload")
async def upload_to_reports(path: str = "", files: list[UploadFile] = File(...)):
    target_dir = REPORTS_DIR
    if path:
        target_dir = REPORTS_DIR / path
        if not _check_reports_path(target_dir):
            return JSONResponse({"error": "not allowed"}, status_code=403)
        target_dir = target_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        if not f.filename:
            continue
        safe_name = Path(f.filename).name
        dest = target_dir / safe_name
        content = await f.read()
        dest.write_bytes(content)
        saved.append(safe_name)
    return JSONResponse({"files": saved, "count": len(saved)})


@app.post("/api/reports/download")
async def download_reports(body: dict = Body()):
    paths = body.get("paths", [])
    if not paths:
        return JSONResponse({"error": "no paths"}, status_code=400)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in paths:
            rel = rel.replace("\\", "/").strip("/")
            item = REPORTS_DIR / rel
            if not _check_reports_path(item):
                continue
            item = item.resolve()
            if not item.exists():
                continue
            if item.is_dir():
                base = item.parent
                for f in item.rglob("*"):
                    if f.is_file():
                        zf.write(f, str(f.relative_to(base)))
            else:
                zf.write(item, item.name)
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=reports_download.zip"},
    )


@app.post("/api/reports/mkdir")
async def reports_mkdir(body: dict = Body()):
    parent = body.get("path", "")
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "no name"}, status_code=400)
    safe_name = Path(name).name
    target_dir = REPORTS_DIR
    if parent:
        target_dir = REPORTS_DIR / parent
        if not _check_reports_path(target_dir):
            return JSONResponse({"error": "not allowed"}, status_code=403)
        target_dir = target_dir.resolve()
    new_dir = target_dir / safe_name
    if new_dir.exists():
        return JSONResponse({"error": "already exists"}, status_code=409)
    new_dir.mkdir(parents=True)
    return JSONResponse({"status": "created", "name": safe_name})


@app.post("/api/reports/rename")
async def reports_rename(body: dict = Body()):
    rel_path = body.get("path", "").replace("\\", "/").strip("/")
    new_name = body.get("name", "").strip()
    if not rel_path or not new_name:
        return JSONResponse({"error": "path and name required"}, status_code=400)
    safe_name = Path(new_name).name
    target = REPORTS_DIR / rel_path
    if not _check_reports_path(target):
        return JSONResponse({"error": "not allowed"}, status_code=403)
    target = target.resolve()
    if not target.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    dest = target.parent / safe_name
    if dest.exists():
        return JSONResponse({"error": "already exists"}, status_code=409)
    target.rename(dest)
    return JSONResponse({"status": "renamed", "name": safe_name})


# ─── Run ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import uvicorn
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    svr = config["server"]
    uvicorn.run("main:app", host=svr["host"], port=svr["port"], reload=True)
