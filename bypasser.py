"""
bypasser.py
===========

All shortener-specific bypass logic.

Public API
----------
    bypass_arolinks(url) -> str
    bypass_vplink(url)    -> str
    bypass(url)           -> str   (dispatches based on the domain)

Every public function raises ``BypassError`` (with a message that is safe to
show to an end user) when a destination URL cannot be recovered.

Strategy summary
----------------
arolinks.com:
    1. Fast path: cloudscraper / requests + BeautifulSoup.  We look for meta
       refreshes, JavaScript redirect variables, timer values and external
       links.  If a timer is detected we briefly wait and retry.
    2. Fallback: headless Chromium via Playwright.  The browser executes the
       JavaScript countdown, we click "continue"-style controls and read the
       resulting URL.

vplink.in:
    1. Primary: Playwright with a realistic user agent.  The code waits for a
       possible Cloudflare challenge, then clicks through the ad gate.
    2. Secondary: cloudscraper, in case the page did not really need a browser.

No paid APIs or external bypass services are used.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import unquote, urlparse

import cloudscraper
import requests
from bs4 import BeautifulSoup

# railway.toml installs Chromium into /app so it survives into the runtime
# image.  Locally that directory does not exist, so Playwright uses its normal
# per-user cache (populated by "playwright install chromium").
_RAILWAY_BROWSERS = "/app/.cache/ms-playwright"
if os.path.isdir(_RAILWAY_BROWSERS):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _RAILWAY_BROWSERS)

try:  # Playwright is optional at import time so the bot can still boot and
    # report a useful error if only the Python package is missing.
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - exercised only on broken installs
    async_playwright = None

    class PlaywrightTimeoutError(Exception):
        """Placeholder so ``except PlaywrightTimeoutError`` still works."""


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SUPPORTED_DOMAINS = ("arolinks.com", "vplink.in")

HTTP_TIMEOUT = 20                 # seconds, for cloudscraper/requests
PAGE_TIMEOUT_MS = 25_000          # milliseconds, for Playwright navigation
TOTAL_TIMEOUT = 90                # seconds, hard cap for one browser attempt

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Running Chromium as root in a container requires --no-sandbox.
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--no-first-run",
    "--no-default-browser-check",
]

# Hosts that are infrastructure / advertising, never a real destination.
# Deliberately conservative: we do not block social networks or file hosts
# because those are common real destinations.
BLOCKED_HOSTS = {
    "cloudflare.com",
    "challenges.cloudflare.com",
    "cloudflareinsights.com",
    "cdnjs.cloudflare.com",
    "googleads.g.doubleclick.net",
    "doubleclick.net",
    "googlesyndication.com",
    "google-analytics.com",
    "googletagmanager.com",
    "gstatic.com",
    "www.google.com",
    "google.co",
    "www.google.co",
    "policies.google.com",
    "support.google.com",
    "accounts.google.com",
    "recaptcha.net",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "cdn.jsdelivr.net",
    "code.jquery.com",
    "propellerads.com",
    "popads.net",
    "popcash.net",
    "adsterra.com",
    "onclickads.net",
    "clickadu.com",
    "hilltopads.net",
    "exoclick.com",
    "juicyads.com",
    "adcash.com",
    "mgid.com",
    "revcontent.com",
    "zedo.com",
    "taboola.com",
    "outbrain.com",
    "adnxs.com",
    "pubmatic.com",
    "rubiconproject.com",
    "openx.net",
    "criteo.com",
    "scorecardresearch.com",
    "quantserve.com",
}


# ---------------------------------------------------------------------------
# Errors and URL helpers
# ---------------------------------------------------------------------------

class BypassError(Exception):
    """Raised when a link cannot be bypassed.  The message is user-facing."""


def normalize_url(raw: str) -> str:
    """Trim a user-supplied value and add ``https://`` when no scheme exists."""
    if not raw or not isinstance(raw, str):
        raise BypassError("Please send a valid URL.")

    value = raw.strip().strip("<>\"'")
    if not value:
        raise BypassError("Please send a valid URL.")

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        value = "https://" + value

    parsed = urlparse(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise BypassError("That does not look like a valid http(s) URL.")
    return value


def extract_domain(url: str) -> str:
    """Return the lowercase hostname of *url* (empty string if none)."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def get_supported_domain(url: str) -> Optional[str]:
    """Return the matching supported root domain, or ``None``."""
    host = extract_domain(url)
    for domain in SUPPORTED_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return None


def is_supported_url(url: str) -> bool:
    return get_supported_domain(url) is not None


def _clean_url(value: str) -> str:
    """Decode common escaping and strip punctuation around an extracted URL."""
    if not isinstance(value, str):
        return ""
    value = html_lib.unescape(value)
    value = value.replace("\\/", "/")
    value = re.sub(r"\\u002[fF]", "/", value)
    value = re.sub(r"\\u003[aA]", ":", value)
    if value.lower().startswith(("http%3a", "https%3a")):
        value = unquote(value)
    value = value.strip().strip("\"'<> \n\r\t,;.")
    if value.startswith("//"):
        value = "https:" + value
    return value


def _is_destination(candidate: str, short_domain: str) -> bool:
    """True if *candidate* looks like a plausible final (non-shortener) URL."""
    candidate = _clean_url(candidate)
    if not candidate:
        return False

    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return False

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False

    # Never treat the shortener itself (or its sibling) as the destination.
    for domain in SUPPORTED_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return False

    for blocked in BLOCKED_HOSTS:
        if host == blocked or host.endswith("." + blocked):
            return False

    return True


# ---------------------------------------------------------------------------
# Static HTML extraction
# ---------------------------------------------------------------------------

# JavaScript redirect patterns.
_JS_ASSIGN_RE = re.compile(
    r"""(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*["']((?:https?:)?//[^"']+)["']""",
    re.I,
)
_JS_LOCATION_RE = re.compile(
    r"""(?:window\.)?location(?:\.href)?\s*=\s*["']((?:https?:)?//[^"']+)["']""",
    re.I,
)
_JS_NAV_RE = re.compile(
    r"""location\.(?:replace|assign)\(\s*["']((?:https?:)?//[^"']+)["']""",
    re.I,
)
_JS_OPEN_RE = re.compile(
    r"""window\.open\(\s*["']((?:https?:)?//[^"']+)["']""",
    re.I,
)
_JS_ANY_URL_RE = re.compile(
    r"""["']((?:https?:)?//[^"'\s<>]{4,})["']""",
    re.I,
)
_BARE_URL_RE = re.compile(r"""https?://[^\s"'<>\\]+""", re.I)
_META_REFRESH_RE = re.compile(r"""url\s*=\s*["']?([^"'\s>]+)""", re.I)

# Countdown patterns.  Only small, sane values (1-60) are accepted.
_TIMER_RES = (
    re.compile(
        r"""(?:var|let|const)\s*(?:time|timer|countdown|counter|seconds?|secs?|wait)[A-Za-z0-9_$]*\s*=\s*(\d{1,3})""",
        re.I,
    ),
    re.compile(r"""(?:startTimer|countdown|timer)\s*[\(\:]\s*(\d{1,3})""", re.I),
    re.compile(r"""\b(\d{1,3})\s*(?:seconds?|secs?)\b""", re.I),
)


def _prepare_js(text: str) -> str:
    """Unescape the bits of JavaScript that commonly wrap URLs."""
    text = html_lib.unescape(text)
    return (
        text.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
    )


def _detect_timer_seconds(text: str) -> Optional[int]:
    """Return a plausible countdown length embedded in *text*, if any."""
    for pattern in _TIMER_RES:
        for raw in pattern.findall(text or ""):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if 1 <= value <= 60:
                return value
    return None


def _extract_from_html(text: str, short_domain: str) -> Optional[str]:
    """Return the best destination candidate found in a static HTML page."""
    if not text:
        return None

    soup = BeautifulSoup(text, "html.parser")

    # 1. <meta http-equiv="refresh" content="5;url=...">
    for meta in soup.find_all("meta"):
        if "refresh" not in (meta.get("http-equiv") or "").lower():
            continue
        match = _META_REFRESH_RE.search(meta.get("content") or "")
        if match:
            candidate = _clean_url(match.group(1))
            if _is_destination(candidate, short_domain):
                return candidate

    # 2. JavaScript.  Prefer variables whose names indicate the final link.
    script_text = "\n".join(
        script.get_text() or "" for script in soup.find_all("script")
    )
    script_text = _prepare_js(script_text)

    preferred = []
    others = []
    for variable, raw_url in _JS_ASSIGN_RE.findall(script_text):
        candidate = _clean_url(raw_url)
        if not _is_destination(candidate, short_domain):
            continue
        if re.search(r"url|link|redirect|destination|target|final|next", variable, re.I):
            preferred.append(candidate)
        else:
            others.append(candidate)
    if preferred:
        return preferred[-1]

    for pattern in (_JS_LOCATION_RE, _JS_NAV_RE, _JS_OPEN_RE):
        for raw_url in pattern.findall(script_text):
            candidate = _clean_url(raw_url)
            if _is_destination(candidate, short_domain):
                return candidate

    # 3. Regular links / forms / iframes / data attributes.
    attributes = []
    for tag in soup.find_all(True):
        for attribute in (
            "href",
            "src",
            "action",
            "data-url",
            "data-href",
            "data-link",
            "data-target",
        ):
            value = tag.get(attribute)
            if isinstance(value, str):
                attributes.append(value)

    # "Last external link" is the generic heuristic requested for unknown
    # layouts.  Reverse order usually puts the reveal link after nav/footer.
    for raw_url in reversed(attributes + others):
        candidate = _clean_url(raw_url)
        if _is_destination(candidate, short_domain):
            return candidate

    # 4. Any quoted URL in inline JavaScript (again, last one wins).
    for raw_url in reversed(_JS_ANY_URL_RE.findall(script_text)):
        candidate = _clean_url(raw_url)
        if _is_destination(candidate, short_domain):
            return candidate

    # 5. Absolute URLs in raw text/attributes (e.g. onclick handlers).
    for raw_url in reversed(_BARE_URL_RE.findall(_prepare_js(text))):
        candidate = _clean_url(raw_url)
        if _is_destination(candidate, short_domain):
            return candidate

    return None


# ---------------------------------------------------------------------------
# Fast path: cloudscraper / requests
# ---------------------------------------------------------------------------

def _request_headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
    }


def _fetch_sync(url: str) -> tuple[str, str]:
    """Blocking fetch.  Runs in a worker thread via ``asyncio.to_thread``."""
    headers = _request_headers()

    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=2,
        )
        response = scraper.get(
            url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True
        )
        if response.status_code < 400 and response.text:
            return response.text, response.url
        logger.warning("cloudscraper returned HTTP %s for %s", response.status_code, url)
    except Exception as exc:  # cloudscraper raises many different exceptions
        logger.warning("cloudscraper failed for %s: %s", url, exc)

    try:
        response = requests.get(
            url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True
        )
        response.raise_for_status()
        return response.text, response.url
    except Exception as exc:
        raise BypassError(
            "Could not load the shortener page. It may be down or blocking "
            "automated requests right now."
        ) from exc


async def _static_bypass(
    url: str, short_domain: str, wait_for_timer: bool = False
) -> str:
    """Try to recover the destination without a browser."""
    try:
        text, final_url = await asyncio.to_thread(_fetch_sync, url)

        if _is_destination(final_url, short_domain):
            return _clean_url(final_url)

        found = _extract_from_html(text, short_domain)
        if found:
            return found

        if wait_for_timer:
            seconds = _detect_timer_seconds(text)
            if seconds:
                wait = min(seconds, 10)
                logger.info(
                    "Detected a %ss timer on %s; waiting %ss", seconds, url, wait
                )
                await asyncio.sleep(wait)
                text, final_url = await asyncio.to_thread(_fetch_sync, url)
                if _is_destination(final_url, short_domain):
                    return _clean_url(final_url)
                found = _extract_from_html(text, short_domain)
                if found:
                    return found

        raise BypassError("The page did not expose a destination link.")
    except BypassError:
        raise
    except Exception as exc:
        logger.exception("Static bypass failed for %s", url)
        raise BypassError(
            "The shortener page could not be parsed. It may have changed."
        ) from exc


# ---------------------------------------------------------------------------
# Browser path: Playwright (async)
# ---------------------------------------------------------------------------

_CLICK_TEXT_RE = re.compile(
    r"continue|get ?link|generate(?: link)?|proceed|verify|unlock|"
    r"click here|i'?m a human|submit|download|go to link|open link|"
    r"destination|skip ad|watch|next|visit",
    re.I,
)


async def _accept_dialog(dialog) -> None:
    try:
        await dialog.accept()
    except Exception:
        pass


async def _try_cloudflare_checkbox(page) -> bool:
    """Best-effort click on a Turnstile/Cloudflare checkbox, if present."""
    try:
        frames = page.frames
    except Exception:
        return False

    for frame in frames:
        try:
            if "challenges.cloudflare.com" not in (frame.url or ""):
                continue
        except Exception:
            continue

        for selector in ("input[type='checkbox']", "label"):
            try:
                locator = frame.locator(selector)
                if await locator.count() == 0:
                    continue
                element = locator.first
                if not await element.is_visible():
                    continue
                await element.click(timeout=2000, force=True)
                logger.info("Clicked a Cloudflare challenge checkbox")
                return True
            except Exception:
                continue
    return False


async def _wait_for_cloudflare(page, max_seconds: int = 15) -> None:
    """Wait until a visible Cloudflare interstitial has gone away."""
    deadline = time.monotonic() + max_seconds
    clicked_checkbox = False
    while time.monotonic() < deadline:
        try:
            if not clicked_checkbox:
                clicked_checkbox = await _try_cloudflare_checkbox(page)

            title = ((await page.title()) or "").lower()
            if any(
                token in title
                for token in (
                    "just a moment",
                    "attention required",
                    "checking your browser",
                    "cloudflare",
                )
            ):
                await page.wait_for_timeout(1000)
                continue

            challenge = await page.query_selector(
                "#challenge-form, #challenge-running, .cf-browser-verification, "
                "cf-challenge, [data-translate='checking_browser']"
            )
            if challenge:
                await page.wait_for_timeout(1000)
                continue
            return
        except Exception:
            return


async def _settle(page, network_idle_ms: int = 5000) -> None:
    """Let redirects, scripts and Cloudflare settle before inspecting the DOM."""
    try:
        await page.wait_for_load_state("networkidle", timeout=network_idle_ms)
    except Exception:
        pass
    await _wait_for_cloudflare(page)
    await page.wait_for_timeout(800)


async def _extract_from_page(page, short_domain: str) -> Optional[str]:
    """Read a destination from the current URL, page HTML or its anchors."""
    try:
        if page.is_closed():
            return None
    except Exception:
        pass

    try:
        current = page.url
        if _is_destination(current, short_domain):
            return _clean_url(current)
    except Exception:
        pass

    try:
        content = await page.content()
    except Exception:
        content = ""

    if content:
        found = _extract_from_html(content, short_domain)
        if found:
            return found

    try:
        hrefs = await page.evaluate(
            "Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
        )
    except Exception:
        hrefs = []

    for href in reversed(hrefs or []):
        if _is_destination(str(href), short_domain):
            return _clean_url(str(href))

    return None


async def _click_continue(page) -> bool:
    """Click the first visible button/link that looks like a continue control."""
    selectors = (
        "button",
        "a",
        "input[type='submit']",
        "input[type='button']",
        "[role='button']",
    )

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(await locator.count(), 40)
        except Exception:
            continue

        for index in range(count):
            element = locator.nth(index)
            try:
                if not await element.is_visible():
                    continue

                label = ""
                try:
                    label = (await element.inner_text(timeout=500)) or ""
                except Exception:
                    pass
                try:
                    label += " " + (
                        (await element.get_attribute("value", timeout=300)) or ""
                    )
                except Exception:
                    pass

                if not _CLICK_TEXT_RE.search(label):
                    continue

                try:
                    await element.scroll_into_view_if_needed(timeout=1000)
                except Exception:
                    pass

                try:
                    await element.click(timeout=2500, no_wait_after=True)
                except Exception:
                    await element.click(
                        timeout=2500, force=True, no_wait_after=True
                    )

                logger.info("Clicked control: %r", label.strip()[:60])
                return True
            except Exception:
                continue

    return False


async def _playwright_bypass(
    url: str, short_domain: str, aggressive: bool = False
) -> str:
    """Render the page in headless Chromium and follow the ad gate."""
    if async_playwright is None:
        raise BypassError(
            "Browser support is not installed on the server (Playwright missing)."
        )

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                headless=True, args=BROWSER_ARGS
            )
        except Exception as exc:
            raise BypassError(
                "Chromium could not start. The server is probably missing "
                "browser dependencies."
            ) from exc

        try:
            try:
                context = await browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1366, "height": 768},
                    locale="en-US",
                    ignore_https_errors=True,
                )
                # Reduce the most obvious "automation" fingerprint.
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )

                page = await context.new_page()
                page.set_default_timeout(10_000)
                page.on(
                    "dialog",
                    lambda dialog: asyncio.create_task(_accept_dialog(dialog)),
                )

                logger.info("Opening %s in Chromium", url)
                await page.goto(
                    url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS
                )
                await _settle(page)

                for current_page in list(context.pages):
                    found = await _extract_from_page(current_page, short_domain)
                    if found:
                        return found

                attempts = 8 if aggressive else 6
                for _ in range(attempts):
                    clicked = await _click_continue(page)

                    if clicked:
                        # The original tab can be closed/replaced after a
                        # click, so ignore navigation errors here and inspect
                        # all pages (including popups) afterwards.
                        try:
                            await page.wait_for_timeout(2000)
                        except Exception:
                            pass
                        try:
                            await page.wait_for_load_state(
                                "domcontentloaded", timeout=4000
                            )
                        except Exception:
                            pass
                    else:
                        await page.wait_for_timeout(1200)

                    # A click may have opened the destination in a new tab.
                    # Check every page in the context, not just the original.
                    for current_page in list(context.pages):
                        try:
                            found = await _extract_from_page(
                                current_page, short_domain
                            )
                        except Exception:
                            found = None
                        if found:
                            return found

                raise BypassError(
                    "The destination link could not be found. The shortener may "
                    "have changed its page or is blocking automated access."
                )
            except BypassError:
                raise
            except PlaywrightTimeoutError as exc:
                raise BypassError(
                    "The shortener page took too long to load. Please try again."
                ) from exc
            except Exception as exc:
                logger.exception("Browser bypass failed for %s", url)
                raise BypassError(
                    "The page could not be processed in the browser. It may be "
                    "blocking automated access."
                ) from exc
        finally:
            # Always release Chromium, including on cancellation/timeouts.
            try:
                await browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public bypass functions
