import html
import json
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import requests

# Render environment variables (falls back to your defaults if not set in Render Dashboard)
BOT_TOKEN = os.getenv("BOT_TOKEN", "9210532213:AAGWUFB5KJAtB_r-0OBjrKGnTrfUqGjThRA").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "-1003969250236"))
PORT = int(os.getenv("PORT", "10000"))

_admin_env = os.getenv("ADMIN_CHAT_ID", "").strip()
ADMIN_CHAT_ID: int | None = int(_admin_env) if _admin_env else None

JSON_FILE = os.getenv("JSON_FILE", "arranged_by_id.json")
PROGRESS_FILE = os.getenv("PROGRESS_FILE", "progress.json")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "30"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "3"))       # Base backoff seconds for non-FloodWait errors
PACING_DELAY = float(os.getenv("PACING_DELAY", "0.6"))   # Gentle delay between successful uploads

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Ensure stdout flushes immediately in Render logs
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# Synchronization & runtime state
state_lock = threading.Lock()
pause_event = threading.Event()
pause_event.set()  # Set = Running/Active; Cleared = Paused
stop_event = threading.Event()

runtime_state = {
    "status": "starting",  # starting | running | paused | completed | error
    "current_pos": 0,
    "total_count": 0,
    "current_id": "-",
    "current_subject": "-",
    "current_topic": "-",
    "last_error": "",
    "notify_chat_id": ADMIN_CHAT_ID,
    "known_chats": set(),
}


class FloodWaitError(Exception):
    """Raised when Telegram rate-limits a request with a retry_after delay."""

    def __init__(self, retry_after: int, message: str = ""):
        self.retry_after = max(1, int(retry_after))
        super().__init__(message or f"FloodWait: retry after {self.retry_after}s")


def extract_retry_after(error_text: str, data: dict | None = None) -> int | None:
    """Extract Telegram's retry_after seconds from JSON parameters or error text."""
    if isinstance(data, dict):
        params = data.get("parameters")
        if isinstance(params, dict) and "retry_after" in params:
            try:
                return int(params["retry_after"])
            except (TypeError, ValueError):
                pass

    patterns = [
        r"retry after (\d+)",
        r"Too Many Requests: retry after (\d+)",
        r'"retry_after"\s*:\s*(\d+)',
    ]
    for pat in patterns:
        match = re.search(pat, error_text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def interruptible_sleep(seconds: float, step: float = 0.5) -> bool:
    """
    Sleep up to `seconds` while checking `stop_event`.
    Returns True if completed normally, False if interrupted by stop_event.
    """
    deadline = time.monotonic() + max(0.0, float(seconds))
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(step, remaining))
    return False


def wait_if_paused() -> bool:
    """
    Block while paused. Updates status to 'paused' and restores active status on resume.
    Returns False if stopping.
    """
    if pause_event.is_set():
        return True

    with state_lock:
        prev_status = runtime_state["status"]
        if prev_status != "completed":
            runtime_state["status"] = "paused"
    print("  [PAUSED] Uploader paused. Tap Resume in bot to continue.", flush=True)

    while not pause_event.is_set():
        if stop_event.is_set():
            return False
        time.sleep(0.3)

    with state_lock:
        if runtime_state["status"] == "paused":
            runtime_state["status"] = "running"
    print("  [RESUMED] Uploader resumed.", flush=True)
    return True


def wait_with_countdown(seconds: int, reason: str) -> None:
    """Sleep for `seconds` (plus a 1s safety buffer) while logging countdown updates."""
    total = max(1, int(seconds)) + 1
    print(f"  [FLOODWAIT] {reason} -> waiting {total}s...", flush=True)
    remaining = total
    while remaining > 0 and not stop_event.is_set():
        chunk = min(15, remaining)
        interruptible_sleep(chunk, step=0.5)
        remaining -= chunk
        if remaining > 0 and not stop_event.is_set():
            print(f"    ...still waiting {remaining}s for FloodWait", flush=True)


def to_sentence_case(text: str) -> str:
    """Convert text to regular sentence case (not all uppercase)."""
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return ""
    return cleaned[0].upper() + cleaned[1:].lower()


