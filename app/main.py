import os
import logging

import cloudinary
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import settings
from app.db.session import engine    # async engine — used only for the /health check

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.core.limiter import limiter  # shared instance — also used by auth.py


# ── Logging setup (replaces ALL print() statements) ────────────────────────────
# 🎓 Concept: Python's logging module gives you levels (DEBUG, INFO, WARNING,
#    ERROR, CRITICAL). Unlike print(), log entries include timestamps, severity,
#    and can be shipped to external tools (Datadog, Sentry) with zero code change.
logging.basicConfig(
    level=logging.INFO if settings.ENVIRONMENT == "production" else logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("iqrat")

# ── Cloudinary — credentials from environment, never hardcoded ─────────────────
cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
    secure=True,
)
logger.info("Cloudinary configured for cloud: %s", settings.CLOUDINARY_CLOUD_NAME)

# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.PROJECT_NAME,
    version="2.0.0",
    # Hide docs in production (optional — remove if you want Swagger in prod)
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
)

# ── Rate limiter (slowapi + Redis) ────────────────────────────────────────────
# limiter instance lives in app.core.limiter — imported above
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ───────────────────────────────────────────────────────────────────────
origins = [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
    "https://iqrat.vercel.app",
]

frontend_url = settings.FRONTEND_URL.rstrip("/")
if frontend_url and frontend_url not in origins:
    origins.append(frontend_url)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files — local dev only (Phase 1 migrates all to Cloudinary) ────────
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Routers ────────────────────────────────────────────────────────────────────
# attendance.py is intentionally NOT registered — dead code (see Phase 0 cleanup)
from app.api.v1.endpoints import auth, users, academic, system  # noqa: E402

app.include_router(auth.router,     prefix="/api/v1/auth",     tags=["Authentication"])
app.include_router(users.router,    prefix="/api/v1/users",    tags=["Users"])
app.include_router(academic.router, prefix="/api/v1/academic", tags=["Academic"])
app.include_router(system.router,   prefix="/api/v1/system",   tags=["System"])

# ── Global exception handler ───────────────────────────────────────────────────
# 🎓 Concept: Without this, any unhandled exception returns a raw 500 with a
#    Python traceback — leaking your internal code structure to the client.
#    This catches everything and returns a clean JSON response instead.
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal server error occurred. Please try again later."},
    )

# ── Health check ───────────────────────────────────────────────────────────────
# 🎓 This is what Railway/Render pings to know your app is alive.
#    Phase 1: rewritten as async def — sync .execute() doesn't work on an
#    async engine. We open a raw async connection directly from the engine
#    (no full session needed just to run SELECT 1).
@app.get("/health", tags=["Health"], include_in_schema=False)
@app.head("/health", include_in_schema=False)
async def health_check():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as e:
        logger.error("Health check DB failure: %s", e)
        db_status = "error"

    status_code = 200 if db_status == "ok" else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "healthy" if db_status == "ok" else "degraded",
            "database": db_status,
            "version": "2.0.0",
            "environment": settings.ENVIRONMENT,
        },
    )

@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
def read_root():
    return {"message": "IQRAT API v2.0 — /health for status, /docs for API reference"}