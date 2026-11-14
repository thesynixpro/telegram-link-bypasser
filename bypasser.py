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
# the current URL is outside this list.  The ad-partner domains rotate, so this
# list is only the first line of defence: `_page_is_ad_gate()` also inspects the
# page markup so new funnel domains are not returned as the final URL.
BLACKLIST_DOMAINS = [
    "arolinks.com",
    "vplink.in",
    "onlinewish.in",
    "crimejasoos.in",
    "pipefitter.in",
    "apnahirework.com",
    "entiredust.in",
]

# Tried in this order on every navigation-loop pass.  Real controls come first;
# generic article links come last so an ad "Continue" button cannot win over
# the actual Verify/Get Link control.  #btn7 is handled separately (its href is
# followed directly, bypassing its ad-opening onclick handler).
CLICK_SELECTORS = [
    "#btn6",                     # arolinks Verify button
    "#getlink",                  # common vplink-style get-link button ID
    "#go-submit",
    'button:has-text("Verify")',
    'button:has-text("Get Link")',
    'button:has-text("Skip Ad")',
    'a:has-text("Get Link")',
    'button:has-text("Continue")',
    'a:has-text("Continue")',
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
TOTAL_BYPASS_TIMEOUT = 240    # seconds, outer safety net for one link

# Chromium refuses to run as root in a container without --no-sandbox.
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
]

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
# Browser helpers
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

        for selector in (
            "input[type='checkbox']",
            "div[role='checkbox']",
            "label",
        ):
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
# Ad-gate detection and arolinks final-gate handling
# ---------------------------------------------------------------------------

async def _page_is_ad_gate(page) -> bool:
    """
    True when the current page is an advertising gate, not a destination.

    The static blacklist cannot cover every rotating ad domain (pipefitter.in,
    apnahirework.com, entiredust.in, ...).  These markers are the giveaway:
    countdown/Verify/Continue screens, ad cookies, and the /readmore hop.
    """
    try:
        html = (await page.content()).lower()
    except Exception:
        return False

    strong_markers = (
        'id="btn6"',
        "id='btn6'",
        'id="btn7"',
        "id='btn7'",
        "adcadg",
        "ce-wait",
        "specialactionhandler",
        'id="go-link"',
        'id="get-link"',
        "aroshield.setup",
        "your link is almost ready",
    )
    if any(marker in html for marker in strong_markers):
        return True

    try:
        parsed = urlparse(page.url)
    except Exception:
        return False

    if "/readmore" in (parsed.path or "").lower():
        return True
    if re.search(r"(?:^|&)(?:st|pgtr)=\d+", (parsed.query or "").lower()):
        return True

    return False


async def _is_arolinks_gate(page) -> bool:
    """True when the arolinks final verification/captcha gate is loaded."""
    try:
        html = (await page.content()).lower()
    except Exception:
        return False

    if "aroshield.setup" in html:
        return True
    return "/links/go" in html and "cf-turnstile" in html


async def _get_ad_referer(page, short_url: str) -> Optional[str]:
    """
    Find the ad-partner URL the shortener redirected to.

    arolinks' final gate is only disclosed when the request's Referer points at
    one of its ad partners.  The redirect target is either the current page URL
    or the window.location.href assignment in the initial HTML.
    """
    short_host = extract_domain(short_url)

    try:
        current = page.url
    except Exception:
        current = ""

    def _origin(value: str) -> Optional[str]:
        try:
            parsed = urlparse(value)
        except ValueError:
            return None
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        return f"{parsed.scheme}://{parsed.netloc}/"

    if current and current.startswith(("http://", "https://")):
        host = extract_domain(current)
        if host and host != short_host and not host.endswith("." + short_host):
            return _origin(current)

    try:
        html = await page.content()
    except Exception:
        return None

    for match in _JS_LOCATION_RE.finditer(html.replace("\\/", "/")):
        candidate = _clean_candidate(match.group(1))
        host = extract_domain(candidate)
        if host and host != short_host:
            origin = _origin(candidate)
            if origin:
                return origin

    return None


async def _navigate_readmore(page) -> bool:
    """
    Follow the real `/readmore` hop directly.

    On pipefitter-style gates, #btn7 looks like a Continue button, but its
    onclick opens an ad tab and returns false.  Navigating to its href skips
    the popup and advances the funnel.
    """
    target = None
    try:
        href = await page.evaluate(
            """
            () => {
                const btn7 = document.querySelector('#btn7[href]');
                if (btn7 && btn7.getAttribute('href')) return btn7.href;
                const link = document.querySelector(
                    'a[href*="/readmore"], a[href*="readmore"]'
                );
                return link ? link.href : null;
            }
            """
        )
        if href:
            target = _clean_candidate(str(href))
    except Exception:
        target = None

    if not target or target.startswith("javascript:"):
        return False

    try:
        logger.info("Following gate hop: %s", target)
        await page.goto(
            target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
        )
        return True
    except Exception as exc:
        logger.info("Could not follow %s: %s", target, exc)
        return False


