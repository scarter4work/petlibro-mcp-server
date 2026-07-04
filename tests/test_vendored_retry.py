import pytest

from petlibro_mcp.vendored.api import PetLibroSession
from petlibro_mcp.vendored.exceptions import PetLibroAPIError


class FakeResponse:
    """Minimal stand-in for an aiohttp response used as an async context manager."""

    def __init__(self, status, json_data):
        self.status = status
        self._json_data = json_data

    async def json(self):
        return self._json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeWebSession:
    """Fake aiohttp ClientSession returning a canned response per call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        resp = self._responses[self.calls]
        self.calls += 1
        return resp


@pytest.fixture
def fake_websession():
    return FakeWebSession([
        FakeResponse(200, {"code": 1009, "msg": "NOT_YET_LOGIN"}),
        FakeResponse(200, {"code": 1234, "msg": "STILL_BROKEN"}),
    ])


async def test_retry_after_relogin_raises_on_failed_retry(fake_websession, monkeypatch):
    """A 1009 NOT_YET_LOGIN triggers re-login + retry. If the retry itself
    fails (non-zero code), the retry data must NOT be swallowed as success."""
    session = PetLibroSession(
        base_url="https://api.us.petlibro.com",
        email="a@b.com", password="pw", region="US",
        websession=fake_websession, token="old-token",
    )

    async def fake_re_login():
        session.token = "new-token"
        return "new-token"

    monkeypatch.setattr(session, "re_login", fake_re_login)

    with pytest.raises(PetLibroAPIError) as exc_info:
        await session.request("POST", "/device/device/manualFeeding", json={})

    assert "1234" in str(exc_info.value)
    assert "STILL_BROKEN" in str(exc_info.value)
    # both the original and the retried request actually happened
    assert fake_websession.calls == 2
