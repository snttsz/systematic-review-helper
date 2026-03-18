import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

NOTEBOOK_URL = "https://notebooklm.google.com/"


def resolve_project_root(start: Path) -> Path:
    current = start.resolve()
    for _ in range(6):
        if (current / ".env").exists() or (current / "app.py").exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    return start.resolve()


BASE_DIR = resolve_project_root(Path(__file__).resolve())
PROFILE_CONFIG_PATH = BASE_DIR / "data" / "memory" / "chrome_profile.json"
DEFAULT_CHROMEDRIVER_PATH = BASE_DIR / "chromedriver.exe"
DRIVER_LOG_PATH = BASE_DIR / "data" / "memory" / "chromedriver.log"
SHADOW_PROFILE_ROOT = BASE_DIR / "data" / "memory" / "chrome_profile_shadow"
MANAGED_PROFILE_ROOT = BASE_DIR / "data" / "memory" / "chrome_profile_managed"
LOGIN_WAIT_TIMEOUT = int(os.getenv("LOGIN_WAIT_TIMEOUT", "600"))


def find_chrome_user_data_dir() -> Path:
    override = os.getenv("CHROME_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    local_appdata = os.getenv("LOCALAPPDATA", "")
    if local_appdata:
        candidate = Path(local_appdata) / "Google" / "Chrome" / "User Data"
        if candidate.exists():
            return candidate

    appdata = os.getenv("APPDATA", "")
    if appdata:
        candidate = Path(appdata) / "Google" / "Chrome" / "User Data"
        if candidate.exists():
            return candidate

    raise FileNotFoundError("Chrome User Data directory not found.")


def parse_cookie_string(cookie_string: str) -> dict:
    cookies = {}
    for part in cookie_string.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name] = value
    return cookies


def cookies_valid(cookie_string: str, user_email: str) -> tuple[bool, str]:
    if not cookie_string or not cookie_string.strip():
        return False, "empty"

    cookies = parse_cookie_string(cookie_string)
    if not cookies:
        return False, "empty"

    try:
        with httpx.Client(cookies=cookies, timeout=20, follow_redirects=True) as client:
            response = client.get(NOTEBOOK_URL)
    except Exception as exc:
        return False, f"request-failed: {exc}"

    if response.status_code in (401, 403):
        return False, f"status-{response.status_code}"

    if "accounts.google.com" in str(response.url):
        return False, "redirect-login"

    if user_email and user_email not in response.text:
        return False, "user-mismatch"

    if response.status_code != 200:
        return False, f"status-{response.status_code}"

    return True, "ok"


def set_env_value(lines: list[str], key: str, value: str) -> list[str]:
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*).*$")

    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            prefix = match.group(1)
            lines[index] = f"{prefix}{value}\n"
            return lines

    lines.append(f"{key} = {value}\n")
    return lines


def update_env_file(env_path: Path, key: str, value: str) -> None:
    normalized_value = value.replace("\r", "").replace("\n", "").strip()
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        lines = []

    lines = set_env_value(lines, key, normalized_value)
    env_path.write_text("".join(lines), encoding="utf-8")


def build_cookie_string(cookies: list[dict]) -> str:
    filtered = [c for c in cookies if c.get("name") and "value" in c]
    filtered.sort(key=lambda c: c["name"].lower())
    return "; ".join(f"{c['name']}={c['value']}" for c in filtered)


def resolve_chromedriver_path() -> str | None:
    env_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
    if env_path:
        return env_path

    if DEFAULT_CHROMEDRIVER_PATH.exists():
        return str(DEFAULT_CHROMEDRIVER_PATH)

    return None


