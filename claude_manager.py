"""Utility functions for reading Claude configuration, skills, MCP, plugins,
and scanning existing session history.  The ACP subprocess lifecycle lives in
acp_manager.py — this module is now read-only utilities only."""

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][0-9;]*\x07|\x1b\].*?\x1b\\')


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', text)


# ─── SessionEventBus (UI-facing, transport-agnostic) ──────────

class SessionEventBus:
    """Per-session event bus that buffers events for late-joining consumers.
    Supports multiple concurrent consumers (multiple tabs)."""

    def __init__(self):
        self._queues: list[asyncio.Queue] = []
        self._history: list[dict] = []
        self._running = False
        self._task: asyncio.Task | None = None
        self._started_at = time.time()
        self._seq = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._done_event: asyncio.Event | None = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def history_length(self) -> int:
        return len(self._history)

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    def subscribe(self, from_seq: int = 0) -> tuple[list[dict], asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue()
        self._queues.append(q)
        if from_seq > 0:
            return [e for e in self._history if e.get("seq", 0) >= from_seq], q
        return list(self._history), q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._queues:
            self._queues.remove(q)

    def publish(self, event: dict, transient: bool = False):
        if not transient:
            self._seq += 1
            event["seq"] = self._seq
            self._history.append(event)
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def snapshot_events(self, from_seq: int = 0) -> list[dict]:
        return [e for e in self._history if e.get("seq", 0) >= from_seq]

    def consolidate_history(self, fn):
        fn(self._history)

    def mark_started(self):
        self._running = True
        if self._done_event is None:
            self._done_event = asyncio.Event()
        self._done_event.clear()

    def mark_done(self):
        self._running = False
        if self._done_event is not None:
            self._done_event.set()

    async def until_done(self):
        if not self._running:
            return
        if self._done_event is not None:
            await self._done_event.wait()


# ─── ClaudeManager (utilities only) ───────────────────────────

class ClaudeManager:
    """Read-only utilities for Claude config, skills, MCP, plugins, and
    session scanning. Does NOT spawn subprocesses — that is AcpManager's job."""

    def __init__(self, config: dict):
        self.executable = config.get("executable_path", "claude")
        self.history_path = Path(config.get("history_path", "~/.claude/history.jsonl")).expanduser()
        self.history_paths: list[Path] = []
        raw_paths = config.get("history_paths", [])
        if raw_paths:
            for p in raw_paths:
                self.history_paths.append(Path(p).expanduser())
        else:
            self.history_paths.append(self.history_path)
        self.settings_path = Path(config.get("settings_path", "~/.claude/settings.json")).expanduser()
        self.local_settings_path = self.settings_path.parent / "settings.local.json"
        self.skills_dir = self.settings_path.parent / "skills"
        self.plugins_path = Path(config.get("plugins_path", "~/.claude/plugins/installed_plugins.json")).expanduser()
        self.sessions_dir = Path(config.get("sessions_dir", "~/.claude/sessions")).expanduser()
        self.default_model = config.get("default_model", "")
        self._mcp_cache: tuple[list[dict], float] | None = None

    # ─── Skills ──────────────────────────────────────────────

    def _iter_skill_files(self):
        if not self.skills_dir.exists():
            return
        seen = set()
        for md_file in sorted(self.skills_dir.glob("*.md")):
            name = md_file.stem
            if name in seen:
                continue
            seen.add(name)
            try:
                content = md_file.read_text("utf-8")
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        content = content[end + 3:].strip()
                yield name, content
            except Exception:
                pass
        for skill_md in sorted(self.skills_dir.glob("*/SKILL.md")):
            name = skill_md.parent.name
            if name in seen:
                continue
            seen.add(name)
            try:
                content = skill_md.read_text("utf-8")
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        content = content[end + 3:].strip()
                yield name, content
            except Exception:
                pass

    def get_custom_skills_prompt(self) -> str:
        parts = []
        for name, content in self._iter_skill_files():
            parts.append(f"<!-- SKILL: {name} -->\n{content}")
        if not parts:
            return ""
        return "\n\n".join(parts)

    def get_skills(self) -> list[dict]:
        skills_list = []
        seen = set()
        if self.skills_dir.exists():
            try:
                for md_file in sorted(self.skills_dir.glob("*.md")):
                    name = md_file.stem
                    if name in seen:
                        continue
                    seen.add(name)
                    description = self._parse_skill_frontmatter_description(md_file)
                    skills_list.append({
                        "name": name, "enabled": True,
                        "source": f"file: {md_file}", "description": description,
                    })
                for skill_md in sorted(self.skills_dir.glob("*/SKILL.md")):
                    name = skill_md.parent.name
                    if name in seen:
                        continue
                    seen.add(name)
                    description = self._parse_skill_frontmatter_description(skill_md)
                    skills_list.append({
                        "name": name, "enabled": True,
                        "source": f"file: {skill_md}", "description": description,
                    })
            except Exception:
                pass
        if self.settings_path.exists():
            try:
                data = self._read_json(self.settings_path)
                skills_cfg = data.get("skills", {})
                existing_names = {s["name"] for s in skills_list}
                for name, config in skills_cfg.items():
                    if name in existing_names:
                        continue
                    if isinstance(config, dict):
                        skills_list.append({
                            "name": name,
                            "enabled": config.get("enabled", True),
                            "source": config.get("source", "settings.json"),
                            "description": "",
                        })
                    else:
                        skills_list.append({
                            "name": name, "enabled": bool(config),
                            "source": "settings.json", "description": "",
                        })
            except Exception:
                pass
        return skills_list

    def get_skill_content(self, name: str) -> str | None:
        md_file = self._find_skill_file(name)
        if not md_file:
            return None
        try:
            content = md_file.read_text("utf-8")
            return self._strip_frontmatter(content)
        except Exception:
            return None

    def _find_skill_file(self, name: str) -> Path | None:
        if not self.skills_dir.exists():
            return None
        flat = self.skills_dir / f"{name}.md"
        if flat.exists():
            return flat
        dir_file = self.skills_dir / name / "SKILL.md"
        if dir_file.exists():
            return dir_file
        return None

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                return content[end + 3:].strip()
        return content

    @staticmethod
    def _parse_skill_frontmatter_description(md_file: Path) -> str:
        try:
            content = md_file.read_text("utf-8")
            if not content.startswith("---"):
                return ""
            end = content.find("---", 3)
            if end <= 0:
                return ""
            frontmatter = content[3:end]
            lines = frontmatter.split("\n")
            in_description = False
            desc_lines = []
            for line in lines:
                if in_description:
                    stripped = line.strip()
                    if stripped and not line.startswith(" ") and not line.startswith("\t"):
                        if ":" in stripped:
                            break
                    desc_lines.append(stripped)
                    continue
                stripped = line.strip()
                if ":" in stripped:
                    key, _, val = stripped.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if key == "description":
                        if val and val[0] in (">", "|"):
                            in_description = True
                            continue
                        else:
                            return val.strip().strip('"').strip("'")
            if desc_lines:
                return " ".join(desc_lines)
            return ""
        except Exception:
            return ""

    # ─── MCP ──────────────────────────────────────────────────

    MCP_DESCRIPTIONS = {
        "chrome-devtools": "Chrome DevTools Protocol",
        "better-playwright-mcp": "Playwright browser automation",
        "filesystem": "Filesystem access",
        "fetch": "Web fetching to markdown",
        "freebird": "Web search and fetch",
        "telegram": "Telegram bot messaging",
        "plugin:context-mode:context-mode": "Context mode plugin",
    }

    PLUGIN_DESCRIPTIONS = {
        "playwright@claude-plugins-official": "Browser automation",
        "frontend-design@claude-plugins-official": "Frontend design",
        "context-mode@context-mode": "Context window optimization",
        "telegram@claude-plugins-official": "Telegram messaging",
    }

    def _describe_mcp(self, name: str, command: str) -> str:
        if name in self.MCP_DESCRIPTIONS:
            return self.MCP_DESCRIPTIONS[name]
        name_lower = name.lower()
        if "playwright" in name_lower:
            return "Browser automation and testing via Playwright"
        if "fetch" in name_lower or "web" in name_lower:
            return "Web fetching and content extraction"
        if "search" in name_lower:
            return "Web search across multiple engines"
        if "file" in name_lower:
            return "Filesystem operations"
        if "telegram" in name_lower:
            return "Telegram messaging via bot"
        if "context" in name_lower:
            return "Context window and session management"
        if "freebird" in name_lower:
            return "Web search and content fetching"
        return f"MCP server: {command.split()[0] if command else name}"

    async def get_mcp_status(self, force: bool = False) -> list[dict]:
        now = time.time()
        if self._mcp_cache and (now - self._mcp_cache[1]) < 60:
            if not force or (now - self._mcp_cache[1]) < 10:
                return self._mcp_cache[0]
        try:
            proc = subprocess.Popen(
                [self.executable, "mcp", "list"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            stdout, stderr = proc.communicate(timeout=30)
            text = (stdout + stderr).decode("utf-8", errors="replace")
            text = strip_ansi(text)
            result = self._parse_mcp_output(text)
            self._mcp_cache = (result, now)
            return result
        except Exception as e:
            return [{"name": "Error", "command": str(e), "status": "error"}]

    def _parse_mcp_output(self, text: str) -> list[dict]:
        servers = []
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("Checking"):
                continue
            idx = line.rfind(" - ")
            if idx > 0:
                before = line[:idx].strip()
                status = line[idx + 3:].strip()
                colon_idx = before.find(": ")
                if colon_idx > 0:
                    name = before[:colon_idx].strip()
                    command = before[colon_idx + 2:].strip()
                else:
                    name = before
                    command = ""
                servers.append({"name": name, "command": command, "status": status})
            elif line:
                servers.append({"name": line, "command": "", "status": "unknown"})
        return servers

    def get_plugins(self) -> list[dict]:
        plugins = []
        if not self.plugins_path.exists():
            return plugins
        try:
            data = self._read_json(self.plugins_path)
            for key, entries in data.get("plugins", {}).items():
                for entry in entries:
                    plugins.append({
                        "name": key,
                        "scope": entry.get("scope", ""),
                        "version": entry.get("version", ""),
                        "install_path": entry.get("installPath", ""),
                        "installed_at": entry.get("installedAt", ""),
                        "description": self.PLUGIN_DESCRIPTIONS.get(key, "Claude Code plugin"),
                    })
        except Exception:
            pass
        return plugins

    def get_mcp_servers_from_settings(self) -> list[dict]:
        servers = []
        seen = set()
        paths = [
            self.settings_path.parent.parent / ".claude.json",
            self.settings_path,
            self.local_settings_path,
        ]
        for path in paths:
            if not path.exists():
                continue
            data = self._read_json(path)
            mcp_servers = data.get("mcpServers", {})
            for name, config in mcp_servers.items():
                if name in seen:
                    continue
                seen.add(name)
                command = config.get("command", "")
                servers.append({
                    "name": name,
                    "command": command,
                    "args": config.get("args", []),
                    "env": config.get("env", {}),
                    "enabled": config.get("enabled", True),
                    "description": self._describe_mcp(name, command),
                })
        return servers

    # ─── Session scanning ─────────────────────────────────────

    async def scan_existing_sessions(self) -> list[dict]:
        grouped: dict[str, dict] = {}
        for hp in self.history_paths:
            if not hp.exists():
                continue
            try:
                content = hp.read_text(encoding="utf-8")
                for line in content.strip().split("\n"):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        sid = entry.get("sessionId")
                        if not sid:
                            continue
                        if sid not in grouped:
                            grouped[sid] = {
                                "claude_session_id": sid,
                                "cwd": entry.get("project", ""),
                                "title": "",
                                "first_prompt": "",
                                "timestamp": entry.get("timestamp", 0),
                                "msg_count": 0,
                                "source_file": str(hp),
                            }
                        g = grouped[sid]
                        g["msg_count"] += 1
                        ts = entry.get("timestamp", 0)
                        if ts < g["timestamp"]:
                            g["timestamp"] = ts
                        display = entry.get("display", "").strip()
                        if not g["first_prompt"] and display and not display.startswith("/"):
                            g["first_prompt"] = display[:120]
                        if not g["title"] and display:
                            g["title"] = display[:80]
                    except json.JSONDecodeError:
                        continue
            except Exception:
                pass
        for g in grouped.values():
            if not g["title"]:
                g["title"] = g["claude_session_id"][:8]
            if not g["first_prompt"]:
                g["first_prompt"] = g["title"]
        return sorted(grouped.values(), key=lambda x: x["timestamp"], reverse=True)

    # ─── DB event synthesis ──────────────────────────────────

    async def get_session_events_from_db(self, db_session_id: int) -> list[dict] | None:
        from database import get_db as db_get_db, execute_fetchall
        database = await db_get_db()
        try:
            rows = await execute_fetchall(
                database,
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC, id ASC",
                (db_session_id,)
            )
        finally:
            await database.close()
        if not rows:
            return None
        events = []
        prev_role = None
        for row in rows:
            role = row["role"]
            if role == "user" and prev_role is not None:
                events.append({"type": "done", "summary": "Completed.", "has_denials": False})
            if role == "user":
                events.append({"type": "user_msg", "text": row["content"]})
            elif role == "assistant":
                events.append({"type": "assistant_text", "text": row["content"]})
            elif role == "tool":
                name = row.get("tool_name", "unknown")
                if name.lower().replace("_", "") == "askuserquestion":
                    try:
                        inp = json.loads(row["tool_input"]) if row.get("tool_input") else {}
                    except Exception:
                        inp = {}
                    events.append({
                        "type": "ask_user_question",
                        "question": inp.get("question", ""),
                        "input": inp,
                    })
                else:
                    try:
                        inp_dict = json.loads(row["tool_input"]) if row.get("tool_input") else {}
                    except Exception:
                        inp_dict = {}
                    events.append({
                        "type": "tool_use",
                        "name": name,
                        "input": inp_dict,
                    })
                if row.get("tool_result"):
                    events.append({"type": "tool_result", "data": row["tool_result"]})
            elif role == "system":
                events.append({"type": "system", "data": {"content": row["content"]}})
            prev_role = role
        if prev_role is not None:
            events.append({"type": "done", "summary": "Completed.", "has_denials": False})
        return events

    def delete_session_events_from_disk(self, db_session_id: int):
        """One-shot cleanup of legacy events cache files from older versions.
        Called from session-delete paths in main.py."""
        path = self.sessions_dir / f"{db_session_id}_events.jsonl"
        if path.exists():
            try:
                path.unlink()
            except Exception:
                pass

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            raw = path.read_bytes()
            if raw[:3] == b'\xef\xbb\xbf':
                raw = raw[3:]
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            print(f"[ClaudeManager] _read_json({path}): {type(e).__name__}: {e}")
            return {}
