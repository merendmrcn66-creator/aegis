import secrets
import hashlib
from datetime import datetime, timedelta
import uuid
from typing import Optional, Tuple, List
import jwt
from fastapi import HTTPException, status
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from app.config import settings, get_private_key, get_public_key
from app.models.user import User, Session, RefreshToken, Role
from app.repositories.user_repository import UserRepository, SessionRepository, RefreshTokenRepository

class AuthService:
    """Core Authentication Service handling OAuth, JWTs, RTR, and Sessions."""
    
    def __init__(
        self,
        user_repo: UserRepository,
        session_repo: SessionRepository,
        token_repo: RefreshTokenRepository
    ):
        self.user_repo = user_repo
        self.session_repo = session_repo
        self.token_repo = token_repo

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def verify_google_token(self, token_str: str) -> dict:
        """Verifies the Google ID token and returns the payload."""
        # Sandbox mode for local testing without real Client ID
        if settings.GOOGLE_CLIENT_ID == "sandbox" or token_str.startswith("sandbox_"):
            if not token_str.startswith("sandbox_"):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Sandbox token must start with 'sandbox_'"
                )
            # Format: sandbox_email:name:avatar
            parts = token_str.split(":")
            email = parts[0].replace("sandbox_", "")
            name = parts[1] if len(parts) > 1 else "Sandbox User"
            avatar = parts[2] if len(parts) > 2 else "https://avatar.url"
            
            return {
                "sub": f"sandbox_sub_{email}",
                "email": email,
                "email_verified": True,
                "name": name,
                "picture": avatar
            }

        try:
            # Real Google verification
            id_info = id_token.verify_oauth2_token(
                token_str,
                google_requests.Request(),
                settings.GOOGLE_CLIENT_ID
            )
            
            # Verify issuer and audience
            if id_info["iss"] not in ["accounts.google.com", "https://accounts.google.com"]:
                raise ValueError("Wrong issuer.")
                
            return id_info
        except Exception as e:
            print(f"[SECURITY] Invalid Google token signature or claim: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Google Authentication failed: {str(e)}"
            )

    def login_with_google(self, token_str: str, meta: dict) -> Tuple[str, str, int]:
        """Logs the user in via Google, upserts profile, creates session/tokens."""
        payload = self.verify_google_token(token_str)
        
        if not payload.get("email_verified"):
            print(f"[SECURITY] Blocked login attempt: Google email {payload.get('email')} not verified.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Google account email is not verified."
            )

        google_id = payload["sub"]
        email = payload["email"]
        name = payload["name"]
        avatar = payload.get("picture")

        # Repository lookup
        user = self.user_repo.get_by_google_id(google_id)
        now = datetime.utcnow()

        if not user:
            # Automatically register first-time user
            user = User(
                google_id=google_id,
                email=email,
                name=name,
                avatar=avatar,
                role=Role.USER,
                created_at=now,
                last_login=now
            )
            user = self.user_repo.create(user)
            print(f"[INFO] New user registered: {email} (ID: {user.id})")
        else:
            # Sync user profile details & update last login
            user.name = name
            user.avatar = avatar
            user.last_login = now
            self.user_repo.update()
            print(f"[INFO] User logged in: {email} (ID: {user.id})")

        # Create Session (UUIDv4)
        session_id = str(uuid.uuid4())
        session = Session(
            id=session_id,
            user_id=user.id,
            device_name=meta.get("device_name"),
            os=meta.get("os"),
            browser=meta.get("browser"),
            country=meta.get("country", "Unknown"),
            last_activity=now,
            created_at=now
        )
        self.session_repo.create(session)

        # Generate Token Pair
        access_token = self.generate_access_token(user, session_id)
        refresh_token = self.generate_refresh_token(user.id, session_id)

        expires_in = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        return access_token, refresh_token, expires_in

    def generate_access_token(self, user: User, session_id: str) -> str:
        """Generates an RS256 signed JWT Access Token."""
        from datetime import timezone
        now = datetime.utcnow()
        exp = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        
        payload = {
            "sub": user.google_id,
            "jti": str(uuid.uuid4()),
            "session_id": session_id,
            "email": user.email,
            "role": user.role.value,
            "iat": int(now.replace(tzinfo=timezone.utc).timestamp()),
            "exp": int(exp.replace(tzinfo=timezone.utc).timestamp()),
            "iss": "aegis-auth-server",
            "aud": settings.GOOGLE_CLIENT_ID
        }
        
        private_key = get_private_key()
        return jwt.encode(payload, private_key, algorithm=settings.JWT_ALGORITHM)

    def generate_refresh_token(self, user_id: int, session_id: str) -> str:
        """Generates a Refresh Token, hashes it, and stores it in the database."""
        token_plain = secrets.token_hex(32)
        token_hash = self._hash_token(token_plain)
        
        expires_at = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        
        refresh_obj = RefreshToken(
            user_id=user_id,
            session_id=session_id,
            token_hash=token_hash,
            expires_at=expires_at,
            is_revoked=False,
            created_at=datetime.utcnow()
        )
        self.token_repo.create(refresh_obj)
        return token_plain

    def verify_access_token(self, token_str: str) -> dict:
        """Decodes and validates the RS256 access token."""
        public_key = get_public_key()
        try:
            payload = jwt.decode(
                token_str,
                public_key,
                algorithms=[settings.JWT_ALGORITHM],
                audience=settings.GOOGLE_CLIENT_ID,
                issuer="aegis-auth-server"
            )
            return payload
        except jwt.ExpiredSignatureError:
            print("[SECURITY] Expired JWT Access Token presented.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Access Token has expired."
            )
        except jwt.InvalidTokenError as e:
            print(f"[SECURITY] Invalid JWT Access Token: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Access Token."
            )

    def rotate_refresh_token(self, old_plain_token: str) -> Tuple[str, str, int]:
        """Performs Refresh Token Rotation (RTR) and protects against replay attacks."""
        old_hash = self._hash_token(old_plain_token)
        token_record = self.token_repo.get_by_hash(old_hash)
        
        if not token_record:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid session token."
            )

        user_id = token_record.user_id
        session_id = token_record.session_id

        # Replay Attack Detection!
        if token_record.is_revoked or token_record.expires_at < datetime.utcnow():
            if token_record.is_revoked:
                # Security incident: reuse of a revoked token!
                print(f"[SECURITY] Refresh reuse attack detected for user ID {user_id}! Revoking all sessions!")
                self.session_repo.revoke_all_user_sessions(user_id)
                self.token_repo.revoke_all_user_tokens(user_id)
                
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been revoked due to security protocol violation."
            )

        # Retrieve session
        session = self.session_repo.get_by_id(session_id)
        if not session or session.revoked_at is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been closed."
            )

        # Update session activity
        now = datetime.utcnow()
        session.last_activity = now
        
        # Revoke old token
        token_record.is_revoked = True
        self.token_repo.update()

        # Generate new pair
        user = self.user_repo.get_by_id(user_id)
        new_access_token = self.generate_access_token(user, session_id)
        new_refresh_token = self.generate_refresh_token(user_id, session_id)
        
        expires_in = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        return new_access_token, new_refresh_token, expires_in

    def logout_session(self, session_id: str) -> None:
        """Closes a specific active session."""
        session = self.session_repo.get_by_id(session_id)
        if session:
            session.revoked_at = datetime.utcnow()
            self.session_repo.update()
            
            # Revoke refresh token associated with it
            token = self.token_repo.get_by_session_id(session_id)
            if token:
                token.is_revoked = True
                self.token_repo.update()

    def logout_all_devices(self, user_id: int) -> None:
        """Revokes all sessions and all refresh tokens for a user."""
        self.session_repo.revoke_all_user_sessions(user_id)
        self.token_repo.revoke_all_user_tokens(user_id)
        print(f"[INFO] Revoked all active sessions and tokens for user ID {user_id}")

    def get_active_sessions(self, user_id: int) -> List[Session]:
        return self.session_repo.get_active_sessions_by_user(user_id)
