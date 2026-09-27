"""Encrypts secrets (Manus keys, YouTube refresh tokens) before they hit SQLite."""
import os
import secrets
from cryptography.fernet import Fernet, InvalidToken
import config


def _load_key() -> bytes:
    env = os.getenv("APP_ENCRYPTION_KEY")
    if env:
        return env.encode()
    path = config.DATA_DIR / ".enc_key"
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path.read_bytes().strip()


_fernet = Fernet(_load_key())


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode() if value else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def flask_secret() -> str:
    path = config.DATA_DIR / ".flask_secret"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path.read_text().strip()
