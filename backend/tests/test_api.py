import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./test_api.db"
for f in ("test_api.db",):
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), f)
    if os.path.exists(p):
        os.remove(p)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def test_health_demo_mode():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["demo_mode"] is True


def test_simulate_end_to_end_to_approval():
    r = client.post("/api/simulate", json={"scenario": "checkout-oom"})
    assert r.status_code == 200, r.text
    inc_id = r.json()["incident_id"]
    detail = client.get(f"/api/incidents/{inc_id}").json()
    assert detail["status"] == "awaiting_approval"
    assert detail["hypotheses"][0]["statement"].startswith("Memory leak")
    assert detail["plan"]["status"] == "pending"
    # Execution without approval must fail.
    plan_id = detail["plan"]["id"]
    assert client.post(f"/api/plans/{plan_id}/execute", json={"dry_run": False}).status_code == 401
    # Login, approve, dry-run, execute, verify.
    login = client.post("/api/auth/login", json={"email": "admin@demo.local", "password": "admin123"}).json()
    headers = {"Authorization": f"Bearer {login['token']}"}
    assert client.post(f"/api/plans/{plan_id}/approve", headers=headers).json()["ok"] is True
    ex = client.post(f"/api/plans/{plan_id}/execute", json={"dry_run": True}, headers=headers)
    assert ex.status_code == 200, ex.text
    ex2 = client.post(f"/api/plans/{plan_id}/execute", json={"dry_run": False}, headers=headers)
    assert ex2.status_code == 200, ex2.text
    ver = client.post(f"/api/incidents/{inc_id}/verify", headers=headers)
    assert ver.status_code == 200
    assert ver.json()["passed"] is True
