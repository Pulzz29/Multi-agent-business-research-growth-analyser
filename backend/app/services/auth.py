"""
Email/password accounts and server-side sessions (PostgreSQL).

* Passwords: salted scrypt (hashlib, memory-hard; N=2**15, r=8, p=1), stored
  as ``scrypt$N$r$p$<salt hex>$<hash hex>`` and compared in constant time.
  Plaintext passwords are never stored or logged.
* Sessions: a random 256-bit token in an HttpOnly cookie; only its SHA-256 is
  stored, with a CSRF token hash (double-submit check), an absolute expiry
  (SESSION_MAX_HOURS, default 12) and an idle expiry (SESSION_IDLE_MINUTES,
  default 120). Logout revokes the session.
* Unknown emails are checked against a dummy hash so the response time does
  not reveal which accounts exist.
"""

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from app.database import db

SCRYPT_N, SCRYPT_R, SCRYPT_P, SALT_BYTES, KEY_BYTES = 2 ** 15, 8, 1, 16, 32
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}$")


class AuthError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def normalize_email(email):
    text = str(email or "").strip().lower()
    if len(text) > 320 or not EMAIL_RE.match(text):
        raise AuthError("Enter a valid email address.")
    return text


def _scrypt(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          maxmem=256 * 1024 * 1024, dklen=KEY_BYTES)


def hash_password(password):
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise AuthError(f"Passwords must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters.")
    salt = secrets.token_bytes(SALT_BYTES)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${_scrypt(password, salt).hex()}"


def verify_password(password, stored):
    try:
        scheme, n, r, p, salt, digest = str(stored).split("$")
        if scheme != "scrypt":
            return False
        actual = _scrypt(str(password or "")[:MAX_PASSWORD_LENGTH], bytes.fromhex(salt), int(n), int(r), int(p))
        return hmac.compare_digest(actual, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = None


def _dummy_hash():
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- accounts

def create_user(email, password, reset=False):
    """Create an account. Existing accounts are never changed unless ``reset``.
    Returns "created", "exists" or "reset"."""
    email = normalize_email(email)
    password_hash = hash_password(password)
    now = utc_now()
    with db.transaction() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(email, password_hash, status, created_at, updated_at) "
                "VALUES (?, ?, 'active', ?, ?) ON CONFLICT (email) DO NOTHING",
                (email, password_hash, now, now))
            return "created"
        if not reset:
            return "exists"
        conn.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (password_hash, now, row[0]))
        conn.execute("UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now, row[0]))
        return "reset"


def authenticate(email, password):
    """The active user's id and email, or None (same work for unknown emails)."""
    try:
        email = normalize_email(email)
    except AuthError:
        verify_password(password, _dummy_hash())
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT id, email, password_hash, status FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        verify_password(password, _dummy_hash())
        return None
    if not verify_password(password, row["password_hash"]) or row["status"] != "active":
        return None
    return {"id": row["id"], "email": row["email"]}


# ---------------------------------------------------------------- sessions

def session_lifetimes():
    max_hours = float(os.getenv("SESSION_MAX_HOURS", "12"))
    idle_minutes = float(os.getenv("SESSION_IDLE_MINUTES", "120"))
    return timedelta(hours=max_hours), timedelta(minutes=idle_minutes)


def create_session(user, now=None):
    """Returns (session_token, csrf_token, expires_at). Only hashes are stored."""
    now = now or utc_now()
    absolute, idle = session_lifetimes()
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    expires = now + absolute
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO user_sessions(token_sha256, user_id, csrf_sha256, created_at, expires_at, "
            "idle_expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_sha(token), user["id"], _sha(csrf), now, expires, min(expires, now + idle), now))
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user["id"]))
    return token, csrf, expires


def validate_session(token, now=None):
    """The session's user and CSRF hash when valid; extends the idle expiry."""
    if not token or len(token) > 200:
        return None
    now = now or utc_now()
    _, idle = session_lifetimes()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT s.token_sha256, s.csrf_sha256, s.expires_at, u.id, u.email FROM user_sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_sha256 = ? AND s.revoked_at IS NULL "
            "AND s.expires_at > ? AND s.idle_expires_at > ? AND u.status = 'active'",
            (_sha(token), now, now)).fetchone()
        if row is None:
            return None
        expires = row["expires_at"]
        conn.execute("UPDATE user_sessions SET last_seen_at = ?, idle_expires_at = ? WHERE token_sha256 = ?",
                     (now, min(expires, now + idle), row["token_sha256"]))
    return {"id": row["id"], "email": row["email"], "csrf_sha256": row["csrf_sha256"]}


def csrf_matches(session, csrf_token):
    return bool(csrf_token) and hmac.compare_digest(session["csrf_sha256"], _sha(str(csrf_token)))


def revoke_session(token, now=None):
    if not token:
        return
    with db.transaction() as conn:
        conn.execute("UPDATE user_sessions SET revoked_at = ? WHERE token_sha256 = ? AND revoked_at IS NULL",
                     (now or utc_now(), _sha(token)))


def cleanup_sessions(now=None):
    """Delete sessions that expired or were revoked more than a day ago."""
    cutoff = (now or utc_now()) - timedelta(days=1)
    with db.transaction() as conn:
        return max(conn.execute(
            "DELETE FROM user_sessions WHERE expires_at < ? OR revoked_at < ?", (cutoff, cutoff)).rowcount, 0)
