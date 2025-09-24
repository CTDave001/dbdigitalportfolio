import asyncio
import time
from typing import List, Optional

# --- Selenium / Chrome ---
from selenium import webdriver
from selenium.common.exceptions import WebDriverException, TimeoutException, NoSuchWindowException, InvalidSessionIdException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# --- OBS WebSocket v5 ---
import simpleobsws


# ===========================
# CONFIG — EDIT IF NEEDED
# ===========================
URLS: List[str] = [
    "https://dbdigitalco.com",
    "https://rivercrest-roofing.webflow.io",
    "https://pipesmith-plumbing.webflow.io",
    "https://liberty-homes-117db2.webflow.io",  # <- problematic one
    "https://primos-painting.webflow.io",
    "https://a-to-z-hvac-services.webflow.io",
    "https://tms-auto-detailing.webflow.io",
]

OBS_HOST = "127.0.0.1"
OBS_PORT = 4455
OBS_PASSWORD = "u0MbOgz71UoHk3N3"

OBS_SCENE_NAME = "REC"                  # your scene
WINDOW_CAPTURE_SOURCE = "ChromeApp"     # Window Capture source name in that scene
BLANK_SCENE_NAME = ""                   # optional "Blank" scene for extra rebind ("" to disable)

# Scrolling feel
SCROLL_PIXELS = 16
SCROLL_DELAY_S = 0.028
BOUNCE_AT_END = True
BOUNCE_PIXELS = 120

# Timing & reliability
MAX_SCROLL_SECONDS = 25
SETTLE_BEFORE_S = 1.2
SETTLE_AFTER_S = 0.7
REBIND_WAIT_S = 1.0     # after OBS rebind
PREROLL_AT_TOP_S = 0.75 # hold at top after record starts

PAGE_LOAD_TIMEOUT_S = 25
DOM_READY_TIMEOUT_S = 18
RETRIES_PER_URL = 2     # total attempts per URL (will recreate browser between attempts)

# Per-site strategies (overrides)
SITE_STRATEGY = {
    # The Webflow site that sometimes misbehaves:
    "liberty-homes-117db2.webflow.io": {
        "page_load_timeout": 40,     # wait longer for first byte
        "dom_ready_timeout": 30,     # wait longer for readyState
        "two_stage_nav": True,       # about:blank -> assign location via JS
        "extra_rebind_wait": 0.6,    # extra time for OBS to hook
        "pre_scroll_pause": 0.5,     # add to PREROLL_AT_TOP_S
    }
}
# ===========================
# END CONFIG
# ===========================


def make_chrome() -> webdriver.Chrome:
    """
    Launch Chrome in kiosk (true fullscreen) & 'app' mode so only webpage is visible.
    Flags reduce GPU/ANGLE quirks and random popups.
    """
    opts = Options()
    opts.add_argument("--kiosk")
    opts.add_argument("--app=data:text/html,")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-features=UseAngle,Translate,PermissionChip,AutofillServerCommunication")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    opts.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_S)
    return driver


# ----- OBS helpers -----
async def obs_connect() -> simpleobsws.WebSocketClient:
    params = simpleobsws.IdentificationParameters()
    client = simpleobsws.WebSocketClient(
        url=f"ws://{OBS_HOST}:{OBS_PORT}",
        password=OBS_PASSWORD,
        identification_parameters=params,
    )
    await client.connect()
    await client.wait_until_identified()
    return client

async def _call_or_raise(client: simpleobsws.WebSocketClient, req: simpleobsws.Request, action_name: str):
    resp = await client.call(req)
    if not resp.ok():
        raise RuntimeError(f"{action_name} failed: {resp.requestStatus}")
    return resp

async def obs_set_scene(client: simpleobsws.WebSocketClient, scene_name: str):
    if not scene_name:
        return
    await _call_or_raise(client, simpleobsws.Request("SetCurrentProgramScene", {"sceneName": scene_name}), "SetCurrentProgramScene")

async def obs_start_record(client: simpleobsws.WebSocketClient):
    resp = await client.call(simpleobsws.Request("StartRecord"))
    if not resp.ok():
        status = resp.requestStatus or {}
        comment = str(status.get("comment", "")).lower().strip()
        if comment not in {"output already active", "recording already active"}:
            raise RuntimeError(f"StartRecord failed: {status}")

async def obs_stop_record(client: simpleobsws.WebSocketClient):
    resp = await client.call(simpleobsws.Request("StopRecord"))
    if not resp.ok():
        status = resp.requestStatus or {}
        comment = str(status.get("comment", "")).lower().strip()
        if comment not in {"output not active", "recording not active"}:
            raise RuntimeError(f"StopRecord failed: {status}")

async def get_scene_item_id(client: simpleobsws.WebSocketClient, scene_name: str, source_name: str) -> int:
    resp = await _call_or_raise(
        client,
        simpleobsws.Request("GetSceneItemId", {"sceneName": scene_name, "sourceName": source_name, "searchOffset": 0}),
        "GetSceneItemId",
    )
    return int(resp.responseData["sceneItemId"])

