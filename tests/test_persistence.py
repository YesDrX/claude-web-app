"""Smoke tests for SessionEventBus persistence path (watchdog + DB writes)."""
import asyncio
import os
import tempfile

import pytest

import database as db_module
from claude_manager import SessionEventBus


@pytest.fixture
def temp_db(monkeypatch):
    """Monkeypatch database.DB_PATH to a temp file so tests never touch app.db."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(db_module, "DB_PATH", path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def initialized_db(temp_db):
    """Create a stub session row so FK on messages.session_id is satisfied."""
    async def _init():
        await db_module.init_db()
        db = await db_module.get_db()
        try:
            cursor = await db.execute(
                "INSERT INTO sessions (title, cwd, prompt, model, mode, status) "
                "VALUES ('test', '', '', '', 'bypassPermissions', 'Idle')"
            )
            await db.commit()
            return cursor.lastrowid
        finally:
            await db.close()
    return asyncio.run(_init())


def test_until_done_does_not_block_when_already_done():
    """Call mark_started + mark_done, then until_done() must not hang."""
    async def scenario():
        bus = SessionEventBus()
        bus.mark_started()
        bus.mark_done()
        await asyncio.wait_for(bus.until_done(), timeout=1.0)
    asyncio.run(scenario())


def test_until_done_wakes_on_mark_done():
    """Call mark_started, schedule a delayed mark_done, until_done must wake."""
    async def scenario():
        bus = SessionEventBus()
        bus.mark_started()
        async def delayed():
            await asyncio.sleep(0.1)
            bus.mark_done()
        asyncio.create_task(delayed())
        await asyncio.wait_for(bus.until_done(), timeout=2.0)
    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_persistence_writes_user_and_assistant(initialized_db):
    """Publish user + assistant chunks, run persistence, verify DB rows."""
    sid = initialized_db
    import main as main_module
    bus = SessionEventBus()
    bus.mark_started()
    bus.publish({"type": "user_msg", "text": "hello"})
    bus.publish({"type": "assistant_text", "text": "Hi "})
    bus.publish({"type": "assistant_text", "text": "there"})
    bus.mark_done()

    await main_module._persist_prompt_on_done(sid, bus, prompt_start_seq=1)

    db = await db_module.get_db()
    try:
        rows = await db_module.execute_fetchall(
            db,
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id",
            (sid,)
        )
    finally:
        await db.close()

    user_rows = [r for r in rows if r["role"] == "user"]
    assistant_rows = [r for r in rows if r["role"] == "assistant"]
    assert len(user_rows) >= 1
    assert len(assistant_rows) == 1
    assert "Hi there" in assistant_rows[0]["content"]


@pytest.mark.asyncio
async def test_persistence_writes_tool_calls(initialized_db):
    """Publish tool_use + tool_result, verify tool row in DB."""
    sid = initialized_db
    import main as main_module
    bus = SessionEventBus()
    bus.mark_started()
    bus.publish({"type": "user_msg", "text": "edit foo"})
    bus.publish({"type": "tool_use", "name": "Edit", "input": {"path": "foo.txt"}})
    bus.publish({"type": "tool_result", "data": "edited"})
    bus.mark_done()

    await main_module._persist_prompt_on_done(sid, bus, prompt_start_seq=1)

    db = await db_module.get_db()
    try:
        rows = await db_module.execute_fetchall(
            db,
            "SELECT role, tool_name, tool_result FROM messages WHERE session_id = ? ORDER BY id",
            (sid,)
        )
    finally:
        await db.close()

    tool_rows = [r for r in rows if r["role"] == "tool"]
    assert len(tool_rows) >= 1
    assert any(r["tool_name"] == "Edit" and "edited" in (r["tool_result"] or "")
               for r in tool_rows)
