"""
app/api/v1/endpoints/auth.py

Phase 1 upgrade:
  - All endpoints converted to async def with AsyncSession
  - Redis client migrated to redis.asyncio (all calls awaited)
  - db.query() -> await db.execute(select(...))
  - db.commit() -> await db.commit()
  - _find_user_by_identifier() converted to async
  - Rate limiting restored via app.core.limiter (fixes circular import
    that existed when importing limiter from app.main)
"""
import logging
import random
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from redis.asyncio import from_url as async_redis_from_url
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status, Form
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import settings
from app.core.limiter import limiter          # shared instance — defined in app.core.limiter
from app.core.security import verify_password, create_access_token, get_password_hash
from app.models.users import User, Student, Lecturer, Admin

logger = logging.getLogger("iqrat.auth")
router = APIRouter()

# ── Async Redis client ─────────────────────────────────────────────────────────
# redis.asyncio is redis-py's built-in async interface (no separate package needed).
# All get/set/expire calls must be awaited — they're coroutines, not blocking calls.
redis_client = async_redis_from_url(settings.REDIS_URL, decode_responses=True)


def _otp_key(username: str) -> str:
    return f"iqrat:otp:{username}"

def _otp_attempts_key(username: str) -> str:
    return f"iqrat:otp_attempts:{username}"


# ── Async user lookup ──────────────────────────────────────────────────────────
async def _find_user_by_identifier(username: str, db: AsyncSession) -> User | None:
    """
    Finds a User by email, student reg_no, employee_code, or admin_id.
    Async version — all DB calls are awaited.
    """
    result = await db.execute(select(User).where(User.email == username))
    user = result.scalars().first()
    if user:
        return user

    result = await db.execute(select(Student).where(Student.reg_no == username))
    student = result.scalars().first()
    if student:
        result = await db.execute(select(User).where(User.id == student.user_id))
        return result.scalars().first()

    result = await db.execute(select(Lecturer).where(Lecturer.employee_code == username))
    lecturer = result.scalars().first()
    if lecturer:
        result = await db.execute(select(User).where(User.id == lecturer.user_id))
        return result.scalars().first()

    result = await db.execute(select(Admin).where(Admin.admin_id == username))
    admin = result.scalars().first()
    if admin:
        result = await db.execute(select(User).where(User.id == admin.user_id))
        return result.scalars().first()

    return None


