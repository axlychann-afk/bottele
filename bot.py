import os
import random
import sqlite3
import threading
import time
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# hardcoded (repo public, jangan share repo kalau token belum di-rotate)
BOT_TOKEN = "8768540984:AAGWX9yJQwaZExHKH0dCSRwKQpB4cK9tJxo"
NINE_API_KEY = "sk-a060f5243df9ce04-dr5klf-b32b06b6"
NINE_BASE = os.environ.get("NINE_BASE", "https://trustworthy-solace-production-068d.up.railway.app")
SYSTEM = os.environ.get("SYSTEM", "Kamu adalah axly ai, asisten singkat dan helpful. Jawab dalam bahasa yang dipakai user.")

# pool: rotate + fallback. urut dari yg paling cepet (Sept 2026 sample).
# tier-1 <2s, tier-2 2-3s, tier-3 >3s cuma dipake kalau tier awal gagal semua.
# CATATAN: 5/*-free = quota habis (balas "Sorry ... can only try 10 times"), jangan masuk.
TIER1 = [
    "gemini/gemini-3.5-flash-lite",
    "openrouter/minimax/minimax-m3:free",
    "ps/poolside/laguna-xs-2.1",
    "ollama/gpt-oss:120b",
    "3/free/glm-5.3-flash",
    "3/free/gemini-3.7-flash",
    "bzl/auto:free",
    "bzl/qwen3.7-flash",
]
TIER2 = [
    "4/tencent/hy3-free",
    "4/orcarouter/free",
    "7/qwen3.8-flash",
    "4/qwen/qwen3.8-27b-free",
    "4/deepseek/deepseek-v4-flash-free",
    "7/hy3",
    "7/deepseek-v4-flash",
    "3/free/deepseek-v4-flash-0731",
]
TIER3 = [
    "3/free/gemini-3.1-pro",
    "gcli/grok-4.5-high",
    "6/z-ai/glm-5.3-free",
    "2/minimax-m3-free",
]
POOL = TIER1 + TIER2 + TIER3

QUOTA_PHRASES = ("can only try", "free quota", "topup", "recharged")

# skip codes that artinya quota/akun, bukan transient. sisanya retry.
SKIP_STATUS = {401, 403}

MAX_HISTORY = 50
MAX_TEXT_FILE = 18_000_000  # 18MB per text file (samain dg Telegram limit)
MAX_FILE_DOWNLOAD = 19_000_000  # 19MB cap (Telegram Bot API limit 20MB)
MAX_ZIP_ENTRIES = 50  # max file di-extract dari zip
MAX_ZIP_ENTRY_SIZE = 2_000_000  # 2MB per entry di dalam zip

DB_PATH = os.environ.get("DB_PATH", "axly.db")
DB_LOCK = threading.Lock()


def db_init():
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                first_seen INTEGER,
                last_seen INTEGER
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id, id);
        """)


def db_upsert_user(user_id: int, first_name: str | None, username: str | None):
    now = int(time.time())
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (id, first_name, username, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username,
                last_seen = excluded.last_seen
        """, (user_id, first_name, username, now, now))


def db_add_message(user_id: int, role: str, content: str):
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, int(time.time())),
        )
        # trim: keep last MAX_HISTORY messages
        conn.execute("""
            DELETE FROM messages WHERE user_id = ? AND id NOT IN (
                SELECT id FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?
            )
        """, (user_id, user_id, MAX_HISTORY))


def db_get_history(user_id: int, limit: int = MAX_HISTORY, cap: int = 2000) -> list[dict]:
    """return history buat dikirim ke AI. cap per-message biar token gak bocor."""
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    rows.reverse()
    out = []
    for r, c in rows:
        if len(c) > cap:
            c = c[:cap] + "..."
        out.append({"role": r, "content": c})
    return out


def db_reset_user(user_id: int):
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))


def db_user_stats(user_id: int) -> tuple[int, int]:
    with DB_LOCK, sqlite3.connect(DB_PATH) as conn:
        n = conn.execute("SELECT COUNT(*) FROM messages WHERE user_id = ?", (user_id,)).fetchone()[0]
        u = conn.execute("SELECT first_seen, last_seen FROM users WHERE id = ?", (user_id,)).fetchone()
    return n, (u[0] if u else 0, u[1] if u else 0)


