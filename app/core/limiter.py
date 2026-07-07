"""
app/core/limiter.py

Single shared Limiter instance for slowapi rate limiting.

Why a separate module?
  auth.py needs @limiter.limit() decorators, but limiter was previously
  created in main.py. Importing from main.py in auth.py creates a circular
  import (main imports auth, auth imports main). Moving it here breaks
  the cycle — both main.py and auth.py import from this neutral module.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address
from app.core.config import settings

limiter = Limiter(key_func=get_remote_address, storage_uri=settings.REDIS_URL)