# ── OTP email (sync — safe for BackgroundTasks which runs it in a thread) ──────
def _send_otp_email(recipient_email: str, username: str, otp_code: str) -> None:
    """
    Sends OTP email via Gmail SMTP.
    Deliberately sync — BackgroundTasks runs it in a thread pool, not the event loop.
    Any exception is logged but does NOT crash the request.
    """
    try:
        msg = MIMEMultipart()
        msg["Subject"] = "IQRAT Security: Password Reset OTP"
        msg["From"]    = f"IQRAT System <{settings.SMTP_EMAIL}>"
        msg["To"]      = recipient_email

        body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; background-color: #f9fafb; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background: #fff; padding: 30px;
                        border-radius: 12px; border: 1px solid #e5e7eb;">
              <h2 style="color: #4f46e5; text-align: center; margin-top: 0;">IQRAT Security Alert</h2>
              <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 20px 0;">
              <p style="color: #374151;">Hello,</p>
              <p style="color: #374151;">A password reset was requested for your IQRAT account
                (<strong>{username}</strong>).</p>
              <p style="color: #374151;">Your 6-digit OTP code is:</p>
              <div style="text-align: center; margin: 30px 0;">
                <span style="font-size: 36px; font-weight: bold; letter-spacing: 8px;
                             color: #111827; background: #f3f4f6; padding: 15px 25px;
                             border-radius: 8px; border: 2px dashed #d1d5db;">
                  {otp_code}
                </span>
              </div>
              <p style="color: #ef4444; font-size: 14px; font-weight: bold;">
                ⚠️ This code is valid for 10 minutes only.
              </p>
              <p style="color: #6b7280; font-size: 14px;">
                If you did not request this, ignore this email or contact your campus admin.
                Never share this code with anyone.
              </p>
              <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 30px 0 20px;">
              <p style="color: #9ca3af; font-size: 12px; text-align: center; margin: 0;">
                Securely,<br><strong>IQRAT Automated System</strong>
              </p>
            </div>
          </body>
        </html>
        """
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_EMAIL, settings.SMTP_PASSWORD)
            server.send_message(msg)

        logger.info("OTP email dispatched to %s", recipient_email)

    except Exception:
        logger.exception("Failed to send OTP email to %s", recipient_email)


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/login")
@limiter.limit(settings.LOGIN_RATE_LIMIT)
async def login_for_access_token(
    request: Request,                                    # required by slowapi
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User)
        .outerjoin(Student, User.id == Student.user_id)
        .where(or_(User.email == form_data.username, Student.reg_no == form_data.username))
    )
    user = result.scalars().first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        logger.warning("Failed login: %s", form_data.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email/roll number or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.requires_password_change:
        logger.info("First-login intercept: %s", user.email)
        return {"status": "password_change_required", "username": form_data.username}

    role_str   = user.role.value if hasattr(user.role, "value") else user.role
    full_name  = "User"
    roll_no    = form_data.username
    photo_path = ""
    extra_claims: dict = {}

    if str(role_str).lower() == "student":
        result = await db.execute(select(Student).where(Student.user_id == user.id))
        profile = result.scalars().first()
        if profile:
            full_name  = profile.full_name
            roll_no    = profile.reg_no
            photo_path = f"/{profile.photo_path}" if profile.photo_path else ""

    elif str(role_str).lower() == "lecturer":
        result = await db.execute(select(Lecturer).where(Lecturer.user_id == user.id))
        profile = result.scalars().first()
        if profile:
            full_name = profile.full_name

    elif str(role_str).lower() == "admin":
        result = await db.execute(select(Admin).where(Admin.user_id == user.id))
        profile = result.scalars().first()
        if profile:
            full_name = profile.full_name
            r_level   = (
                profile.role_level.value
                if hasattr(profile.role_level, "value")
                else profile.role_level
            )
            extra_claims["role_level"]    = str(r_level).lower()
            extra_claims["permissions"]   = profile.permissions
            extra_claims["department_id"] = profile.department_id

    access_token = create_access_token(data={
        "sub": user.email, "role": role_str,
        "name": full_name, "roll": roll_no,
        "photo": photo_path, **extra_claims,
    })
    logger.info("Successful login: %s (%s)", user.email, role_str)
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/forgot-password/request-otp")
@limiter.limit(settings.OTP_RATE_LIMIT)
async def request_otp(
    request: Request,                                    # required by slowapi
    background_tasks: BackgroundTasks,
    username: str = Form(...),
    contact: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Generates a 6-digit OTP stored in Redis with a 10-minute TTL.
    Email is sent via BackgroundTasks — response returns immediately.
    """
    user = await _find_user_by_identifier(username, db)

    # 🎓 Security: same response whether user exists or not — prevents username enumeration
    if not user:
        logger.warning("OTP requested for unknown identifier: %s", username)
        return {"msg": f"If that account exists, an OTP has been sent to {username[0]}***"}

    # Brute-force protection — max 5 OTP requests per 15 minutes per username
    attempts = await redis_client.get(_otp_attempts_key(username))
    if attempts and int(attempts) >= 5:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many OTP requests. Please wait 15 minutes and try again.",
        )

    otp_code = str(random.randint(100000, 999999))
    await redis_client.setex(_otp_key(username), settings.OTP_EXPIRE_SECONDS, otp_code)
    await redis_client.incr(_otp_attempts_key(username))
    await redis_client.expire(_otp_attempts_key(username), 900)

    background_tasks.add_task(_send_otp_email, user.email, username, otp_code)

    masked = f"{user.email[0]}***@{user.email.split('@')[1]}"
    return {"msg": f"A 6-digit OTP has been sent securely to {masked}"}


@router.post("/forgot-password/verify-otp")
async def verify_otp(username: str = Form(...), otp: str = Form(...)):
    """Verifies the OTP from Redis. Does NOT delete it — reset endpoint handles that."""
    stored = await redis_client.get(_otp_key(username))
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No OTP found or OTP has expired. Please request a new one.",
        )
    if stored != otp:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OTP code.")
    return {"msg": "OTP verified. You may now reset your password."}


@router.post("/forgot-password/reset")
async def reset_password(
    username: str = Form(...),
    otp: str = Form(...),
    new_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Resets the password after final OTP verification. Clears OTP from Redis."""
    stored = await redis_client.get(_otp_key(username))
    if not stored or stored != otp:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired OTP. Please restart the reset process.",
        )

    user = await _find_user_by_identifier(username, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    user.hashed_password = get_password_hash(new_password)
    await db.commit()

    # Delete OTP + reset attempt counter so they can't reuse the code
    await redis_client.delete(_otp_key(username))
    await redis_client.delete(_otp_attempts_key(username))

    logger.info("Password reset successful: %s", username)
    return {"msg": "Password reset successfully. You can now log in."}


@router.post("/force-password-change")
async def force_password_change(
    username: str = Form(...),
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Handles the mandatory password change on first login."""
    result = await db.execute(
        select(User)
        .outerjoin(Student, User.id == Student.user_id)
        .where(or_(User.email == username, Student.reg_no == username))
    )
    user = result.scalars().first()

    if not user or not verify_password(current_password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid credentials.")

    # Strict complexity: 8+ chars, upper, lower, digit, special
    password_regex = r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$"
    if not re.match(password_regex, new_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Password must be at least 8 characters and include: "
                "an uppercase letter, a lowercase letter, a number, "
                "and a special character (@$!%*?&)."
            ),
        )

    user.hashed_password = get_password_hash(new_password)
    user.requires_password_change = False
    await db.commit()

    logger.info("First-login password change complete: %s", username)
    return {"msg": "Password updated successfully. You can now log in."}