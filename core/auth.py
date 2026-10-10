"""
Sistema de autenticación y permisos de usuario.
Usuarios y contraseñas hasheadas en config/users.json.
"""

import os
import uuid
from functools import wraps
from flask import session, redirect, url_for, request

from werkzeug.security import generate_password_hash, check_password_hash
from core.db_storage import db_load, db_save

CONFIG_DIR = os.path.join(os.path.dirname(__file__), '..', 'config')
USERS_PATH = os.path.join(CONFIG_DIR, 'users.json')


def load_users() -> dict:
    return db_load(USERS_PATH) or {'users': []}


def save_users(data: dict):
    db_save(USERS_PATH, data)


def needs_setup() -> bool:
    """True si no existe ningún usuario admin todavía."""
    data = load_users()
    return not any(u.get('is_admin') for u in data.get('users', []))


def get_user_by_id(user_id: str) -> dict | None:
    for u in load_users().get('users', []):
        if u['id'] == user_id:
            return u
    return None


def get_user_by_username(username: str) -> dict | None:
    for u in load_users().get('users', []):
        if u['username'].lower() == username.lower():
            return u
    return None


def get_current_user() -> dict | None:
    uid = session.get('user_id')
    if not uid:
        return None
    return get_user_by_id(uid)


def login_user(username: str, password: str) -> dict | None:
    """Valida credenciales y devuelve el usuario si son correctas."""
    user = get_user_by_username(username)
    if not user:
        return None
    if not check_password_hash(user['password_hash'], password):
        return None
    session['user_id']   = user['id']
    session['username']  = user['username']
    session['is_admin']  = user.get('is_admin', False)
    session.permanent    = True
    return user


def logout_user():
    session.clear()


def create_user(username: str, password: str, is_admin: bool = False, accounts: list = None) -> dict:
    data = load_users()
    user = {
        'id':            str(uuid.uuid4()),
        'username':      username.strip(),
        'password_hash': generate_password_hash(password),
        'is_admin':      is_admin,
        'accounts':      accounts or [],  # lista de alias; vacío = todos (solo admin)
    }
    data['users'].append(user)
    save_users(data)
    return user


def update_user(user_id: str, **kwargs) -> bool:
    data  = load_users()
    users = data.get('users', [])
    for u in users:
        if u['id'] == user_id:
            if 'password' in kwargs:
                u['password_hash'] = generate_password_hash(kwargs.pop('password'))
            u.update(kwargs)
            save_users(data)
            return True
    return False


def delete_user(user_id: str) -> bool:
    data  = load_users()
    users = data.get('users', [])
    new   = [u for u in users if u['id'] != user_id]
    if len(new) == len(users):
        return False
    data['users'] = new
    save_users(data)
    return True


def list_users() -> list:
    return [
        {k: v for k, v in u.items() if k != 'password_hash'}
        for u in load_users().get('users', [])
    ]


def user_can_access(alias: str) -> bool:
    """True si el usuario actual tiene acceso a ese alias."""
    user = get_current_user()
    if not user:
        return False
    if user.get('is_admin'):
        return True
    permitted = user.get('accounts', [])
    return alias in permitted


def get_permitted_accounts(all_accounts: list) -> list:
    """Filtra la lista de cuentas según permisos del usuario actual."""
    user = get_current_user()
    if not user:
        return []
    if user.get('is_admin'):
        return all_accounts
    permitted = set(user.get('accounts', []))
    return [a for a in all_accounts if a.get('alias') in permitted]


# ── Decoradores ───────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))
        if not session.get('is_admin'):
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated
