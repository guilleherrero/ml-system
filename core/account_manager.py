import os
from datetime import datetime, timezone
from typing import Optional

from .models import MLAccount
from .ml_client import MLClient
from .db_storage import db_load, db_save

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "accounts.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class AccountManager:
    """Manages MercadoLibre accounts."""

    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = os.path.abspath(config_path)
        self._accounts: list[MLAccount] = []
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self):
        data = db_load(self.config_path)
        self._accounts = [MLAccount.from_dict(a) for a in (data or {}).get("accounts", [])]

    def _save(self):
        db_save(self.config_path, {"accounts": [a.to_dict() for a in self._accounts]})

    def _on_token_refresh(self, account: MLAccount):
        """Called by MLClient after a token refresh — persists new tokens."""
        self._save()

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def add_account(
        self,
        alias: str,
        client_id: str,
        client_secret: str,
        refresh_token: str = '',
    ) -> MLAccount:
        """Crea una cuenta nueva. refresh_token puede venir vacío si la cuenta
        se crea ANTES del flujo OAuth (admin agrega la app → después conecta)."""
        if any(a.alias == alias for a in self._accounts):
            raise ValueError(f"An account with alias '{alias}' already exists.")

        account = MLAccount(
            alias=alias,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            created_at=_now_iso(),
        )
        self._accounts.append(account)
        self._save()
        return account

    def remove_account(self, alias: str):
        """HARD delete — elimina la cuenta del JSON. Usar con cuidado.
        Para soft-delete usar pause_account()."""
        before = len(self._accounts)
        self._accounts = [a for a in self._accounts if a.alias != alias]
        if len(self._accounts) == before:
            raise ValueError(f"Account '{alias}' not found.")
        self._save()

    # ── Sprint Admin: soft-delete + reactivación ─────────────────────────────

    def pause_account(self, alias: str, reason: str = '') -> MLAccount:
        """Soft delete: marca la cuenta como pausada (active=False) pero conserva
        sus datos. El cron de purga puede borrarla después de 90 días sin uso.
        """
        acc = self.get_account(alias)
        if not acc:
            raise ValueError(f"Account '{alias}' not found.")
        acc.active = False
        acc.paused_at = _now_iso()
        acc.paused_reason = (reason or '')[:300]
        self._save()
        return acc

    def reactivate_account(self, alias: str) -> MLAccount:
        """Vuelve a activar una cuenta pausada. Limpia los flags de pausa."""
        acc = self.get_account(alias)
        if not acc:
            raise ValueError(f"Account '{alias}' not found.")
        acc.active = True
        acc.paused_at = None
        acc.paused_reason = None
        self._save()
        return acc

    def cuentas_para_purgar(self, dias: int = 90) -> list[MLAccount]:
        """Lista cuentas pausadas hace más de N días — candidatas a purga
        automática por el cron."""
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=dias)
        out = []
        for a in self._accounts:
            if a.active or not a.paused_at:
                continue
            try:
                ts = datetime.fromisoformat(a.paused_at.replace('Z', '+00:00'))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    out.append(a)
            except Exception:
                continue
        return out

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