async def toggle_scene_item_visibility(client: simpleobsws.WebSocketClient, scene_name: str, scene_item_id: int, enabled: bool):
    await _call_or_raise(
        client,
        simpleobsws.Request("SetSceneItemEnabled", {"sceneName": scene_name, "sceneItemId": scene_item_id, "sceneItemEnabled": enabled}),
        "SetSceneItemEnabled",
    )

async def refresh_window_capture_binding(client: simpleobsws.WebSocketClient, scene_name: str, source_name: str):
    """Blink Window Capture off→on so OBS rebinds to the current browser window."""
    scene_item_id = await get_scene_item_id(client, scene_name, source_name)
    await toggle_scene_item_visibility(client, scene_name, scene_item_id, False)
    await asyncio.sleep(0.2)
    await toggle_scene_item_visibility(client, scene_name, scene_item_id, True)

async def scene_blink(client: simpleobsws.WebSocketClient, to_scene: str, back_scene: str):
    """Optional: switch to a blank scene, then back, as an extra-strong rebind."""
    if not to_scene or not back_scene:
        return
    await obs_set_scene(client, to_scene)
    await asyncio.sleep(0.25)
    await obs_set_scene(client, back_scene)


# ----- Page helpers -----
def strategy_for(url: str):
    for host, strat in SITE_STRATEGY.items():
        if host in url:
            return strat
    return {}

def switch_to_newest_window(driver: webdriver.Chrome):
    """If the site opened a new window/tab, switch to it."""
    try:
        handles = driver.window_handles
        if handles:
            driver.switch_to.window(handles[-1])
    except WebDriverException:
        pass

def wait_dom_ready(driver: webdriver.Chrome, timeout: int):
    """Wait until document.readyState == 'complete' and body exists."""
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )

def ensure_has_content(driver: webdriver.Chrome, min_height: int = 200) -> bool:
    """Return True if page appears non-empty and not about:blank."""
    try:
        url = driver.current_url or ""
        if url.startswith("about:blank"):
            return False
        vh = driver.execute_script("return window.innerHeight || 0;")
        dh = driver.execute_script("return document.body ? document.body.scrollHeight : 0;")
        return (vh and dh and (dh >= min_height))
    except WebDriverException:
        return False

def focus_window(driver: webdriver.Chrome):
    """Click near top-left to ensure the window is focused for Graphics Capture."""
    try:
        ActionChains(driver).move_by_offset(10, 10).click().perform()
        ActionChains(driver).move_by_offset(-10, -10).perform()
    except Exception:
        pass

def force_top(driver: webdriver.Chrome):
    try:
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.15)
        driver.execute_script("window.scrollTo(0, 0);")
    except WebDriverException:
        pass

def click_cookie_banners(driver: webdriver.Chrome):
    """Try to dismiss common consent/cookie banners that block scroll/focus."""
    selectors = [
        "button[aria-label*='accept' i]",
        "button[aria-label*='agree' i]",
        "button:contains('Accept')",  # JS contains handled below
        "button:contains('I agree')",
        "button.cookie-accept",
        "button#onetrust-accept-btn-handler",
        "#onetrust-accept-btn-handler",
        "button[mode='accept-all']",
    ]
    # Try attribute-based first
    for sel in selectors:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in elems:
                if el.is_displayed():
                    el.click()
                    time.sleep(0.1)
        except Exception:
            pass
    # Fallback: text-based querySelectorAll with JS (case-insensitive match)
    try:
        driver.execute_script("""
            [...document.querySelectorAll('button, [role=button], a')].forEach(el=>{
                const t=(el.innerText||'').trim().toLowerCase();
                if(['accept','accept all','i agree','allow all'].some(k=>t===k || t.includes(k))){
                    try{ el.click(); }catch(e){}
                }
            });
        """)
    except Exception:
        pass


# ----- Scrolling -----
def at_bottom(driver: webdriver.Chrome) -> bool:
    try:
        return driver.execute_script(
            "return (window.innerHeight + window.scrollY) >= document.body.scrollHeight;"
        )
    except WebDriverException:
        return True  # fail-safe to stop

def smooth_scroll_to_bottom(driver: webdriver.Chrome, px: int, delay_s: float, max_seconds: float, bounce: bool, bounce_px: int):
    start = time.time()
    try:
        driver.execute_script("document.documentElement.style.scrollBehavior='auto';"
                              "document.body.style.scrollBehavior='auto';")
    except WebDriverException:
        pass

    while True:
        try:
            driver.execute_script(f"window.scrollBy(0, {int(px)});")
        except WebDriverException:
            break
        time.sleep(delay_s)
        if at_bottom(driver):
            break
        if (time.time() - start) > max_seconds:
            break

    if bounce:
        try:
            driver.execute_script(f"window.scrollBy(0, {-int(bounce_px)});")
            time.sleep(0.18)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.12)
        except WebDriverException:
            pass


