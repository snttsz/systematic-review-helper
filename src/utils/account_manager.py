import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Condition

from src.api.notebooklm_api import NotebookLMAPI
from src.utils.refresh_notebooklm_cookies import refresh_cookies


@dataclass(frozen=True)
class AccountContext:
    account_id: str
    sessions_path: Path


class AccountManager:
    def __init__(self, data_dir: Path, cooldown_minutes: int | None = None) -> None:
        self._data_dir = data_dir
        self._accounts_path = data_dir / "memory" / "accounts.json"
        self._sessions_root = data_dir / "memory" / "sessions"
        self._lock = Condition()
        env_cooldown = int(os.getenv("CONVERSATION_LIMIT_COOLDOWN_MINUTES", "180"))
        self._cooldown_minutes = cooldown_minutes if cooldown_minutes is not None else env_cooldown
        self._accounts = self._load_accounts()
        self._active_account_id = self._accounts.get("active_account_id")
        self._api_cache: dict[str, NotebookLMAPI] = {}
        self._refreshing: set[str] = set()
        if not self._active_account_id:
            self._active_account_id = self._accounts["accounts"][0]["id"]
            self._accounts["active_account_id"] = self._active_account_id
            self._save_accounts()

    def ensure_active_api(self) -> NotebookLMAPI:
        while True:
            account = self._get_active_account()
            if account is None:
                self._wait_for_available_account()
                continue
            return self._ensure_api_for_account(account)

    def get_active_context(self) -> AccountContext:
        while True:
            account = self._get_active_account()
            if account is None:
                self._wait_for_available_account()
                continue
            return AccountContext(account_id=account["id"], sessions_path=self._sessions_path(account["id"]))

    def record_question(self, account_id: str) -> None:
        with self._lock:
            account = self._find_account(account_id)
            stats = account.setdefault("stats", {})
            stats["questions_asked"] = int(stats.get("questions_asked", 0)) + 1
            stats["questions_since_limit"] = int(stats.get("questions_since_limit", 0)) + 1
            now = self._now_iso()
            stats.setdefault("first_used_at", now)
            stats["last_used_at"] = now
            self._save_accounts()

    def mark_limit_and_rotate(self, account_id: str) -> bool:
        with self._lock:
            account = self._find_account(account_id)
            cooldown_until = time.time() + (self._cooldown_minutes * 60)
            account["cooldown_until"] = cooldown_until
            stats = account.setdefault("stats", {})
            stats["limit_hits"] = int(stats.get("limit_hits", 0)) + 1
            total_at_limit = int(stats.get("total_questions_at_limit", 0))
            questions_since = int(stats.get("questions_since_limit", 0))
            stats["total_questions_at_limit"] = total_at_limit + questions_since
            stats["questions_since_limit"] = 0
            if stats["limit_hits"] > 0:
                stats["avg_questions_per_limit"] = stats["total_questions_at_limit"] / stats["limit_hits"]
            stats["last_limit_at"] = self._now_iso()

            next_account = self._select_next_available(account_id, announce=True)
            if not next_account:
                self._save_accounts()
                return False

            self._active_account_id = next_account["id"]
            self._accounts["active_account_id"] = self._active_account_id
            self._save_accounts()
            print(f"Switching to account {self._active_account_id}.")
            self._lock.notify_all()
            return True

    def refresh_account(self, account_id: str) -> bool:
        account = self._find_account(account_id)
        return self._refresh_account(account)

    def add_new_account(self, user_email: str | None = None) -> str:
        with self._lock:
            account_id = self._new_account_id()
            managed_dir = self._data_dir / "memory" / "chrome_profile_managed" / account_id
            profile_config_path = self._data_dir / "memory" / f"chrome_profile_{account_id}.json"
            account = {
                "id": account_id,
                "label": user_email or account_id,
                "user_email": user_email or "",
                "profile_name": "Default",
                "user_data_dir": str(managed_dir),
                "managed_profile_dir": str(managed_dir),
                "use_managed_profile": True,
                "profile_config_path": str(profile_config_path),
                "headless_ok": True,
                "cooldown_until": 0,
                "cookies": "",
                "stats": {
                    "questions_asked": 0,
                    "questions_since_limit": 0,
                    "limit_hits": 0,
                    "total_questions_at_limit": 0,
                    "avg_questions_per_limit": 0,
                },
            }
            self._accounts["accounts"].append(account)
            self._save_accounts()

        self._refresh_account_for_new(account)
        return account_id

    def _refresh_account_for_new(self, account: dict) -> None:
        self._refresh_account(account)
        with self._lock:
            if not self._active_account_id:
                self._active_account_id = account["id"]
                self._accounts["active_account_id"] = self._active_account_id
                self._save_accounts()

    def _refresh_account(self, account: dict) -> bool:
        with self._lock:
            account_id = account["id"]
            if account_id in self._refreshing:
                while account_id in self._refreshing:
                    self._lock.wait(timeout=5)
                return bool(account.get("cookies"))
            self._refreshing.add(account_id)

        cookies_updated, cookie_string = refresh_cookies(
            profile_override=account.get("profile_name"),
            user_data_dir_override=account.get("user_data_dir"),
            force=True,
            allow_headless=True,
            prompt_override=None,
            use_shadow_profile=None,
            use_managed_profile=bool(account.get("use_managed_profile", True)),
            managed_profile_dir_override=account.get("managed_profile_dir"),
            update_env=False,
            profile_config_path=Path(account.get("profile_config_path", "")) if account.get("profile_config_path") else None,
            user_email_override=account.get("user_email") or None,
        )
        with self._lock:
            if cookie_string:
                account["cookies"] = cookie_string
                self._save_accounts()
                self._api_cache.pop(account["id"], None)
            self._refreshing.discard(account_id)
            self._lock.notify_all()
        if cookie_string:
            return True
        return bool(cookies_updated)

    def _ensure_api_for_account(self, account: dict) -> NotebookLMAPI:
        account_id = account["id"]
        with self._lock:
            api = self._api_cache.get(account_id)
            if api:
                return api

        api = NotebookLMAPI(cookies=account.get("cookies") or None, user_email=account.get("user_email") or "")
        if api.check_success_login():
            with self._lock:
                self._api_cache[account_id] = api
            return api

        self._refresh_account(account)
        api = NotebookLMAPI(cookies=account.get("cookies") or None, user_email=account.get("user_email") or "")
        if not api.check_success_login():
            return api

        with self._lock:
            self._api_cache[account_id] = api
        return api

    def _get_active_account(self) -> dict | None:
        with self._lock:
            account = self._find_account(self._active_account_id)
            if self._is_available(account):
                return account

            next_account = self._select_next_available(account["id"], announce=True)
            if not next_account:
                return None

            self._active_account_id = next_account["id"]
            self._accounts["active_account_id"] = self._active_account_id
            self._save_accounts()
            print(f"Switching to account {self._active_account_id}.")
            return next_account

    def _wait_for_available_account(self) -> None:
        with self._lock:
            soonest = None
            for account in self._accounts.get("accounts", []):
                cooldown_until = float(account.get("cooldown_until", 0) or 0)
                if cooldown_until <= time.time():
                    soonest = 0
                    break
                if soonest is None or cooldown_until < soonest:
                    soonest = cooldown_until

            if soonest is None:
                print("No accounts available. Waiting 30 seconds...")
                self._lock.wait(timeout=30)
                return

            if soonest <= time.time():
                return

            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(soonest))
            wait_seconds = max(1, int(soonest - time.time()))
            print(f"All accounts in cooldown. Next retry at {when}.")
            self._lock.wait(timeout=wait_seconds)

    def _select_next_available(self, current_id: str, announce: bool = False) -> dict | None:
        accounts = self._accounts.get("accounts", [])
        if not accounts:
            return None

        start_index = next((i for i, acc in enumerate(accounts) if acc["id"] == current_id), -1)
        for offset in range(1, len(accounts) + 1):
            idx = (start_index + offset) % len(accounts)
            candidate = accounts[idx]
            if self._is_available(candidate):
                return candidate
            if announce:
                cooldown_until = float(candidate.get("cooldown_until", 0) or 0)
                if cooldown_until > 0:
                    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cooldown_until))
                    print(f"Account {candidate['id']} in cooldown until {when}.")
        return None

    def _is_available(self, account: dict) -> bool:
        cooldown_until = float(account.get("cooldown_until", 0) or 0)
        return time.time() >= cooldown_until

    def _find_account(self, account_id: str | None) -> dict:
        if not account_id:
            return self._accounts["accounts"][0]
        for account in self._accounts.get("accounts", []):
            if account.get("id") == account_id:
                return account
        return self._accounts["accounts"][0]

    def _sessions_path(self, account_id: str) -> Path:
        self._sessions_root.mkdir(parents=True, exist_ok=True)
        target = self._sessions_root / f"sessions_ids.{account_id}.json"
        legacy = self._data_dir / "memory" / "sessions_ids.json"
        if not target.exists() and legacy.exists():
            try:
                target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        return target

    def _load_accounts(self) -> dict:
        if self._accounts_path.exists():
            try:
                data = json.loads(self._accounts_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("accounts"):
                    self._normalize_limits(data)
                    return data
            except json.JSONDecodeError:
                pass

        default_account = self._build_default_account()
        data = {"active_account_id": default_account["id"], "accounts": [default_account]}
        self._accounts_path.parent.mkdir(parents=True, exist_ok=True)
        self._accounts_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data

    def _normalize_limits(self, data: dict) -> None:
        now = time.time()
        for account in data.get("accounts", []):
            stats = account.get("stats", {})
            limit_hits = int(stats.get("limit_hits", 0))
            cooldown_until = float(account.get("cooldown_until", 0) or 0)
            if limit_hits > 0 and cooldown_until <= now:
                account["cooldown_until"] = now + (self._cooldown_minutes * 60)

    def _build_default_account(self) -> dict:
        base_profile = self._data_dir / "memory" / "chrome_profile.json"
        profile_data = {}
        if base_profile.exists():
            try:
                profile_data = json.loads(base_profile.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                profile_data = {}

        account_id = "account-1"
        return {
            "id": account_id,
            "label": profile_data.get("profile_name", "Default"),
            "user_email": os.getenv("USER_EMAIL", ""),
            "profile_name": profile_data.get("profile_name", "Default"),
            "user_data_dir": profile_data.get("user_data_dir", ""),
            "managed_profile_dir": profile_data.get("managed_profile_dir", ""),
            "use_managed_profile": bool(profile_data.get("use_managed_profile", True)),
            "profile_config_path": str(self._data_dir / "memory" / "chrome_profile.json"),
            "headless_ok": bool(profile_data.get("headless_ok", True)),
            "cooldown_until": 0,
            "cookies": os.getenv("COOKIES", ""),
            "stats": {
                "questions_asked": 0,
                "questions_since_limit": 0,
                "limit_hits": 0,
                "total_questions_at_limit": 0,
                "avg_questions_per_limit": 0,
            },
        }

    def _save_accounts(self) -> None:
        self._accounts_path.parent.mkdir(parents=True, exist_ok=True)
        self._accounts_path.write_text(json.dumps(self._accounts, indent=2), encoding="utf-8")

    def _new_account_id(self) -> str:
        timestamp = time.strftime("%Y%m%d%H%M%S")
        return f"account-{timestamp}"

    def _now_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")
