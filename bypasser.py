"""
bypasser.py
===========

Browser-based bypass logic for arolinks.com and vplink.in.

Why this version exists
-----------------------
The old implementation used static HTTP requests plus BeautifulSoup/regex.  That
works for simple redirect pages, but arolinks.com and vplink.in hide the real
destination behind JavaScript countdowns and a chain of advertising domains:

    vplink.in -> onlinewish.in -> crimejasoos.in -> real destination

Static parsing stops at the first ad wall and returns an intermediate URL, which
is why users still saw ads.  This module therefore drives a real Chromium
browser through the whole funnel and only returns a URL once it has escaped the
shortener and its known ad partners.

Public API
----------
    bypass_arolinks(url) -> str
    bypass_vplink(url)   -> str
    bypass(url)          -> str   (validates the domain and dispatches)

All public functions raise ``BypassError`` with a message that is safe to show
to an end user.

Maintenance
-----------
Shortener sites change their markup frequently.  If a link stops working,
update ``CLICK_SELECTORS`` (near the top of this file) and
``BLACKLIST_DOMAINS``.  The navigation loop itself is intentionally generic so
new ad-partner domains can be added without touching the loop.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import os
import random
import re
import time
from typing import Optional
from urllib.parse import urlparse

import cloudscraper
from bs4 import BeautifulSoup

# Railway installs Chromium into /app so it is present in the runtime image.
# Locally that directory does not exist and Playwright uses its normal cache.
_RAILWAY_BROWSERS = "/app/.cache/ms-playwright"
if os.path.isdir(_RAILWAY_BROWSERS):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _RAILWAY_BROWSERS)

try:  # Keep the bot bootable even if only the Python package is missing.
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - only on broken installs
    async_playwright = None

    class PlaywrightTimeoutError(Exception):
        """Placeholder so ``except PlaywrightTimeoutError`` still works."""


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SUPPORTED_DOMAINS = ("arolinks.com", "vplink.in")

# These hosts are NEVER a valid destination.  The bot keeps navigating until
# the current URL is outside this list.  Add new ad-funnel domains here.
BLACKLIST_DOMAINS = [
    "arolinks.com",
    "vplink.in",
    "onlinewish.in",
    "crimejasoos.in",
]

# Tried in this order on every navigation-loop pass.  If a site changes its
# button text or IDs, update this list first -- nothing else in the loop needs
# to change.
CLICK_SELECTORS = [
    'button:has-text("Continue")',
    'button:has-text("Get Link")',
    'button:has-text("Skip Ad")',
    'a:has-text("Continue")',
    'a:has-text("Get Link")',
    "#btn6",       # common arolinks download button ID
    "#getlink",    # common vplink-style get-link button ID
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1920, "height": 1080}

NAV_TIMEOUT_MS = 30_000       # per navigation, as required by the spec
MAX_ITERATIONS = 8            # hard cap for the navigation loop
WAIT_MIN_MS = 4_000           # let JS countdowns / Cloudflare settle
WAIT_MAX_MS = 6_000
STABLE_SECONDS = 3.0          # URL must stop changing for this long
TOTAL_BYPASS_TIMEOUT = 180    # seconds, outer safety net for one link
FAST_PATH_TIMEOUT = 25        # seconds, cloudscraper request

# Chromium refuses to run as root in a container without --no-sandbox.
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--no-first-run",
    "--no-default-browser-check",
]

_META_REFRESH_RE = re.compile(r"""url\s*=\s*["']?([^"'\s>]+)""", re.I)
_JS_LOCATION_RE = re.compile(
    r"""(?:window\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']""", re.I
)


# ---------------------------------------------------------------------------
# Errors and URL helpers
# ---------------------------------------------------------------------------

class BypassError(Exception):
    """Raised when a link cannot be bypassed.  The message is user-facing."""


def normalize_url(raw: str) -> str:
    """Trim user input and add ``https://`` when no scheme was supplied."""
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
    """Return the lowercase hostname of *url* (or an empty string)."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def get_supported_domain(url: str) -> Optional[str]:
    """Return the supported shortener domain for *url*, or ``None``."""
    host = extract_domain(url)
    for domain in SUPPORTED_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return None


def is_supported_url(url: str) -> bool:
    return get_supported_domain(url) is not None


def _is_blacklisted(url_or_host: str) -> bool:
    """True when a URL/hostname belongs to a shortener or known ad partner."""
    value = (url_or_host or "").strip().lower()
    if not value:
        return True

    if "://" in value:
        try:
            host = (urlparse(value).hostname or "").lower().rstrip(".")
        except ValueError:
            return True
    else:
        host = value.rstrip(".")

    if not host:
        return True

    for domain in BLACKLIST_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _is_final_url(url: str) -> bool:
    """True only for a valid http(s) URL outside the ad-funnel blacklist."""
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False

    return not _is_blacklisted(parsed.hostname)


def _clean_candidate(value: str) -> str:
    """Normalize escaping/punctuation around a URL extracted from HTML."""
    if not isinstance(value, str):
        return ""

    value = html_lib.unescape(value)
    value = (
        value.replace("\\/", "/")
        .replace("\\u002f", "/")
        .replace("\\u002F", "/")
        .replace("\\u003a", ":")
        .replace("\\u003A", ":")
    )
    value = value.strip().strip("\"'<> \n\r\t,;.")
    if value.startswith("//"):
        value = "https:" + value
    return value


# ---------------------------------------------------------------------------
# Fast path for arolinks.com
# ---------------------------------------------------------------------------

def _fast_path_sync(url: str) -> Optional[str]:
    """
    Try to recover the destination from the raw HTML with cloudscraper.

    This is only used for arolinks.com.  We return a candidate ONLY if it is
    present in a meta refresh or ``window.location.href`` assignment and is not
    blacklisted.  Anything else falls through to Playwright.
    """
    headers = {
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

    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        response = scraper.get(
            url, headers=headers, timeout=FAST_PATH_TIMEOUT, allow_redirects=True
        )
    except Exception as exc:
        logger.info("arolinks fast path could not fetch %s: %s", url, exc)
        return None

    if response.status_code >= 400 or not response.text:
        logger.info("arolinks fast path got HTTP %s", response.status_code)
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # 1. <meta http-equiv="refresh" content="5;url=https://...">
    for meta in soup.find_all("meta"):
        if "refresh" not in (meta.get("http-equiv") or "").lower():
            continue
        match = _META_REFRESH_RE.search(meta.get("content") or "")
        if not match:
            continue
        candidate = _clean_candidate(match.group(1))
        if _is_final_url(candidate):
            logger.info("arolinks fast path found meta refresh: %s", candidate)
            return candidate

    # 2. window.location.href = "https://..."
    scripts = "\n".join(
        script.get_text() or "" for script in soup.find_all("script")
    )
    scripts = scripts.replace("\\/", "/")
    for match in _JS_LOCATION_RE.finditer(scripts):
        candidate = _clean_candidate(match.group(1))
        if _is_final_url(candidate):
            logger.info("arolinks fast path found JS location: %s", candidate)
            return candidate

    return None


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------

async def _accept_dialog(dialog) -> None:
    try:
        await dialog.accept()
    except Exception:
        pass


def _current_page(context, page):
    """Return *page*, or the newest open page if it was closed by a click."""
    try:
        if page is not None and not page.is_closed():
            return page
    except Exception:
        pass

    open_pages = [p for p in context.pages if not p.is_closed()]
    return open_pages[-1] if open_pages else page


async def _try_cloudflare_checkbox(page) -> bool:
    """
    Best-effort click on a Cloudflare Turnstile checkbox.

    Managed challenges often clear on their own once JavaScript runs; this only
    helps when an actual checkbox frame is rendered.
    """
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


async def _wait_for_stable_url(page, seconds: float = STABLE_SECONDS) -> str:
    """
    Wait until ``page.url`` has not changed for *seconds*.

    This prevents returning a URL in the middle of a multi-hop redirect, which
    was one of the original bugs ("part of the link").
    """
    try:
        last = page.url
    except Exception:
        return ""

    stable_since = time.monotonic()
    deadline = stable_since + seconds + 12

    while time.monotonic() < deadline:
        try:
            await page.wait_for_timeout(500)
            current = page.url
        except Exception:
            return last

        if current != last:
            last = current
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= seconds:
            return last

    return last


async def _click_next_control(page) -> bool:
    """
    Click the first visible control from ``CLICK_SELECTORS``.

    Selectors are attempted in order and failures are best-effort: a missing
    or hidden element simply moves on to the next selector.
    """
    for selector in CLICK_SELECTORS:
        try:
            locator = page.locator(selector)
            if await locator.count() == 0:
                continue

            element = locator.first
            if not await element.is_visible():
                continue

            try:
                await element.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass

            try:
                await element.click(timeout=3000, no_wait_after=True)
            except Exception:
                await element.click(
                    timeout=3000, force=True, no_wait_after=True
                )

            logger.info("Clicked selector: %s", selector)
            return True
        except Exception:
            continue

    return False


async def _find_destination_link(page) -> Optional[str]:
    """
    Fallback: inspect the page's anchors for a non-blacklisted destination.

    Anchors are ranked so that visible "Download", "Continue", "Here" or
    "Get Link" text wins over generic navigation links.
    """
    try:
        anchors = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
                href: a.href,
                text: (a.innerText || a.textContent || '').trim()
            }))
            """
        )
    except Exception:
        return None

    page_host = extract_domain(getattr(page, "url", "") or "")
    best_href: Optional[str] = None
    best_score = -1

    for anchor in anchors or []:
        href = _clean_candidate(str(anchor.get("href") or ""))
        if not href.startswith(("http://", "https://")):
            continue
        if not _is_final_url(href):
            continue

        text = str(anchor.get("text") or "").lower()
        score = 0

        if any(
            keyword in text
            for keyword in ("download", "continue", "here", "get link", "open")
        ):
            score += 10
        if "download" in href.lower():
            score += 3
        if page_host and extract_domain(href) != page_host:
            score += 2

        if score > best_score:
            best_score = score
            best_href = href

    return best_href


