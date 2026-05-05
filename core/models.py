from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime


@dataclass
class MLAccount:
    alias: str
    client_id: str
    client_secret: str
    refresh_token: str = ''   # vacío hasta que se complete OAuth flow
    access_token: Optional[str] = None
    token_expires_at: Optional[str] = None  # ISO format string for JSON serialization
    user_id: Optional[int] = None
    nickname: Optional[str] = None
    active: bool = True
    # Sprint Admin (05/05/2026): tracking soft-delete + auto-purga
    created_at: Optional[str] = None     # ISO timestamp de creación
    paused_at: Optional[str] = None      # ISO timestamp del soft delete (None = activa)
    paused_reason: Optional[str] = None  # razón opcional dada por el admin

    def is_token_valid(self) -> bool:
        if not self.access_token or not self.token_expires_at:
            return False
        expires = datetime.fromisoformat(self.token_expires_at)
        # Refresh if less than 5 minutes remaining
        return (expires - datetime.now()).total_seconds() > 300

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MLAccount":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
