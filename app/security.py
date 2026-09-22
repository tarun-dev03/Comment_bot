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


def session_serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(settings.session_secret, salt="comment-bot-session")


def create_session_cookie(user_id: int) -> str:
    return session_serializer().dumps({"user_id": user_id})


def load_session_cookie(cookie: str, max_age_seconds: int = 60 * 60 * 24 * 30) -> int | None:
    try:
        data = session_serializer().loads(cookie, max_age=max_age_seconds)
        return int(data["user_id"])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return None
