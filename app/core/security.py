"""
app/core/security.py

Phase 0 fix:
  - datetime.utcnow() → datetime.now(timezone.utc)

  Why this matters:
  datetime.utcnow() returns a "naive" datetime — a datetime with no timezone
  info attached. Python 3.12 deprecated it because naive datetimes cause silent
  bugs when compared across timezones. datetime.now(timezone.utc) returns an
  "aware" datetime — it explicitly carries UTC timezone info, so comparisons
  are always unambiguous. Same UTC time, just correctly labeled.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt
from passlib.context import CryptContext

from app.core.config import settings

# ── Password hashing ───────────────────────────────────────────────────────────
# bcrypt is the industry standard for password hashing — it's slow by design
# (making brute-force attacks expensive) and includes a salt automatically.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# ── JWT creation ───────────────────────────────────────────────────────────────
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()

    expire = datetime.now(timezone.utc) + (
        expires_delta
        if expires_delta
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)