def load_json_safe(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return default
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            last_obj_end = content.rfind("}")
            if last_obj_end != -1:
                trimmed = content[: last_obj_end + 1].rstrip().rstrip(",") + "
]"
                return json.loads(trimmed)
            raise


def save_progress(progress_data: dict) -> None:
    """Atomically save progress so interrupted runs don't corrupt state."""
    with state_lock:
        parent_dir = os.path.dirname(os.path.abspath(PROGRESS_FILE))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        tmp_path = f"{PROGRESS_FILE}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(progress_data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, PROGRESS_FILE)


def tg_request(method: str, payload: dict | None = None, files: dict | None = None, timeout: int = 120) -> dict:
    url = f"{API_BASE}/{method}"
    try:
        if files:
            resp = requests.post(url, data=payload or {}, files=files, timeout=timeout)
        else:
            resp = requests.post(url, json=payload or {}, timeout=timeout)
    except requests.RequestException as exc:
        retry_after = extract_retry_after(str(exc))
        if retry_after is not None:
            raise FloodWaitError(retry_after, str(exc)) from exc
        raise

    data = {}
    try:
        data = resp.json()
    except ValueError:
        pass

    retry_after = extract_retry_after(resp.text or "", data if isinstance(data, dict) else None)
    if resp.status_code == 429 or retry_after is not None:
        raise FloodWaitError(retry_after or 10, f"HTTP {resp.status_code} in {method}: {resp.text}")

    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code} in {method}: {resp.text}")

    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {data}")

    return data["result"]


def call_with_floodwait_and_retries(
    action_fn,
    action_label: str,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_DELAY,
):
    """Run a Telegram API call, always honoring FloodWait timers and retrying transient errors."""
    attempt = 0
    while True:
        if stop_event.is_set():
            raise RuntimeError("Operation stopped.")
        try:
            return action_fn()
        except FloodWaitError as fw:
            wait_with_countdown(fw.retry_after, action_label)
        except Exception as exc:
            retry_after = extract_retry_after(str(exc))
            if retry_after is not None:
                wait_with_countdown(retry_after, action_label)
                continue

            attempt += 1
            if attempt >= max_retries:
                raise RuntimeError(f"{action_label} failed after {max_retries} attempts: {exc}") from exc

            sleep_for = base_delay * min(attempt, 10)
            print(f"  [RETRY {attempt}/{max_retries}] {action_label}: {exc} (sleeping {sleep_for}s)", flush=True)
            interruptible_sleep(sleep_for, step=0.5)


def get_or_create_subject_topic(subject: str, progress: dict, force_recreate: bool = False) -> int:
    """Create or retrieve a forum topic for `subject` and persist its message_thread_id."""
    clean_subject = (subject or "General").strip() or "General"
    subjects_map = progress.setdefault("subjects", {})

    if not force_recreate and clean_subject in subjects_map:
        cached_id = subjects_map[clean_subject].get("message_thread_id")
        if cached_id is not None:
            return int(cached_id)

    topic_name = clean_subject[:128]  # Telegram forum topic name limit is 128 chars

    def _create():
        return tg_request(
            "createForumTopic",
            {
                "chat_id": GROUP_ID,
                "name": topic_name,
            },
        )

    result = call_with_floodwait_and_retries(_create, f"createForumTopic('{clean_subject}')")
    thread_id = int(result["message_thread_id"])
    subjects_map[clean_subject] = {
        "subject": clean_subject,
        "message_thread_id": thread_id,
    }
    save_progress(progress)
    print(f"[TOPIC READY] Subject='{clean_subject}' -> thread_id={thread_id}", flush=True)
    return thread_id


def build_caption(subject: str, topic: str) -> str:
    subj_upper_bold = f"<b>{html.escape((subject or '').strip().upper())}</b>"
    topic_sentence = html.escape(to_sentence_case(topic))
    return (
        f"<blockquote>SUBJECT &gt; {subj_upper_bold}
"
        f"TOPIC &gt; {topic_sentence}</blockquote>"
    )


