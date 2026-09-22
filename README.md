# YouTube Live Comment Bot

Shareable web app: sign in with **email**, connect **Google/YouTube**, paste a **live stream URL**, and run a bot that posts varied chat messages every **1–10 seconds** (random interval) via the official YouTube Live Chat API.

Works in the browser on phone or laptop—share your deployed URL so others can use their own accounts.

## Warning

- Automated or high-volume live chat may violate [YouTube Terms of Service](https://www.youtube.com/t/terms) and can lead to restrictions on your Google/YouTube account.
- YouTube Data API **quota** limits how many chat messages you can send per day (default projects are roughly ~50 inserts/day). This app defaults to `MAX_MESSAGES_PER_DAY=40` until you request a [quota increase](https://support.google.com/youtube/contact/yt_api_form).

## Quick start (local)

```bash
cd Comment_bot
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — at minimum set SECRET_KEY, SESSION_SECRET, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
uvicorn app.main:app --reload
```

Open `http://localhost:8000`.

With `EMAIL_DEV_MODE=true` (default), magic login links are printed to the terminal instead of sent by email.

## Google Cloud setup

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **YouTube Data API v3**.
3. Configure **OAuth consent screen** (External; add test users while in testing).
4. Create **OAuth client ID** → Web application.
5. Add authorized redirect URIs:
   - `http://localhost:8000/auth/google/callback`
   - `https://YOUR_DOMAIN/auth/google/callback`
6. Copy Client ID and Client Secret into `.env`.

Required OAuth scope (configured in app): `https://www.googleapis.com/auth/youtube.force-ssl`

## Email login (production)

Set in `.env`:

- `EMAIL_DEV_MODE=false`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`
- `APP_URL` must match your public URL (used in magic links)

## Environment variables

See [.env.example](.env.example).

| Variable | Purpose |
|----------|---------|
| `APP_URL` | Public base URL |
| `SECRET_KEY` | Encrypts stored Google refresh tokens |
| `SESSION_SECRET` | Signs session cookies |
| `DATABASE_URL` | e.g. `sqlite+aiosqlite:///./comment_bot.db` or Postgres async URL |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth |
| `MAX_MESSAGES_PER_DAY` | App-side cap (default 40) |

## Docker

```bash
docker build -t comment-bot .
docker run -p 8000:8000 --env-file .env comment-bot
```

Deploy the image to Render, Railway, Fly.io, etc. Set all env vars and use a persistent volume or Postgres for `DATABASE_URL`.

**Render (recommended for HTTPS + any network):** step-by-step in [DEPLOY_RENDER.md](DEPLOY_RENDER.md). Use the included [`render.yaml`](render.yaml) Blueprint (Docker + Postgres).

## Usage flow

1. **Sign in with email** → click magic link.
2. **Connect Google** → grant YouTube access.
3. Paste an **active live stream** URL (not a VOD).
4. **Start bot** → random non-repeating messages every 1–10 seconds.
5. **Stop** when finished.

Optional: add custom phrases on the dashboard; they merge with built-in templates in [`data/comment_templates.json`](data/comment_templates.json).

## Tests

```bash
pytest
```

## Project layout

- `app/main.py` — FastAPI entrypoint
- `app/auth/` — email magic link + Google OAuth
- `app/youtube/live_chat.py` — URL parsing, live chat ID, message insert
- `app/bot/` — asyncio worker + message generator
- `app/templates/` — mobile-friendly UI
