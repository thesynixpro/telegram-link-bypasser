# Telegram Link Bypasser (arolinks.com + vplink.in)

A self-hosted Telegram bot that follows ad-monetized shortener links through
their JavaScript countdowns and ad funnels and replies with the **real final
destination URL**.

The previous version used static HTTP requests and BeautifulSoup/regex. That
approach stopped at the first ad wall and returned intermediate URLs such as
`onlinewish.in` / `crimejasoos.in`, so users still saw ads. This version drives
headless Chromium, keeps clicking through the funnel, and only returns a URL
once it is outside the blacklist and has stopped changing.

> **Legal / ToS warning**
> Bypassing a URL shortener violates that service's Terms of Service. This
> project is provided for personal/educational use only, with no warranty. Use
> it only for links you are authorized to open. You are responsible for how you
> use it.

---

## Project structure

| File | Purpose |
| --- | --- |
| `bot.py` | Telegram entry point: `/start`, `/bypass <url>`, and plain-message URL detection. Long polling, async handlers, full logging. |
| `bypasser.py` | All bypass logic: blacklist, `_is_final_url`, cloudscraper fast path for arolinks, and the Playwright domain-escape loop for both sites. |
| `requirements.txt` | Pinned Python dependencies. |
| `Procfile` | Declares a Railway/Heroku-style worker: `worker: python bot.py`. |
| `railway.toml` | Nixpacks build that installs Python deps and Chromium with system libraries. |
| `.env.example` | Template for local environment variables. |
| `README.md` | This guide. |

---

## How the domain-escape loop works

The core fix is in `_escape_ad_funnel()` in `bypasser.py`:

1. Launch headless Chromium with a Windows Chrome User-Agent and a
   `1920x1080` viewport.
2. Open the shortener URL with `wait_until="domcontentloaded"` and a 30-second
   timeout.
3. Run at most **8 iterations**. On each pass:
   - Wait a random **4–6 seconds** for JavaScript countdowns / Cloudflare.
   - Inspect every open tab. A URL is accepted only if its host is **not** in
     `BLACKLIST_DOMAINS` **and** the URL has been stable for 3 seconds.
   - Otherwise click the first visible control from `CLICK_SELECTORS`
     (`Continue`, `Get Link`, `Skip Ad`, `#btn6`, `#getlink`, ...) and wait for
     the next page.
4. If the funnel is still blocked after the loop, scan the page anchors for a
   non-blacklisted `http(s)` link, prioritizing link text like `Download`,
   `Continue`, or `Here`.
5. If no final URL exists, raise
   `BypassError("Could not escape the ad funnel — the shortener may have changed its layout.")`

`arolinks.com` also gets an optional cloudscraper fast path: if the raw HTML
contains a meta refresh or `window.location.href` pointing outside the
blacklist, that URL is returned immediately. Otherwise the browser path runs.
`vplink.in` always goes straight to Playwright because of Cloudflare and the
multi-hop funnel.

The shortener/ad-partner blacklist is:

```text
arolinks.com
vplink.in
onlinewish.in
crimejasoos.in
```

Everything is closed in a `finally` block. There is no paid API and no external
bypass service.

---

## 1. Create the Telegram bot

1. Open Telegram and start a chat with **@BotFather**.
2. Send `/newbot`, choose a name, and pick a username ending in `bot`.
3. Copy the token BotFather returns (looks like `123456789:AAE...`).
4. For groups, disable privacy mode with `/setprivacy` if you want the bot to
   see all messages. Private chats work without this.

---

## 2. Test locally

Python **3.10+** is required.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt

# Download Chromium. On a fresh Linux machine also install system libraries:
playwright install chromium
# or: playwright install --with-deps chromium

cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN=your_real_token

python bot.py
```

Then message the bot:

```text
/start
/bypass https://arolinks.com/example
https://vplink.in/example
```

The bot first replies `⏳ Bypassing…`, then edits that message with either
`✅ Destination:` and the final URL or a `❌` error.

### Quick offline checks

```bash
python -c "import bypasser; print(bypasser._is_final_url('https://onlinewish.in/x'))"
# False
python -c "import bypasser; print(bypasser._is_final_url('https://example.com/file'))"
# True
python -m py_compile bot.py bypasser.py
```

---

## 3. Push to GitHub

```bash
git add .
git commit -m "Browser-based ad-funnel bypass for arolinks and vplink"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

Make sure `.env` is **not** committed (it is already in `.gitignore`); only
`.env.example` should be tracked.

---

## 4. Deploy on Railway

