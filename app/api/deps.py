"""
app/api/deps.py — Single source of truth for ALL FastAPI dependencies.

Phase 1 upgrade:
  - get_db() now yields AsyncSession (was sync Session)
  - All dependency functions are async (they were already, now fully correct)
  - AsyncGenerator type hint for get_db()

Rule: Never redefine get_db(), get_current_user(), get_current_admin() anywhere else.
"""
import logging
from typing import AsyncGenerator

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.users import User, UserRole

logger = logging.getLogger("iqrat.deps")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


# ── Async database session ─────────────────────────────────────────────────────
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yields an async SQLAlchemy session.

    🎓 AsyncGenerator vs Generator:
      The sync version used Generator[Session, None, None].
      The async version uses AsyncGenerator[AsyncSession, None].
      FastAPI handles both — it knows to 'async for' the async one automatically.
      The finally block still runs after the request completes, closing the session.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


# ── Current authenticated user ─────────────────────────────────────────────────
async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials. Please log in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError as e:
        logger.warning("JWT decode failure: %s", e)
        raise credentials_exception

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalars().first()

    if user is None:
        logger.warning("JWT valid but user not found: %s", email)
        raise credentials_exception

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been deactivated. Contact your administrator.",
        )
    return user


# ── Role guards ────────────────────────────────────────────────────────────────
async def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required.")
    return current_user


async def get_current_student(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.STUDENT:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Student role required.")
    return current_user


async def get_current_lecturer(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.LECTURER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Lecturer role required.")
    return current_user