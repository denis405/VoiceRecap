import json
import time
from dataclasses import asdict, dataclass

import aiosqlite

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    status      TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    decided_at  INTEGER
);
CREATE TABLE IF NOT EXISTS transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                INTEGER NOT NULL,
    created_at             INTEGER NOT NULL,
    kind                   TEXT NOT NULL,
    status                 TEXT NOT NULL,
    duration_sec           INTEGER NOT NULL DEFAULT 0,
    file_size              INTEGER,
    transcript_chars       INTEGER NOT NULL DEFAULT 0,
    summary_chars          INTEGER NOT NULL DEFAULT 0,
    stt_seconds            REAL NOT NULL DEFAULT 0,
    stt_cost               REAL NOT NULL DEFAULT 0,
    llm_prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    llm_completion_tokens  INTEGER NOT NULL DEFAULT 0,
    llm_reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    llm_cost               REAL NOT NULL DEFAULT 0,
    stt_model              TEXT,
    llm_model              TEXT,
    processing_ms          INTEGER,
    transcript_id          INTEGER
);
CREATE INDEX IF NOT EXISTS requests_user_time ON requests (user_id, created_at);
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript_replies (
    transcript_id  INTEGER PRIMARY KEY,
    replies        TEXT NOT NULL  -- JSON: [[label, text], ...]
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id       INTEGER PRIMARY KEY,
    stt_language  TEXT
);
"""


@dataclass
class User:
    id: int
    username: str | None
    full_name: str | None
    status: str
    created_at: int
    decided_at: int | None


# request statuses
REQ_OK = "ok"
REQ_EMPTY = "empty"
REQ_ERROR = "error"
REQ_TOO_LONG = "too_long"
REQ_TOO_BIG = "too_big"


@dataclass
class Request:
    """One voice / video note the bot was asked to process; costs are USD as reported by OpenRouter."""

    user_id: int
    kind: str  # "voice" | "video_note"
    status: str = REQ_ERROR
    duration_sec: int = 0
    file_size: int | None = None
    transcript_chars: int = 0
    summary_chars: int = 0
    stt_seconds: float = 0.0
    stt_cost: float = 0.0
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_reasoning_tokens: int = 0
    llm_cost: float = 0.0
    stt_model: str | None = None
    llm_model: str | None = None
    processing_ms: int | None = None
    transcript_id: int | None = None


@dataclass
class Usage:
    requests: int
    users: int
    ok: int
    audio_sec: int  # successfully processed audio only
    max_duration_sec: int
    transcript_chars: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    stt_cost: float
    llm_cost: float
    last_at: int | None

    @property
    def cost(self) -> float:
        return self.stt_cost + self.llm_cost


USAGE_SELECT = """
    COUNT(*),
    COUNT(DISTINCT user_id),
    COALESCE(SUM(status = 'ok'), 0),
    COALESCE(SUM(CASE WHEN status = 'ok' THEN duration_sec END), 0),
    COALESCE(MAX(CASE WHEN status = 'ok' THEN duration_sec END), 0),
    COALESCE(SUM(transcript_chars), 0),
    COALESCE(SUM(llm_prompt_tokens), 0),
    COALESCE(SUM(llm_completion_tokens), 0),
    COALESCE(SUM(llm_reasoning_tokens), 0),
    COALESCE(SUM(stt_cost), 0),
    COALESCE(SUM(llm_cost), 0),
    MAX(created_at)
