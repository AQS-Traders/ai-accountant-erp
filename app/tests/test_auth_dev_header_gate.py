"""Security regression tests — development header auth gate and JWT defaults.

Covers issue category 1: "production development-header authentication".

The legacy ``X-User-Id`` header fallback in ``app/auth.py`` used to be
honoured in EVERY environment with no gate at all, so any caller could
impersonate any user without a token (full cross-tenant impersonation).
``authenticate_header`` and ``authenticate_user`` now require BOTH
``app_env == "development"`` AND the explicit ``ALLOW_DEV_HEADER_AUTH=true``
opt-in, which defaults to false — so a misconfigured deployment fails CLOSED.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from fastapi import HTTPException

import app.auth as auth

USER_ID = "22222222-2222-2222-2222-222222222222"
MEMBER_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = "33333333-3333-3333-3333-333333333333"


def _settings(app_env: str, allow_dev_header_auth: bool, jwt_secret: str):
    return SimpleNamespace(
        app_env=app_env,
        allow_dev_header_auth=allow_dev_header_auth,
        jwt_secret=jwt_secret,
        supabase_url="https://example.supabase.co",
    )


@pytest.fixture
def configure(monkeypatch):
    """Return a helper that installs a fake settings object on app.auth."""

    def _configure(app_env: str, allow_dev_header_auth: bool = False,
                   *, jwt_secret: str = "test-secret-key") -> None:
        cfg = _settings(app_env, allow_dev_header_auth, jwt_secret)
        monkeypatch.setattr(auth, "get_settings", lambda: cfg)

    return _configure


class TestDevHeaderAuthGate:
    """The header fallback must be dead outside local development."""

    def test_flag_defaults_to_false(self):
        """An unconfigured deployment must not trust headers."""
        from app.config import Settings

        assert Settings.model_fields["allow_dev_header_auth"].default is False

    @pytest.mark.asyncio
    async def test_production_rejects_x_user_id_only(self, configure):
        configure("production", allow_dev_header_auth=True)
        with pytest.raises(HTTPException) as exc:
            await auth.authenticate_header(
                authorization=None,
                x_user_id=USER_ID,
                x_organization_id=OTHER_ORG,
            )
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_staging_rejects_x_user_id_only(self, configure):
        configure("staging", allow_dev_header_auth=True)
        with pytest.raises(HTTPException) as exc:
            await auth.authenticate_header(
                authorization=None, x_user_id=USER_ID,
                x_organization_id=OTHER_ORG,
            )
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_development_without_opt_in_rejects(self, configure):
        """APP_ENV=development alone is NOT enough — opt-in is required."""
        configure("development", allow_dev_header_auth=False)
        with pytest.raises(HTTPException) as exc:
            await auth.authenticate_header(
                authorization=None, x_user_id=USER_ID,
                x_organization_id=OTHER_ORG,
            )
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_onboarding_endpoint_also_gated(self, configure):
        """authenticate_user (pre-organization onboarding) must be gated too."""
        configure("production", allow_dev_header_auth=True)
        with pytest.raises(HTTPException) as exc:
            await auth.authenticate_user(authorization=None, x_user_id=USER_ID)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_opt_in_derives_org_from_membership_not_header(
        self, configure, monkeypatch
    ):
        """Even when enabled, X-Organization-Id must NOT be trusted."""
        configure("development", allow_dev_header_auth=True)

        async def _fake_fetch_many(table, **kwargs):
            assert table == "organization_members"
            return [{"organization_id": str(MEMBER_ORG), "role_id": None}]

        monkeypatch.setattr(auth, "fetch_many", _fake_fetch_many)

        ctx = await auth.authenticate_header(
            authorization=None,
            x_user_id=USER_ID,
            x_organization_id=OTHER_ORG,  # attacker-supplied, must be ignored
        )
        assert ctx.organization_id == MEMBER_ORG
        assert str(ctx.organization_id) != OTHER_ORG


class TestLegacyHs256Defaults:
    """The default JWT secret must never act as a signing key."""

    def test_change_me_secret_is_refused(self, configure):
        configure("production", jwt_secret="change-me")
        now = int(time.time())
        forged = pyjwt.encode(
            {"sub": USER_ID, "iat": now, "exp": now + 3600},
            "change-me",
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            auth._decode_jwt(forged)
        assert exc.value.status_code == 401

    def test_empty_secret_is_refused(self, configure):
        """With an empty configured secret, HS256 must be refused cleanly.

        PyJWT cannot even *mint* a token with an empty HMAC key, so the
        token below is signed with an arbitrary key.  The point of the
        assertion is that the empty-secret guard in ``_decode_jwt`` short
        circuits BEFORE any decode attempt, so the caller gets a 401 rather
        than an unhandled ``InvalidKeyError`` (HTTP 500).
        """
        configure("production", jwt_secret="")
        now = int(time.time())
        token = pyjwt.encode(
            {"sub": USER_ID, "iat": now, "exp": now + 3600},
            "some-other-key",
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            auth._decode_jwt(token)
        assert exc.value.status_code == 401