1. In [Railway](https://railway.app/), click **New Project** →
   **Deploy from GitHub repo** and select this repository.
2. Open the service → **Variables** → add:

   ```text
   TELEGRAM_BOT_TOKEN=your_real_token
   ```

3. Railway reads `railway.toml` and runs during build:

   ```text
   pip install --upgrade pip
   pip install -r requirements.txt
   PLAYWRIGHT_BROWSERS_PATH=/app/.cache/ms-playwright \
     python -m playwright install chromium --with-deps
   ```

   Browsers are written to `/app/.cache/ms-playwright`, and `bypasser.py`
   automatically points Playwright there when the directory exists. The start
   command is `python bot.py`.
4. Open **Deployments → View logs**. A healthy deploy ends with:

   ```text
   Bot is starting with long polling...
   ```

   Earlier in the build log you should see Playwright downloading Chromium (for
   example `Downloading Chromium ...` and `Chromium ... downloaded to
   /app/.cache/ms-playwright`). A successful build also logs `Installing
   dependencies` / apt package installation for Chromium.
5. Send `/start` to the bot.

### Railway notes and warnings

- **Run exactly one replica.** Telegram long polling must have a single
  `getUpdates` consumer; two replicas will fight over updates.
- **Memory:** headless Chromium needs roughly **300–600 MB** at runtime. Give
  the service at least **1 GB** of RAM. Railway's free tier may not be enough
  for heavy use; the container can be killed for OOM under load.
- **Build time:** downloading Chromium and its OS libraries makes the first
  build take several minutes.
- **Python version:** Nixpacks usually chooses Python 3.11/3.12. If it picks
  3.13+ and `greenlet` fails to build, set
  `NIXPACKS_PYTHON_VERSION=3.12` in Railway variables and redeploy.
- **If `playwright install --with-deps` fails** (for example, no `apt-get` in
  the builder), use a Dockerfile based on
  `mcr.microsoft.com/playwright/python:v1.48.0-jammy`, run
  `pip install -r requirements.txt`, and start with `python bot.py`.
- No public domain or port is needed because the bot uses polling, not webhooks.

---

## 5. Reading Railway logs when deployment fails

| Symptom in logs | Meaning / fix |
| --- | --- |
| `TELEGRAM_BOT_TOKEN is not set` | Variable missing in Railway → Variables, or the service did not redeploy. |
| `Executable doesn't exist at .../chrome-linux/chrome` | Chromium was not installed in the runtime image. Check the build command and that `PLAYWRIGHT_BROWSERS_PATH=/app/.cache/ms-playwright` is set. |
| `Host system is missing dependencies` / `libasound.so.2` | `--with-deps` did not install system libraries. Use the official Playwright Docker image or install deps manually. |
| Build log stops at `playwright install` | The builder lacks apt/root. Switch to the Docker image approach above. |
| `Chromium could not start` at runtime | Usually missing shared libraries or not enough memory. Check the build log and raise the memory limit. |
| `Conflict: terminated by other getUpdates request` | More than one replica is running. Scale to exactly one. |
| Repeated `Could not escape the ad funnel` | The site changed its ad funnel. Update the selectors (see Maintenance). |
| `Navigation timeout` for every link | Site is slow/blocking, or the container is resource-starved. Retry and check CPU/RAM. |

---

## 6. Maintenance (what to edit when a site changes)

Shortener sites change markup and ad partners often. The exact places to edit
in `bypasser.py`:

- **Lines 78–84: `BLACKLIST_DOMAINS`** — add any new intermediate ad domain
  (for example a new `*.in` funnel site). This is the list that prevents the bot
  from returning an ad page.
- **Lines 88–98: `CLICK_SELECTORS`** — update button text or IDs. This is the
  first thing to change when the loop clicks nothing.
- **Lines 542–560: the navigation loop comment and `for iteration in
  range(1, MAX_ITERATIONS + 1)` block** — change the wait time or iteration
  count only if needed. The loop body is generic; prefer editing the selectors.
- **Lines 431–475: `_find_destination_link`** — adjust anchor ranking if the
  fallback picks a navigation link instead of the real destination.

After editing, run `python -m py_compile bypasser.py` and retest one known link
before redeploying.

---

## 7. Legal and resource summary

Bypassing these shorteners violates their Terms of Service and may violate the
terms of the destination sites. This project is for personal/educational use
only, with no warranty. Playwright/Chromium requires roughly 500 MB of RAM, so
Railway's free tier may not be sufficient for heavy use. Keep only one bot
replica running and monitor memory if you process many links.
