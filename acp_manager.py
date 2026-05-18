"""ACP subprocess manager. Spawns node ./node_modules/@agentclientprotocol/
claude-agent-acp/dist/index.js, speaks JSON-RPC 2.0 over stdio NDJSON.

One AcpSession per DB session. Idle timeout → close. Next prompt respawns + load."""
import asyncio, json, os, queue, subprocess, threading, time
from pathlib import Path
from claude_manager import SessionEventBus

ACP_MODES = ["default", "acceptEdits", "bypassPermissions", "plan"]

# ─── Normalize ACP update → frontend event ───────────────────

def _acp_to_event(update: dict) -> list[dict]:
    su = update.get("sessionUpdate", "")
    if su == "agent_message_chunk":
        c = update.get("content", {})
        return [{"type": "assistant_text", "text": c.get("text", "")}] if c.get("type") == "text" else []
    if su == "agent_thought_chunk":
        return [{"type": "thinking_text", "text": update.get("content", {}).get("text", "")}]
    if su == "tool_call":
        title = update.get("title", "unknown")
        inp = update.get("rawInput", {})
        if title.lower().replace("_", "") == "askuserquestion":
            return [{"type": "ask_user_question", "question": inp.get("question", ""), "input": inp}]
        return [{"type": "tool_use", "name": title, "input": inp}]
    if su == "tool_call_update":
        if update.get("status") != "completed":
            return []
        parts = []
        for item in update.get("content", []):
            if isinstance(item, dict):
                inner = item.get("content", {})
                if isinstance(inner, dict):
                    parts.append(inner.get("text", ""))
                elif isinstance(inner, str):
                    parts.append(inner)
        return [{"type": "tool_result", "data": "".join(parts)[:2000]}]
    return []


# ─── AcpProcess: subprocess + JSON-RPC ───────────────────────

