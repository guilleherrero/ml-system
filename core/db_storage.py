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
        _migrate_keys(_conn)
    return _conn


def use_db() -> bool:
    return bool(_db_url)


def _key(path: str) -> str:
    """Normalize path to a stable PostgreSQL key (resolves ../ components)."""
    return os.path.normpath(os.path.abspath(path))


def _migrate_keys(conn):
    """Rename any un-normalized keys left from before the _key() fix."""
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT key FROM kv_store')
            rows = cur.fetchall()
        for (old_key,) in rows:
            new_key = os.path.normpath(os.path.abspath(old_key))
            if new_key != old_key:
                with conn.cursor() as cur:
                    cur.execute(
                        'UPDATE kv_store SET key = %s WHERE key = %s AND NOT EXISTS '
                        '(SELECT 1 FROM kv_store WHERE key = %s)',
                        (new_key, old_key, new_key)
                    )
    except Exception as e:
        print(f'[db] key migration: {e}')


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
            cur.execute('SELECT value FROM kv_store WHERE key = %s', (_key(path),))
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
            """, (_key(path), payload))
    except Exception as e:
        raise RuntimeError(f'db_save failed for {path}: {e}') from e
