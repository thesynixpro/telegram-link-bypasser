# Telegram Link Bypasser (arolinks.com + vplink.in)

A small self-hosted Telegram bot that follows ad-gateway links from
**arolinks.com** and **vplink.in** and replies with the final destination URL.
It uses only free, self-contained tooling: `python-telegram-bot`,
`cloudscraper`, `BeautifulSoup` and headless Chromium via Playwright.

> **Legal / ToS warning**
> Bypassing a URL shortener may violate that service's Terms of Service and/or
> the terms of the sites it links to. Use this bot only for links you are
> authorized to open. The project is provided as-is, with no warranty, and you
> are responsible for how you use it.

---

## Project structure

| File | Purpose |
| --- | --- |
| `bot.py` | Telegram entry point: handlers for `/start`, `/bypass <url>` and plain URL messages; reads `TELEGRAM_BOT_TOKEN`; runs long polling. |
| `bypasser.py` | All bypass logic. Fast static extraction for arolinks, Playwright-first flow for vplink, generic heuristics, timeouts, and the `BypassError` exception. |
| `requirements.txt` | Pinned Python dependencies. |
| `Procfile` | Tells Railway (and Heroku-style hosts) to run a background worker. |
| `railway.toml` | Railway/Nixpacks build config. Installs dependencies and Chromium with system libraries at build time. |
| `.env.example` | Template for local environment variables. |
| `README.md` | This file. |

---

## How the bypassing works

### arolinks.com (`bypass_arolinks`)

1. **Static fast path** (`cloudscraper`, with a plain `requests` fallback):
   - Downloads the HTML with realistic browser headers.
   - Looks for `<meta http-equiv="refresh" content="...url=...">`.
   - Searches inline JavaScript for redirects:
     `var url = "..."`, `location.href = "..."`,
     `location.replace("...")`, `window.open("...")`.
   - Chooses the last external link that is not the shortener itself.
   - If a countdown value is visible in the HTML/JS (for example
     `var seconds = 10`), it waits briefly and retries.
2. **Playwright fallback**: launches headless Chromium, lets the countdown
   run, clicks "Continue"/"Get Link"/similar controls, and reads the final
   URL from the page URL, page HTML, or its links. It also notices destination
   pages opened in a new tab.

### vplink.in (`bypass_vplink`)

1. **Playwright is the primary method.** The page is behind Cloudflare and
   uses ad intermediates, so JavaScript must run. The code:
   - opens the page with a realistic user agent,
   - waits for a Cloudflare challenge to clear (title/selector checks),
   - waits for the page to settle,
   - clicks countdown/continue controls (handling new tabs and dialogs),
   - captures the destination from `page.url`, the HTML, or external anchors.
2. **cloudscraper is the secondary method**, tried only if Playwright fails.

Both paths use a browser `--no-sandbox` configuration because Railway runs
containers as root. The Chromium instance is always closed in a `finally`
block. Each stage has a timeout: 20 s for HTTP, 25 s for navigation, and a
90 s hard cap for a browser attempt.

---

## 1. Create the Telegram bot

1. Open Telegram and start a chat with **@BotFather**.
2. Send `/newbot`, choose a name and a username ending in `bot`.
3. Copy the token BotFather gives you (looks like
   `123456789:AAE...`).
4. Optionally send `/setprivacy` to BotFather and disable privacy mode if you
   want the bot to see all group messages. For private chats this is not
   needed.

---

## 2. Local testing

Python **3.10+** is required.

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install the browser used by Playwright
playwright install chromium
# On a fresh Linux machine you may also need the system libraries:
# playwright install --with-deps chromium

# 4. Configure the token
cp .env.example .env
# Edit .env and put your real token after TELEGRAM_BOT_TOKEN=

