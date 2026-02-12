from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


class Storage:
    """
    Simple JSON file storage.

    Notes:
    - Keeps an in-memory dict at self.data.
    - All getters read from self.data (kept in sync on save()).
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Any] = self.load()

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            if not raw:
                return {}
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            data = {}
        self.data = data
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- basic helpers ----------
    def get(self) -> Dict[str, Any]:
        return self.data

    def set(self, data: Dict[str, Any]) -> None:
        self.save(data)

    # ---------- users ----------
    def _users(self) -> Dict[str, Any]:
        users = self.data.get("users", {})
        if not isinstance(users, dict):
            users = {}
            self.data["users"] = users
        return users

    def get_user(self, user_id: int) -> Dict[str, Any]:
        users = self._users()
        u = users.get(str(user_id), {})
        return u if isinstance(u, dict) else {}

    def set_user(self, user_id: int, user_data: Dict[str, Any]) -> None:
        users = self._users()
        users[str(user_id)] = user_data if isinstance(user_data, dict) else {}
        self.save(self.data)

    # Language
    def get_user_lang(self, user_id: int) -> str:
        user = self.get_user(user_id)
        return str(user.get("lang", "ar") or "ar")

    def set_user_lang(self, user_id: int, lang: str) -> None:
        user = self.get_user(user_id)
        user["lang"] = str(lang)
        self.set_user(user_id, user)

    # Credentials file path (saved per user)
    def get_user_creds_file(self, user_id: int) -> Optional[str]:
        user = self.get_user(user_id)
        v = user.get("creds_file")
        return str(v) if v else None

    def set_user_creds_file(self, user_id: int, creds_file: str) -> None:
        user = self.get_user(user_id)
        user["creds_file"] = str(creds_file)
        self.set_user(user_id, user)

    # ---------- groups ----------
    def _groups(self) -> Dict[str, Any]:
        groups = self.data.get("groups", {})
        if not isinstance(groups, dict):
            groups = {}
            self.data["groups"] = groups
        return groups

    def get_group(self, chat_id: int) -> Dict[str, Any]:
        groups = self._groups()
        g = groups.get(str(chat_id), {})
        return g if isinstance(g, dict) else {}

    def set_group(self, chat_id: int, group_data: Dict[str, Any]) -> None:
        groups = self._groups()
        groups[str(chat_id)] = group_data if isinstance(group_data, dict) else {}
        self.save(self.data)

    # ---------- pending setup (user -> chat_id) ----------
    def _pending(self) -> Dict[str, Any]:
        pending = self.data.get("pending", {})
        if not isinstance(pending, dict):
            pending = {}
            self.data["pending"] = pending
        return pending

    def set_pending(self, user_id: int, chat_id: int) -> None:
        pending = self._pending()
        pending[str(user_id)] = {"chat_id": str(chat_id)}
        self.save(self.data)

    def clear_pending(self, user_id: int) -> None:
        pending = self._pending()
        pending.pop(str(user_id), None)
        self.save(self.data)

    def get_pending_group_for(self, user_id: int) -> Optional[int]:
        pending = self._pending()
        item = pending.get(str(user_id))
        if not isinstance(item, dict):
            return None
        chat_id = item.get("chat_id")
        if chat_id is None:
            return None
        try:
            return int(chat_id)
        except Exception:
            return None
