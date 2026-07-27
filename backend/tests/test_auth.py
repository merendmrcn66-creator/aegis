import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from app.database import Base
from app.models.user import User, Session, RefreshToken, Role
from app.repositories.user_repository import UserRepository, SessionRepository, RefreshTokenRepository
from app.services.auth_service import AuthService
from app.config import get_public_key, get_private_key

# Set up in-memory database for unit testing
@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

@pytest.fixture
def auth_service(db_session):
    user_repo = UserRepository(db_session)
    session_repo = SessionRepository(db_session)
    token_repo = RefreshTokenRepository(db_session)
    return AuthService(user_repo, session_repo, token_repo)

# ── Tests ─────────────────────────────────────────────────────────────────────

def test_google_sandbox_login(auth_service, db_session):
    # Perform sandbox login
    token_str = "sandbox_testuser@example.com:Eren Ekinci:https://avatar.url/erene"
    meta = {
        "device_name": "Test Device",
        "os": "Windows",
        "browser": "Chrome",
        "country": "TR"
    }
    
    access_token, refresh_token, expires_in = auth_service.login_with_google(token_str, meta)
    
    assert access_token is not None
    assert refresh_token is not None
    assert expires_in == 900 # 15 minutes in seconds
    
    # Verify User created
    user = auth_service.user_repo.get_by_email("testuser@example.com")
    assert user is not None
    assert user.name == "Eren Ekinci"
    assert user.role == Role.USER
    assert user.google_id == "sandbox_sub_testuser@example.com"
    
    # Verify Session created
    sessions = auth_service.get_active_sessions(user.id)
    assert len(sessions) == 1
    assert sessions[0].device_name == "Test Device"
    assert sessions[0].os == "Windows"
    assert sessions[0].browser == "Chrome"
    assert sessions[0].country == "TR"

def test_jwt_rs256_generation_and_decoding(auth_service):
    user = User(
        google_id="test_google_sub_123",
        email="jwt@test.com",
        name="JWT User",
        role=Role.USER
    )
    auth_service.user_repo.create(user)
    
    session_id = "test-session-uuid-123"
    token = auth_service.generate_access_token(user, session_id)
    
    # Decode and verify claims
    payload = auth_service.verify_access_token(token)
    assert payload["sub"] == "test_google_sub_123"
    assert payload["email"] == "jwt@test.com"
    assert payload["role"] == "User"
    assert payload["session_id"] == "test-session-uuid-123"
    assert "jti" in payload
    assert "exp" in payload

def test_refresh_token_rotation_success(auth_service):
    # Setup user and session
    user = User(google_id="sub_rtr", email="rtr@test.com", name="RTR User", role=Role.USER)
    auth_service.user_repo.create(user)
    
    session = Session(id="session_rtr", user_id=user.id, country="TR")
    auth_service.session_repo.create(session)
    
    # Generate initial refresh token
    plain_token = auth_service.generate_refresh_token(user.id, session.id)
    
    # Rotate token
    new_access, new_refresh, exp = auth_service.rotate_refresh_token(plain_token)
    
    assert new_access is not None
    assert new_refresh is not None
    assert new_refresh != plain_token
    
    # Verify old token is revoked
    old_hash = auth_service._hash_token(plain_token)
    old_record = auth_service.token_repo.get_by_hash(old_hash)
    assert old_record.is_revoked is True
    
    # Verify new token is active
    new_hash = auth_service._hash_token(new_refresh)
    new_record = auth_service.token_repo.get_by_hash(new_hash)
    assert new_record is not None
    assert new_record.is_revoked is False

def test_refresh_token_replay_attack_revocation(auth_service):
    # Setup user and session
    user = User(google_id="sub_replay", email="replay@test.com", name="Replay User", role=Role.USER)
    auth_service.user_repo.create(user)
    
    session1 = Session(id="session_replay_1", user_id=user.id, country="TR")
    auth_service.session_repo.create(session1)
    session2 = Session(id="session_replay_2", user_id=user.id, country="TR")
    auth_service.session_repo.create(session2)
    
    # Generate refresh token for session 1
    plain_token = auth_service.generate_refresh_token(user.id, session1.id)
    
    # First rotation (Valid)
    _, new_refresh, _ = auth_service.rotate_refresh_token(plain_token)
    
    # Replay attack: attempt to use the OLD token again
    with pytest.raises(HTTPException) as excinfo:
        auth_service.rotate_refresh_token(plain_token)
        
    assert excinfo.value.status_code == 401
    
    # Security action check: ALL sessions and tokens for this user must be revoked!
    active_sessions = auth_service.get_active_sessions(user.id)
    assert len(active_sessions) == 0  # Both sessions revoked!
    
    # Check that new_refresh is also revoked
    new_hash = auth_service._hash_token(new_refresh)
    new_record = auth_service.token_repo.get_by_hash(new_hash)
    assert new_record.is_revoked is True

def test_logout_endpoints(auth_service):
    # Setup user and sessions
    user = User(google_id="sub_logout", email="logout@test.com", name="Logout User", role=Role.USER)
    auth_service.user_repo.create(user)
    
    s1 = Session(id="s1", user_id=user.id, country="TR")
    auth_service.session_repo.create(s1)
    s2 = Session(id="s2", user_id=user.id, country="TR")
    auth_service.session_repo.create(s2)
    
    auth_service.generate_refresh_token(user.id, s1.id)
    auth_service.generate_refresh_token(user.id, s2.id)
    
    # Logout single device (s1)
    auth_service.logout_session(s1.id)
    active = auth_service.get_active_sessions(user.id)
    assert len(active) == 1
    assert active[0].id == "s2"
    
    # Logout all devices
    auth_service.logout_all_devices(user.id)
    active_all = auth_service.get_active_sessions(user.id)
    assert len(active_all) == 0