# ---------------------------------------------------------------------------
# Core browser navigation: escape the ad funnel
# ---------------------------------------------------------------------------

async def _escape_ad_funnel(url: str) -> str:
    """
    Open *url* in Chromium and keep navigating until the URL leaves every
    blacklisted shortener/ad-partner domain and has stabilized.
    """
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
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport=VIEWPORT,
                locale="en-US",
                ignore_https_errors=True,
            )
            # Remove the most obvious automation fingerprint.
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
            try:
                await page.goto(
                    url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                # Do not abort: the funnel may already be rendering.
                logger.warning("Initial navigation timed out for %s", url)
            except Exception as exc:
                raise BypassError(
                    "The shortener page could not be opened. It may be down."
                ) from exc

            await _try_cloudflare_checkbox(page)

            # -----------------------------------------------------------------
            # NAVIGATION LOOP
            #
            # On every pass:
            #   1. Wait 4-6 seconds so JS countdowns / Cloudflare can finish.
            #   2. Check every open tab.  If the URL is outside the blacklist
            #      AND has been stable for 3 seconds, we have escaped.
            #   3. Otherwise click the next "Continue"/"Get Link"/"Skip Ad"
            #      control and wait for the next page to load.
            #
            # If a site changes, the only part to edit is CLICK_SELECTORS at
            # the top of this file (and BLACKLIST_DOMAINS for new ad hosts).
            # -----------------------------------------------------------------
            for iteration in range(1, MAX_ITERATIONS + 1):
                page = _current_page(context, page)

                try:
                    await page.wait_for_timeout(
                        random.randint(WAIT_MIN_MS, WAIT_MAX_MS)
                    )
                except Exception:
                    page = _current_page(context, page)

                for candidate_page in list(context.pages):
                    try:
                        if candidate_page.is_closed():
                            continue
                        candidate_url = candidate_page.url
                    except Exception:
                        continue

                    if not _is_final_url(candidate_url):
                        continue

                    stable_url = await _wait_for_stable_url(
                        candidate_page, STABLE_SECONDS
                    )
                    if _is_final_url(stable_url):
                        logger.info(
                            "Escaped the ad funnel after %d iteration(s): %s",
                            iteration,
                            stable_url,
                        )
                        return stable_url

                clicked = await _click_next_control(page)
                if clicked:
                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=NAV_TIMEOUT_MS
                        )
                    except Exception:
                        # Popups / SPA transitions may not fire a load event.
                        pass
                else:
                    logger.info(
                        "Iteration %d: no clickable control found", iteration
                    )

            # Final verification after the loop.
            page = _current_page(context, page)
            for candidate_page in list(context.pages):
                try:
                    if candidate_page.is_closed():
                        continue
                    stable_url = await _wait_for_stable_url(
                        candidate_page, STABLE_SECONDS
                    )
                except Exception:
                    continue
                if _is_final_url(stable_url):
                    return stable_url

            # Last resort: a destination anchor on the blocked page.
            fallback = await _find_destination_link(page)
            if fallback and _is_final_url(fallback):
                logger.info("Using destination anchor fallback: %s", fallback)
                return fallback

            raise BypassError(
                "Could not escape the ad funnel — the shortener may have "
                "changed its layout."
            )
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
    """Bypass an arolinks.com link and return the final destination URL."""
    url = normalize_url(url)
    if get_supported_domain(url) != "arolinks.com":
        raise BypassError("That link does not belong to arolinks.com.")

    # Optional fast path: many arolinks pages leak the target in raw HTML.
    try:
        fast_result = await asyncio.wait_for(
            asyncio.to_thread(_fast_path_sync, url), timeout=FAST_PATH_TIMEOUT + 5
        )
        if fast_result and _is_final_url(fast_result):
            return fast_result
    except Exception as exc:
        logger.info("arolinks.com fast path skipped: %s", exc)

    try:
        return await asyncio.wait_for(
            _escape_ad_funnel(url), timeout=TOTAL_BYPASS_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        raise BypassError(
            "arolinks.com took too long to respond. Please try again later."
        ) from exc


async def bypass_vplink(url: str) -> str:
    """Bypass a vplink.in link and return the final destination URL."""
    url = normalize_url(url)
    if get_supported_domain(url) != "vplink.in":
        raise BypassError("That link does not belong to vplink.in.")

    # vplink.in is Cloudflare-protected and uses a multi-hop ad funnel.
    # Static HTTP is unreliable here, so go straight to Playwright.
    try:
        return await asyncio.wait_for(
            _escape_ad_funnel(url), timeout=TOTAL_BYPASS_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        raise BypassError(
            "vplink.in took too long to load. Please try again later."
        ) from exc


async def bypass(url: str) -> str:
    """Validate *url* and dispatch to the matching shortener implementation."""
    url = normalize_url(url)
    domain = get_supported_domain(url)

    if domain == "arolinks.com":
        return await bypass_arolinks(url)
    if domain == "vplink.in":
        return await bypass_vplink(url)

    raise BypassError(
        "Unsupported domain. I can only bypass arolinks.com and vplink.in links."
    )
