import hashlib
import hmac
import os
import secrets
import time
from fastapi import Request, Response
from fastapi.responses import RedirectResponse

COOKIE_NAME = "claude_code_web_auth"
PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-SHA256. Returns format: pbkdf2:sha256:iterations$salt_hex$hash_hex"""
    salt = os.urandom(32)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2:sha256:{PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(stored: str, provided: str) -> bool:
    """Verify a password against a PBKDF2 hash or a plain-text fallback."""
    if not stored:
        return False
    # If it looks like a hash, verify as hash
    if stored.startswith("pbkdf2:sha256:"):
        try:
            _, _, iter_str = stored.split(":")[:3]
            _rest = stored.split(":", 3)[3] if ":" in stored[stored.index(":", stored.index(":") + 1) + 1:] else ""
            # Parse: pbkdf2:sha256:200000$salt$hash
            parts = stored.split("$", 1)
            if len(parts) != 2:
                return False
            algo_and_iter = parts[0]  # pbkdf2:sha256:200000
            salt_hex, hash_hex = parts[1].split("$", 1)
            iterations = int(algo_and_iter.split(":")[-1])
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
            dk = hashlib.pbkdf2_hmac('sha256', provided.encode(), salt, iterations)
            return hmac.compare_digest(dk, expected)
        except Exception:
            return False
    # Fallback: plain-text comparison (for backwards compat during migration)
    return stored == provided


class AuthManager:
    def __init__(self, username: str, password_hash: str):
        self.username = username
        self.password_hash = password_hash

    def verify_password(self, password: str) -> bool:
        return verify_password(self.password_hash, password)

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.time() + (24 * 3600)
        self._cleanup()
        return token

    def validate_token(self, token: str) -> bool:
        self._cleanup()
        if token in self._tokens:
            if self._tokens[token] > time.time():
                return True
            del self._tokens[token]
        return False

    def revoke_token(self, token: str):
        self._tokens.pop(token, None)

    def _cleanup(self):
        now = time.time()
        expired = [t for t, exp in self._tokens.items() if exp <= now]
        for t in expired:
            del self._tokens[t]

    _tokens: dict[str, float] = {}

    async def middleware(self, request: Request, call_next):
        public_paths = {"/login", "/static"}
        if request.url.path in public_paths or request.url.path.startswith("/static/"):
            return await call_next(request)

        token = request.cookies.get(COOKIE_NAME)
        if not token or not self.validate_token(token):
            if request.url.path.startswith("/api/") or request.url.path.startswith("/ws/"):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return RedirectResponse(url="/login", status_code=302)

        response = await call_next(request)
        return response


auth_manager: AuthManager = None


def init_auth(username: str, password_hash: str):
    global auth_manager
    auth_manager = AuthManager(username, password_hash)
    return auth_manager


def get_auth() -> AuthManager:
    return auth_manager


def login_required(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token or not auth_manager or not auth_manager.validate_token(token):
        return False
    return True


def set_auth_cookie(response: Response, token: str):
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400,
        secure=False
    )


def clear_auth_cookie(response: Response):
    response.delete_cookie(key=COOKIE_NAME)