async def _turnstile_token(page) -> str:
    """Return the current Cloudflare Turnstile token, if the widget solved."""
    try:
        return (
            await page.evaluate(
                """
                () => {
                    const input = document.querySelector(
                        'input[name="cf-turnstile-response"]'
                    );
                    return input ? input.value : '';
                }
                """
            )
            or ""
        )
    except Exception:
        return ""


async def _capture_links_go(response, captured: dict) -> None:
    """Store the destination URL returned by arolinks' POST /links/go."""
    try:
        if "/links/go" not in (response.url or ""):
            return
    except Exception:
        return

    try:
        data = await response.json()
    except Exception:
        data = None

    if isinstance(data, dict):
        url = data.get("url") or data.get("link") or data.get("destination")
        if isinstance(url, str):
            cleaned = _clean_candidate(url)
            if cleaned and cleaned != "#" and _is_final_url(cleaned):
                captured["url"] = cleaned
                return

    try:
        location = response.headers.get("location")
    except Exception:
        location = None
    if location and _is_final_url(location):
        captured["url"] = _clean_candidate(location)


async def _solve_arolinks_gate(page, captured: dict) -> Optional[str]:
    """
    Complete the arolinks "Your link is almost ready" gate.

    The page renders an explicit Cloudflare Turnstile, waits 8 seconds
    (AroShield), then jQuery-submits #go-link to /links/go.  That endpoint
    answers with JSON `{url: ...}`.  Chromium must be used because Cloudflare
    challenges the POST otherwise.

    #get-link is only a label (href="javascript:void(0)"); clicking it triggers
    an ad-overlay click handler that can hijack the tab, so submission is
    driven through the form instead.
    """
    if not await _is_arolinks_gate(page):
        return None

    logger.info("arolinks final gate detected; waiting for verification")

    async def form_ready() -> bool:
        try:
            return bool(
                await page.evaluate(
                    """
                    () => {
                        const f = document.getElementById('go-link');
                        return !!f && f.classList.contains('go-link');
                    }
                    """
                )
            )
        except Exception:
            return False

    async def submit_go_form() -> None:
        try:
            await page.evaluate(
                """
                () => {
                    const f = document.getElementById('go-link');
                    if (!f) return;
                    if (window.jQuery) window.jQuery(f).submit();
                    else if (f.requestSubmit) f.requestSubmit();
                    else f.submit();
                }
                """
            )
            logger.info("Submitted arolinks #go-link form")
        except Exception:
            pass

    # 1. Wait for Turnstile to produce a token (click the checkbox if shown).
    token_deadline = time.monotonic() + 45
    while time.monotonic() < token_deadline:
        if captured.get("url"):
            return captured["url"]
        await _try_cloudflare_checkbox(page)
        if await _turnstile_token(page):
            logger.info("Turnstile token obtained")
            break
        await page.wait_for_timeout(1000)

    # 2. Wait for AroShield's countdown to finish, then submit (the page often
    #    auto-submits on its own; this is just a guaranteed trigger).
    countdown_deadline = time.monotonic() + 20
    while time.monotonic() < countdown_deadline:
        if captured.get("url"):
            return captured["url"]
        if await form_ready():
            break
        await page.wait_for_timeout(1000)

    for attempt in range(3):
        if captured.get("url"):
            return captured["url"]

        if await _turnstile_token(page):
            await submit_go_form()

        # 3. Wait for the AJAX response carrying the destination.
        for _ in range(15):
            if captured.get("url"):
                return captured["url"]
            try:
                current = page.url
            except Exception:
                current = ""
            if _is_final_url(current) and not await _page_is_ad_gate(page):
                return current
            await page.wait_for_timeout(1000)

        # A token is single-use: refresh the widget and try again.
        logger.info("No /links/go answer yet; refreshing Turnstile")
        try:
            await page.click("#aro-ts-refresh", timeout=3000)
        except Exception:
            try:
                await page.evaluate(
                    "() => { if (window.turnstile) window.turnstile.reset(); }"
                )
            except Exception:
                pass
        await page.wait_for_timeout(3000)

    return captured.get("url")


# ---------------------------------------------------------------------------
# Core browser navigation: escape the ad funnel
# ---------------------------------------------------------------------------

async def _find_gate_page(context):
    """Return the first open tab showing the arolinks final gate."""
    for candidate in list(context.pages):
        try:
            if candidate.is_closed():
                continue
            if await _is_arolinks_gate(candidate):
                return candidate
        except Exception:
            continue
    return None


