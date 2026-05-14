import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "app.db")


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def execute_fetchall(db, query: str, params=None):
    if params:
        cursor = await db.execute(query, params)
    else:
        cursor = await db.execute(query)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def execute_fetchone(db, query: str, params=None):
    if params:
        cursor = await db.execute(query, params)
    else:
        cursor = await db.execute(query)
    row = await cursor.fetchone()
    return dict(row) if row else None


async def init_db():
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claude_session_id TEXT UNIQUE,
                title TEXT NOT NULL DEFAULT 'Untitled Session',
                cwd TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'bypassPermissions',
                status TEXT NOT NULL DEFAULT 'Idle',
                tokens_used INTEGER NOT NULL DEFAULT 0,
                command TEXT NOT NULL DEFAULT 'claude',
                env_vars TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                cwd TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'bypassPermissions',
                command TEXT NOT NULL DEFAULT 'claude',
                env_vars TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system', 'tool')),
                content TEXT NOT NULL DEFAULT '',
                tool_name TEXT,
                tool_input TEXT,
                tool_result TEXT,
                tokens_used INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
            CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
        """)
        await db.commit()

        # Migrations: add missing columns to existing tables
        for table, cols in [
            ("sessions", [("command", "'claude'"), ("env_vars", "''"), ("cache_key", "''")]),
            ("templates", [("command", "'claude'"), ("env_vars", "''")]),
        ]:
            for col, default in cols:
                try:
                    await db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
                    )
                    await db.commit()
                except Exception:
                    pass

        # Migration: rename permission_profile to mode if old column exists
        for table in ["sessions", "templates"]:
            try:
                await db.execute(f"ALTER TABLE {table} RENAME COLUMN permission_profile TO mode")
                await db.commit()
                print(f"[DB] Renamed permission_profile → mode in {table}")
            except Exception:
                pass
    finally:
        await db.close()
