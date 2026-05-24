"""HTTP test for `GET /usage` (JSON). Uses dependency override + monkeypatched
`build_usage_report` so it runs without Postgres."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from wolfpaw.api import create_app
from wolfpaw.auth.deps import require_user_id
from wolfpaw.metering.usage_report import PeriodReport, UsageReport, ModelRow


def test_usage_requires_auth():
    app = create_app()
    client = TestClient(app)
    r = client.get("/usage")
    assert r.status_code == 401


def test_usage_returns_serialized_report(monkeypatch):
    uid = uuid4()
    fake_report = UsageReport(
        user_id=uid, scope="default",
        generated_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        timezone="UTC", include_by_agent=False,
        periods=[
            PeriodReport(
                label="Today (May 24)",
                start=datetime(2026, 5, 24, tzinfo=timezone.utc),
                end=datetime(2026, 5, 25, tzinfo=timezone.utc),
                models=[ModelRow(
                    model="claude-haiku-4-5",
                    input_tokens=1000, output_tokens=200,
                    cache_read_tokens=0, cache_write_tokens=0,
                    input_cost_cents=1, output_cost_cents=1,
                    cache_cost_cents=0, total_cost_cents=2,
                )],
                by_agent=[],
                compute_cost_cents=0, total_cost_cents=2,
            ),
        ],
    )

    async def fake_build(user_id, scope="default"):
        return fake_report

    monkeypatch.setattr(
        "wolfpaw.metering.routes.build_usage_report", fake_build,
    )

    app = create_app()
    app.dependency_overrides[require_user_id] = lambda: uid
    client = TestClient(app)
    r = client.get("/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "default"
    assert body["timezone"] == "UTC"
    assert len(body["periods"]) == 1
    period = body["periods"][0]
    assert period["label"] == "Today (May 24)"
    assert period["models"][0]["model"] == "claude-haiku-4-5"
    assert period["total_cost_cents"] == 2


def test_usage_rejects_unknown_scope(monkeypatch):
    """Bad scope returns 400 with the error message inline."""
    uid = uuid4()
    app = create_app()
    app.dependency_overrides[require_user_id] = lambda: uid
    client = TestClient(app)
    r = client.get("/usage", params={"scope": "weekly"})
    assert r.status_code == 400
    assert "weekly" in r.json()["detail"]


def test_usage_scope_query_param_forwarded(monkeypatch):
    """`?scope=month` should reach build_usage_report as the parsed scope."""
    uid = uuid4()
    captured = {}

    async def fake_build(user_id, scope="default"):
        captured["scope"] = scope
        return UsageReport(
            user_id=uid, scope=scope,
            generated_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
            timezone="UTC", include_by_agent=(scope == "month"),
            periods=[],
        )

    monkeypatch.setattr(
        "wolfpaw.metering.routes.build_usage_report", fake_build,
    )
    app = create_app()
    app.dependency_overrides[require_user_id] = lambda: uid
    client = TestClient(app)
    r = client.get("/usage", params={"scope": "month"})
    assert r.status_code == 200
    assert captured["scope"] == "month"
    assert r.json()["include_by_agent"] is True