# ---------------------------------------------------------------------------

async def bypass_arolinks(url: str) -> str:
    """Bypass an arolinks.com link.  Returns the destination URL."""
    url = normalize_url(url)
    if get_supported_domain(url) != "arolinks.com":
        raise BypassError("That link does not belong to arolinks.com.")

    # Fast path first: most arolinks pages leak the target in the HTML.
    try:
        return await asyncio.wait_for(
            _static_bypass(url, "arolinks.com", wait_for_timer=True),
            timeout=HTTP_TIMEOUT * 2 + 10,
        )
    except (BypassError, asyncio.TimeoutError) as exc:
        logger.info("arolinks.com static attempt failed: %s", exc)

    # Fallback: let JavaScript run the countdown and click through.
    try:
        return await asyncio.wait_for(
            _playwright_bypass(url, "arolinks.com"), timeout=TOTAL_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        raise BypassError(
            "arolinks.com took too long to respond. Please try again later."
        ) from exc


async def bypass_vplink(url: str) -> str:
    """Bypass a vplink.in link.  Returns the destination URL."""
    url = normalize_url(url)
    if get_supported_domain(url) != "vplink.in":
        raise BypassError("That link does not belong to vplink.in.")

    primary_error: Optional[BypassError] = None

    # Playwright is the primary method: vplink is Cloudflare-protected and
    # uses JavaScript/ad intermediates.
    try:
        return await asyncio.wait_for(
            _playwright_bypass(url, "vplink.in", aggressive=True),
            timeout=TOTAL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        primary_error = BypassError(
            "vplink.in took too long to load. Please try again later."
        )
    except BypassError as exc:
        primary_error = exc

    logger.info(
        "vplink.in browser attempt failed (%s); trying cloudscraper", primary_error
    )

    try:
        return await asyncio.wait_for(
            _static_bypass(url, "vplink.in"), timeout=HTTP_TIMEOUT + 15
        )
    except (BypassError, asyncio.TimeoutError) as exc:
        logger.info("vplink.in static attempt failed: %s", exc)
        raise primary_error or BypassError(
            "Could not bypass this vplink.in link."
        ) from exc


async def bypass(url: str) -> str:
    """Validate *url* and dispatch to the correct shortener implementation."""
    url = normalize_url(url)
    domain = get_supported_domain(url)

    if domain == "arolinks.com":
        return await bypass_arolinks(url)
    if domain == "vplink.in":
        return await bypass_vplink(url)

    raise BypassError(
        "Unsupported domain. I can only bypass arolinks.com and vplink.in links."
    )
