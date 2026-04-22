import json
import os
from typing import Optional

from .models import MLAccount
from .ml_client import MLClient

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "accounts.json")


class AccountManager:
    """Manages MercadoLibre accounts."""

    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = os.path.abspath(config_path)
        self._accounts: list[MLAccount] = []
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self):
        if not os.path.exists(self.config_path):
            self._accounts = []
            return
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._accounts = [MLAccount.from_dict(a) for a in data.get("accounts", [])]

    def _save(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(
                {"accounts": [a.to_dict() for a in self._accounts]},
                f,
                indent=2,
                ensure_ascii=False,
            )

    def _on_token_refresh(self, account: MLAccount):
        """Called by MLClient after a token refresh — persists new tokens."""
        self._save()

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def add_account(
        self,
        alias: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> MLAccount:
        if any(a.alias == alias for a in self._accounts):
            raise ValueError(f"An account with alias '{alias}' already exists.")

        account = MLAccount(
            alias=alias,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
        self._accounts.append(account)
        self._save()
        return account

    def remove_account(self, alias: str):
        before = len(self._accounts)
        self._accounts = [a for a in self._accounts if a.alias != alias]
        if len(self._accounts) == before:
            raise ValueError(f"Account '{alias}' not found.")
        self._save()

    def get_account(self, alias: str) -> Optional[MLAccount]:
        return next((a for a in self._accounts if a.alias == alias), None)

    def list_accounts(self) -> list[MLAccount]:
        return list(self._accounts)

    # ── Clients ───────────────────────────────────────────────────────────────

    def get_client(self, alias: str) -> MLClient:
        account = self.get_account(alias)
        if not account:
            raise ValueError(f"Account '{alias}' not found.")
        return MLClient(account, on_token_refresh=self._on_token_refresh)

    def get_all_clients(self) -> dict[str, MLClient]:
        return {a.alias: self.get_client(a.alias) for a in self._accounts if a.active}