db_init()

fail_counts: dict[str, int] = {}

# ext / mime yg diperlakukan sbg text (sisanya -> binary preview)
TEXT_EXT = {
    "txt","md","rst","py","js","ts","jsx","tsx","mjs","cjs",
    "json","jsonc","yaml","yml","toml","ini","cfg","conf","env",
    "xml","html","htm","css","scss","sass","less",
    "sh","bash","zsh","fish","ps1","bat","cmd",
    "c","cpp","cc","cxx","h","hpp","hxx","rs","go","java","kt","swift","m","mm",
    "rb","php","pl","lua","r","scala","clj","ex","exs","erl","hs",
    "sql","graphql","gql","proto",
    "csv","tsv","log","diff","patch",
    "dockerfile","makefile","gitignore","gitattributes","editorconfig",
    "vue","svelte","astro",
}
TEXT_MIME_PREFIX = ("text/", "application/json", "application/xml",
                    "application/javascript", "application/x-yaml",
                    "application/x-shellscript")

client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))


# model tier-1 yg punya vision (untuk image input dari user).
VISION_PREFERRED = [
    "gemini/gemini-3.5-flash-lite",
    "openrouter/minimax/minimax-m3:free",
    "3/free/glm-5.3-flash",
    "3/free/gemini-3.7-flash",
    "bzl/qwen3.7-flash",
]
VISION_POOL = VISION_PREFERRED + [m for m in POOL if m not in VISION_PREFERRED]


