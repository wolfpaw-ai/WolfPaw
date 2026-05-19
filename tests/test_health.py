from fastapi.testclient import TestClient

from wolfpaw.api import create_app


def test_health_returns_ok():
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "env" in body


def test_health_emits_trace_id_header():
    client = TestClient(create_app())
    response = client.get("/health")
    assert "x-trace-id" in response.headers
    assert len(response.headers["x-trace-id"]) == 32


def test_health_honors_incoming_trace_id():
    client = TestClient(create_app())
    incoming = "abcdef1234567890abcdef1234567890"
    response = client.get("/health", headers={"x-trace-id": incoming})
    assert response.headers["x-trace-id"] == incoming
