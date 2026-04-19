"""
auth_router.py — JWT cookie-based auth for Tether.
Passwords hashed with bcrypt; tokens signed with HS256.
"""
import os
import uuid
import logging
from datetime import datetime, timezone, timedelta

import aiosqlite
import bcrypt
import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from database import DB_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth")

JWT_ALGORITHM = "HS256"
ACCESS_TTL_MIN = 60
REFRESH_TTL_DAYS = 7


def _secret() -> str:
    s = os.environ.get("JWT_SECRET")
    if not s:
        raise RuntimeError("JWT_SECRET env var is not set.")
    return s


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(user_id: str, email: str) -> str:
    return jwt.encode(
        {"sub": user_id, "email": email, "type": "access",
         "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TTL_MIN)},
        _secret(), algorithm=JWT_ALGORITHM,
    )


def create_refresh_token(user_id: str) -> str:
    return jwt.encode(
        {"sub": user_id, "type": "refresh",
         "exp": datetime.now(timezone.utc) + timedelta(days=REFRESH_TTL_DAYS)},
        _secret(), algorithm=JWT_ALGORITHM,
    )


def _set_cookies(response: Response, access: str, refresh: str) -> None:
    kw = dict(httponly=True, secure=False, samesite="lax", path="/")
    response.set_cookie("access_token",  access,  max_age=ACCESS_TTL_MIN * 60, **kw)
    response.set_cookie("refresh_token", refresh, max_age=REFRESH_TTL_DAYS * 86400, **kw)


async def get_current_user(request: Request) -> dict:
    """Shared dependency — resolves and returns the authenticated user dict."""
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, _secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user_id = payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    user = dict(row)
    user.pop("password_hash", None)
    return user


# ── Pydantic models ────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/register")
async def register(req: RegisterRequest, response: Response):
    email = req.email.lower().strip()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM users WHERE email = ?", (email,)) as cur:
            if await cur.fetchone():
                raise HTTPException(status_code=400, detail="Email already registered")
        user_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO users (id, email, password_hash, name, role, created_at) "
            "VALUES (?, ?, ?, ?, 'user', ?)",
            (user_id, email, hash_password(req.password), req.name,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    _set_cookies(response, create_access_token(user_id, email), create_refresh_token(user_id))
    return {"id": user_id, "email": email, "name": req.name, "role": "user"}


@router.post("/login")
async def login(req: LoginRequest, response: Response):
    email = req.email.lower().strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE email = ?", (email,)) as cur:
            row = await cur.fetchone()
    if not row or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user_id = row["id"]
    _set_cookies(response, create_access_token(user_id, email), create_refresh_token(user_id))
    return {"id": user_id, "email": row["email"], "name": row["name"] or "", "role": row["role"] or "user"}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return {"message": "Logged out"}


@router.get("/me")
async def get_me(request: Request):
    user = await get_current_user(request)
    return {"id": user["id"], "email": user["email"],
            "name": user.get("name", ""), "role": user.get("role", "user")}


@router.post("/refresh")
async def refresh_token(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=401, detail="No refresh token")
    try:
        payload = jwt.decode(token, _secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user_id = payload["sub"]
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    access = create_access_token(user_id, row["email"])
    response.set_cookie("access_token", access, httponly=True, secure=False,
                        samesite="lax", max_age=ACCESS_TTL_MIN * 60, path="/")
    return {"message": "Token refreshed"}
