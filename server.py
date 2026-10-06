import contextlib
import hashlib
import hmac
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

APP_NAME = "MJ Bridge"
DB_PATH = Path(os.environ.get("MJ_BRIDGE_DB", "mj_bridge.db"))
BRIDGE_KEY = os.environ.get("MJ_BRIDGE_KEY", "change-me-now")
# ChatGPT MCP uses a capability URL derived from the local bridge key.
# The key itself never appears in MCP tool arguments or ChatGPT conversation.
MCP_PATH_TOKEN = hashlib.sha256(BRIDGE_KEY.encode()).hexdigest()
MCP_PATH = f"/mcp/{MCP_PATH_TOKEN}"
MAX_MESSAGE_CHARS = int(os.environ.get("MJ_BRIDGE_MAX_MESSAGE_CHARS", "50000"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread TEXT NOT NULL DEFAULT 'main',
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_messages_recipient_id
                ON messages(recipient, id);
            CREATE INDEX IF NOT EXISTS idx_messages_thread_id
                ON messages(thread, id);

            CREATE TABLE IF NOT EXISTS shared_context (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def _key_ok(candidate: str) -> bool:
    if not isinstance(candidate, str):
        return False
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(),
        hashlib.sha256(BRIDGE_KEY.encode()).digest(),
    )


def _require_key(candidate: str) -> None:
    if not _key_ok(candidate):
        raise ValueError("Invalid bridge key")


def _clean_text(value: str, field: str, max_len: int = 200) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    if len(value) > max_len:
        raise ValueError(f"{field} is too long")
    return value


def _send(sender: str, recipient: str, message: str, thread: str = "main") -> dict[str, Any]:
    sender = _clean_text(sender, "sender")
    recipient = _clean_text(recipient, "recipient")
    thread = _clean_text(thread, "thread")
    message = (message or "").strip()
    if not message:
        raise ValueError("message is required")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"message exceeds {MAX_MESSAGE_CHARS} characters")
    created_at = now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages(thread, sender, recipient, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (thread, sender, recipient, message, created_at),
        )
        message_id = int(cur.lastrowid)
    return {
        "ok": True,
        "id": message_id,
        "thread": thread,
        "sender": sender,
        "recipient": recipient,
        "created_at": created_at,
    }


def _inbox(agent: str, after_id: int = 0, thread: str = "main", limit: int = 50) -> list[dict[str, Any]]:
    agent = _clean_text(agent, "agent")
    thread = _clean_text(thread, "thread")
    after_id = max(0, int(after_id))
    limit = min(max(1, int(limit)), 100)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, thread, sender, recipient, body, created_at, read_at
            FROM messages
            WHERE recipient = ? AND thread = ? AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (agent, thread, after_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _ack(agent: str, message_id: int) -> dict[str, Any]:
    agent = _clean_text(agent, "agent")
    message_id = int(message_id)
    read_at = now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE messages SET read_at = COALESCE(read_at, ?) WHERE id = ? AND recipient = ?",
            (read_at, message_id, agent),
        )
    return {"ok": cur.rowcount == 1, "message_id": message_id, "read_at": read_at if cur.rowcount else None}


def _set_context(key: str, value: str, updated_by: str) -> dict[str, Any]:
    key = _clean_text(key, "key", 300)
    updated_by = _clean_text(updated_by, "updated_by")
    value = (value or "").strip()
    if not value:
        raise ValueError("value is required")
    if len(value) > MAX_MESSAGE_CHARS:
        raise ValueError(f"value exceeds {MAX_MESSAGE_CHARS} characters")
    updated_at = now_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO shared_context(key, value, updated_by, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (key, value, updated_by, updated_at),
        )
    return {"ok": True, "key": key, "updated_by": updated_by, "updated_at": updated_at}


def _get_context(key: str | None = None) -> dict[str, Any]:
    with _connect() as conn:
        if key:
            row = conn.execute(
                "SELECT key, value, updated_by, updated_at FROM shared_context WHERE key = ?",
                (key,),
            ).fetchone()
            return {"item": dict(row) if row else None}
        rows = conn.execute(
            "SELECT key, value, updated_by, updated_at FROM shared_context ORDER BY key"
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


init_db()

mcp = MCPServer(
    APP_NAME,
    instructions=(
        "A private shared mailbox/context bridge for MJ. "
        "Use bridge_send to hand work to another agent, bridge_inbox to read replies, "
        "and shared context tools to keep both agents synchronized. Never reveal the bridge key."
    ),
)


@mcp.tool()
def bridge_send(
    sender: str,
    recipient: str,
    message: str,
    thread: str = "main",
) -> dict[str, Any]:
    """Send a message to another agent through MJ Bridge. Write action."""
    return _send(sender, recipient, message, thread)


@mcp.tool()
def bridge_inbox(
    agent: str,
    after_id: int = 0,
    thread: str = "main",
    limit: int = 50,
) -> dict[str, Any]:
    """Read messages addressed to an agent. Read-only action."""
    return {"messages": _inbox(agent, after_id, thread, limit)}


@mcp.tool()
def bridge_ack(agent: str, message_id: int) -> dict[str, Any]:
    """Mark a received message as read. Write action."""
    return _ack(agent, message_id)


@mcp.tool()
def bridge_set_context(
    key: str,
    value: str,
    updated_by: str,
) -> dict[str, Any]:
    """Create or update a shared project-context item. Write action."""
    return _set_context(key, value, updated_by)


@mcp.tool()
def bridge_get_context(key: str = "") -> dict[str, Any]:
    """Read one shared context item by key, or all items when key is blank. Read-only action."""
    return _get_context(key.strip() or None)


@mcp.tool()
def bridge_snapshot(
    agent: str,
    after_id: int = 0,
    thread: str = "main",
) -> dict[str, Any]:
    """Read new messages plus all shared context in one call. Read-only action."""
    return {
        "messages": _inbox(agent, after_id, thread, 100),
        "context": _get_context().get("items", []),
    }


def _bearer_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return ""
    return auth.split(" ", 1)[1].strip()


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": APP_NAME})


async def api_send(request: Request) -> JSONResponse:
    if not _key_ok(_bearer_key(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        result = _send(
            payload.get("sender", "Muse"),
            payload.get("recipient", "ChatGPT"),
            payload.get("message", ""),
            payload.get("thread", "main"),
        )
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def api_inbox(request: Request) -> JSONResponse:
    if not _key_ok(_bearer_key(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        q = request.query_params
        return JSONResponse({
            "messages": _inbox(
                q.get("agent", "Muse"),
                int(q.get("after_id", "0")),
                q.get("thread", "main"),
                int(q.get("limit", "50")),
            )
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def api_context(request: Request) -> JSONResponse:
    if not _key_ok(_bearer_key(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        if request.method == "GET":
            key = request.query_params.get("key", "").strip()
            return JSONResponse(_get_context(key or None))
        payload = await request.json()
        return JSONResponse(_set_context(
            payload.get("key", ""),
            payload.get("value", ""),
            payload.get("updated_by", "Muse"),
        ))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# Register plain HTTP fallback endpoints on the MCP server.
# These handlers enforce the bridge key themselves (except /health).
mcp.custom_route("/health", methods=["GET"])(health)
mcp.custom_route("/api/send", methods=["POST"])(api_send)
mcp.custom_route("/api/inbox", methods=["GET"])(api_inbox)
mcp.custom_route("/api/context", methods=["GET", "POST"])(api_context)

# Host 0.0.0.0 avoids localhost-only DNS-rebinding defaults in the SDK.
# Put this service behind HTTPS when deployed publicly.
app = mcp.streamable_http_app(
    streamable_http_path=MCP_PATH,
    host="0.0.0.0",
    stateless_http=True,
    json_response=True,
)

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    print(f"MJ Bridge MCP path: {MCP_PATH}")
    print("Keep this path private; do not paste it into chat. Add it directly to the ChatGPT custom MCP URL.")
    uvicorn.run(app, host="0.0.0.0", port=port)
