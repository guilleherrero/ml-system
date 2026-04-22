import requests
from datetime import datetime, timedelta
from typing import Any

from .models import MLAccount

ML_API_BASE = "https://api.mercadolibre.com"
ML_AUTH_URL = "https://api.mercadolibre.com/oauth/token"


class MLApiError(Exception):
    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.status_code = status_code


class MLClient:
    """API client for a single MercadoLibre account."""

    def __init__(self, account: MLAccount, on_token_refresh=None):
        self.account = account
        self.on_token_refresh = on_token_refresh  # callback to persist updated tokens
        self.session = requests.Session()

    def _ensure_token(self):
        if not self.account.is_token_valid():
            self._refresh_token()

    def _refresh_token(self):
        resp = requests.post(ML_AUTH_URL, data={
            "grant_type": "refresh_token",
            "client_id": self.account.client_id,
            "client_secret": self.account.client_secret,
            "refresh_token": self.account.refresh_token,
        })
        if resp.status_code != 200:
            raise MLApiError(
                f"Token refresh failed for '{self.account.alias}': {resp.text}",
                resp.status_code,
            )
        data = resp.json()
        self.account.access_token = data["access_token"]
        self.account.refresh_token = data["refresh_token"]
        expires_in = data.get("expires_in", 21600)
        self.account.token_expires_at = (
            datetime.now() + timedelta(seconds=expires_in)
        ).isoformat()
        self.account.user_id = data.get("user_id")

        if self.on_token_refresh:
            self.on_token_refresh(self.account)

    def _get(self, path: str, params: dict = None) -> Any:
        self._ensure_token()
        resp = self.session.get(
            f"{ML_API_BASE}{path}",
            headers={"Authorization": f"Bearer {self.account.access_token}"},
            params=params or {},
        )
        if resp.status_code == 401:
            # Token might have just expired, try one refresh
            self._refresh_token()
            resp = self.session.get(
                f"{ML_API_BASE}{path}",
                headers={"Authorization": f"Bearer {self.account.access_token}"},
                params=params or {},
            )
        if not resp.ok:
            raise MLApiError(f"GET {path} failed: {resp.text}", resp.status_code)
        return resp.json()

    def _put(self, path: str, payload: dict) -> Any:
        self._ensure_token()
        resp = self.session.put(
            f"{ML_API_BASE}{path}",
            headers={
                "Authorization": f"Bearer {self.account.access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if not resp.ok:
            raise MLApiError(f"PUT {path} failed: {resp.text}", resp.status_code)
        return resp.json()

    def _post(self, path: str, payload: dict) -> Any:
        self._ensure_token()
        resp = self.session.post(
            f"{ML_API_BASE}{path}",
            headers={
                "Authorization": f"Bearer {self.account.access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if not resp.ok:
            raise MLApiError(f"POST {path} failed: {resp.text}", resp.status_code)
        return resp.json()

    # ── User ─────────────────────────────────────────────────────────────────

    def get_me(self) -> dict:
        return self._get("/users/me")

    # ── Listings ──────────────────────────────────────────────────────────────

    def get_my_listings(self, limit: int = 50, offset: int = 0, status: str = "active") -> dict:
        user_id = self.account.user_id
        if not user_id:
            me = self.get_me()
            user_id = me["id"]
            self.account.user_id = user_id
        return self._get(
            f"/users/{user_id}/items/search",
            params={"limit": limit, "offset": offset, "status": status},
        )

    def get_item(self, item_id: str) -> dict:
        return self._get(f"/items/{item_id}")

    def update_item(self, item_id: str, payload: dict) -> dict:
        return self._put(f"/items/{item_id}", payload)

    # ── Questions ─────────────────────────────────────────────────────────────

    def get_unanswered_questions(self) -> dict:
        user_id = self.account.user_id
        if not user_id:
            me = self.get_me()
            user_id = me["id"]
        return self._get(
            "/questions/search",
            params={"seller_id": user_id, "status": "UNANSWERED"},
        )

    def answer_question(self, question_id: int, text: str) -> dict:
        return self._post("/answers", {"question_id": question_id, "text": text})

    # ── Orders ────────────────────────────────────────────────────────────────

    def get_recent_orders(self, limit: int = 50) -> dict:
        return self._get("/orders/search/recent", params={"limit": limit})

    # ── Fees ──────────────────────────────────────────────────────────────────

    def get_listing_fee_rate(self, listing_type_id: str, price: float = 10000) -> float | None:
        """Consulta la API de ML y devuelve la tasa de comisión real para un tipo de publicación."""
        try:
            raw = self._get(
                "/sites/MLA/listing_prices",
                params={"price": price, "listing_type_id": listing_type_id},
            )
            # La API devuelve una lista; tomar el primer elemento
            data   = raw[0] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else {})
            fee = data.get("sale_fee_amount", 0)
            if price > 0 and fee:
                return round(fee / price, 4)
        except MLApiError:
            pass
        return None
