from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, EmailStr

class GoogleAuthRequest(BaseModel):
    id_token: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # in seconds

class SessionResponse(BaseModel):
    id: str
    device_name: Optional[str] = None
    os: Optional[str] = None
    browser: Optional[str] = None
    country: str
    last_activity: datetime
    created_at: datetime

    class Config:
        from_attributes = True

class UserResponse(BaseModel):
    id: int
    email: EmailStr
    name: str
    avatar: Optional[str] = None
    role: str
    created_at: datetime
    last_login: datetime

    class Config:
        from_attributes = True
