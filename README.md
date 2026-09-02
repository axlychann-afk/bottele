# axly-bot (Telegram + 9router)

Telegram bot pakai AI dari 9router (OpenAI-compatible).

## Run lokal
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy env.example .env
# edit .env kalau perlu
python bot.py
```

## VPS
```
git clone ... && cd axly-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp env.example .env  # edit kalau perlu
nohup python bot.py > bot.log 2>&1 &
```

## Railway
Push repo, set env vars:
- `BOT_TOKEN`
- `NINE_API_KEY`
- `NINE_BASE` (default sudah benar)
- `MODEL` (default `7/deepseek-v4-flash`)

Procfile sudah ada (`worker: python bot.py`). Untuk Railway free plan, `worker` bisa idle-restart — kalau mau selalu hidup, pakai `web` (sudah ada `railway.json` dengan `web: python bot.py`).

Bot pakai pool 22 model (tested work dari key ini Sept 2026). Random pick tiap chat, fallback otomatis ke next kalau satu gagal. Model yg gagal 3x atau balas pesan "quota habis" diskip sementara.

## Commands
- `/start` — sapa + info pool
- `/reset` — hapus history chat ini
- `/status` — lihat pool + fail counter
- `/resetpool` — reset fail counter (kalau semua ke-skip)

Note: combo "claude" di dashboard9router = rotasi 24 model, tapi gak bisa dipanggil langsung via API key ini (combo fitur dashboard). Bot simulasi combo sendiri. Claude asli (`7/claude-*`) butuh deposit. `5/*-free` semua quota habis, di-exclude dari pool.