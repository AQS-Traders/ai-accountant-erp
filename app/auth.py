"""
ERP AI Agent — JWT Authentication
===================================
Validates Supabase JWT tokens using the JWKS public-key endpoint and
resolves user identity + organisation membership.

The Supabase project uses ECC P-256 (ES256) as the current JWT signing
key.  Legacy HS256 (shared-secret) tokens are still accepted as a
fallback for tokens issued before the key rotation.

Flow:
  Authorization: Bearer <JWT>
      ↓
  Fetch JWKS from Supabase OIDC discovery endpoint
      ↓
  Verify token signature with the matching public key (ES256)
  — or fall back to HS256 with the legacy JWT secret
      ↓
  Extract user_id (sub claim)
      ↓
  Query organization_members to resolve organization_id
      ↓
  Query organization_roles to resolve permissions
      ↓
  Return AuthContext
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import jwt as pyjwt
from jwt import PyJWKClient, PyJWKClientError
import structlog
from fastapi import Header, HTTPException, status

from app.config import get_settings
from app.database import fetch_many, fetch_one

log = structlog.get_logger(__name__)

# Algorithms we accept for verification.
# ES256 = current Supabase ECC P-256 key.
# RS256 = possible future / alternative key type.
# HS256 = legacy shared-secret key (fallback only).
_ALLOWED_ALGORITHMS = ["ES256", "RS256", "HS256"]

# Clock-skew tolerance: this machine's clock has been observed drifting
# minutes behind real time (Windows time sync not holding).  180s absorbs
# normal drift for iat/nbf/exp alike (PyJWT applies the leeway symmetrically;
# 3 minutes is standard practice and does not weaken the auth model).
_CLOCK_LEEWAY_SECONDS = 180


def _clock_skew_message(token: str) -> str:
    """Explain a future-iat rejection and quantify the local clock skew."""
    try:
        claims = pyjwt.decode(token, options={"verify_signature": False})
        issued_at = claims.get("iat")
        if issued_at is not None:
            skew = float(issued_at) - datetime.now(timezone.utc).timestamp()
            minutes = max(1, int(skew // 60) + 1)
            return (
                "Your computer clock is behind real time by about "
                f"{minutes} minute(s), so this freshly issued token looks "
                "'not yet valid'. Sync the Windows clock (run fix_clock.bat, "
                "or Settings > Time & language > Date & time > Sync now) "
                "and try again."
            )
    except Exception:  # noqa: BLE001 - message building must never fail
        pass
    return (
        "Your computer clock is behind real time, so this freshly issued "
        "token looks 'not yet valid'. Sync the Windows clock (run "
        "fix_clock.bat) and try again."
    )

# Cached JWKS client (one per process).
_jwks_client: Optional[PyJWKClient] = None


@dataclass
class AuthContext:
    """Authenticated user context."""
    user_id: uuid.UUID
    organization_id: uuid.UUID
    role_code: str = ""
    role_permissions: List[str] = field(default_factory=list)


@dataclass
class UserContext:
    """Authenticated user WITHOUT organization resolution.

    Used by the endpoints that run BEFORE an organization exists — the
    first-time onboarding assistant analyses and populates organization
    fields while the user still has no membership, so it must not require
    the membership/role lookup :func:`authenticate` performs.
    """
    user_id: uuid.UUID


# ---------------------------------------------------------------------------
# JWKS
# ---------------------------------------------------------------------------

def _get_jwks_client() -> PyJWKClient:
    """Return (cached) PyJWKClient for the Supabase JWKS endpoint.

    The endpoint is publicly accessible — no API key required.
    URL: ``{SUPABASE_URL}/auth/v1/.well-known/jwks.json``
    """
    global _jwks_client
    if _jwks_client is None:
        settings = get_settings()
        jwks_url = f"{settings.supabase_url}/auth/v1/.well-known/jwks.json"
        _jwks_client = PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)
        log.debug("auth.jwks_client_created", url=jwks_url)
    return _jwks_client


def _decode_jwt(token: str) -> Dict[str, Any]:
    """Decode and verify a Supabase JWT token.

    Strategy is chosen based on the token's ``alg`` header:

    * **ES256 / RS256** — verify against the JWKS public key endpoint.
    * **HS256** — verify against the legacy shared secret from ``.env``.
    """
    settings = get_settings()

    # --- Inspect header (for logging + kid lookup) -------------------------
    try:
        unverified_header = pyjwt.get_unverified_header(token)
    except pyjwt.DecodeError:
        raise HTTPException(status_code=401, detail="Malformed token")

    token_alg = unverified_header.get("alg", "unknown")
    token_kid = unverified_header.get("kid")
    log.debug(
        "auth.jwt_header",
        alg=token_alg,
        kid=token_kid,
    )

    # --- Path A: Asymmetric verification (ES256 / RS256) via JWKS ---------
    if token_alg in ("ES256", "ES384", "ES512", "RS256", "RS384", "RS512"):
        try:
            signing_key = _get_jwks_client().get_signing_key(
                token_kid or ""
            )
            payload = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=[token_alg],
                options={"verify_exp": True, "verify_aud": False},
                leeway=_CLOCK_LEEWAY_SECONDS,
            )
            return payload
        except pyjwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token has expired")
        except pyjwt.ImmatureSignatureError:
            # The token's iat/nbf is in the FUTURE relative to THIS
            # machine's clock: the local clock is behind real time.  This
            # is a machine problem, not an auth problem - say so clearly.
            raise HTTPException(
                status_code=401,
                detail=_clock_skew_message(token),
            )
        except PyJWKClientError as exc:
            # The token's kid is not in the current JWKS (typical cause: the
            # browser replayed a token issued under a previous signing key)
            # or the JWKS endpoint was temporarily unreachable.
            log.warning(
                "auth.jwks_verify_failed",
                category="jwks_key_or_fetch",
                alg=token_alg,
                kid_present=token_kid is not None,
                error=str(exc),
            )
            raise HTTPException(
                status_code=401,
                detail="Invalid authentication token: signing key not recognized "
                       "(stale token or JWKS unavailable)",
            )
        except pyjwt.InvalidTokenError as exc:
            # Signature verification or claim validation (iat/nbf with leeway)
            # failed against the correct key.
            log.warning(
                "auth.jwks_verify_failed",
                category="signature_or_claims",
                alg=token_alg,
                kid_present=token_kid is not None,
                error=str(exc),
            )
            raise HTTPException(
                status_code=401,
                detail="Invalid authentication token: signature verification failed",
            )

    # --- Path B: Symmetric verification (HS256 legacy shared secret) -----
    if token_alg in ("HS256", "HS384", "HS512"):
        jwt_secret = settings.jwt_secret
        if jwt_secret and jwt_secret != "change-me":
            for secret_bytes in _resolve_secret_candidates(jwt_secret):
                try:
                    payload = pyjwt.decode(
                        token,
                        secret_bytes,
                        algorithms=[token_alg],
                        options={"verify_exp": True, "verify_aud": False},
                        leeway=_CLOCK_LEEWAY_SECONDS,
                    )
                    return payload
                except pyjwt.ExpiredSignatureError:
                    raise HTTPException(
                        status_code=401, detail="Token has expired"
                    )
                except pyjwt.ImmatureSignatureError:
                    # Local clock behind real time - say so clearly.
                    raise HTTPException(
                        status_code=401, detail=_clock_skew_message(token)
                    )
                except pyjwt.InvalidTokenError:
                    continue

    # All attempts exhausted or unsupported algorithm
    log.warning(
        "auth.jwt_invalid",
        alg=token_alg,
        kid=token_kid,
    )
    raise HTTPException(status_code=401, detail="Invalid authentication token")


def _resolve_secret_candidates(raw_secret: str) -> List[bytes]:
    """Return possible byte values for the legacy HS256 secret."""
    candidates: List[bytes] = []
    # Base64-decoded (modern Supabase projects store the secret encoded).
    try:
        candidates.append(base64.b64decode(raw_secret))
    except Exception:
        pass
    # Raw UTF-8 string.
    candidates.append(raw_secret.encode("utf-8"))
    return candidates


# ---------------------------------------------------------------------------
# Membership / role resolution
# ---------------------------------------------------------------------------

async def _resolve_membership(
    user_id: uuid.UUID,
) -> Optional[Dict[str, Any]]:
    """Find the first active organization membership for the user."""
    members = await fetch_many(
        "organization_members",
        filters={"user_id": str(user_id), "status": "ACTIVE"},
        limit=1,
    )
    if not members:
        # Try without status filter
        members = await fetch_many(
            "organization_members",
            filters={"user_id": str(user_id)},
            limit=1,
        )
    return members[0] if members else None


async def _resolve_role(role_id: str) -> Dict[str, Any]:
    """Fetch the role definition and permissions."""
    role = await fetch_one("organization_roles", filters={"id": role_id})
    return role or {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def authenticate(
    authorization: Optional[str] = None,
) -> AuthContext:
    """Authenticate a request from the Authorization header.

    Extracts the Bearer token, decodes the JWT, resolves the user's
    organisation membership and role permissions.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )

    token = authorization[7:]  # Strip "Bearer "
    payload = _decode_jwt(token)

    # Extract user_id from the 'sub' claim
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token: missing subject")

    try:
        user_id = uuid.UUID(sub)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token: malformed user ID")

    # Resolve organisation membership
    membership = await _resolve_membership(user_id)
    if not membership:
        raise HTTPException(
            status_code=403,
            detail="User is not a member of any organization",
        )

    organization_id = uuid.UUID(membership["organization_id"])
    role_id = membership.get("role_id")

    # Resolve role permissions
    role_code = ""
    role_permissions: List[str] = []
    if role_id:
        role = await _resolve_role(role_id)
        role_code = role.get("code", "")
        role_permissions = role.get("permissions", [])

    return AuthContext(
        user_id=user_id,
        organization_id=organization_id,
        role_code=role_code,
        role_permissions=role_permissions,
    )