async def _escape_ad_funnel(url: str) -> str:
    """
    Open *url* in Chromium and keep navigating until the URL leaves every
    blacklisted shortener/ad-partner domain and has stabilized.
    """
    if async_playwright is None:
        raise BypassError(
            "Browser support is not installed on the server (Playwright missing)."
        )

    captured: dict[str, str] = {}

    async with async_playwright() as playwright:
        try:
            # channel="chromium" selects Chrome's new headless mode (Playwright
            # 1.49+). It looks much more like a normal browser to Cloudflare
            # than the legacy headless shell. Fall back if unavailable.
            try:
                browser = await playwright.chromium.launch(
                    channel="chromium", headless=True, args=BROWSER_ARGS
                )
            except Exception:
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
            # Remove the most obvious automation fingerprints.
            await context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                Object.defineProperty(navigator, 'hardwareConcurrency', {
                    get: () => 8
                });
                Object.defineProperty(navigator, 'deviceMemory', {
                    get: () => 8
                });
                window.chrome = window.chrome || {runtime: {}};

                if (navigator.permissions && navigator.permissions.query) {
                    const originalQuery = navigator.permissions.query.bind(
                        navigator.permissions
                    );
                    navigator.permissions.query = (parameters) => (
                        parameters && parameters.name === 'notifications'
                            ? Promise.resolve({state: Notification.permission})
                            : originalQuery(parameters)
                    );
                }

                try {
                    const getParameter =
                        WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function (p) {
                        if (p === 37445) return 'Intel Inc.';
                        if (p === 37446) return 'Intel Iris OpenGL Engine';
                        return getParameter.apply(this, arguments);
                    };
                } catch (e) {}
                """
            )

            def _watch_responses(target_page) -> None:
                target_page.on(
                    "response",
                    lambda response: asyncio.ensure_future(
                        _capture_links_go(response, captured)
                    ),
                )

            context.on("page", _watch_responses)

            page = await context.new_page()
            _watch_responses(page)
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

            # -----------------------------------------------------------------
            # arolinks shortcut.
            #
            # After the first hop the shortener knows which ad partner sent the
            # visitor.  Re-requesting the short URL with that partner as the
            # Referer skips the article chain and returns the final gate
            # directly.  Without it the server serves a cheap redirect page.
            # -----------------------------------------------------------------
            if get_supported_domain(url) == "arolinks.com":
                ad_referer = await _get_ad_referer(page, url)
                if ad_referer:
                    logger.info("Reopening gate with Referer %s", ad_referer)
                    try:
                        await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT_MS,
                            referer=ad_referer,
                        )
                    except Exception as exc:
                        logger.info("Gate navigation failed: %s", exc)

                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=10_000
                        )
                    except Exception:
                        pass

                    gate_url = await _solve_arolinks_gate(page, captured)
                    if gate_url and _is_final_url(gate_url):
                        logger.info("Gate returned destination: %s", gate_url)
                        return gate_url

            await _try_cloudflare_checkbox(page)

            # -----------------------------------------------------------------
            # NAVIGATION LOOP
            #
            # On every pass:
            #   1. Solve the final arolinks gate if it is open in any tab.
            #   2. Wait 4-6 seconds so JS countdowns / Cloudflare can finish.
            #   3. Check every open tab.  If the URL is outside the blacklist,
            #      is not an ad gate and has been stable for 3 seconds, we have
            #      escaped.
            #   4. Follow a real /readmore hop when present.
            #   5. Otherwise click the next Verify/Get Link/Continue control.
            #
            # If a site changes, update CLICK_SELECTORS and BLACKLIST_DOMAINS
            # at the top of this file.
            # -----------------------------------------------------------------
            for iteration in range(1, MAX_ITERATIONS + 1):
                page = _current_page(context, page)

                gate_page = await _find_gate_page(context)
                if gate_page is not None:
                    gate_url = await _solve_arolinks_gate(gate_page, captured)
                    if gate_url and _is_final_url(gate_url):
                        logger.info("Gate returned destination: %s", gate_url)
                        return gate_url
                if captured.get("url") and _is_final_url(captured["url"]):
                    return captured["url"]

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
                    if await _page_is_ad_gate(candidate_page):
                        continue

                    stable_url = await _wait_for_stable_url(
                        candidate_page, STABLE_SECONDS
                    )
                    if not _is_final_url(stable_url):
                        continue
                    if await _page_is_ad_gate(candidate_page):
                        continue

                    logger.info(
                        "Escaped the ad funnel after %d iteration(s): %s",
                        iteration,
                        stable_url,
                    )
                    return stable_url

                if await _navigate_readmore(page):
                    continue

                # The arolinks gate auto-submits; its anchors carry an ad
                # overlay that hijacks clicks, so never click them.
                if await _is_arolinks_gate(page):
                    continue

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
            if captured.get("url") and _is_final_url(captured["url"]):
                return captured["url"]

            page = _current_page(context, page)
            for candidate_page in list(context.pages):
                try:
                    if candidate_page.is_closed():
                        continue
                    if await _page_is_ad_gate(candidate_page):
                        continue
                    stable_url = await _wait_for_stable_url(
                        candidate_page, STABLE_SECONDS
                    )
                except Exception:
                    continue
                if _is_final_url(stable_url):
                    return stable_url

            # Last resort: a destination anchor on the blocked page.
            if not await _page_is_ad_gate(page):
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

    # No static fast path: arolinks hides the destination behind a Turnstile
    # gate, and the pre-gate redirect only leads to rotating ad articles.
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