class AcpProcess:
    def __init__(self, working_dir=None, env_vars=""):
        self.working_dir = working_dir
        self._p: subprocess.Popen | None = None
        self._stopped = False
        self._rid = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._on_msg: callable | None = None
        self._on_exit: callable | None = None
        self._extra_env = {}
        if env_vars:
            for line in env_vars.strip().split("\n"):
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    self._extra_env[k.strip()] = v.strip().strip('"').strip("'")

    @property
    def running(self): return self._p is not None and self._p.poll() is None

    def start(self):
        script = Path(__file__).parent / "node_modules" / "@agentclientprotocol" / "claude-agent-acp" / "dist" / "index.js"
        env = os.environ.copy()
        env.update(self._extra_env)
        claude_dir = os.path.expandvars(r"%USERPROFILE%\.local\bin")
        if os.path.isdir(claude_dir):
            env["PATH"] = claude_dir + (";" if os.name == "nt" else ":") + env.get("PATH", "")
        cwd = self.working_dir or os.getcwd()
        self._p = subprocess.Popen(["node", str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=cwd, env=env)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def set_on_message(self, cb): self._on_msg = cb
    def set_on_exit(self, cb): self._on_exit = cb

    def _read(self):
        try:
            for line in iter(self._p.stdout.readline, b''):
                if self._stopped: break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace").strip())
                except json.JSONDecodeError:
                    continue
                # Check "method" first: incoming requests from the agent carry
                # BOTH "id" AND "method" and must dispatch to _on_msg, not be
                # mistaken for response messages.
                if "method" in msg:
                    if self._on_msg:
                        self._on_msg(msg)
                elif "id" in msg and isinstance(msg["id"], int):
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "result" in msg:
                            fut.get_loop().call_soon_threadsafe(fut.set_result, msg["result"])
                        elif "error" in msg:
                            fut.get_loop().call_soon_threadsafe(fut.set_exception,
                                RuntimeError(msg["error"].get("message", "ACP error")))
        except Exception as e:
            print(f"[AcpProcess] read error: {e}")
        finally:
            if self._on_exit: self._on_exit()

    def _read_err(self):
        try:
            for line in iter(self._p.stderr.readline, b''):
                if self._stopped: break
                t = line.decode("utf-8", errors="replace").rstrip()
                if t: print(f"[AcpProcess:stderr] {t}")
        except Exception as e:
            print(f"[AcpProcess:stderr] read error: {type(e).__name__}: {e}")

    async def request(self, method, params=None, timeout=300):
        if not self.running: raise RuntimeError("ACP not running")
        self._rid += 1
        rid = self._rid
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params: msg["params"] = params
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        self._send(msg)
        return await asyncio.wait_for(fut, timeout=timeout)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params: msg["params"] = params
        self._send(msg)

    def respond(self, rid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        self._send(msg)

    def _send(self, msg):
        if self._p and self._p.stdin:
            try:
                self._p.stdin.write((json.dumps(msg) + "\n").encode())
                self._p.stdin.flush()
            except Exception as e:
                print(f"[AcpProcess] _send error: {type(e).__name__}: {e}")

    def stop(self):
        self._stopped = True
        if self._p and self._p.poll() is None:
            try:
                self._p.terminate()
                self._p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._p.kill()
            except Exception as e:
                print(f"[AcpProcess] stop terminate error: {type(e).__name__}: {e}")
        for fut in self._pending.values():
            if not fut.done():
                fut.get_loop().call_soon_threadsafe(fut.set_exception, RuntimeError("ACP stopped"))
        self._pending.clear()


# ─── AcpSession ──────────────────────────────────────────────

class AcpSession:
    def __init__(self, db_id, working_dir="", env_vars="", idle_timeout=1800, effort="max",
                 mode="bypassPermissions"):
        self.db_id = db_id
        self.working_dir = working_dir
        self.env_vars = env_vars
        self.idle_timeout = idle_timeout
        self._effort = effort
        self._needs_history_preamble: bool = False
        self._proc: AcpProcess | None = None
        self._sid: str | None = None  # ACP session id
        self._claude_sid: str | None = None  # Claude resume id
        self._bus: SessionEventBus | None = None
        self._mode = mode or "bypassPermissions"
        self._model = ""
        self._idle_task: asyncio.Task | None = None
        self._modes = list(ACP_MODES)
        self._pending_perms: dict[str, tuple[int, list]] = {}  # client_req_id -> (rpc_id, options)
        self._on_event: callable | None = None  # called for each event (DB persistence)
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def running(self): return self._proc is not None and self._proc.running
    @property
    def session_id(self): return self._sid
    @property
    def claude_session_id(self): return self._claude_sid
    def set_claude_session_id(self, s): self._claude_sid = s

    async def connect(self, resume=False):
        if self.running:
            return
        self._loop = asyncio.get_event_loop()
        self._proc = AcpProcess(self.working_dir, self.env_vars)
        self._proc.set_on_message(self._on_notification)
        self._proc.set_on_exit(self._on_exit)
        self._proc.start()
        await self._proc.request("initialize", {"protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}}}, timeout=60)
        cwd = self.working_dir or os.getcwd()
        # Resolve symlinks so the cwd key matches what ACP stores in its jslog paths.
        try:
            cwd = os.path.realpath(cwd)
        except Exception as e:
            print(f"[AcpSession {self.db_id}] connect realpath({cwd}) error: {type(e).__name__}: {e}")
        prior_sid = self._claude_sid
        path_taken = "session/new (fresh)"
        load_error = None
        if resume and self._claude_sid:
            try:
                r = await self._proc.request("session/load",
                    {"sessionId": self._claude_sid, "cwd": cwd, "mcpServers": [],
                     "_meta": {"claudeCode": {"options": {"effort": self._effort}}}}, timeout=30)
                path_taken = f"session/load (resume {self._claude_sid[:8]}...)"
            except Exception as e:
                load_error = str(e)
                resume = False
        if not resume:
            sdk_options = {"effort": self._effort}
            if self._claude_sid:
                sdk_options["resume"] = self._claude_sid
            params = {"cwd": cwd, "mcpServers": [],
                      "_meta": {"claudeCode": {"options": sdk_options}}}
            r = await self._proc.request("session/new", params, timeout=30)
            if load_error:
                path_taken = (f"session/load failed: {load_error} -> "
                              f"session/new (_meta.resume {self._claude_sid[:8] if self._claude_sid else '?'}...)")
            elif self._claude_sid:
                path_taken = f"session/new (_meta.resume {self._claude_sid[:8]}...)"
        new_sid = r.get("sessionId") or self._claude_sid
        self._sid = new_sid
        self._claude_sid = new_sid
        # Log the result so silent resume failures don't go unnoticed.
        if prior_sid and new_sid and prior_sid != new_sid:
            self._needs_history_preamble = True
            print(f"[AcpSession {self.db_id}] connect: {path_taken} -> "
                  f"sessionId changed {prior_sid[:8]}... -> {new_sid[:8]}... "
                  f"(context may not be carried — DB preamble will be injected)")
        else:
            self._needs_history_preamble = False
            sid_display = new_sid[:8] if new_sid else "?"
            print(f"[AcpSession {self.db_id}] connect: {path_taken} -> sessionId={sid_display}")
        await self.set_mode(self._mode)
        self._reset_idle()

    async def disconnect(self):
        self._cancel_idle()
        self._cancel_pending_permissions()
        sid = self._sid
        self._sid = None          # clear BEFORE await so send_prompt sees it's gone
        if self._proc and self._proc.running and sid:
            try: await self._proc.request("session/close", {"sessionId": sid}, timeout=5)
            except Exception as e:
                print(f"[AcpSession {self.db_id}] disconnect session/close error "
                      f"(subprocess being torn down anyway): {type(e).__name__}: {e}")
        if self._proc: self._proc.stop(); self._proc = None

    async def send_prompt(self, text) -> SessionEventBus:
        if self._bus is None:
            self._bus = SessionEventBus()
        bus = self._bus
        bus.set_event_loop(self._loop)
        bus.mark_started()
        self._cancel_idle()
        bus._task = asyncio.create_task(self._run(bus, text))
        return bus

    async def _run(self, bus, text):
        prompt_task = asyncio.create_task(self._proc.request(
            "session/prompt",
            {"sessionId": self._sid, "prompt": [{"type": "text", "text": text}]},
            timeout=600,
        ))
        async def _watch_proc():
            while True:
                await asyncio.sleep(2)
                if not (self._proc and self._proc.running):
                    return RuntimeError("ACP subprocess exited mid-prompt")
        watcher = asyncio.create_task(_watch_proc())
        try:
            done, _ = await asyncio.wait({prompt_task, watcher},
                return_when=asyncio.FIRST_COMPLETED)
            if watcher in done and prompt_task not in done:
                err = watcher.result()
                prompt_task.cancel()
                raise err if isinstance(err, BaseException) else RuntimeError(str(err))
            r = prompt_task.result()
            bus.publish({"type": "done", "summary": "Completed." if r.get("stopReason") == "end_turn"
                else f"Stopped: {r.get('stopReason', '')}", "has_denials": False,
                "data": {"stop_reason": r.get("stopReason"), "usage": r.get("usage", {})}})
        except Exception as e:
            print(f"[AcpSession {self.db_id}] _run error: {type(e).__name__}: {e}")
            bus.publish({"type": "error", "text": str(e)})
        finally:
            if not watcher.done():
                watcher.cancel()
            bus.mark_done()
            self._reset_idle()

    async def interrupt(self):
        self._cancel_pending_permissions()
        if self._proc and self._proc.running and self._sid:
            self._proc.notify("session/cancel", {"sessionId": self._sid})

    async def set_mode(self, m):
        self._mode = m
        if self._proc and self._proc.running and self._sid:
            try: await self._proc.request("session/set_mode", {"sessionId": self._sid, "modeId": m}, timeout=10)
            except Exception as e:
                print(f"[AcpSession {self.db_id}] set_mode({m}) error: {type(e).__name__}: {e}")

    async def set_model(self, m):
        self._model = m
        if self._proc and self._proc.running and self._sid:
            try: await self._proc.request("session/set_model", {"sessionId": self._sid, "modelId": m}, timeout=10)
            except Exception as e:
                print(f"[AcpSession {self.db_id}] set_model({m}) error: {type(e).__name__}: {e}")

    def set_effort(self, eff):
        self._effort = eff

    def consume_history_preamble(self) -> bool:
        v = self._needs_history_preamble
        self._needs_history_preamble = False
        return v

    def get_modes(self): return list(self._modes)

    # ─── Notification / request dispatch ──────────────────────

    def _on_notification(self, msg):
        method = msg.get("method", "")
        rpc_id = msg.get("id")
        params = msg.get("params", {}) or {}

        if method == "session/update":
            update = params.get("update", {})
            su = update.get("sessionUpdate", "")
            if su == "system_init":
                sid = update.get("session_id") or update.get("sessionId") or ""
                if sid: self._claude_sid = sid
                if self._bus: self._bus.publish({"type": "system_init", "session_id": sid})
                return
            if su == "config_option_update":
                for opt in update.get("configOptions", []):
                    if opt.get("category") == "mode":
                        self._modes = [m.get("value", m) if isinstance(m, dict) else m for m in opt.get("options", [])]
                return
            for evt in _acp_to_event(update):
                if self._bus: self._bus.publish(evt)
            return

        if method == "session/request_permission" and rpc_id is not None:
            tool_call = params.get("toolCall", {}) or {}
            options = params.get("options", []) or []
            client_req_id = f"perm-{rpc_id}"
            self._pending_perms[client_req_id] = (rpc_id, options)
            if self._bus:
                self._bus.publish({
                    "type": "permission_request",
                    "requestId": client_req_id,
                    "title": tool_call.get("title", "Tool"),
                    "kind": tool_call.get("kind", "execute"),
                    "rawInput": tool_call.get("rawInput", {}),
                    "options": options,
                })
            return

        if method == "fs/read_text_file":
            self._handle_fs_read(rpc_id, params)
            return

        if method == "fs/write_text_file":
            self._handle_fs_write(rpc_id, params)
            return

        # Unknown request: never let the agent hang on us.
        if rpc_id is not None and self._proc:
            self._proc.respond(
                rpc_id,
                error={"code": -32601,
                       "message": f"Method not handled: {method}"})

    # ─── Permission response ───────────────────────────────────

    async def confirm_permission(self, request_id: str, option_id: str):
        entry = self._pending_perms.pop(request_id, None)
        if not entry or not self._proc:
            return
        rpc_id, options = entry
        if option_id == "cancel":
            outcome = {"outcome": "cancelled"}
        else:
            real = None
            for opt in options:
                if opt.get("kind") == option_id or opt.get("optionId") == option_id:
                    real = opt.get("optionId"); break
            if real is None and options:
                real = options[0].get("optionId")
            outcome = {"outcome": "selected", "optionId": real} if real is not None else {"outcome": "cancelled"}
        self._proc.respond(rpc_id, {"outcome": outcome})

    def _cancel_pending_permissions(self):
        if not self._proc:
            self._pending_perms.clear()
            return
        for client_req_id, (rpc_id, opts) in list(self._pending_perms.items()):
            try:
                self._proc.respond(rpc_id, {"outcome": {"outcome": "cancelled"}})
            except Exception as e:
                print(f"[AcpSession {self.db_id}] cancel perm error: {type(e).__name__}: {e}")
        self._pending_perms.clear()

    # ─── FS handlers (sync — runs in _read thread) ─────────────

    def _handle_fs_read(self, msg_id, params):
        if not self._proc:
            return
        try:
            path = params.get("path", "")
            if not path or not os.path.isabs(path):
                self._proc.respond(msg_id, error={"code": -32000, "message": f"Invalid path: {path}"})
                return
            if "\x00" in path:
                self._proc.respond(msg_id, error={"code": -32000, "message": "NULL bytes in path"})
                return
            p = Path(path)
            if not p.exists():
                self._proc.respond(msg_id, error={"code": -32000, "message": f"File not found: {path}"})
                return
            if not p.is_file():
                self._proc.respond(msg_id, error={"code": -32000, "message": f"Not a file: {path}"})
                return
            size = p.stat().st_size
            if size > 10 * 1024 * 1024:
                self._proc.respond(msg_id, error={"code": -32000, "message": f"File too large ({size} bytes)"})
                return
            line_start = params.get("line")
            line_limit = params.get("limit")
            if line_start is not None or line_limit is not None:
                text = p.read_text("utf-8")
                lines = text.splitlines()
                start = max(0, (line_start or 1) - 1)
                end = len(lines) if line_limit is None else start + line_limit
                content = "\n".join(lines[start:end])
            else:
                content = p.read_text("utf-8")
            self._proc.respond(msg_id, {"content": content})
        except PermissionError:
            self._proc.respond(msg_id, error={"code": -32000, "message": f"Permission denied: {params.get('path', '')}"})
        except Exception as e:
            print(f"[AcpSession {self.db_id}] fs_read error: {type(e).__name__}: {e}")
            self._proc.respond(msg_id, error={"code": -32000, "message": str(e)})

    def _handle_fs_write(self, msg_id, params):
        if not self._proc:
            return
        try:
            path = params.get("path", "")
            content = params.get("content", "")
            if not path or not os.path.isabs(path):
                self._proc.respond(msg_id, error={"code": -32000, "message": f"Invalid path: {path}"})
                return
            if "\x00" in path:
                self._proc.respond(msg_id, error={"code": -32000, "message": "NULL bytes in path"})
                return
            if not isinstance(content, str):
                self._proc.respond(msg_id, error={"code": -32000, "message": "content must be a string"})
                return
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, "utf-8")
            self._proc.respond(msg_id, {})
        except PermissionError:
            self._proc.respond(msg_id, error={"code": -32000, "message": f"Permission denied: {params.get('path', '')}"})
        except Exception as e:
            print(f"[AcpSession {self.db_id}] fs_write error: {type(e).__name__}: {e}")
            self._proc.respond(msg_id, error={"code": -32000, "message": str(e)})

    def _on_exit(self):
        if self._bus and self._bus.running:
            self._bus.publish({"type": "error", "text": "ACP process exited"})
            self._bus.mark_done()

    def _reset_idle(self):
        self._cancel_idle()
        self._idle_task = asyncio.create_task(self._idle())

    def _cancel_idle(self):
        if self._idle_task: self._idle_task.cancel(); self._idle_task = None

    async def _idle(self):
        try:
            await asyncio.sleep(self.idle_timeout)
            print(f"[AcpSession {self.db_id}] idle timeout fired ({self.idle_timeout}s) - tearing down subprocess")
            await self.disconnect()
        except asyncio.CancelledError: pass


# ─── AcpManager: registry ────────────────────────────────────

class AcpManager:
    def __init__(self, idle_timeout=1800):
        self._sessions: dict[int, AcpSession] = {}
        self._buses: dict[int, SessionEventBus] = {}
        self._efforts: dict[int, str] = {}
        self.idle_timeout = idle_timeout

    def get_event_bus(self, db_id): return self._buses.get(db_id)
    def cleanup_event_bus(self, db_id):
        b = self._buses.pop(db_id, None)
        if b: b.mark_done()

    async def send_prompt(self, db_id, prompt, working_dir="", env_vars="",
                          claude_sid=None, history_preamble_fn=None, mode="bypassPermissions") -> SessionEventBus:
        s = self._sessions.get(db_id)
        effort = self._efforts.get(db_id, "max")
        if not s or not s.running or s.session_id is None:
            s = AcpSession(db_id, working_dir, env_vars, self.idle_timeout, effort=effort, mode=mode)
            if claude_sid: s.set_claude_session_id(claude_sid)
            self._sessions[db_id] = s
        else:
            s.set_effort(effort)
        if not s.running:
            await s.connect(resume=bool(claude_sid))
        # Inject DB history preamble if resume produced a new sessionId
        injected = 0
        if history_preamble_fn is not None and s.consume_history_preamble():
            try:
                preamble = await history_preamble_fn()
                if preamble:
                    prompt = preamble + "\n\n" + prompt
                    injected = len(preamble)
            except Exception as e:
                print(f"[AcpManager] history_preamble_fn failed for sid={db_id}: "
                      f"{type(e).__name__}: {e}")
        bus = await s.send_prompt(prompt)
        self._buses[db_id] = bus
        if injected:
            print(f"[AcpManager] injected DB preamble for sid={db_id} ({injected} chars)")
        return bus

    async def interrupt_session(self, db_id):
        s = self._sessions.get(db_id)
        if s: await s.interrupt(); self.cleanup_event_bus(db_id)

    async def close_session(self, db_id):
        s = self._sessions.pop(db_id, None)
        if s: await s.disconnect()
        self.cleanup_event_bus(db_id)

    async def set_mode(self, db_id, mode):
        s = self._sessions.get(db_id)
        if s: await s.set_mode(mode)

    async def set_model(self, db_id, model):
        s = self._sessions.get(db_id)
        if s: await s.set_model(model)

    async def confirm_permission(self, db_id, request_id, option_id):
        s = self._sessions.get(db_id)
        if s: await s.confirm_permission(request_id, option_id)

    def get_effort(self, db_id) -> str:
        return self._efforts.get(db_id, "max")

    def set_effort(self, db_id, eff):
        self._efforts[db_id] = eff
        s = self._sessions.get(db_id)
        if s: s.set_effort(eff)

    def consume_history_preamble_flag(self, db_id) -> bool:
        s = self._sessions.get(db_id)
        return s.consume_history_preamble() if s else False

    def get_session(self, db_id): return self._sessions.get(db_id)
