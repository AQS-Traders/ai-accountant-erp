"""Work Stream R2 - clock-skew resilience in JWT verification.

This machine's clock drifts behind real time; a freshly issued token's
``iat`` is then in the LOCAL future and would be rejected as "not yet
valid".  The verifier must tolerate moderate skew (180s leeway) and
report a CLEAR clock-sync message beyond it - never the misleading
"signature verification failed".
"""

import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from app.auth import _clock_skew_message, _decode_jwt

SECRET = "test-secret-key"


def _hs256_token(iat_offset_s: float) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": "00000000-0000-4000-8000-000000000001",
            "iat": now + int(iat_offset_s),
            "exp": now + 3600,
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def fake_settings(monkeypatch):
    import app.auth as auth

    cfg = SimpleNamespace(
        jwt_secret=SECRET,
        supabase_url="https://example.supabase.co",
    )
    monkeypatch.setattr(auth, "get_settings", lambda: cfg)


class TestClockSkewResilience:
    def test_moderate_future_iat_is_tolerated(self, fake_settings):
        """A token issued up to 180s 'in the local future' still verifies
        (the machine's clock drift is absorbed by the leeway)."""
        payload = _decode_jwt(_hs256_token(120))
        assert payload["sub"] == "00000000-0000-4000-8000-000000000001"

    def test_current_iat_still_verifies(self, fake_settings):
        payload = _decode_jwt(_hs256_token(0))
        assert payload["sub"] == "00000000-0000-4000-8000-000000000001"

    def test_large_future_iat_reports_clock_sync(self, fake_settings):
        """Beyond the leeway the error names the REAL problem: the clock."""
        with pytest.raises(HTTPException) as exc:
            _decode_jwt(_hs256_token(600))
        assert "clock" in exc.value.detail.lower()

    def test_skew_message_names_the_fix(self):
        msg = _clock_skew_message(_hs256_token(300))
        assert "minute" in msg
        assert "fix_clock.bat" in msg
