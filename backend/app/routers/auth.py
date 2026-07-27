from typing import List, Optional
from fastapi import APIRouter, Depends, Request, Response, HTTPException, status, Cookie
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.user_repository import UserRepository, SessionRepository, RefreshTokenRepository
from app.services.auth_service import AuthService
from app.schemas.auth import GoogleAuthRequest, TokenResponse, UserResponse, SessionResponse

router = APIRouter(prefix="/auth", tags=["Authentication"])
security = HTTPBearer()

def get_auth_service(db: Session = Depends(get_db)) -> AuthService:
    user_repo = UserRepository(db)
    session_repo = SessionRepository(db)
    token_repo = RefreshTokenRepository(db)
    return AuthService(user_repo, session_repo, token_repo)

def get_current_token_payload(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    auth_service: AuthService = Depends(get_auth_service)
) -> dict:
    return auth_service.verify_access_token(credentials.credentials)

def get_current_user(
    payload: dict = Depends(get_current_token_payload),
    db: Session = Depends(get_db)
):
    session_repo = SessionRepository(db)
    session_id = payload.get("session_id")
    if session_id:
        session = session_repo.get_by_id(session_id)
        if not session or session.revoked_at is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been logged out."
            )
            
    user_repo = UserRepository(db)
    user = user_repo.get_by_google_id(payload["sub"])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found."
        )
    return user

# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/google", response_model=TokenResponse)
def login_google(
    auth_req: GoogleAuthRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service)
):
    # Parse headers for session metadata
    user_agent = request.headers.get("user-agent", "Unknown")
    ip_address = request.client.host if request.client else "Unknown"
    
    # Extract OS and browser signatures from User-Agent for session metrics
    os_name = "Unknown"
    if "Windows" in user_agent: os_name = "Windows"
    elif "Macintosh" in user_agent: os_name = "macOS"
    elif "X11" in user_agent or "Linux" in user_agent: os_name = "Linux"
    elif "Android" in user_agent: os_name = "Android"
    elif "iPhone" in user_agent: os_name = "iOS"

    browser_name = "Unknown"
    if "Chrome" in user_agent: browser_name = "Chrome"
    elif "Safari" in user_agent: browser_name = "Safari"
    elif "Firefox" in user_agent: browser_name = "Firefox"
    elif "Edge" in user_agent: browser_name = "Edge"
    
    meta = {
        "device_name": f"{os_name} Device",
        "os": os_name,
        "browser": browser_name,
        "country": "Unknown"  # Fallback
    }

    access_token, refresh_token, expires_in = auth_service.login_with_google(auth_req.id_token, meta)

    # Set Refresh Token cookie (HttpOnly, Secure, SameSite=Strict)
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=7 * 24 * 3600,  # 7 days
        path="/auth"
    )

    return TokenResponse(
        access_token=access_token,
        expires_in=expires_in
    )

from fastapi import Header

@router.post("/refresh", response_model=TokenResponse)
def refresh_session(
    response: Response,
    refresh_token: Optional[str] = Cookie(None),
    x_refresh_token: Optional[str] = Header(None),
    auth_service: AuthService = Depends(get_auth_service)
):
    token = refresh_token or x_refresh_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid."
        )

    access_token, new_refresh_token, expires_in = auth_service.rotate_refresh_token(token)

    # Overwrite cookie with the rotated refresh token (RTR)
    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=7 * 24 * 3600,
        path="/auth"
    )

    return TokenResponse(
        access_token=access_token,
        expires_in=expires_in
    )

@router.post("/logout")
def logout(
    response: Response,
    payload: dict = Depends(get_current_token_payload),
    auth_service: AuthService = Depends(get_auth_service)
):
    session_id = payload.get("session_id")
    if session_id:
        auth_service.logout_session(session_id)
        
    # Clear HTTP Cookie
    response.delete_cookie(key="refresh_token", path="/auth")
    return {"message": "Successfully logged out."}

@router.post("/logout/all")
def logout_all(
    response: Response,
    current_user = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
):
    auth_service.logout_all_devices(current_user.id)
    response.delete_cookie(key="refresh_token", path="/auth")
    return {"message": "Successfully logged out of all active devices."}

@router.get("/me", response_model=UserResponse)
def get_me(current_user = Depends(get_current_user)):
    return current_user

@router.get("/sessions", response_model=List[SessionResponse])
def get_active_sessions(
    current_user = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
):
    return auth_service.get_active_sessions(current_user.id)

@router.get("/config")
def get_auth_config():
    from app.config import settings
    return {"google_client_id": settings.GOOGLE_CLIENT_ID}
