import os
import asyncio
import random
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
NINE_API_KEY = os.environ["NINE_API_KEY"]
NINE_BASE = os.environ.get("NINE_BASE", "https://trustworthy-solace-production-068d.up.railway.app")
SYSTEM = os.environ.get("SYSTEM", "Kamu adalah axly ai, asisten singkat dan helpful. Jawab dalam bahasa yang dipakai user.")

# pool: rotate + fallback. udah ditest work dari key ini (Sept 2026).
# CATATAN: 5/*-free = quota habis (balas "Sorry ... can only try 10 times"), jangan masuk.
POOL = [
    "2/minimax-m3-free",
    "3/free/gemini-3.1-pro",
    "3/free/gemini-3.7-flash",
    "3/free/deepseek-v4-flash-0731",
    "3/free/deepseek-v4-pro-0813",
    "3/free/glm-5.3-flash",
    "4/qwen/qwen3.8-27b-free",
    "4/deepseek/deepseek-v4-flash-free",
    "4/orcarouter/free",
    "4/tencent/hy3-free",
    "6/z-ai/glm-5.3-free",
    "openrouter/minimax/minimax-m3:free",
    "ollama/gpt-oss:120b",
    "bzl/auto:free",
    "bzl/qwen3.7-flash",
    "alims-intl/qwen3-coder-plus",
    "gcli/grok-4.5-high",
    "gemini/gemini-3.5-flash-lite",
    "ps/poolside/laguna-xs-2.1",
    "7/deepseek-v4-flash",
    "7/qwen3.8-flash",
    "7/hy3",
]

QUOTA_PHRASES = ("can only try", "free quota", "topup", "recharged")

# skip codes that artinya quota/akun, bukan transient. sisanya retry.
SKIP_STATUS = {401, 403}

MAX_HISTORY = 20
histories: dict[int, list[dict]] = {}
fail_counts: dict[str, int] = {}

client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global client
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))
    return client


async def call_one(model: str, messages: list[dict]) -> str:
    c = get_client()
    r = await c.post(
        f"{NINE_BASE}/v1/chat/completions",
        headers={"Authorization": f"Bearer {NINE_API_KEY}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages},
    )
    r.raise_for_status()
    data = r.json()
    txt = data["choices"][0]["message"].get("content") or ""
    return txt.strip()


async def chat(messages: list[dict]) -> tuple[str, str]:
    """try each model in pool until one returns. returns (text, model_used)."""
    order = POOL.copy()
    random.shuffle(order)
    last_err = ""
    for m in order:
        if fail_counts.get(m, 0) >= 3:
            continue
        try:
            txt = await call_one(m, messages)
            # deteksi quota-exhausted reply (gak error HTTP, tapi useless)
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
        await asyncio.sleep(0.2)
    raise RuntimeError(f"all models failed. last: {last_err}")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"halo, axly ai siap. pool: {len(POOL)} model. /status buat cek.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    text = update.message.text.strip()
    h = histories.setdefault(cid, [])
    h.append({"role": "user", "content": text})
    if len(h) > MAX_HISTORY:
        del h[:-MAX_HISTORY]
    try:
        reply, used = await chat([{"role": "system", "content": SYSTEM}, *h])
    except Exception as e:
        await update.message.reply_text(f"err: {e}")
        return
    h.append({"role": "assistant", "content": reply})
    short = reply[:4000]
    tag = f"\n\n[{used}]" if len(reply) > 4000 or os.environ.get("DEBUG_TAG") else ""
    await update.message.reply_text(short + tag)


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    histories.pop(update.effective_chat.id, None)
    await update.message.reply_text("history cleared.")


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
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("resetpool", reset_pool_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()