def send_item_to_topic(item: dict, subject: str, progress: dict) -> tuple[int, int]:
    """
    Send video/document directly into the subject's forum topic in serial order.
    If a cached thread_id returns 'message thread not found', recreates the topic once and retries.
    Returns (message_id, thread_id).
    """
    item_type = (item.get("type") or "").strip().lower()
    file_id = item["file_id"]
    caption = build_caption(subject, item.get("topic", ""))

    if item_type == "video":
        method = "sendVideo"
        media_field = "video"
    else:
        method = "sendDocument"
        media_field = "document"

    thread_id = get_or_create_subject_topic(subject, progress)
    recreated_once = False

    def _send():
        nonlocal thread_id, recreated_once
        payload = {
            "chat_id": GROUP_ID,
            "message_thread_id": thread_id,
            media_field: file_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        try:
            return tg_request(method, payload)
        except RuntimeError as err:
            if "message thread not found" in str(err).lower() and not recreated_once:
                recreated_once = True
                print(
                    f"  [INFO] Cached thread_id={thread_id} not found for '{subject}'; "
                    f"recreating forum topic...",
                    flush=True,
                )
                thread_id = get_or_create_subject_topic(subject, progress, force_recreate=True)
                retry_payload = {
                    "chat_id": GROUP_ID,
                    "message_thread_id": thread_id,
                    media_field: file_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                }
                return tg_request(method, retry_payload)
            raise

    msg = call_with_floodwait_and_retries(
        _send,
        f"upload id={item.get('id')} file={item.get('file_name')}",
    )
    return int(msg["message_id"]), thread_id


def make_unique_key(item: dict, index: int) -> str:
    """Unique key per record so duplicate IDs in JSON are still tracked without skipping."""
    return f"{item.get('id', 'unknown')}::{index}::{item.get('file_id', '')}"


def sort_key_for_item(pair: tuple[int, dict]):
    orig_idx, item = pair
    raw_id = str(item.get("id", "")).strip()
    if raw_id.isdigit():
        return (0, int(raw_id), orig_idx)
    try:
        return (0, float(raw_id), orig_idx)
    except ValueError:
        return (1, raw_id, orig_idx)


def build_control_keyboard() -> dict:
    """Build inline keyboard with Stats, Pause/Resume, and Progress File buttons."""
    is_paused = not pause_event.is_set()
    toggle_text = "▶ Resume" if is_paused else "⏸ Pause"
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Stats", "callback_data": "action_stats"},
                {"text": toggle_text, "callback_data": "action_toggle_pause"},
            ],
            [
                {"text": "📁 Progress File", "callback_data": "action_progress_file"},
            ],
        ]
    }


def get_stats_snapshot() -> dict:
    """Return structured live metrics for both Telegram Stats and HTTP health endpoint."""
    try:
        items = load_json_safe(JSON_FILE, [])
        total_items = len(items) if isinstance(items, list) else 0
    except Exception:
        total_items = 0

    try:
        prog = load_json_safe(PROGRESS_FILE, {})
        uploaded_count = len(prog.get("uploaded", [])) if isinstance(prog, dict) else 0
        completed_count = len(prog.get("completed_keys", [])) if isinstance(prog, dict) else 0
        subjects_count = len(prog.get("subjects", {})) if isinstance(prog, dict) else 0
        done_count = max(uploaded_count, completed_count)
    except Exception:
        done_count = 0
        subjects_count = 0

    if total_items > 0:
        done_count = min(done_count, total_items)
        remaining = max(0, total_items - done_count)
        pct = round((done_count / total_items) * 100.0, 2)
    else:
        remaining = 0
        pct = 0.0

    with state_lock:
        return {
            "status": runtime_state["status"],
            "completed": done_count,
            "total": total_items,
            "remaining": remaining,
            "percentage": pct,
            "subjects_count": subjects_count,
            "current_id": runtime_state["current_id"],
            "current_subject": runtime_state["current_subject"],
            "current_topic": runtime_state["current_topic"],
            "last_error": runtime_state["last_error"],
        }


