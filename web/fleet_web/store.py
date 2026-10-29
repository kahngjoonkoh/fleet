"""Accounts, login sessions and the audit log, kept in one SQLite file under /data."""

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("FLEET_DB", "/data/fleet-web.sqlite3"))
SESSION_SECONDS = 7 * 24 * 3600
MIN_PASSWORD = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    must_change INTEGER NOT NULL DEFAULT 1,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_login_at INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    ip TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at INTEGER NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    ip TEXT
);
"""


def connect():
    db = sqlite3.connect(DB_PATH, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    return db


# scrypt from the standard library: n=2^15 costs about 50 ms and 32 MB per check.
N, R, P = 2**15, 8, 1


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, maxmem=64 * 1024 * 1024)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${N}${R}${P}${b64(salt)}${b64(digest)}"


def check_password(password, stored):
    _, n, r, p, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r),
                               p=int(p), maxmem=64 * 1024 * 1024)
    return hmac.compare_digest(candidate, base64.b64decode(digest))


# Checked against when the email is unknown, so a wrong email takes as long as a wrong password.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def temp_password():
    return secrets.token_urlsafe(15)  # 20 characters


def password_problem(password):
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > 256:
        return "Use at most 256 characters."
    return None


def audit(db, actor, action, detail=None, ip=None):
    db.execute("INSERT INTO audit (at, actor, action, detail, ip) VALUES (?, ?, ?, ?, ?)",
               (int(time.time()), actor, action, detail, ip))


def create_user(db, email, is_admin=False):
    """Creates an account with a one-time password and returns that password."""
    password = temp_password()
    db.execute("INSERT INTO users (email, pw_hash, is_admin, must_change, created_at) VALUES (?, ?, ?, 1, ?)",
               (email.strip().lower(), hash_password(password), int(is_admin), int(time.time())))
    return password


def reset_password(db, user_id):
    password = temp_password()
    db.execute("UPDATE users SET pw_hash = ?, must_change = 1 WHERE id = ?", (hash_password(password), user_id))
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return password


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db, user_id, ip):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    db.execute("INSERT INTO sessions (token_hash, user_id, csrf, created_at, expires_at, ip) VALUES (?, ?, ?, ?, ?, ?)",
               (_token_hash(token), user_id, secrets.token_urlsafe(24), now, now + SESSION_SECONDS, ip))
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id))
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return token


def session_user(db, token):
    """Returns the session joined with its user, or None if missing, expired or disabled."""
    if not token:
        return None
    return db.execute(
        "SELECT s.token_hash, s.csrf, u.id, u.email, u.is_admin, u.must_change FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0",
        (_token_hash(token), int(time.time()))).fetchone()


def end_session(db, token):
    db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
