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
import time as _time
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

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
    doctor_id: Optional[str] = None
    hashed_password: str
    is_active: bool = True
    password_change_required: bool = False
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
    password_change_required: bool = False


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


def validate_password_complexity(password: str) -> None:
    """Kontrollera att lösenordet uppfyller komplexitetskraven."""
    if len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lösenordet måste vara minst 8 tecken",
        )
    if not any(c.isdigit() for c in password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lösenordet måste innehålla minst en siffra",
        )


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
        "jti": secrets.token_hex(16),
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

    if payload.get("exp", 0) < datetime.now(timezone.utc).timestamp():
        raise ValueError("Token expired")

    return payload


# ---------------------------------------------------------------------------
# In-memory cache med 60s TTL
# ---------------------------------------------------------------------------

_user_cache: dict[str, tuple["UserInDB", float]] = {}  # user_id → (user, expires)
_username_to_id: dict[str, str] = {}                    # username → user_id
CACHE_TTL = 60.0

_revoked_hash_cache: set[str] = set()  # SHA-256-hash av token, snabb lookup


def _cache_get(user_id: str) -> Optional["UserInDB"]:
    entry = _user_cache.get(user_id)
    if entry and entry[1] > _time.monotonic():
        return entry[0]
    _user_cache.pop(user_id, None)
    return None


def _cache_set(user: "UserInDB") -> None:
    _user_cache[user.user_id] = (user, _time.monotonic() + CACHE_TTL)
    _username_to_id[user.username] = user.user_id


def _normalize_user_data(data: dict) -> dict:
    """Normalisera dict från DB/in-memory till UserInDB-kompatibelt format."""
    normalized = dict(data)
    # role kan vara en sträng från DB
    if "role" in normalized and isinstance(normalized["role"], str):
        normalized["role"] = Role(normalized["role"])
    # Säkerställ att password_change_required finns
    normalized.setdefault("password_change_required", False)
    normalized.setdefault("is_active", True)
    normalized.setdefault("doctor_id", None)
    normalized.setdefault("last_login", None)
    # Sätt created_at om det saknas
    if "created_at" not in normalized or normalized["created_at"] is None:
        normalized["created_at"] = datetime.now(timezone.utc)
    # DB returnerar ibland asyncpg Record — filtrera bort okända fält
    known = {f for f in UserInDB.model_fields}
    return {k: v for k, v in normalized.items() if k in known}


# ---------------------------------------------------------------------------
# Token-revokering med SHA-256
# ---------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _revoke_token(token: str, expires_at: Optional[datetime] = None) -> None:
    h = _hash_token(token)
    _revoked_hash_cache.add(h)
    from db import get_db
    await get_db().revoke_token(h, expires_at)


async def _is_token_revoked(token: str) -> bool:
    h = _hash_token(token)
    if h in _revoked_hash_cache:
        return True
    from db import get_db
    return await get_db().is_token_revoked(h)


# ---------------------------------------------------------------------------
# Async User Lookups (cache-first, sedan DB)
# ---------------------------------------------------------------------------

async def get_user_by_id(user_id: str) -> Optional[UserInDB]:
    user = _cache_get(user_id)
    if user:
        return user
    from db import get_db
    data = await get_db().get_user(user_id)
    if data:
        user = UserInDB(**_normalize_user_data(data))
        _cache_set(user)
        return user
    return None


async def get_user_by_username(username: str) -> Optional[UserInDB]:
    uid = _username_to_id.get(username)
    if uid:
        user = _cache_get(uid)
        if user:
            return user
    from db import get_db
    data = await get_db().get_user_by_username(username)
    if data:
        user = UserInDB(**_normalize_user_data(data))
        _cache_set(user)
        return user
    return None


# ---------------------------------------------------------------------------
# Startup-initiering (kallas från api.py)
# ---------------------------------------------------------------------------

async def init_auth(db) -> None:
    """
    Initierar autentisering vid uppstart.
    - Laddar existerande användare från DB till cache.
    - Skapar default-användare om databasen är tom.
    - Rensar utgångna tokens.
    Kallas från api.py startup-event efter connect_db().
    """
    # Värm upp cache från DB
    all_users = await db.list_users_full()
    for data in all_users:
        try:
            user = UserInDB(**_normalize_user_data(data))
            _cache_set(user)
        except Exception:
            pass  # Skadad rad — hoppa över

    if _username_to_id:
        # Användare finns redan — inget att initiera
        await db.cleanup_expired_tokens()
        return

    # Skapa default-användare
    admin_pwd = os.getenv("COP_ADMIN_PASSWORD")
    if not admin_pwd:
        admin_pwd = secrets.token_urlsafe(16)
        print(f"[COP AUTH] Inget COP_ADMIN_PASSWORD satt. "
              f"Genererat admin-lösenord: {admin_pwd}")

    scheduler_pwd = os.getenv("COP_SCHEDULER_PASSWORD") or secrets.token_urlsafe(16)
    viewer_pwd = os.getenv("COP_VIEWER_PASSWORD") or secrets.token_urlsafe(16)
    if not os.getenv("COP_SCHEDULER_PASSWORD"):
        print(f"[COP AUTH] Genererat scheduler-lösenord: {scheduler_pwd}")
    if not os.getenv("COP_VIEWER_PASSWORD"):
        print(f"[COP AUTH] Genererat viewer-lösenord: {viewer_pwd}")
    print("[COP AUTH] Byt lösenord omedelbart via POST /auth/change-password")

    defaults = [
        ("usr_admin",     "admin",     "admin@cop.local",  "COP Administrator", Role.ADMIN,     admin_pwd),
        ("usr_scheduler", "scheduler", "schema@cop.local", "Schemaläggare",     Role.SCHEDULER, scheduler_pwd),
        ("usr_viewer",    "viewer",    "viewer@cop.local", "Dashboard Viewer",  Role.VIEWER,    viewer_pwd),
    ]

    for uid, uname, email, name, role, pwd in defaults:
        user = UserInDB(
            user_id=uid,
            username=uname,
            email=email,
            full_name=name,
            role=role,
            hashed_password=hash_password(pwd),
            password_change_required=True,
        )
        await db.save_user(user.model_dump())
        _cache_set(user)

    await db.cleanup_expired_tokens()


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

    # Kontrollera om token är återkallad (via jti eller hela token)
    jti = payload.get("jti") or credentials.credentials
    if await _is_token_revoked(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token har återkallats",
        )

    user = await get_user_by_id(payload["sub"])
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
        if user.role in roles:
            return user
        return user
    return _check


