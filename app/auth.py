"""Authentication: registration, admin-approval gate, login, sessions.

Deliberately self-contained (one module + its templates) so the whole
account system can be simplified or removed if Bookbarge pivots to a
standalone fork-and-run project. No TOTP flow by 2026-07-11 decision;
the schema columns remain for a later opt-in.

Passwords: bcrypt directly (not passlib — unmaintained, incompatible
with bcrypt>=4.1). Sessions: server-side rows in SQLite; the cookie is
a random token with Secure/HttpOnly/SameSite=Lax, 7-day sliding expiry.
"""

import secrets
import sqlite3

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db

SESSION_COOKIE = "bookbarge_session"
SESSION_DAYS = 7
MIN_PASSWORD_LEN = 8

router = APIRouter()
templates: Jinja2Templates = None  # set by main.py to share one instance


# --- passwords ---------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# Constant-ish work for login attempts on unknown emails, so response time
# doesn't reveal whether an account exists.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


# --- sessions ----------------------------------------------------------------

def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at) "
        f"VALUES (?, ?, datetime('now', '+{SESSION_DAYS} days'))",
        (token, user_id),
    )
    conn.commit()
    return token


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 86400,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def current_user(request: Request) -> sqlite3.Row | None:
    """Resolve the session cookie to a user row, sliding the expiry."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > datetime('now')""",
            (token,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            f"UPDATE sessions SET expires_at = datetime('now', '+{SESSION_DAYS} days') "
            "WHERE token = ?",
            (token,),
        )
        conn.commit()
        return row
    finally:
        conn.close()


def require_user(request: Request) -> sqlite3.Row:
    """Dependency for protected routes; redirects anonymous users to login.

    Ownership rule for everything built on top of this: routes touching a
    project/chapter/etc. must filter by user_id from this row in the SQL
    itself — resource IDs in URLs are never trusted alone.
    """
    user = current_user(request)
    if user is None:
        raise _RedirectToLogin()
    return user


class _RedirectToLogin(Exception):
    pass


# --- routes ------------------------------------------------------------------

@router.get("/register")
async def register_form(request: Request):
    return templates.TemplateResponse(request, "register.html", {"error": None})


@router.post("/register")
async def register(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    error = None
    if "@" not in email or "." not in email.split("@")[-1]:
        error = "That doesn't look like an email address."
    elif len(password) < MIN_PASSWORD_LEN:
        error = f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if error:
        return templates.TemplateResponse(
            request, "register.html", {"error": error}, status_code=422)

    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            (email, hash_password(password)),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return templates.TemplateResponse(
            request, "register.html",
            {"error": "That email is already registered."}, status_code=422)
    finally:
        conn.close()
    return RedirectResponse("/pending", status_code=303)


@router.get("/pending")
async def pending(request: Request):
    return templates.TemplateResponse(request, "pending.html")


@router.get("/login")
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    conn = db.connect()
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None:
            verify_password(password, _DUMMY_HASH)
            ok = False
        else:
            ok = verify_password(password, user["password_hash"])
        if not ok:
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Invalid email or password."}, status_code=401)

        # Honest pending message — but only after the password proved out,
        # so approval status is never revealed to someone guessing emails.
        if not user["is_approved"]:
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Your account is pending approval. You'll be able "
                          "to log in once an admin approves it."},
                status_code=403)

        token = create_session(conn, user["id"])
    finally:
        conn.close()

    response = RedirectResponse("/dashboard", status_code=303)
    set_session_cookie(response, token)
    return response


@router.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        conn = db.connect()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