# 5. Run the bot
python bot.py
```

Then message your bot:

```text
/start
/bypass https://arolinks.com/example
https://vplink.in/example
```

You must run `playwright install chromium` locally before testing. On Railway,
`railway.toml` performs that step automatically during the build.

---

## 3. Deploy to Railway

1. **Push the project to GitHub.** Make sure `.env` is **not** committed (only
   `.env.example` should be in the repository).
2. In [Railway](https://railway.app/), create a **New Project** and choose
   **Deploy from GitHub repo**. Select this repository.
3. Open the new service and go to **Variables**. Add:

   ```text
   TELEGRAM_BOT_TOKEN=your_real_token
   ```

4. Railway reads `railway.toml` and runs:

   ```text
   pip install --upgrade pip
   pip install -r requirements.txt
   PLAYWRIGHT_BROWSERS_PATH=/app/.cache/ms-playwright \
     python -m playwright install --with-deps chromium
   ```

   Browsers are written to `/app/.cache/ms-playwright` so they survive into the
   runtime image. `bypasser.py` detects that directory and points Playwright at
   it automatically. The start command is `python bot.py` (the `Procfile` also
   declares a `worker`).
5. Open **Deployments → View logs** and wait for
   `Bot is starting with long polling...`.
6. Send `/start` to your bot on Telegram.

### Important Railway notes

- **Run exactly one replica.** Telegram long polling uses a single
  `getUpdates` consumer; two replicas will fight over updates. Do not enable
  horizontal scaling for this service.
- **Memory:** headless Chromium typically needs 300–600 MB at runtime. Give
  the service at least **1 GB** of RAM. If the container is killed with an
  out-of-memory error, raise the memory limit or run Chromium less often.
- **Build time:** downloading Chromium and its Linux dependencies makes the
  first build noticeably slower (a few minutes).
- **Python version:** Nixpacks normally selects Python 3.11 or 3.12, which are
  compatible with the pinned Playwright version. If Railway selects Python
  3.13+ and the `greenlet` dependency fails to build, add the variable
  `NIXPACKS_PYTHON_VERSION=3.12` in Railway and redeploy.
- **If `playwright install --with-deps` fails during the Nixpacks build**
  (for example because `apt-get` is unavailable), switch the Railway service
  to a Dockerfile based on the official image
  `mcr.microsoft.com/playwright/python:v1.44.0-jammy`, then run
  `pip install -r requirements.txt` and `python bot.py` in that image.
- The bot uses polling, not webhooks, so no public domain or port is needed.

---

## 4. Configuration reference

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | Token from @BotFather. |

Other tuning constants live at the top of `bypasser.py`:
`HTTP_TIMEOUT`, `PAGE_TIMEOUT_MS`, `TOTAL_TIMEOUT`, `USER_AGENT`,
`BROWSER_ARGS` and `BLOCKED_HOSTS`.

---

## 5. Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `TELEGRAM_BOT_TOKEN is not set` | Missing `.env` locally or missing Railway variable. |
| `Executable doesn't exist ... chromium` | Run `playwright install chromium` (local) or check the Railway build log. |
| `Target page, context or browser has been closed` | A navigation or popup closed the page; retry the link. The handler catches this and reports an error. |
| Every link times out | Railway container may be short on memory/CPU. Increase resources or retry later. |
| Cloudflare never clears | The site changed its challenge. Update the selectors in `_wait_for_cloudflare` and `_click_continue`. |
| "Destination link could not be found" | The shortener changed its HTML/JS. Inspect the page and adjust `_extract_from_html` / click regexes. |

---

## 6. Maintenance

Shortener sites change their markup, advertising flow and anti-bot protection
frequently. Expect to revisit:

- `_extract_from_html` — static URL/meta-refresh/JS-variable patterns.
- `_detect_timer_seconds` — countdown formats.
- `_wait_for_cloudflare` — challenge page indicators.
- `_click_continue` — button labels and selectors.
- `BLOCKED_HOSTS` — new advertising/tracking domains.

Keep Playwright and Chromium reasonably current (`playwright==...` in
`requirements.txt` plus `python -m playwright install --with-deps chromium`),
because Cloudflare and site scripts evolve.

Finally: respect the law and the target sites' terms. This bot is intended for
personal, authorized use only.
