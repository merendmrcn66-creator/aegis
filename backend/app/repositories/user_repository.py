from typing import Optional, List
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.user import User, Session as UserSession, RefreshToken
from app.repositories.base import BaseRepository

class UserRepository(BaseRepository[User]):
    def __init__(self, db: Session):
        super().__init__(User, db)

    def get_by_google_id(self, google_id: str) -> Optional[User]:
        return self.db.query(User).filter(User.google_id == google_id).first()

    def get_by_email(self, email: str) -> Optional[User]:
        return self.db.query(User).filter(User.email == email).first()

class SessionRepository(BaseRepository[UserSession]):
    def __init__(self, db: Session):
        super().__init__(UserSession, db)

    def get_active_sessions_by_user(self, user_id: int) -> List[UserSession]:
        return self.db.query(UserSession).filter(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None)
        ).all()

    def revoke_all_user_sessions(self, user_id: int) -> None:
        sessions = self.get_active_sessions_by_user(user_id)
        now = datetime.utcnow()
        for s in sessions:
            s.revoked_at = now
        self.db.commit()

class RefreshTokenRepository(BaseRepository[RefreshToken]):
    def __init__(self, db: Session):
        super().__init__(RefreshToken, db)

    def get_by_hash(self, token_hash: str) -> Optional[RefreshToken]:
        return self.db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()

    def get_by_session_id(self, session_id: str) -> Optional[RefreshToken]:
        return self.db.query(RefreshToken).filter(
            RefreshToken.session_id == session_id,
            RefreshToken.is_revoked.is_(False)
        ).first()

    def revoke_all_user_tokens(self, user_id: int) -> None:
        tokens = self.db.query(RefreshToken).filter(
            RefreshToken.user_id == user_id,
            RefreshToken.is_revoked.is_(False)
        ).all()
        for t in tokens:
            t.is_revoked = True
        self.db.commit()
