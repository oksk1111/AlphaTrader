"""
Dashboard Authentication Module
- API key based auth (env var or auto-generated)
- Google OAuth login with JWT session
- Rate limiting on auth failures
- Cookie support for browser sessions
"""

import os
import secrets
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import requests as http_requests
from fastapi import Request, HTTPException, Response
from fastapi.responses import JSONResponse, RedirectResponse


# --- API Key Management ---

def _load_or_generate_api_key() -> str:
    """Load DASHBOARD_API_KEY from env, or generate a random one."""
    key = os.environ.get("DASHBOARD_API_KEY", "").strip()
    if key:
        print("[Auth] DASHBOARD_API_KEY loaded from environment variable.")
        return key

    key = secrets.token_urlsafe(32)
    print("=" * 60)
    print("[Auth] WARNING: DASHBOARD_API_KEY not set in environment.")
    print(f"[Auth] Generated random key for this session:")
    print(f"[Auth]   {key}")
    print(f"[Auth] Access dashboard at: http://<host>:8501/?key={key}")
    print(f"[Auth] Set DASHBOARD_API_KEY env var to use a persistent key.")
    print("=" * 60)
    return key


API_KEY: str = _load_or_generate_api_key()

# Cookie names
AUTH_COOKIE_NAME = "dashboard_auth"  # API key cookie (legacy)
SESSION_COOKIE_NAME = "session"       # Google OAuth JWT session cookie
# Cookie max age: 30 days
AUTH_COOKIE_MAX_AGE = 30 * 24 * 60 * 60


# --- JWT Secret ---

def _load_or_generate_jwt_secret() -> str:
    """Load JWT_SECRET from env, or derive from API_KEY."""
    secret = os.environ.get("JWT_SECRET", "").strip()
    if secret:
        return secret
    # Derive from API_KEY for consistency across restarts (if API_KEY is set)
    return f"jwt-{API_KEY}-secret"


JWT_SECRET: str = _load_or_generate_jwt_secret()
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 30


# --- Simple Password Login ---

LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", "").strip()
if LOGIN_PASSWORD:
    print("[Auth] Password login enabled.")
else:
    print("[Auth] Password login disabled (LOGIN_PASSWORD not set).")


# --- Google OAuth Config ---

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
OAUTH_REDIRECT_URI = os.environ.get(
    "OAUTH_REDIRECT_URI", "http://158.180.81.25:8501/auth/callback"
).strip()

# Allowed emails (comma-separated). Empty = allow all Google accounts.
_allowed_emails_raw = os.environ.get("ALLOWED_EMAILS", "").strip()
ALLOWED_EMAILS: list = [
    e.strip().lower() for e in _allowed_emails_raw.split(",") if e.strip()
]

if GOOGLE_CLIENT_ID:
    print(f"[Auth] Google OAuth enabled. Redirect URI: {OAUTH_REDIRECT_URI}")
    if ALLOWED_EMAILS:
        print(f"[Auth] Allowed emails: {ALLOWED_EMAILS}")
    else:
        print("[Auth] WARNING: ALLOWED_EMAILS not set — any Google account can log in.")
else:
    print("[Auth] Google OAuth not configured (GOOGLE_CLIENT_ID not set).")


def verify_password(password: str) -> bool:
    """Verify login password (constant-time comparison)."""
    if not password:
        return False
    return secrets.compare_digest(password.strip(), LOGIN_PASSWORD)


# --- Rate Limiting ---

# Track failed auth attempts: {ip: [(timestamp, ...), ...]}
_failed_attempts: dict = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_FAILURES = 5


def _is_rate_limited(client_ip: str) -> bool:
    """Check if client IP has exceeded max auth failures in the window."""
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW

    # Clean old entries
    _failed_attempts[client_ip] = [
        ts for ts in _failed_attempts[client_ip] if ts > cutoff
    ]

    return len(_failed_attempts[client_ip]) >= RATE_LIMIT_MAX_FAILURES


def _record_failure(client_ip: str):
    """Record a failed auth attempt."""
    _failed_attempts[client_ip].append(time.time())