def format_stats_message() -> str:
    """Build real-time progress stats message for Telegram."""
    snap = get_stats_snapshot()
    status_label = {
        "starting": "🔄 Starting",
        "running": "🟢 Running",
        "paused": "⏸ Paused",
        "completed": "✅ Completed",
        "error": "❌ Error",
    }.get(snap["status"], snap["status"])

    lines = [
        "<b>📊 Upload Progress Stats</b>",
        "<blockquote>",
        f"<b>Status:</b> {html.escape(status_label)}",
        f"<b>Completed:</b> {snap['completed']} / {snap['total']} ({snap['percentage']:.1f}%)",
        f"<b>Remaining:</b> {snap['remaining']}",
        f"<b>Forum Topics:</b> {snap['subjects_count']}",
        f"<b>Current ID:</b> {html.escape(str(snap['current_id']))}",
        f"<b>Current Subject:</b> {html.escape(str(snap['current_subject']))}",
        f"<b>Current Topic:</b> {html.escape(to_sentence_case(str(snap['current_topic'])) or '-')}",
        "</blockquote>",
    ]
    if snap["last_error"]:
        lines.append(f"<b>Last Error:</b> <code>{html.escape(snap['last_error'][:200])}</code>")

    return "
".join(lines)


def send_bot_message(chat_id: int, text: str, with_keyboard: bool = True) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if with_keyboard:
        payload["reply_markup"] = build_control_keyboard()
    try:
        tg_request("sendMessage", payload, timeout=30)
    except Exception as exc:
        print(f"  [WARN] Failed to send message to chat_id={chat_id}: {exc}", flush=True)


def edit_bot_message(chat_id: int, message_id: int, text: str, with_keyboard: bool = True) -> None:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if with_keyboard:
        payload["reply_markup"] = build_control_keyboard()
    try:
        tg_request("editMessageText", payload, timeout=30)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            print(f"  [WARN] Failed to edit message in chat_id={chat_id}: {exc}", flush=True)


