"""
User accounts and sign-in sessions.

- Passwords are stored as salted scrypt hashes (standard library only).
- A sign-in creates a random session token. The browser gets it in an
  HttpOnly, SameSite=Lax cookie; the server stores only its SHA-256, so a
  leaked users.sqlite cannot be replayed as a login.
- `current_user` is the FastAPI dependency every protected route uses.

Storage lives in data/users.sqlite, separate from any user database.
"""

import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app import config

logger = logging.getLogger("auth")

USERS_DB = os.path.join(config.DATA_DIR, "users.sqlite")
COOKIE_NAME = "eng2sql_session"

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256  # bounds the work an attacker can make scrypt do

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1

# Failed sign-ins allowed per (client, email) before a temporary lockout.
MAX_FAILED_SIGNINS = 5
LOCKOUT_SECONDS = 600

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()

# Called with the new user when the very first account is created, so the
# app can hand over databases that existed before sign-in was added.
on_first_account: list = []


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(USERS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name          TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL REFERENCES users(id),
                expires_at INTEGER NOT NULL
            )
            """
        )
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))


def count_users() -> int:
    with closing(_connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def _public(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "email": row["email"], "name": row["name"]}


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p),
            dklen=len(digest_hex) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# Verified against when the email is unknown, so a wrong email and a wrong
# password take the same time and can't be told apart.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise HTTPException(400, f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")


# --------------------------------------------------------------------------
# Accounts and sessions
# --------------------------------------------------------------------------

def create_user(email: str, name: str, password: str) -> dict:
    email = email.strip().lower()
    name = " ".join(name.split())[:80]

    if not _EMAIL.fullmatch(email) or len(email) > 254:
        raise HTTPException(400, "Enter a valid email address.")
    if not name:
        raise HTTPException(400, "Enter your name.")
    _validate_password(password)

    user = {"id": str(uuid.uuid4()), "email": email, "name": name}
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user["id"], email, name, hash_password(password), int(time.time())),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with this email already exists.")

    return user


def authenticate(email: str, password: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()

    if len(password) > MAX_PASSWORD_LENGTH:
        return None
    if not verify_password(password, row["password_hash"] if row else _DUMMY_HASH):
        return None
    return _public(row) if row else None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + config.SESSION_DAYS * 86400
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (_token_hash(token), user_id, expires_at),
        )
    return token


def user_for_token(token: str | None) -> dict | None:
    if not token:
        return None
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            (_token_hash(token), int(time.time())),
        ).fetchone()
    return _public(row) if row else None


def delete_session(token: str | None) -> None:
    if not token:
        return
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def change_password(user_id: str, current_password: str, new_password: str, keep_token: str | None) -> None:
    """Sets a new password and signs out every other device."""
    _validate_password(new_password)

    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or not verify_password(current_password, row["password_hash"]):
            raise HTTPException(400, "Current password is incorrect.")

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user_id)
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, _token_hash(keep_token or "")),
        )


# --------------------------------------------------------------------------
# Request guards
# --------------------------------------------------------------------------

def same_origin(request: Request) -> None:
    """
    Rejects state-changing requests sent from another website. The session
    cookie is SameSite=Lax already; this is a second, independent check.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if not origin or urlsplit(origin).netloc == request.headers.get("host"):
        return
    if origin in config.ALLOWED_ORIGINS or "*" in config.ALLOWED_ORIGINS:
        return
    raise HTTPException(403, "Cross-site request blocked.")


def current_user(request: Request, _: None = Depends(same_origin)) -> dict:
    """Dependency for protected routes: the signed-in user, or 401."""
    user = user_for_token(request.cookies.get(COOKIE_NAME))
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def _throttle_key(request: Request, email: str) -> str:
    client = request.client.host if request.client else "unknown"
    return f"{client}|{email.strip().lower()}"


def _check_lockout(key: str) -> None:
    cutoff = time.time() - LOCKOUT_SECONDS
    with _failures_lock:
        recent = [t for t in _failures.get(key, []) if t > cutoff]
        if recent:
            _failures[key] = recent
        else:
            _failures.pop(key, None)
        if len(recent) >= MAX_FAILED_SIGNINS:
            raise HTTPException(429, "Too many failed sign-in attempts. Try again in a few minutes.")


def _record_failure(key: str) -> None:
    with _failures_lock:
        _failures.setdefault(key, []).append(time.time())


def _start_session(request: Request, response: Response, user: dict) -> dict:
    response.set_cookie(
        COOKIE_NAME,
        create_session(user["id"]),
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return {"user": user}


def signup_enabled() -> bool:
    # The first account can always be created, or nobody could ever sign in.
    return config.ALLOW_SIGNUP or count_users() == 0


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(same_origin)])


class SignupRequest(BaseModel):
    name: str
    email: str
    password: str


class SigninRequest(BaseModel):
    email: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


@router.get("/status")
def status(request: Request):
    """Always 200 — tells the UI whether to show the app or the sign-in page."""
    user = user_for_token(request.cookies.get(COOKIE_NAME))
    return {
        "authenticated": user is not None,
        "user": user,
        "signup_enabled": signup_enabled(),
        "first_run": count_users() == 0,
    }


@router.post("/signup")
def signup(req: SignupRequest, request: Request, response: Response):
    if not signup_enabled():
        raise HTTPException(403, "Creating new accounts is turned off on this server.")

    first = count_users() == 0
    user = create_user(req.email, req.name, req.password)

    if first:
        for hook in on_first_account:
            hook(user)

    logger.info(f"New account created: {user['id']}")
    return _start_session(request, response, user)


@router.post("/signin")
def signin(req: SigninRequest, request: Request, response: Response):
    key = _throttle_key(request, req.email)
    _check_lockout(key)

    user = authenticate(req.email, req.password)
    if not user:
        _record_failure(key)
        raise HTTPException(401, "Incorrect email or password.")

    with _failures_lock:
        _failures.pop(key, None)

    return _start_session(request, response, user)


@router.post("/signout")
def signout(request: Request, response: Response):
    delete_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "signed_out"}


@router.post("/password")
def update_password(req: PasswordChangeRequest, request: Request, user: dict = Depends(current_user)):
    change_password(
        user["id"], req.current_password, req.new_password, request.cookies.get(COOKIE_NAME)
    )
    return {"status": "updated"}