def resolve_chrome_binary() -> str | None:
    env_path = os.getenv("CHROME_BINARY", "").strip()
    if env_path and Path(env_path).exists():
        return env_path

    candidates = [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return None


def resolve_managed_profile_dir(config: dict, override: str | None) -> Path:
    if override:
        return Path(override).expanduser()

    env_path = os.getenv("CHROME_MANAGED_PROFILE_DIR", "").strip()
    if env_path:
        return Path(env_path).expanduser()

    stored = (config.get("managed_profile_dir") or "").strip()
    if stored:
        return Path(stored)

    return MANAGED_PROFILE_ROOT


def should_use_managed_profile(override: bool | None) -> bool:
    if override is not None:
        return override

    env_value = os.getenv("CHROME_USE_MANAGED_PROFILE", "").strip().lower()
    if env_value:
        return env_value not in {"0", "false", "no"}

    return True


def default_user_data_dirs() -> list[Path]:
    candidates = []
    local_appdata = os.getenv("LOCALAPPDATA", "")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Google" / "Chrome" / "User Data")

    appdata = os.getenv("APPDATA", "")
    if appdata:
        candidates.append(Path(appdata) / "Google" / "Chrome" / "User Data")

    return [path for path in candidates if path.exists()]


def is_default_user_data_dir(user_data_dir: str) -> bool:
    try:
        target = Path(user_data_dir).resolve()
    except OSError:
        return False

    for candidate in default_user_data_dirs():
        if candidate.resolve() == target:
            return True

    return False


def should_use_shadow_profile(user_data_dir: str, override: bool | None) -> bool:
    if override is not None:
        return override

    env_value = os.getenv("CHROME_SHADOW_PROFILE", "").strip().lower()
    if env_value:
        return env_value not in {"0", "false", "no"}

    return is_default_user_data_dir(user_data_dir)


def prepare_shadow_profile(user_data_dir: str, profile_name: str) -> Path:
    source_dir = Path(user_data_dir)
    if SHADOW_PROFILE_ROOT.exists():
        shutil.rmtree(SHADOW_PROFILE_ROOT, ignore_errors=True)

    if not source_dir.exists():
        raise FileNotFoundError(f"Chrome user data directory not found: {source_dir}")

    ignore = shutil.ignore_patterns(
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "ShaderCache",
        "Service Worker/CacheStorage",
        "Service Worker/ScriptCache",
        "Default/Cache",
        "Default/Code Cache",
        "Default/GPUCache",
        "Default/GrShaderCache",
        "Default/ShaderCache",
        "SingletonLock",
        "SingletonSocket",
        "SingletonCookie",
    )

    shutil.copytree(source_dir, SHADOW_PROFILE_ROOT, ignore=ignore)

    shadow_profile_dir = SHADOW_PROFILE_ROOT / profile_name
    if not shadow_profile_dir.exists():
        raise FileNotFoundError(f"Chrome profile not found in shadow copy: {shadow_profile_dir}")

    return SHADOW_PROFILE_ROOT


def ensure_profile_not_in_use(user_data_dir: str, require_no_chrome: bool) -> None:
    profile_dir = Path(user_data_dir)
    lock_files = [
        profile_dir / "SingletonLock",
        profile_dir / "SingletonCookie",
        profile_dir / "SingletonSocket",
    ]

    if any(path.exists() for path in lock_files):
        raise RuntimeError(
            "Chrome profile directory is in use. Close Chrome that uses this profile."
        )

    if not require_no_chrome or os.name != "nt":
        return

    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return

    if "chrome.exe" in result.stdout.lower():
        raise RuntimeError(
            "Chrome is running. Close all Chrome windows before refreshing cookies."
        )


def create_chrome_driver(options: webdriver.ChromeOptions) -> webdriver.Chrome:
    chromedriver_path = resolve_chromedriver_path()
    if chromedriver_path:
        service = Service(chromedriver_path)
        try:
            log_handle = DRIVER_LOG_PATH.open("a", encoding="utf-8")
            service.log_output = log_handle
            driver = webdriver.Chrome(service=service, options=options)
            driver._log_handle = log_handle
            return driver
        except WebDriverException as exc:
            pass

    try:
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError as exc:
        raise RuntimeError(
            "webdriver-manager is not installed and local ChromeDriver failed."
        ) from exc

    driver_path = ChromeDriverManager().install()
    service = Service(driver_path)
    log_handle = DRIVER_LOG_PATH.open("a", encoding="utf-8")
    service.log_output = log_handle
    driver = webdriver.Chrome(service=service, options=options)
    driver._log_handle = log_handle
    return driver


def load_profile_config(path: Path) -> dict:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(data, dict):
        return {}

    return data


def update_profile_config(path: Path, updates: dict) -> dict:
    data = load_profile_config(path)
    for key, value in updates.items():
        if value is not None:
            data[key] = value

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def save_profile_config(
    path: Path,
    profile_name: str,
    user_data_dir: str,
    headless_ok: bool | None = None,
) -> None:
    updates = {
        "profile_name": profile_name,
        "user_data_dir": user_data_dir,
    }
    if headless_ok is not None:
        updates["headless_ok"] = headless_ok

    update_profile_config(path, updates)


def resolve_profile_settings(
    profile_override: str | None,
    user_data_dir_override: str | None,
) -> tuple[str, str, bool | None, bool]:
    config = load_profile_config(PROFILE_CONFIG_PATH)

    profile_name = (
        (profile_override or "").strip()
        or (config.get("profile_name") or "").strip()
        or os.getenv("CHROME_PROFILE", "").strip()
        or "Default"
    )

    user_data_dir = (
        (user_data_dir_override or "").strip()
        or (config.get("user_data_dir") or "").strip()
        or os.getenv("CHROME_USER_DATA_DIR", "").strip()
    )
    if not user_data_dir:
        user_data_dir = str(find_chrome_user_data_dir())

    first_time = not config.get("profile_name") or not config.get("user_data_dir")
    if first_time:
        save_profile_config(PROFILE_CONFIG_PATH, profile_name, user_data_dir)

    return profile_name, user_data_dir, config.get("headless_ok"), first_time


def wait_for_login(driver: webdriver.Chrome, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = driver.current_url
        if "notebooklm.google.com" in current:
            return
        time.sleep(2)

    raise TimeoutException("Timed out waiting for NotebookLM login.")


def wait_for_valid_cookies(
    driver: webdriver.Chrome,
    timeout: int = LOGIN_WAIT_TIMEOUT,
    poll_interval: int = 3,
) -> str:
    deadline = time.time() + timeout
    user_email = os.getenv("USER_EMAIL", "").strip()

    while time.time() < deadline:
        cookie_string = build_cookie_string(driver.get_cookies())
        if cookie_string:
            valid, _ = cookies_valid(cookie_string, user_email)
            if valid:
                return cookie_string
        time.sleep(poll_interval)

    raise TimeoutException("Timed out waiting for a valid NotebookLM session.")


def extract_cookies_with_selenium(
    user_data_dir: str,
    profile_name: str,
    wait_for_login_flow: bool = False,
    headless: bool = False,
    use_shadow_profile: bool = False,
) -> str:
    require_no_chrome = is_default_user_data_dir(user_data_dir) and not use_shadow_profile
    ensure_profile_not_in_use(user_data_dir, require_no_chrome)

    shadow_root: Path | None = None
    if use_shadow_profile:
        shadow_root = prepare_shadow_profile(user_data_dir, profile_name)
        user_data_dir = str(shadow_root)

    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"
    options.add_argument(f"--user-data-dir={user_data_dir}")
    options.add_argument(f"--profile-directory={profile_name}")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-features=AutomationControlled")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--remote-debugging-port=0")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")

    chrome_binary = resolve_chrome_binary()
    if chrome_binary:
        options.binary_location = chrome_binary

    driver = create_chrome_driver(options)
    try:
        driver.set_page_load_timeout(30)
        driver.get(NOTEBOOK_URL)
        try:
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            pass
        time.sleep(2)

        if wait_for_login_flow and not headless:
            if "accounts.google.com" in driver.current_url:
                print("Waiting for you to finish login in the browser...", flush=True)
            cookie_string = wait_for_valid_cookies(driver)
            return cookie_string

        cookie_string = build_cookie_string(driver.get_cookies())
        if not cookie_string:
            raise RuntimeError("No cookies found after visiting NotebookLM.")

        return cookie_string
    finally:
        driver.quit()
        log_handle = getattr(driver, "_log_handle", None)
        if log_handle:
            log_handle.close()
        if shadow_root:
            shutil.rmtree(shadow_root, ignore_errors=True)


def refresh_cookies(
    profile_override: str | None = None,
    user_data_dir_override: str | None = None,
    force: bool = False,
    allow_headless: bool = True,
    prompt_override: bool | None = None,
    use_shadow_profile: bool | None = None,
    use_managed_profile: bool | None = None,
    managed_profile_dir_override: str | None = None,
) -> bool:
    env_path = BASE_DIR / ".env"

    load_dotenv(env_path)

    current_cookie_string = os.getenv("COOKIES", "").strip()
    user_email = os.getenv("USER_EMAIL", "").strip()

    if not force:
        valid, reason = cookies_valid(current_cookie_string, user_email)
        if valid:
            return False
        print(f"Cookies invalid or missing ({reason}). Starting refresh...")

    profile_name, user_data_dir, headless_ok, first_time = resolve_profile_settings(
        profile_override,
        user_data_dir_override,
    )

    config = load_profile_config(PROFILE_CONFIG_PATH)
    managed_enabled = should_use_managed_profile(use_managed_profile)
    if managed_enabled:
        managed_dir = resolve_managed_profile_dir(config, managed_profile_dir_override)
        managed_dir.mkdir(parents=True, exist_ok=True)
        user_data_dir = str(managed_dir)
        profile_name = "Default"
        first_time = not any(managed_dir.iterdir())
        update_profile_config(
            PROFILE_CONFIG_PATH,
            {
                "profile_name": profile_name,
                "user_data_dir": user_data_dir,
                "managed_profile_dir": str(managed_dir),
                "use_managed_profile": True,
            },
        )

    wait_for_login_flow = True if prompt_override is None else prompt_override
    headless_attempt = allow_headless

    shadow_profile = False
    if not managed_enabled:
        shadow_profile = should_use_shadow_profile(user_data_dir, use_shadow_profile)

    attempts: list[tuple[bool, bool]] = []
    if headless_attempt:
        attempts.append((True, False))
    attempts.append((False, wait_for_login_flow))

    last_error: Exception | None = None

    for headless, wait_for_login in attempts:
        try:
            new_cookie_string = extract_cookies_with_selenium(
                user_data_dir=user_data_dir,
                profile_name=profile_name,
                wait_for_login_flow=wait_for_login,
                headless=headless,
                use_shadow_profile=shadow_profile,
            )
        except Exception as exc:
            last_error = exc
            if headless:
                print("Headless attempt failed. Retrying with browser...")
                continue
            raise

        valid, reason = cookies_valid(new_cookie_string, user_email)
        if valid:
            update_env_file(env_path, "COOKIES", new_cookie_string)
            os.environ["COOKIES"] = new_cookie_string
            print(f"Updated {env_path}.")
            print("New cookies validated successfully.")
            updates = {"logged_in": True, "headless_ok": True}
            update_profile_config(PROFILE_CONFIG_PATH, updates)
            return True

        print(f"Warning: cookie validation failed after update ({reason}).")
        update_profile_config(PROFILE_CONFIG_PATH, {"logged_in": False})
        if headless:
            print("Headless cookies invalid. Retrying with browser...")
            continue

        return False

    if last_error:
        raise last_error

    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh NotebookLM session cookies in .env using a real Chrome profile."
    )
    parser.add_argument(
        "--profile",
        default=os.getenv("CHROME_PROFILE", "Default"),
        help="Chrome profile directory name (e.g., 'Default', 'Profile 1').",
    )
    parser.add_argument(
        "--user-data-dir",
        default=os.getenv("CHROME_USER_DATA_DIR", ""),
        help="Path to Chrome User Data directory (overrides auto-detection).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh cookies even if current ones look valid.",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Do not wait for interactive login; continue immediately.",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Disable headless mode even when a profile is already saved.",
    )
    parser.add_argument(
        "--managed-profile-dir",
        default=os.getenv("CHROME_MANAGED_PROFILE_DIR", ""),
        help="Path to a persistent managed Chrome profile directory.",
    )
    managed_group = parser.add_mutually_exclusive_group()
    managed_group.add_argument(
        "--use-managed-profile",
        action="store_true",
        help="Use a managed Chrome profile stored in the project (default).",
    )
    managed_group.add_argument(
        "--use-real-profile",
        action="store_true",
        help="Use the real Chrome user profile directory.",
    )
    shadow_group = parser.add_mutually_exclusive_group()
    shadow_group.add_argument(
        "--shadow-profile",
        action="store_true",
        help="Use a temporary copy of the Chrome profile to avoid DevTools restrictions.",
    )
    shadow_group.add_argument(
        "--no-shadow-profile",
        action="store_true",
        help="Use the real Chrome profile directory directly.",
    )

    args = parser.parse_args()

    shadow_override = None
    if args.shadow_profile:
        shadow_override = True
    elif args.no_shadow_profile:
        shadow_override = False

    use_managed_override = None
    if args.use_managed_profile:
        use_managed_override = True
    elif args.use_real_profile:
        use_managed_override = False

    refresh_cookies(
        profile_override=args.profile,
        user_data_dir_override=args.user_data_dir,
        force=args.force,
        allow_headless=not args.no_headless,
        prompt_override=not args.no_prompt,
        use_shadow_profile=shadow_override,
        use_managed_profile=use_managed_override,
        managed_profile_dir_override=args.managed_profile_dir,
    )


if __name__ == "__main__":
    main()