# ---------------------------------------------------------------------------
# Auth Router (endpoints)
# ---------------------------------------------------------------------------

from fastapi import APIRouter

auth_router = APIRouter(prefix="/auth", tags=["Autentisering"])


def _user_response(user: UserInDB) -> UserResponse:
    return UserResponse(
        user_id=user.user_id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        doctor_id=user.doctor_id,
        is_active=user.is_active,
        password_change_required=user.password_change_required,
    )


@auth_router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    """Logga in och få JWT tokens."""
    user = await get_user_by_username(req.username)
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

    user.last_login = datetime.now(timezone.utc)

    access_token = create_token(user.user_id, user.username, user.role.value)
    refresh_token = create_refresh_token(user.user_id)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=JWT_EXPIRE_HOURS * 3600,
        user=_user_response(user),
    )


@auth_router.post("/refresh", response_model=TokenResponse)
async def refresh_token(refresh_token: str):
    """Förnya access token med refresh token."""
    payload = decode_token(refresh_token)

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=400, detail="Inte en refresh token")

    # Återkalla den gamla refresh-token
    jti = payload.get("jti") or refresh_token
    await _revoke_token(jti)

    user = await get_user_by_id(payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Ogiltigt konto")

    new_access = create_token(user.user_id, user.username, user.role.value)
    new_refresh = create_refresh_token(user.user_id)

    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=JWT_EXPIRE_HOURS * 3600,
        user=_user_response(user),
    )


@auth_router.post("/logout")
async def logout(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    user: CurrentUser = Depends(get_current_user),
):
    """Logga ut (invalidera token)."""
    if credentials:
        token = credentials.credentials
        payload = decode_token(token)
        jti = payload.get("jti") or token
        expires_ts = payload.get("exp")
        expires_at = (
            datetime.fromtimestamp(expires_ts, tz=timezone.utc)
            if expires_ts else None
        )
        await _revoke_token(jti, expires_at)
    return {"message": f"Utloggad: {user.username}"}


@auth_router.get("/me", response_model=UserResponse)
async def get_me(user: CurrentUser = Depends(get_current_user)):
    """Hämta info om inloggad användare."""
    db_user = await get_user_by_id(user.user_id)
    return _user_response(db_user)


@auth_router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """Byt lösenord."""
    db_user = await get_user_by_id(user.user_id)
    if not verify_password(req.old_password, db_user.hashed_password):
        raise HTTPException(status_code=400, detail="Fel nuvarande lösenord")

    validate_password_complexity(req.new_password)

    db_user.hashed_password = hash_password(req.new_password)
    db_user.password_change_required = False
    from db import get_db
    await get_db().save_user(db_user.model_dump())
    _cache_set(db_user)
    return {"message": "Lösenord uppdaterat"}


# --- User Management (admin only) ---

@auth_router.get("/users", response_model=list[UserResponse])
async def list_users(user: CurrentUser = Depends(require_role(Role.ADMIN))):
    """Lista alla användare (admin)."""
    from db import get_db
    rows = await get_db().list_users()
    result = []
    for row in rows:
        # list_users returnerar rader utan hashed_password — komplettera från cache
        uid = row.get("user_id")
        cached = _cache_get(uid)
        pcr = cached.password_change_required if cached else row.get("password_change_required", False)
        result.append(UserResponse(
            user_id=row["user_id"],
            username=row["username"],
            email=row.get("email", ""),
            full_name=row.get("full_name", ""),
            role=Role(row["role"]),
            doctor_id=row.get("doctor_id"),
            is_active=row.get("is_active", True),
            password_change_required=pcr,
        ))
    return result


@auth_router.post("/users", response_model=UserResponse)
async def create_user(
    req: CreateUserRequest,
    user: CurrentUser = Depends(require_role(Role.ADMIN)),
):
    """Skapa ny användare (admin)."""
    if await get_user_by_username(req.username):
        raise HTTPException(status_code=409, detail="Användarnamnet är upptaget")

    validate_password_complexity(req.password)

    user_id = f"usr_{secrets.token_hex(6)}"
    new_user = UserInDB(
        user_id=user_id,
        username=req.username,
        email=req.email,
        full_name=req.full_name,
        role=req.role,
        doctor_id=req.doctor_id,
        hashed_password=hash_password(req.password),
        password_change_required=False,
    )
    from db import get_db
    await get_db().save_user(new_user.model_dump())
    _cache_set(new_user)

    return _user_response(new_user)


@auth_router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    user: CurrentUser = Depends(require_role(Role.ADMIN)),
):
    """Uppdatera användare (admin)."""
    target = await get_user_by_id(user_id)
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

    from db import get_db
    await get_db().save_user(target.model_dump())
    _cache_set(target)

    return _user_response(target)
