# Deploy on Render (HTTPS, any network)

Use this guide to run Comment Bot at a fixed **https://** URL so phones and laptops on any network can use it.

## Before you start

- GitHub repo: `tarun-dev03/Comment_bot`
- [Render](https://render.com) account (sign in with GitHub)
- Google Cloud OAuth client (see main [README.md](README.md))
- SMTP for magic-link email (Gmail app password, SendGrid, Resend SMTP, etc.)

## Important: free vs paid web tier

| Render web plan | Behavior |
|-----------------|----------|
| **Free** | Service **sleeps** after ~15 min idle. The comment **bot stops** when the instance sleeps. OK for trying the UI; **not** reliable for long-running bots. |
| **Starter ($7/mo)** | Stays awake. Use this if the bot should run for a whole stream. |

Postgres on Render's free tier **expires after 30 days** and will ask for $7/mo. To keep your app **100% free forever without paying**, use **Neon.tech** (free cloud PostgreSQL) or fallback to **SQLite**.

---

## Free Database Options (No Expiration)

### Option A — Neon.tech Free PostgreSQL (Recommended)
Neon offers a serverless PostgreSQL database with 0.5 GB storage that **never expires** and requires no credit card:
1. Sign up at [neon.tech](https://neon.tech) (1 click with GitHub or Google).
2. Click **Create Project** (named e.g. `comment-bot`).
3. Under **Dashboard** or **Connection Details**, copy the connection string:
   ```
   postgresql://user:password@ep-xyz.region.aws.neon.tech/neondb?sslmode=require
   ```
4. In Render: Open your web service **comment-bot** → **Environment** → set `DATABASE_URL` to this connection string → **Save Changes**.
5. Render automatically redeploys, connects to Neon, and initializes all tables!

### Option B — SQLite (Zero setup)
If you don't want an external database:
1. In Render: Open your web service **comment-bot** → **Environment**.
2. Remove `DATABASE_URL` (or leave it unset).
3. The app will automatically fall back to local SQLite (`comment_bot.db`).
*Note: Render free web instances have ephemeral disks, meaning data resets whenever the instance sleeps or redeploys.*

---

## Option 1 — Blueprint Deploy

1. In Render: **New** → **Blueprint**.
2. Connect repo **`tarun-dev03/Comment_bot`**.
3. Render reads [`render.yaml`](render.yaml) and creates the web service **comment-bot** (Docker).
4. After deploy, open the web service → **Environment** and configure:

   | Variable | Value |
   |----------|--------|
   | `DATABASE_URL` | Free Neon Postgres URL (from Option A above), or leave unset for SQLite |
   | `APP_URL` | Your service URL, e.g. `https://comment-bot-xxxx.onrender.com` (no trailing slash) |
   | `GOOGLE_CLIENT_ID` | From Google Cloud |
   | `GOOGLE_CLIENT_SECRET` | From Google Cloud |
   | `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Your mail provider (optional if using Google login only) |

   `SECRET_KEY` and `SESSION_SECRET` are auto-generated.

5. If an old, expired Render database `comment-bot-db` exists in your Render dashboard, delete it to clear warning banners.

---

## Option 2 — Manual Web Service

1. In Render: **New** → **Web Service** → connect repo → **Language: Docker**.
2. **Environment** → add variables from [.env.example](.env.example). Set `DATABASE_URL` to your free Neon Postgres URL.
3. Set `APP_URL` to the web service URL Render shows after creation.
4. Deploy.

---

## Google OAuth (required)

In [Google Cloud Console](https://console.cloud.google.com/) → **Credentials** → your OAuth client:

**Authorized redirect URIs** (add both if you still develop locally):

```
https://YOUR-SERVICE.onrender.com/auth/google/callback
http://localhost:8000/auth/google/callback
```

Replace `YOUR-SERVICE` with your actual Render hostname.

OAuth consent screen: add your Gmail (and friends) as **Test users** while the app is in Testing mode.

---

## Email login on Render

Optional if everyone uses **Sign in with Google** (no SMTP required).

For magic-link email login, set `EMAIL_DEV_MODE=false` and configure SMTP. Magic links use `APP_URL`; they must point to your **Render HTTPS URL**, not localhost.

If login emails don’t arrive, check Render **Logs** for SMTP errors and verify `SMTP_FROM` is allowed by your provider.

---

## Using the app from any network

1. In Render, open **comment-bot** and copy the **exact** URL at the top (e.g. `https://comment-bot.onrender.com` or `https://comment-bot-xxxx.onrender.com`). **Do not guess** — if the URL 404s in the browser, Google OAuth will fail too.
2. Set **`APP_URL`** in Environment to that exact URL (no trailing `/`).
3. Open that URL on phone or laptop.
2. Sign in with email → open link from inbox.
3. **Connect Google** → paste **live** stream URL → **Start**.
4. **Stop** when done.

Share that HTTPS link; each person uses their own email and Google account.

---

## Checklist

- [ ] `APP_URL` matches the browser URL exactly (https).
- [ ] Google redirect URI matches `{APP_URL}/auth/google/callback`.
- [ ] Database configured (`DATABASE_URL` set to Neon or empty for SQLite); logs show "Database initialized".
- [ ] `/health` returns `{"ok":true}` (Render health check).
- [ ] Starter plan (or accept free-tier sleep) if the bot must run continuously.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Render Postgres expired / suspended | Render free databases expire after 30 days. Don't pay $7/mo; delete the expired database in Render and follow Option A above to get a free permanent Postgres at [Neon.tech](https://neon.tech), then set `DATABASE_URL` on Render. |
| Google “redirect_uri_mismatch” | Redirect URI in Google Cloud must match Render URL exactly. |
| “Connect Google” fails | Set `GOOGLE_CLIENT_*` on Render; redeploy. |
| Bot stops mid-stream | Free tier slept — upgrade to Starter or restart and Start again. |
| Quota / 403 errors | YouTube API daily limit; see README quota section. |
