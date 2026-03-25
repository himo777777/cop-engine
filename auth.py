"""
COP Engine — Autentisering & RBAC
==================================
JWT-baserad autentisering med rollbaserad åtkomstkontroll.

Roller:
  - ADMIN        Systemadministratör (full åtkomst)
  - SCHEDULER    Schemaläggare (schema + frånvaro)
  - DOCTOR       Läkare (läs eget schema, rapportera frånvaro)
  - VIEWER       Läsbehörighet (dashboard)

Användning:
  from auth import require_role, Role, get_current_user

  @app.get("/admin/users")
  async def list_users(user = Depends(require_role(Role.ADMIN))):
      ...
"""

import os
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr

# ---------------------------------------------------------------------------
# JWT — lightweight implementation (PyJWT)
# ---------------------------------------------------------------------------
try:
    import jwt as pyjwt
except ImportError:
    pyjwt = None

# Fallback: om PyJWT inte finns, använd enkel HMAC-token
import hmac
import json
import base64

JWT_SECRET = os.getenv("COP_JWT_SECRET", secrets.token_hex(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("COP_JWT_EXPIRE_HOURS", "12"))
REFRESH_EXPIRE_DAYS = 30

security = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Roller & Permissions
# ---------------------------------------------------------------------------

class Role(str, Enum):
    ADMIN = "admin"
    SCHEDULER = "scheduler"
    DOCTOR = "doctor"
    VIEWER = "viewer"


# Hierarkisk behörighet: admin > scheduler > doctor > viewer
ROLE_HIERARCHY = {
    Role.ADMIN: 4,
    Role.SCHEDULER: 3,
    Role.DOCTOR: 2,
    Role.VIEWER: 1,
}

# Granulära permissions per endpoint-grupp
PERMISSIONS = {
    "schedule:read":       {Role.ADMIN, Role.SCHEDULER, Role.DOCTOR, Role.VIEWER},
    "schedule:write":      {Role.ADMIN, Role.SCHEDULER},
    "schedule:generate":   {Role.ADMIN, Role.SCHEDULER},
    "absence:read":        {Role.ADMIN, Role.SCHEDULER, Role.DOCTOR},
    "absence:write":       {Role.ADMIN, Role.SCHEDULER, Role.DOCTOR},
    "absence:chain":       {Role.ADMIN, Role.SCHEDULER},
    "config:read":         {Role.ADMIN, Role.SCHEDULER, Role.DOCTOR, Role.VIEWER},
    "config:write":        {Role.ADMIN},
    "users:read":          {Role.ADMIN},
    "users:write":         {Role.ADMIN},
    "statistics:read":     {Role.ADMIN, Role.SCHEDULER, Role.VIEWER},
    "agent:use":           {Role.ADMIN, Role.SCHEDULER},
    "solver:reoptimize":   {Role.ADMIN, Role.SCHEDULER},
}


# ---------------------------------------------------------------------------
# User Model
# ---------------------------------------------------------------------------

class UserInDB(BaseModel):
    user_id: str
    username: str
    email: str
    full_name: str
    role: Role
    doctor_id: Optional[str] = None  # Koppling till data_model doctor ID
    hashed_password: str
    is_active: bool = True
    created_at: datetime = datetime.now(timezone.utc)
    last_login: Optional[datetime] = None


class UserResponse(BaseModel):
    user_id: str
    username: str
    email: str
    full_name: str
    role: Role
    doctor_id: Optional[str] = None
    is_active: bool


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    email: str
    full_name: str
    password: str
    role: Role = Role.VIEWER
    doctor_id: Optional[str] = None


class UpdateUserRequest(BaseModel):
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[Role] = None
    doctor_id: Optional[str] = None
    is_active: Optional[bool] = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


# ---------------------------------------------------------------------------
# Password Hashing (PBKDF2)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash password med PBKDF2-SHA256 + random salt."""
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${hashed.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    """Verifiera password mot hash."""
    try:
        salt, stored_hash = hashed.split("$")
        computed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
        return hmac.compare_digest(computed.hex(), stored_hash)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# JWT Token Management
# ---------------------------------------------------------------------------

def create_token(user_id: str, username: str, role: str,
                 expires_delta: Optional[timedelta] = None) -> str:
    """Skapa JWT access token."""
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(hours=JWT_EXPIRE_HOURS))

    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": now,
        "exp": expire,
        "type": "access",
    }

    if pyjwt:
        return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    else:
        return _simple_encode(payload)


def create_refresh_token(user_id: str) -> str:
    """Skapa refresh token (längre livstid)."""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=REFRESH_EXPIRE_DAYS)

    payload = {
        "sub": user_id,
        "iat": now,
        "exp": expire,
        "type": "refresh",
        "jti": secrets.token_hex(16),
    }

    if pyjwt:
        return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    else:
        return _simple_encode(payload)