async def authenticate_user(
    authorization: Optional[str] = None,
    x_user_id: Optional[str] = None,
) -> UserContext:
    """Authenticate a request WITHOUT resolving an organization.

    Identical token verification to :func:`authenticate` (ES256 via JWKS,
    legacy HS256 fallback, dev header fallback), but it deliberately does
    not look up — or require — an organization membership.  First-time
    onboarding happens before the user belongs to any organization.
    """
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]  # Strip "Bearer "
        payload = _decode_jwt(token)
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Invalid token: missing subject")
        try:
            return UserContext(user_id=uuid.UUID(sub))
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid token: malformed user ID")

    # Legacy development header fallback (no organization required).
    if x_user_id:
        try:
            return UserContext(user_id=uuid.UUID(x_user_id))
        except (ValueError, TypeError):
            raise HTTPException(status_code=401, detail="Invalid user ID")

    raise HTTPException(
        status_code=401,
        detail="Authentication required: provide Bearer token or X-User-Id header",
    )


async def authenticate_user_header(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> UserContext:
    """FastAPI dependency for endpoints that must work before an
    organization exists (organization onboarding).  Provides user identity
    only."""
    return await authenticate_user(authorization=authorization, x_user_id=x_user_id)


async def authenticate_header(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-Id"),
) -> AuthContext:
    """FastAPI dependency that supports both JWT and legacy header auth.

    Priority:
    1. Bearer JWT token (production)
    2. X-User-Id + X-Organization-Id headers (development fallback)
    """
    # Try JWT first
    if authorization and authorization.startswith("Bearer "):
        return await authenticate(authorization)

    # Fallback to header-based auth (development ONLY)
    if x_user_id:
        try:
            user_id = uuid.UUID(x_user_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=401, detail="Invalid user ID")

        # Always derive organization_id from the user's ACTUAL
        # membership, never trust the client-supplied X-Organization-Id.
        membership = await _resolve_membership(user_id)
        if not membership:
            raise HTTPException(
                status_code=403,
                detail="User is not a member of any organization",
            )

        organization_id = uuid.UUID(membership["organization_id"])
        role_code = ""
        role_permissions: List[str] = []
        if membership.get("role_id"):
            role = await _resolve_role(membership["role_id"])
            role_code = role.get("code", "")
            role_permissions = role.get("permissions", [])

        return AuthContext(
            user_id=user_id,
            organization_id=organization_id,
            role_code=role_code,
            role_permissions=role_permissions,
        )

    raise HTTPException(
        status_code=401,
        detail="Authentication required: provide Bearer token or X-User-Id header",
    )