async def download_telegram_file(bot, file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    buf = await f.download_as_bytearray()
    return bytes(buf)


def _is_text_file(filename: str, mime: str | None) -> bool:
    if mime:
        if mime.startswith(TEXT_MIME_PREFIX):
            return True
        if mime.startswith("image/") or mime.startswith("audio/") or mime.startswith("video/"):
            return False
    name = (filename or "").lower()
    if "." in name:
        ext = name.rsplit(".", 1)[-1]
        if ext in TEXT_EXT:
            return True
    return False


def _format_text_user_msg(text: str, filename: str, mime: str | None, truncated: bool) -> str:
    head = f"[file: {filename or 'unknown'}{' (' + mime + ')' if mime else ''}]"
    body = text
    if truncated:
        body = body + f"\n\n[... truncated, total >{MAX_TEXT_FILE} chars]"
    return f"{head}\n```\n{body}\n```"


def _format_binary_user_msg(data: bytes, filename: str, mime: str | None) -> str:
    head = f"[file: {filename or 'unknown'}{' (' + mime + ')' if mime else ''}, {len(data)} bytes]"
    preview = data[:512]
    try:
        text_view = preview.decode("utf-8", errors="replace")
        sample = "".join(c if (32 <= ord(c) < 127 or c in "\n\r\t") else "." for c in text_view)
    except Exception:
        sample = preview.hex()
    b64 = __import__("base64").b64encode(preview).decode()
    return f"{head}\nfile ini binary, parser khusus belum ada. preview hex:\n```\n{preview.hex()[:512]}\n```\nbase64 (512 byte pertama):\n{b64[:256]}...\ntext-safe:\n```\n{sample}\n```"


def _extract_zip_text(data: bytes, zip_name: str) -> tuple[str, str | None]:
    """return (user_msg, err). kalau err != None, msg gagal."""
    import io, zipfile
    head = f"[archive: {zip_name}, {len(data)} bytes]"
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return "", "file ini bukan zip valid."
    files = zf.namelist()
    if not files:
        return "", "zip kosong."
    listing = ", ".join(files[:MAX_ZIP_ENTRIES])
    if len(files) > MAX_ZIP_ENTRIES:
        listing += f" ... (+{len(files)-MAX_ZIP_ENTRIES} more)"
    chunks = [head, f"entries ({len(files)}): {listing}", ""]
    read_count = 0
    skipped = []
    for name in files[:MAX_ZIP_ENTRIES]:
        try:
            info = zf.getinfo(name)
            if info.is_dir():
                continue
            if info.file_size > MAX_ZIP_ENTRY_SIZE:
                skipped.append(f"{name} ({info.file_size//1024}KB)")
                continue
            content = zf.read(name)
            # decide text vs binary
            try:
                txt = content.decode("utf-8")
                is_text = True
            except UnicodeDecodeError:
                txt = content.decode("latin-1", errors="replace")
                # kalau banyak non-printable, treat as binary
                printable = sum(1 for c in txt[:512] if 32 <= ord(c) < 127 or c in "\n\r\t")
                is_text = (printable / max(1, len(txt[:512]))) > 0.85
            if is_text:
                truncated = len(txt) > MAX_ZIP_ENTRY_SIZE
                txt = txt[:MAX_ZIP_ENTRY_SIZE]
                chunks.append(f"--- {name} ---")
                if truncated:
                    txt += f"\n[... truncated at {MAX_ZIP_ENTRY_SIZE} chars]"
                chunks.append(txt)
                chunks.append("")
                read_count += 1
            else:
                skipped.append(f"{name} (binary)")
        except Exception as e:
            skipped.append(f"{name} (err: {e})")
    if skipped:
        chunks.append(f"[skipped: {', '.join(skipped[:20])}]")
    chunks.append(f"[read {read_count} text file(s) from archive]")
    return "\n".join(chunks), None


async def call_one(model: str, messages: list[dict], stream: bool = False):
    r = await client.post(
        f"{NINE_BASE}/v1/chat/completions",
        headers={"Authorization": f"Bearer {NINE_API_KEY}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "max_tokens": 2000, "stream": stream},
    )
    r.raise_for_status()
    return r


async def chat(messages: list[dict], prefer_vision: bool = False) -> tuple[str, str]:
    """pick dari tier-1 random; kalau gagal, lanjut tier-2; terakhir tier-3. Returns (text, model)."""
    # kalau vision: hanya model yg punya vision capability. skip tier non-vision.
    if prefer_vision:
        tiers = ([m for m in TIER1 if m in VISION_PREFERRED], [], [])
    else:
        tiers = (TIER1, TIER2, TIER3)
    pool = VISION_POOL if prefer_vision else POOL
    tries_per_tier = 2
    last_err = ""
    for tier in tiers:
        if not tier:
            continue
        order = [m for m in tier if m in pool]
        random.shuffle(order)
        attempts = 0
        for m in order:
            if fail_counts.get(m, 0) >= 3:
                continue
            attempts += 1
            if attempts > tries_per_tier:
                break
            try:
                r = await call_one(m, messages)
                data = r.json()
                txt = data["choices"][0]["message"].get("content") or ""
                txt = txt.strip()
                low = txt.lower()
                if any(p in low for p in QUOTA_PHRASES):
                    fail_counts[m] = fail_counts.get(m, 0) + 1
                    last_err = f"quota: {txt[:80]}"
                    continue
                fail_counts[m] = 0
                return txt, m
            except httpx.HTTPStatusError as e:
                last_err = f"{e.response.status_code}: {e.response.text[:120]}"
                if e.response.status_code in SKIP_STATUS:
                    fail_counts[m] = fail_counts.get(m, 0) + 1
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                fail_counts[m] = fail_counts.get(m, 0) + 1
    raise RuntimeError(f"all models failed. last: {last_err}")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"halo, axly ai siap. pool: {len(POOL)} model. /status buat cek.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    cid = msg.chat.id
    uid = user.id if user else cid

    if user:
        db_upsert_user(uid, user.first_name, user.username)

    text = (msg.text or msg.caption or "").strip()
    parts: list[dict] = []
    has_image = False

    # gambar dari photo (compressed)
    if msg.photo:
        has_image = True
        smallest = msg.photo[-1]
        try:
            data = await download_telegram_file(ctx.bot, smallest.file_id)
            b64 = __import__("base64").b64encode(data).decode()
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        except Exception as e:
            await msg.reply_text(f"err download photo: {e}")
            return

    doc = msg.document
    if doc and not has_image:
        mime = doc.mime_type
        fname = doc.file_name or ""
        size = doc.file_size or 0
        if size > MAX_FILE_DOWNLOAD:
            await msg.reply_text(f"file kegedean ({size//1_000_000}MB). max 19MB.")
            return
        try:
            data = await download_telegram_file(ctx.bot, doc.file_id)
        except Exception as e:
            await msg.reply_text(f"err download doc: {e}")
            return
        # image document -> vision
        if mime and mime.startswith("image/"):
            has_image = True
            b64 = __import__("base64").b64encode(data).decode()
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        # zip archive -> extract text entries
        elif (fname or "").lower().endswith(".zip") or mime in ("application/zip", "application/x-zip-compressed"):
            zip_msg, zip_err = _extract_zip_text(data, fname)
            if zip_err:
                await msg.reply_text(zip_err)
                return
            parts.append({"type": "text", "text": zip_msg})
        elif _is_text_file(fname, mime):
            try:
                content = data.decode("utf-8", errors="replace")
            except Exception as e:
                await msg.reply_text(f"decode gagal: {e}")
                return
            truncated = len(content) > MAX_TEXT_FILE
            content = content[:MAX_TEXT_FILE]
            file_text = _format_text_user_msg(content, fname, mime, truncated)
            parts.append({"type": "text", "text": file_text})
        else:
            file_text = _format_binary_user_msg(data, fname, mime)
            parts.append({"type": "text", "text": file_text})

    if text:
        parts.insert(0, {"type": "text", "text": text})

    if not parts:
        return

    user_msg = {"role": "user", "content": parts if len(parts) > 1 or has_image else parts[0]["text"]}
    # save user msg ke history. cap 4000 char biar DB gak bengkak kalau kirim file gede.
    if text and not has_image:
        save_content = text[:4000]
    elif has_image:
        save_content = f"[image] {text[:200] if text else ''}".strip()
    elif parts and isinstance(parts[0].get("text"), str):
        save_content = f"[file] {parts[0]['text'][:4000]}"
    else:
        save_content = "[empty]"
    db_add_message(uid, "user", save_content)

    history = db_get_history(uid)
    # kirim typing indicator biar user tau bot lagi proses
    try:
        await ctx.bot.send_chat_action(chat_id=msg.chat_id, action="typing")
    except Exception:
        pass
    try:
        reply, used = await chat([{"role": "system", "content": SYSTEM}, *history], prefer_vision=has_image)
    except RuntimeError as e:
        await msg.reply_text(f"maaf, semua model gagal. coba lagi.\n{e}")
        return
    except Exception as e:
        await msg.reply_text(f"err: {type(e).__name__}: {e}")
        return
    db_add_message(uid, "assistant", reply)
    short = reply[:4000]
    tag = f"\n\n[{used}]" if len(reply) > 4000 or os.environ.get("DEBUG_TAG") else ""
    await msg.reply_text(short + tag)


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = msg.from_user.id if msg.from_user else msg.chat.id
    db_reset_user(uid)
    await msg.reply_text("history cleared.")


async def me_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = msg.from_user.id if msg.from_user else msg.chat.id
    n, (first, last) = db_user_stats(uid)
    from datetime import datetime
    def fmt(ts):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"
    await msg.reply_text(
        f"user: {msg.from_user.first_name if msg.from_user else 'anon'} (id={uid})\n"
        f"history: {n} pesan\n"
        f"first seen: {fmt(first)}\n"
        f"last seen: {fmt(last)}"
    )


async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    active = sum(1 for m in POOL if fail_counts.get(m, 0) < 3)
    lines = [f"- {m} (fails: {fail_counts.get(m,0)})" for m in POOL]
    await update.message.reply_text(f"pool: {active}/{len(POOL)} aktif\n" + "\n".join(lines))


async def reset_pool_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fail_counts.clear()
    await update.message.reply_text("pool fail counter reset.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("me", me_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("resetpool", reset_pool_cmd))
    # satu handler all-in. skip COMMAND biar /start dll kena CommandHandler dulu.
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()