def send_progress_document(chat_id: int, caption: str = "📁 <b>Progress File</b> (<code>progress.json</code>)") -> bool:
    """Send progress.json to `chat_id`."""
    if not os.path.exists(PROGRESS_FILE):
        send_bot_message(chat_id, "⚠️ <code>progress.json</code> does not exist yet.", with_keyboard=True)
        return False

    try:
        prog_data = load_json_safe(PROGRESS_FILE, {})
        save_progress(prog_data)
    except Exception:
        pass

    filename_only = os.path.basename(PROGRESS_FILE) or "progress.json"

    def _send_doc():
        with open(PROGRESS_FILE, "rb") as fh:
            return tg_request(
                "sendDocument",
                payload={
                    "chat_id": str(chat_id),
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                files={"document": (filename_only, fh, "application/json")},
                timeout=120,
            )

    try:
        call_with_floodwait_and_retries(_send_doc, f"sendDocument({filename_only}) to {chat_id}", max_retries=5)
        print(f"[PROGRESS SENT] Delivered {PROGRESS_FILE} to chat_id={chat_id}", flush=True)
        return True
    except Exception as exc:
        print(f"  [WARN] Failed to send {PROGRESS_FILE} to chat_id={chat_id}: {exc}", flush=True)
        return False


def get_target_delivery_chats() -> list[int]:
    """Determine which chat IDs should automatically receive progress.json on completion."""
    targets: list[int] = []
    with state_lock:
        if ADMIN_CHAT_ID is not None:
            targets.append(int(ADMIN_CHAT_ID))
        elif runtime_state["notify_chat_id"] is not None:
            targets.append(int(runtime_state["notify_chat_id"]))
        for cid in runtime_state["known_chats"]:
            if cid not in targets:
                targets.append(int(cid))
    return targets


def handle_callback_query(cq: dict) -> None:
    cq_id = cq.get("id")
    data = (cq.get("data") or "").strip()
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = msg.get("message_id")

    if chat_id is not None:
        with state_lock:
            runtime_state["known_chats"].add(int(chat_id))
            if runtime_state["notify_chat_id"] is None and ADMIN_CHAT_ID is None:
                runtime_state["notify_chat_id"] = int(chat_id)

    answer_text = "Done"
    if data == "action_stats":
        answer_text = "Refreshing stats..."
        stats_text = format_stats_message()
        if chat_id is not None and message_id is not None:
            edit_bot_message(int(chat_id), int(message_id), stats_text, with_keyboard=True)
        elif chat_id is not None:
            send_bot_message(int(chat_id), stats_text, with_keyboard=True)

    elif data == "action_toggle_pause":
        if pause_event.is_set():
            pause_event.clear()
            with state_lock:
                if runtime_state["status"] != "completed":
                    runtime_state["status"] = "paused"
            answer_text = "⏸ Paused"
        else:
            pause_event.set()
            with state_lock:
                if runtime_state["status"] != "completed":
                    runtime_state["status"] = "running"
            answer_text = "▶ Resumed"

        if chat_id is not None and message_id is not None:
            edit_bot_message(int(chat_id), int(message_id), format_stats_message(), with_keyboard=True)
        elif chat_id is not None:
            send_bot_message(int(chat_id), format_stats_message(), with_keyboard=True)

    elif data == "action_progress_file":
        answer_text = "Sending progress.json..."
        if chat_id is not None:
            send_progress_document(int(chat_id))

    if cq_id:
        try:
            tg_request("answerCallbackQuery", {"callback_query_id": cq_id, "text": answer_text}, timeout=15)
        except Exception:
            pass


def handle_message(msg: dict) -> None:
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    if chat_id is None:
        return

    with state_lock:
        runtime_state["known_chats"].add(int(chat_id))
        if runtime_state["notify_chat_id"] is None and ADMIN_CHAT_ID is None:
            runtime_state["notify_chat_id"] = int(chat_id)

    cmd = text.split()[0].lower() if text else ""
    if cmd.startswith("/start"):
        welcome = (
            "<b>🤖 Topic Uploader Control Panel</b>
"
            "Use the buttons below to monitor real-time progress, pause or resume uploads, "
            "or download <code>progress.json</code>."
        )
        send_bot_message(int(chat_id), welcome, with_keyboard=True)


def clear_webhook_if_any() -> None:
    """Ensure polling works cleanly on Render even if a webhook was previously attached."""
    try:
        tg_request("deleteWebhook", {"drop_pending_updates": False}, timeout=20)
    except Exception as exc:
        print(f"  [WARN] deleteWebhook check: {exc}", flush=True)


def polling_loop() -> None:
    """Background thread polling Telegram Bot API for /start and button callbacks."""
    clear_webhook_if_any()
    offset: int | None = None
    print("[BOT CONTROLLER] Listening for /start and button actions...", flush=True)
    while not stop_event.is_set():
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=35)
            if resp.status_code == 409:
                print("  [WARN] 409 Conflict in getUpdates (another instance may be stopping). Retrying in 5s...", flush=True)
                interruptible_sleep(5, step=0.5)
                continue
            if not resp.ok:
                interruptible_sleep(2, step=0.5)
                continue
            body = resp.json()
            if not isinstance(body, dict) or not body.get("ok"):
                interruptible_sleep(2, step=0.5)
                continue

            updates = body.get("result") or []
            for upd in updates:
                upd_id = upd.get("update_id")
                if upd_id is not None:
                    offset = int(upd_id) + 1

                if "message" in upd:
                    handle_message(upd["message"])
                elif "callback_query" in upd:
                    handle_callback_query(upd["callback_query"])

        except Exception as exc:
            if not stop_event.is_set():
                print(f"  [WARN] Polling error: {exc}", flush=True)
                interruptible_sleep(3, step=0.5)


class RenderHealthHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP server so Render Web Service health checks pass on $PORT."""

    def do_GET(self):
        if self.path in ("/", "/health", "/healthz", "/stats"):
            snap = get_stats_snapshot()
            body = json.dumps({"ok": True, **snap}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        # Suppress noisy HTTP health-check access logs in Render
        return


def start_health_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), RenderHealthHandler)
    t = threading.Thread(target=server.serve_forever, name="render-health-http", daemon=True)
    t.start()
    print(f"[RENDER HTTP] Health server listening on 0.0.0.0:{PORT}", flush=True)
    return server


def run_uploader() -> None:
    if not Path(JSON_FILE).exists():
        raise FileNotFoundError(f"Missing {JSON_FILE}")

    items = load_json_safe(JSON_FILE, [])
    if not isinstance(items, list):
        raise ValueError(f"{JSON_FILE} must contain a JSON array.")

    progress = load_json_safe(
        PROGRESS_FILE,
        {
            "subjects": {},
            "uploaded": [],
            "completed_keys": [],
        },
    )
    progress.setdefault("subjects", {})
    progress.setdefault("uploaded", [])
    progress.setdefault("completed_keys", [])
    save_progress(progress)

    completed_set = set(progress["completed_keys"])

    # Sort serially by id while preserving original order for ties
    indexed_items = sorted(enumerate(items), key=sort_key_for_item)
    total_count = len(indexed_items)

    with state_lock:
        runtime_state["total_count"] = total_count
        runtime_state["status"] = "running" if pause_event.is_set() else "paused"

    for pos, (orig_idx, item) in enumerate(indexed_items, start=1):
        if not wait_if_paused():
            return

        rec_key = make_unique_key(item, orig_idx)
        if rec_key in completed_set:
            continue

        item_id = str(item.get("id", ""))
        subject = str(item.get("subject", "General") or "General").strip() or "General"
        topic = str(item.get("topic", ""))

        with state_lock:
            runtime_state["current_pos"] = pos
            runtime_state["current_id"] = item_id
            runtime_state["current_subject"] = subject
            runtime_state["current_topic"] = topic
            runtime_state["status"] = "running" if pause_event.is_set() else "paused"

        message_id, thread_id = send_item_to_topic(item, subject, progress)

        record = {
            "id": item_id,
            "message_id": message_id,
            "subject": subject,
            "topic": topic,
        }
        progress["uploaded"].append(record)
        progress["completed_keys"].append(rec_key)
        completed_set.add(rec_key)
        save_progress(progress)

        print(
            f"[{pos}/{total_count}] UPLOADED id={item_id} | msg_id={message_id} | "
            f"thread_id={thread_id} | subject={subject} | topic={to_sentence_case(topic)}",
            flush=True,
        )

        if not wait_if_paused():
            return
        interruptible_sleep(PACING_DELAY, step=0.2)

    # Zero-skip verification check
    missing = [
        (orig_idx, item)
        for orig_idx, item in indexed_items
        if make_unique_key(item, orig_idx) not in completed_set
    ]
    if missing:
        raise RuntimeError(
            f"Verification failed: {len(missing)} item(s) still missing after run. "
            f"First missing id={missing[0][1].get('id')}."
        )

    with state_lock:
        runtime_state["status"] = "completed"
        runtime_state["current_id"] = "-"
        runtime_state["current_subject"] = "-"
        runtime_state["current_topic"] = "-"

    print(f"All {total_count} items uploaded serially in topics with zero skips.", flush=True)

    # Automatically send progress.json after all messages are sent
    targets = get_target_delivery_chats()
    if targets:
        for cid in targets:
            send_progress_document(
                cid,
                caption=(
                    f"✅ <b>All {total_count} items uploaded!</b>
"
                    f"Here is your final <code>{os.path.basename(PROGRESS_FILE)}</code> file."
                ),
            )
    else:
        print(
            f"[INFO] No user chat registered yet; {PROGRESS_FILE} is saved and can be fetched "
            f"anytime via the 'Progress File' button after sending /start.",
            flush=True,
        )


def handle_shutdown_signal(signum, frame):
    print(f"
[SIGNAL {signum}] Graceful shutdown requested...", flush=True)
    stop_event.set()


def main() -> None:
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    http_server = start_health_server()

    poller = threading.Thread(target=polling_loop, name="tg-poller", daemon=True)
    poller.start()

    try:
        run_uploader()
    except Exception as exc:
        with state_lock:
            runtime_state["status"] = "error"
            runtime_state["last_error"] = str(exc)
        print(f"[ERROR] {exc}", flush=True)

    # Keep Render service & bot buttons responsive until SIGTERM/SIGINT
    while not stop_event.is_set():
        time.sleep(1)

    http_server.shutdown()
    print("[EXIT] Shutdown complete.", flush=True)


if __name__ == "__main__":
    main()
