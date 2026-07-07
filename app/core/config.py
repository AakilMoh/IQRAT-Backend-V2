from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Project ────────────────────────────────────────────────────────────────
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "IQRAT"
    ENVIRONMENT: str = "development"          # "development" | "production"

    # ── Database ───────────────────────────────────────────────────────────────
    # In development: set individual fields in .env
    # In production (Railway/Render): set DATABASE_URL directly — takes priority
    DATABASE_URL: str = ""
    POSTGRES_USER: str = ""
    POSTGRES_PASSWORD: str = ""
    POSTGRES_SERVER: str = ""
    POSTGRES_PORT: str = "5432"
    POSTGRES_DB: str = ""

    @property
    def db_url(self) -> str:
        """Returns the final DB URL — prefers DATABASE_URL if set (Railway/Neon style)."""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # ── JWT / Security ─────────────────────────────────────────────────────────
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # ── Cloudinary ─────────────────────────────────────────────────────────────
    CLOUDINARY_CLOUD_NAME: str
    CLOUDINARY_API_KEY: str
    CLOUDINARY_API_SECRET: str

    # ── SMTP (Gmail) ───────────────────────────────────────────────────────────
    SMTP_EMAIL: str
    SMTP_PASSWORD: str                        # Gmail App Password (16 chars, no spaces)
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587

    # ── Redis ──────────────────────────────────────────────────────────────────
    # Local dev default — override in production with your Redis provider URL
    REDIS_URL: str = "redis://localhost:6379/0"
    OTP_EXPIRE_SECONDS: int = 600             # 10 minutes

    # ── CORS ───────────────────────────────────────────────────────────────────
    FRONTEND_URL: str = "http://localhost:5173"

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    LOGIN_RATE_LIMIT: str = "10/minute"
    OTP_RATE_LIMIT: str = "5/minute"

    class Config:
        env_file = ".env"
        case_sensitive = True
        # Allows extra fields in .env without crashing (useful during migration)
        extra = "ignore"


# Single shared instance — imported everywhere as: from app.core.config import settings
settings = Settings()