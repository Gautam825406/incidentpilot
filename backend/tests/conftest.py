"""Shared test fixtures.

The in-memory rate limiters (app/main.py) stay ENABLED in tests — the
existing suite must pass with limiting on. Because every test hits the
API from the same TestClient IP ("testclient"), limiter state is reset
before each test so one test's login/webhook calls never 429 the next.
Rate-limit behaviour itself is covered in test_phase4.py, which lowers
the limits via monkeypatch.

Note: the limiters are imported lazily inside the fixture, not at module
level — importing app.main here would bind the app to the default
DATABASE_URL before the test modules set their scratch SQLite URL.
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    from app.main import investigate_limiter, login_limiter, webhook_limiter

    login_limiter.reset()
    webhook_limiter.reset()
    investigate_limiter.reset()
    yield
    login_limiter.reset()
    webhook_limiter.reset()
    investigate_limiter.reset()
