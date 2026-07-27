import traceback
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.routers.auth import router as auth_router
from app.database import engine, Base

# Rate Limiter setup (using client IP address as key)
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Aegis Authentication API",
    version="1.0.0",
    description="Production-grade secure Google Identity and JWT Auth backend",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Configure rate limiter exceptions
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS Configuration (strictly configured, no wildcard *)
origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Cookie"],
)

# Global Exception Handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"[ERROR] Global exception caught: {exc}")
    traceback.print_exc()
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred. Please contact the administrator."}
    )

# Development tables initialization
@app.on_event("startup")
def on_startup():
    if settings.DATABASE_URL.startswith("sqlite"):
        print("[INFO] SQLite database environment detected. Pre-initializing tables...")
        Base.metadata.create_all(bind=engine)

app.include_router(auth_router)
