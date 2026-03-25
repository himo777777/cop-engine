"""
COP Engine — Security Middleware
================================
Rate limiting, request validation, och säkerhetsheaders.
"""

import os
import logging
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("cop.security")

# Rate limiting config
RATE_LIMIT = int(os.getenv("COP_RATE_LIMIT", "60"))  # requests per minute
RATE_LIMIT_AUTH = int(os.getenv("COP_RATE_LIMIT_AUTH", "10"))  # auth attempts per minute


def add_security_headers(app: FastAPI):
    """Lägg till säkerhetsheaders på alla svar."""

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        response: Response = await call_next(request)

        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        # Cache control for API responses
        if request.url.path.startswith("/api") or request.url.path.startswith("/auth"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"

        return response


def setup_rate_limiting(app: FastAPI):
    """Konfigurera rate limiting."""
    try:
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.util import get_remote_address
        from slowapi.errors import RateLimitExceeded

        limiter = Limiter(key_func=get_remote_address)
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        logger.info(f"Rate limiting configured: {RATE_LIMIT}/min general, {RATE_LIMIT_AUTH}/min auth")
        return limiter
    except ImportError:
        logger.warning("slowapi not installed, rate limiting disabled")
        return None


def setup_security(app: FastAPI):
    """Aktivera alla säkerhetsfunktioner."""
    add_security_headers(app)
    limiter = setup_rate_limiting(app)
    logger.info("Security middleware configured")
    return limiter
