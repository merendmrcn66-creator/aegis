import os
from typing import Optional
from pydantic_settings import BaseSettings
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

class Settings(BaseSettings):
    GOOGLE_CLIENT_ID: str = "sandbox"
    
    # RS256 JWT Settings
    JWT_ALGORITHM: str = "RS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    
    # Private / Public key file paths or raw PEM strings
    JWT_PRIVATE_KEY_PEM: Optional[str] = None
    JWT_PUBLIC_KEY_PEM: Optional[str] = None
    
    # Database URL (supports SQLite or PostgreSQL)
    DATABASE_URL: str = "sqlite:///./aegis_auth.db"
    
    # CORS Origins (comma separated list)
    ALLOWED_ORIGINS: str = "http://localhost:8000,http://127.0.0.1:8000,http://localhost:8080,http://127.0.0.1:8080,desktop://"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()

# Ephemeral key pair state
_ephemeral_private_key: Optional[str] = None
_ephemeral_public_key: Optional[str] = None

def get_private_key() -> str:
    global _ephemeral_private_key
    if settings.JWT_PRIVATE_KEY_PEM:
        if os.path.exists(settings.JWT_PRIVATE_KEY_PEM):
            with open(settings.JWT_PRIVATE_KEY_PEM, "r", encoding="utf-8") as f:
                return f.read()
        return settings.JWT_PRIVATE_KEY_PEM
    
    if _ephemeral_private_key is None:
        _generate_ephemeral_keys()
    return _ephemeral_private_key

def get_public_key() -> str:
    global _ephemeral_public_key
    if settings.JWT_PUBLIC_KEY_PEM:
        if os.path.exists(settings.JWT_PUBLIC_KEY_PEM):
            with open(settings.JWT_PUBLIC_KEY_PEM, "r", encoding="utf-8") as f:
                return f.read()
        return settings.JWT_PUBLIC_KEY_PEM
        
    if _ephemeral_public_key is None:
        _generate_ephemeral_keys()
    return _ephemeral_public_key

def _generate_ephemeral_keys():
    global _ephemeral_private_key, _ephemeral_public_key
    # Generate ephemeral RSA key pair for development
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")
    
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("utf-8")
    
    _ephemeral_private_key = private_pem
    _ephemeral_public_key = public_pem
