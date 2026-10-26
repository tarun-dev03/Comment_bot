import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings


def get_fernet() -> Fernet:
    settings = get_settings()
    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    fernet_key = __import__("base64").urlsafe_b64encode(digest)
    return Fernet(fernet_key)


def encrypt_text(plain: str) -> str:
    return get_fernet().encrypt(plain.encode()).decode()


def decrypt_text(cipher: str) -> str:
    try:
        return get_fernet().decrypt(cipher.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Invalid encrypted token") from e


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_login_token() -> str:
    return secrets.token_urlsafe(32)


def login_token_expiry(minutes: int = 15) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


TEN_YEARS = 60 * 60 * 24 * 365 * 10


def session_serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(settings.session_secret, salt="comment-bot-session")


def create_session_cookie(user_id: int) -> str:
    return session_serializer().dumps({"user_id": user_id})


def apply_session_cookie(response, user_id: int, request=None) -> None:
    from app.config import get_settings

    settings = get_settings()
    is_secure = False
    if request:
        proto = request.headers.get("x-forwarded-proto", "").lower()
        scheme = getattr(request.url, "scheme", "").lower()
        is_secure = (proto == "https" or scheme == "https")
    else:
        is_secure = settings.use_secure_cookies()

    response.set_cookie(
        "session",
        create_session_cookie(user_id),
        httponly=True,
        samesite="lax",
        secure=is_secure,
        max_age=TEN_YEARS,
        path="/",
    )
    # Remove any explicit logout flag so session takes effect
    try:
        response.delete_cookie("logged_out", path="/")
    except Exception:
        pass


def load_session_cookie(cookie: str, max_age_seconds: int = TEN_YEARS) -> int | None:
    try:
        data = session_serializer().loads(cookie, max_age=max_age_seconds)
        return int(data["user_id"])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return None