"""


def _where(since: int | None, user_id: int | None) -> tuple[str, list]:
    conds, params = [], []
    if since is not None:
        conds.append("created_at >= ?")
        params.append(since)
    if user_id is not None:
        conds.append("user_id = ?")
        params.append(user_id)
    return (f"WHERE {' AND '.join(conds)}" if conds else ""), params


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # users

    async def get_user(self, user_id: int) -> User | None:
        async with self.conn.execute(
            "SELECT id, username, full_name, status, created_at, decided_at FROM users WHERE id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        return User(*row) if row else None

    async def add_pending(self, user_id: int, username: str | None, full_name: str | None) -> User:
        now = int(time.time())
        await self.conn.execute(
            "INSERT OR IGNORE INTO users (id, username, full_name, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, full_name, PENDING, now),
        )
        await self.conn.commit()
        return await self.get_user(user_id)

    async def update_profile(self, user_id: int, username: str | None, full_name: str | None) -> None:
        await self.conn.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE id = ?",
            (username, full_name, user_id),
        )
        await self.conn.commit()

    async def set_status(self, user_id: int, status: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE users SET status = ?, decided_at = ? WHERE id = ?",
            (status, int(time.time()), user_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_users(self) -> list[User]:
        async with self.conn.execute(
            "SELECT id, username, full_name, status, created_at, decided_at FROM users ORDER BY created_at"
        ) as cur:
            return [User(*row) for row in await cur.fetchall()]

    # user settings (a separate table: the admin isn't in `users`)

    async def get_stt_language(self, user_id: int) -> str | None:
        """The user's choice ("auto" or an ISO-639-1 code); None if they never picked one."""
        async with self.conn.execute("SELECT stt_language FROM user_settings WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_stt_language(self, user_id: int, code: str) -> None:
        await self.conn.execute(
            "INSERT INTO user_settings (user_id, stt_language) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET stt_language = excluded.stt_language",
            (user_id, code),
        )
        await self.conn.commit()

    # transcripts

    async def save_transcript(self, user_id: int, text: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO transcripts (user_id, text, created_at) VALUES (?, ?, ?)",
            (user_id, text, int(time.time())),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_transcript(self, transcript_id: int, user_id: int) -> str | None:
        async with self.conn.execute(
            "SELECT text FROM transcripts WHERE id = ? AND user_id = ?",
            (transcript_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def save_replies(self, transcript_id: int, replies: list[tuple[str, str]]) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO transcript_replies (transcript_id, replies) VALUES (?, ?)",
            (transcript_id, json.dumps(replies, ensure_ascii=False)),
        )
        await self.conn.commit()

    async def get_replies(self, transcript_id: int, user_id: int) -> list[tuple[str, str]] | None:
        async with self.conn.execute(
            "SELECT r.replies FROM transcript_replies r JOIN transcripts t ON t.id = r.transcript_id "
            "WHERE r.transcript_id = ? AND t.user_id = ?",
            (transcript_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        return [tuple(r) for r in json.loads(row[0])] if row else None

    # usage stats

    async def add_request(self, req: Request) -> int:
        fields = asdict(req)
        fields["created_at"] = int(time.time())
        cur = await self.conn.execute(
            f"INSERT INTO requests ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
            tuple(fields.values()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def usage(self, since: int | None = None, user_id: int | None = None) -> Usage:
        where, params = _where(since, user_id)
        async with self.conn.execute(f"SELECT {USAGE_SELECT} FROM requests {where}", params) as cur:
            return Usage(*await cur.fetchone())

    async def usage_by_user(self, since: int | None = None) -> list[tuple[int, Usage]]:
        """Per-user usage, most expensive first."""
        where, params = _where(since, None)
        async with self.conn.execute(
            f"SELECT user_id, {USAGE_SELECT} FROM requests {where} "
            "GROUP BY user_id ORDER BY SUM(stt_cost) + SUM(llm_cost) DESC, COUNT(*) DESC",
            params,
        ) as cur:
            return [(row[0], Usage(*row[1:])) for row in await cur.fetchall()]

    async def export_requests(self) -> tuple[list[str], list[tuple]]:
        async with self.conn.execute(
            """SELECT r.id, datetime(r.created_at, 'unixepoch') AS created_utc, r.user_id,
                      u.username, u.full_name, r.kind, r.status, r.duration_sec, r.file_size,
                      r.transcript_chars, r.summary_chars, r.stt_seconds, r.stt_cost,
                      r.llm_prompt_tokens, r.llm_completion_tokens, r.llm_reasoning_tokens, r.llm_cost,
                      r.stt_cost + r.llm_cost AS total_cost, r.stt_model, r.llm_model, r.processing_ms
               FROM requests r LEFT JOIN users u ON u.id = r.user_id
               ORDER BY r.id"""
        ) as cur:
            rows = await cur.fetchall()
            return [d[0] for d in cur.description], rows

    # meta

    async def get_meta(self, key: str) -> str | None:
        async with self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()