# ----- Robust per-URL flow with site-specific strategy -----
async def record_url_once(client: simpleobsws.WebSocketClient, driver: webdriver.Chrome, url: str) -> bool:
    """Returns True on success, False if we should retry/recreate driver."""
    strat = strategy_for(url)
    page_to = int(strat.get("page_load_timeout", PAGE_LOAD_TIMEOUT_S))
    dom_to  = int(strat.get("dom_ready_timeout", DOM_READY_TIMEOUT_S))

    try:
        if strat.get("two_stage_nav"):
            # Stage 1: ensure we are in a live window
            driver.get("about:blank")
            # Navigate via JS assignment (sometimes more reliable with kiosk/app mode)
            driver.execute_script(f"window.location.href = '{url}';")
        else:
            driver.get(url)
    except (TimeoutException, WebDriverException):
        return False

    switch_to_newest_window(driver)

    # Wait until DOM is ready (soft-fail)
    try:
        wait_dom_ready(driver, dom_to)
    except Exception:
        pass

    time.sleep(SETTLE_BEFORE_S)

    # Cookie/banner cleanup to avoid blocked scroll/focus
    click_cookie_banners(driver)

    # Health check (avoid about:blank or empty DOM); one refresh attempt
    if not ensure_has_content(driver):
        try:
            driver.refresh()
            wait_dom_ready(driver, dom_to)
            time.sleep(0.5)
            click_cookie_banners(driver)
        except Exception:
            return False
        if not ensure_has_content(driver):
            return False

    # Stable title so OBS matches strictly on title
    try:
        driver.execute_script("document.title = 'OBS-CAPTURE';")
    except Exception:
        pass

    # Optional strong rebind via blank scene
    if BLANK_SCENE_NAME:
        await scene_blink(client, BLANK_SCENE_NAME, OBS_SCENE_NAME)

    # Ensure correct scene & rebind Window Capture
    await obs_set_scene(client, OBS_SCENE_NAME)
    try:
        await refresh_window_capture_binding(client, OBS_SCENE_NAME, WINDOW_CAPTURE_SOURCE)
    except Exception as e:
        print(f"[WARN] Could not refresh Window Capture binding: {e}")

    # Give OBS time to hook
    rebind_wait = REBIND_WAIT_S + float(strat.get("extra_rebind_wait", 0))
    await asyncio.sleep(rebind_wait)

    # Focus and force top
    focus_window(driver)
    force_top(driver)

    # Start recording
    print("→ Start recording")
    try:
        await obs_start_record(client)
    except RuntimeError as e:
        print(f"[WARN] StartRecord failed (continuing): {e}")

    # Hold at top before scroll
    force_top(driver)
    time.sleep(PREROLL_AT_TOP_S + float(strat.get("pre_scroll_pause", 0)))

    # Scroll and stop
    smooth_scroll_to_bottom(driver, SCROLL_PIXELS, SCROLL_DELAY_S, MAX_SCROLL_SECONDS, BOUNCE_AT_END, BOUNCE_PIXELS)

    print("→ Stop recording")
    try:
        await obs_stop_record(client)
    except RuntimeError as e:
        print(f"[WARN] StopRecord failed (continuing): {e}")
    time.sleep(SETTLE_AFTER_S)
    print("✓ Clip done\n")
    return True


async def record_url_with_retries(client: simpleobsws.WebSocketClient, driver: Optional[webdriver.Chrome], url: str) -> webdriver.Chrome:
    """
    Try to record a URL up to RETRIES_PER_URL times.
    If the session dies or fails health checks, recreate the driver and retry.
    Returns a (possibly new) driver instance to continue with.
    On total failure, logs and skips to next URL with a fresh driver to avoid poisoning.
    """
    attempt = 0
    while attempt < RETRIES_PER_URL:
        attempt += 1
        print(f"[Attempt {attempt}/{RETRIES_PER_URL}] {url}")
        if driver is None:
            driver = make_chrome()

        try:
            ok = await record_url_once(client, driver, url)
            if ok:
                return driver
            else:
                print("…retrying (page health/binding issue)…")
        except (InvalidSessionIdException, NoSuchWindowException, WebDriverException) as e:
            print(f"[WARN] Driver/session issue: {e} — recreating browser and retrying…")

        # Recreate driver for next attempt
        try:
            driver.quit()
        except Exception:
            pass
        driver = None
        time.sleep(0.8)

    # All attempts failed: SKIP this URL (do NOT poison the rest)
    print(f"[SKIP] Giving up on {url} after {RETRIES_PER_URL} attempts. Continuing to next.")
    if driver is None:
        driver = make_chrome()  # ensure we return a live driver for subsequent URLs
    return driver


# ----- Entrypoint -----
async def main():
    client = await obs_connect()
    driver: Optional[webdriver.Chrome] = make_chrome()
    try:
        total = len(URLS)
        for i, url in enumerate(URLS, start=1):
            print(f"[{i}/{total}]")
            driver = await record_url_with_retries(client, driver, url)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        await client.disconnect()
        print("All clips finished (skipped ones were logged). Check your OBS recordings folder.")


if __name__ == "__main__":
    asyncio.run(main())