def decode_token(token: str) -> dict:
    """Dekodera och validera JWT token."""
    try:
        if pyjwt:
            return pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        else:
            return _simple_decode(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ogiltig eller utgången token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# Fallback-kodning utan PyJWT
def _simple_encode(payload: dict) -> str:
    """Enkel HMAC-baserad token (fallback)."""
    # Konvertera datetime till timestamp
    serializable = {}
    for k, v in payload.items():
        if isinstance(v, datetime):
            serializable[k] = v.timestamp()
        else:
            serializable[k] = v

    data = base64.urlsafe_b64encode(json.dumps(serializable).encode()).decode()
    sig = hmac.new(JWT_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{data}.{sig}"


def _simple_decode(token: str) -> dict:
    """Dekodera enkel HMAC-token (fallback)."""
    parts = token.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError("Invalid token format")

    data, sig = parts
    expected_sig = hmac.new(JWT_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("Invalid signature")

    payload = json.loads(base64.urlsafe_b64decode(data))

    # Kontrollera expiration
    if payload.get("exp", 0) < datetime.now(timezone.utc).timestamp():
        raise ValueError("Token expired")

    return payload


# ---------------------------------------------------------------------------
# User Store — in-memory primary, syncs to MongoDB when available
# ---------------------------------------------------------------------------

_users: dict[str, UserInDB] = {}
_revoked_tokens: set[str] = set()  # Blacklistade tokens


def _sync_user_to_db(user: "UserInDB"):
    """Synka användare till MongoDB i bakgrunden (fire-and-forget)."""
    try:
        from db import get_db
        db = get_db()
        if db.using_mongo:
            import asyncio
            data = {
                "user_id": user.user_id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role.value,
                "doctor_id": user.doctor_id,
                "is_active": user.is_active,
                "hashed_password": user.hashed_password,
            }
            asyncio.get_event_loop().create_task(db.save_user(data))
    except Exception:
        pass  # Non-critical — in-memory is primary


def _init_default_users():
    """Skapa standardanvändare vid uppstart."""
    if _users:
        return

    # Admin-konto
    admin = UserInDB(
        user_id="usr_admin",
        username="admin",
        email="admin@cop.local",
        full_name="COP Administrator",
        role=Role.ADMIN,
        hashed_password=hash_password(os.getenv("COP_ADMIN_PASSWORD", "cop-admin-2026")),
    )
    _users[admin.user_id] = admin

    # Schema-konto (för schemaläggare)
    scheduler = UserInDB(
        user_id="usr_scheduler",
        username="scheduler",
        email="schema@cop.local",
        full_name="Schemaläggare",
        role=Role.SCHEDULER,
        hashed_password=hash_password("schema-2026"),
    )
    _users[scheduler.user_id] = scheduler

    # Viewer-konto (för dashboard)
    viewer = UserInDB(
        user_id="usr_viewer",
        username="viewer",
        email="viewer@cop.local",
        full_name="Dashboard Viewer",
        role=Role.VIEWER,
        hashed_password=hash_password("viewer-2026"),
    )
    _users[viewer.user_id] = viewer


_init_default_users()


def get_user_by_username(username: str) -> Optional[UserInDB]:
    for user in _users.values():
        if user.username == username:
            return user
    return None


def get_user_by_id(user_id: str) -> Optional[UserInDB]:
    return _users.get(user_id)


# ---------------------------------------------------------------------------
# FastAPI Dependencies
# ---------------------------------------------------------------------------

class CurrentUser(BaseModel):
    user_id: str
    username: str
    role: Role
    doctor_id: Optional[str] = None


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    """Hämta inloggad användare från JWT token."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autentisering krävs",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(credentials.credentials)

    # Kontrollera om token är blacklistad
    jti = payload.get("jti")
    if jti and jti in _revoked_tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token har återkallats",
        )

    user = get_user_by_id(payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Användaren finns inte eller är inaktiverad",
        )

    return CurrentUser(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        doctor_id=user.doctor_id,
    )


def require_role(*roles: Role):
    """Dependency som kräver specifik roll."""
    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Behörighet saknas. Kräver: {', '.join(r.value for r in roles)}",
            )
        return user
    return _check


def require_permission(permission: str):
    """Dependency som kräver specifik permission."""
    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        allowed_roles = PERMISSIONS.get(permission, set())
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Behörighet '{permission}' saknas för rollen '{user.role.value}'",
            )
        return user
    return _check


def require_self_or_role(doctor_id_param: str = "doctor_id", *roles: Role):
    """Tillåt åtkomst om användaren är sig själv ELLER har rätt roll."""
    async def _check(
        user: CurrentUser = Depends(get_current_user),
        **kwargs,
    ) -> CurrentUser:
        # Admin/scheduler har alltid åtkomst
        if user.role in roles:
            return user
        # Läkare kan se sitt eget schema
        # (doctor_id kontrolleras i endpoint-koden)
        return user
    return _check


# ---------------------------------------------------------------------------
# Auth Router (endpoints)
# ---------------------------------------------------------------------------

from fastapi import APIRouter

auth_router = APIRouter(prefix="/auth", tags=["Autentisering"])


@auth_router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    """Logga in och få JWT tokens."""
    user = get_user_by_username(req.username)
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Felaktigt användarnamn eller lösenord",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kontot är inaktiverat",
        )

    # Uppdatera last_login
    user.last_login = datetime.now(timezone.utc)

    access_token = create_token(user.user_id, user.username, user.role.value)
    refresh_token = create_refresh_token(user.user_id)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=JWT_EXPIRE_HOURS * 3600,
        user=UserResponse(
            user_id=user.user_id,
            username=user.username,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            doctor_id=user.doctor_id,
            is_active=user.is_active,
        ),
    )


@auth_router.post("/refresh", response_model=TokenResponse)
async def refresh_token(refresh_token: str):
    """Förnya access token med refresh token."""
    payload = decode_token(refresh_token)

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=400, detail="Inte en refresh token")

    user = get_user_by_id(payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Ogiltigt konto")

    new_access = create_token(user.user_id, user.username, user.role.value)
    new_refresh = create_refresh_token(user.user_id)

    # Blacklista gamla refresh token
    jti = payload.get("jti")
    if jti:
        _revoked_tokens.add(jti)

    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=JWT_EXPIRE_HOURS * 3600,
        user=UserResponse(
            user_id=user.user_id,
            username=user.username,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            doctor_id=user.doctor_id,
            is_active=user.is_active,
        ),
    )


@auth_router.post("/logout")
async def logout(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    user: CurrentUser = Depends(get_current_user),
):
    """Logga ut (invalidera token)."""
    if credentials:
        token = credentials.credentials
        _revoked_tokens.add(token)
        try:
            from db import get_db
            await get_db().revoke_token(token)
        except Exception:
            pass
    return {"message": f"Utloggad: {user.username}"}


@auth_router.get("/me", response_model=UserResponse)
async def get_me(user: CurrentUser = Depends(get_current_user)):
    """Hämta info om inloggad användare."""
    db_user = get_user_by_id(user.user_id)
    return UserResponse(
        user_id=db_user.user_id,
        username=db_user.username,
        email=db_user.email,
        full_name=db_user.full_name,
        role=db_user.role,
        doctor_id=db_user.doctor_id,
        is_active=db_user.is_active,
    )


@auth_router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """Byt lösenord."""
    db_user = get_user_by_id(user.user_id)
    if not verify_password(req.old_password, db_user.hashed_password):
        raise HTTPException(status_code=400, detail="Fel nuvarande lösenord")

    db_user.hashed_password = hash_password(req.new_password)
    return {"message": "Lösenord uppdaterat"}


# --- User Management (admin only) ---

@auth_router.get("/users", response_model=list[UserResponse])
async def list_users(user: CurrentUser = Depends(require_role(Role.ADMIN))):
    """Lista alla användare (admin)."""
    return [
        UserResponse(
            user_id=u.user_id,
            username=u.username,
            email=u.email,
            full_name=u.full_name,
            role=u.role,
            doctor_id=u.doctor_id,
            is_active=u.is_active,
        )
        for u in _users.values()
    ]


@auth_router.post("/users", response_model=UserResponse)
async def create_user(
    req: CreateUserRequest,
    user: CurrentUser = Depends(require_role(Role.ADMIN)),
):
    """Skapa ny användare (admin)."""
    if get_user_by_username(req.username):
        raise HTTPException(status_code=409, detail="Användarnamnet är upptaget")

    user_id = f"usr_{secrets.token_hex(6)}"
    new_user = UserInDB(
        user_id=user_id,
        username=req.username,
        email=req.email,
        full_name=req.full_name,
        role=req.role,
        doctor_id=req.doctor_id,
        hashed_password=hash_password(req.password),
    )
    _users[user_id] = new_user
    _sync_user_to_db(new_user)

    return UserResponse(
        user_id=new_user.user_id,
        username=new_user.username,
        email=new_user.email,
        full_name=new_user.full_name,
        role=new_user.role,
        doctor_id=new_user.doctor_id,
        is_active=new_user.is_active,
    )


@auth_router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    user: CurrentUser = Depends(require_role(Role.ADMIN)),
):
    """Uppdatera användare (admin)."""
    target = get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="Användaren finns inte")

    if req.email is not None:
        target.email = req.email
    if req.full_name is not None:
        target.full_name = req.full_name
    if req.role is not None:
        target.role = req.role
    if req.doctor_id is not None:
        target.doctor_id = req.doctor_id
    if req.is_active is not None:
        target.is_active = req.is_active

    return UserResponse(
        user_id=target.user_id,
        username=target.username,
        email=target.email,
        full_name=target.full_name,
        role=target.role,
        doctor_id=target.doctor_id,
        is_active=target.is_active,
    )