def _get_client_ip(request: Request) -> str:
    """Extract client IP, respecting X-Forwarded-For for proxied requests."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --- API Key Auth Extraction ---

def _extract_key_from_request(request: Request) -> Optional[str]:
    """
    Extract API key from request in priority order:
    1. Authorization: Bearer <key> header
    2. ?key=<key> query parameter
    3. dashboard_auth cookie
    """
    # 1. Authorization header
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    # 2. Query parameter
    key_param = request.query_params.get("key")
    if key_param:
        return key_param.strip()

    # 3. Cookie
    cookie_key = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie_key:
        return cookie_key.strip()

    return None


def verify_key(provided_key: Optional[str]) -> bool:
    """Constant-time comparison of provided key against the API key."""
    if not provided_key:
        return False
    return secrets.compare_digest(provided_key, API_KEY)


# --- Google OAuth Functions ---

def get_google_auth_url(state: str = "") -> str:
    """Generate Google OAuth authorization URL."""
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "email profile",
        "access_type": "offline",
        "prompt": "select_account",
    }
    if state:
        params["state"] = state
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"


def exchange_code_for_user(code: str) -> Optional[dict]:
    """
    Exchange OAuth authorization code for user info.
    Returns dict with 'email', 'name', 'picture' or None on failure.
    """
    try:
        # Exchange code for token
        token_resp = http_requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        token_data = token_resp.json()

        if "access_token" not in token_data:
            print(f"[Auth] OAuth token exchange failed: {token_data.get('error', 'unknown')}")
            return None

        # Fetch user info
        user_resp = http_requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
            timeout=10,
        )
        user_info = user_resp.json()

        return {
            "email": user_info.get("email", ""),
            "name": user_info.get("name", ""),
            "picture": user_info.get("picture", ""),
        }
    except Exception as e:
        print(f"[Auth] OAuth exchange error: {e}")
        return None


def is_email_allowed(email: str) -> bool:
    """Check if email is in the allowed list. Empty list = allow all."""
    if not ALLOWED_EMAILS:
        return True
    return email.lower().strip() in ALLOWED_EMAILS


# --- JWT Session Management ---

def create_session_token(email: str, name: str) -> str:
    """Create a JWT session token."""
    payload = {
        "email": email,
        "name": name,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_session_token(token: str) -> Optional[dict]:
    """
    Verify a JWT session token.
    Returns payload dict with 'email', 'name' or None if invalid/expired.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return {"email": payload.get("email", ""), "name": payload.get("name", "")}
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def _extract_session_from_request(request: Request) -> Optional[dict]:
    """Extract and verify Google OAuth session from cookie."""
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_cookie:
        return None
    return verify_session_token(session_cookie)


# --- Public Auth Functions ---

def _is_browser_request(request: Request) -> bool:
    """Determine if this is a browser request (vs API/programmatic)."""
    # API paths always get JSON responses
    if request.url.path.startswith("/api/"):
        return False
    # Check Accept header
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return True
    # Default: treat as browser if not explicitly asking for JSON
    if "application/json" in accept:
        return False
    return True


def require_auth(request: Request) -> bool:
    """
    Verify authentication for a request.
    - For browser requests: redirects to /login on failure
    - For API requests: raises HTTPException 401
    Returns True if authenticated.
    """
    client_ip = _get_client_ip(request)

    # Check rate limit first
    if _is_rate_limited(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many failed authentication attempts. Try again later."
        )

    # 1. Try API key auth (header, query param, dashboard_auth cookie)
    provided_key = _extract_key_from_request(request)
    if verify_key(provided_key):
        return True

    # 2. Try Google OAuth session cookie
    session = _extract_session_from_request(request)
    if session and is_email_allowed(session.get("email", "")):
        return True

    # Record failure only if they actually tried a key
    if provided_key is not None:
        _record_failure(client_ip)

    # Redirect browsers to login, return 401 for API calls
    if _is_browser_request(request):
        raise HTTPException(
            status_code=302,
            detail="Redirect to login",
            headers={"Location": "/login"},
        )

    raise HTTPException(
        status_code=401,
        detail="Unauthorized. Provide API key via Authorization header, ?key= parameter, or log in with Google."
    )


def is_authenticated(request: Request) -> bool:
    """
    Check if request is authenticated without raising exceptions.
    Used for endpoints that work differently based on auth status.
    """
    client_ip = _get_client_ip(request)
    if _is_rate_limited(client_ip):
        return False

    # Check API key
    provided_key = _extract_key_from_request(request)
    if verify_key(provided_key):
        return True

    # Check Google OAuth session
    session = _extract_session_from_request(request)
    if session and is_email_allowed(session.get("email", "")):
        return True

    return False


def set_auth_cookie(response: Response, key: str):
    """Set the auth cookie on a response for browser sessions."""
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=key,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def set_session_cookie(response: Response, token: str):
    """Set the Google OAuth session cookie."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response):
    """Clear the session cookie (logout)."""
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    response.delete_cookie(key=AUTH_COOKIE_NAME)
