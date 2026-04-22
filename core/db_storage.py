"""
Persistent storage layer.
- DATABASE_URL set → PostgreSQL (Render)
- DATABASE_URL not set → local JSON files (Mac)
"""
import json
import os

_db_url = os.environ.get('DATABASE_URL', '').strip()
_conn   = None


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        import psycopg2
        url = _db_url
        if 'sslmode' not in url:
            url += ('&' if '?' in url else '?') + 'sslmode=require'
        _conn = psycopg2.connect(url)
        _conn.autocommit = True
        with _conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
    return _conn


def use_db() -> bool:
    return bool(_db_url)


def db_load(path: str):
    """Load JSON from PostgreSQL (by path key) or from filesystem."""
    if not use_db():
        if not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM kv_store WHERE key = %s', (path,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
    except Exception:
        return None


def db_save(path: str, data):
    """Save JSON to PostgreSQL (by path key) or to filesystem."""
    if not use_db():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return
    try:
        payload = json.dumps(data, ensure_ascii=False)
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kv_store (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW()
            """, (path, payload))
    except Exception as e:
        raise RuntimeError(f'db_save failed for {path}: {e}') from e
