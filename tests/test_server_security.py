"""tests/test_server_security.py — P1 安全加固测试 (API Key + CORS).

覆盖 ROADMAP P1 两项:
  #4 API Key middleware — `API_KEY` env → 校验 `X-API-Key` header,
      未设置 env 时保持 no-auth (向后兼容)
  #5 CORS 收紧 — origin 白名单配置化 (`CORS_ALLOW_ORIGINS` env, 逗号分隔),
      默认收紧为空列表 (未配置 origin 的跨域请求被拒)

用 create_app() 工厂 + monkeypatch env 构造不同配置的 app, TestClient in-process
验证, 不真起 uvicorn。
"""

import pytest
from fastapi.testclient import TestClient

from multimedia_parsing.server import create_app
from multimedia_parsing.security import (
    API_KEY_HEADER,
    load_api_key,
    load_cors_allow_origins,
)


# ---------------------------------------------------------------------------
# 单元: env 解析函数
# ---------------------------------------------------------------------------


class TestLoadApiKey:
    def test_unset_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_KEY", raising=False)
        assert load_api_key() is None

    def test_set_returns_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_KEY", "secret-123")
        assert load_api_key() == "secret-123"

    def test_blank_string_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_KEY", "   ")
        assert load_api_key() is None


class TestLoadCorsAllowOrigins:
    def test_unset_returns_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
        assert load_cors_allow_origins() == []

    def test_parses_csv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "CORS_ALLOW_ORIGINS", "https://a.com, https://b.com ,,https://c.com"
        )
        assert load_cors_allow_origins() == [
            "https://a.com",
            "https://b.com",
            "https://c.com",
        ]

    def test_blank_string_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "")
        assert load_cors_allow_origins() == []

    def test_star_wildcard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
        assert load_cors_allow_origins() == ["*"]


# ---------------------------------------------------------------------------
# #4 API Key middleware
# ---------------------------------------------------------------------------


class TestApiKeyMiddleware:
    @pytest.fixture
    def client_with_key(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        monkeypatch.setenv("API_KEY", "secret-123")
        return TestClient(create_app())

    def test_no_env_keeps_no_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """向后兼容: env 未配置时无 key 也能正常访问."""
        monkeypatch.delenv("API_KEY", raising=False)
        client = TestClient(create_app())
        assert client.get("/batches").status_code == 200

    def test_missing_key_returns_401(
        self, client_with_key: TestClient
    ) -> None:
        r = client_with_key.get("/batches")
        assert r.status_code == 401
        assert "API key" in r.json()["detail"]

    def test_wrong_key_returns_401(self, client_with_key: TestClient) -> None:
        r = client_with_key.get(
            "/batches", headers={API_KEY_HEADER: "wrong-key"}
        )
        assert r.status_code == 401

    def test_valid_key_allowed(self, client_with_key: TestClient) -> None:
        r = client_with_key.get(
            "/batches", headers={API_KEY_HEADER: "secret-123"}
        )
        assert r.status_code == 200

    def test_health_exempt(self, client_with_key: TestClient) -> None:
        """/health 免认证 — 供探活/监控."""
        r = client_with_key.get("/health")
        assert r.status_code == 200

    def test_openapi_exempt(self, client_with_key: TestClient) -> None:
        r = client_with_key.get("/openapi.json")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# #5 CORS 收紧
# ---------------------------------------------------------------------------


def _preflight(client: TestClient, origin: str) -> object:
    return client.options(
        "/parse",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
        },
    )


class TestCorsTightened:
    def test_default_rejects_unconfigured_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认 (env 未配置): 白名单为空, 跨域请求被拒 (无 allow-origin header)."""
        monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
        client = TestClient(create_app())
        r = _preflight(client, "https://evil.com")
        assert "access-control-allow-origin" not in r.headers

    def test_configured_origin_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
        client = TestClient(create_app())
        r = _preflight(client, "https://app.example.com")
        assert r.headers.get("access-control-allow-origin") == "https://app.example.com"

    def test_other_origin_still_rejected_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
        client = TestClient(create_app())
        r = _preflight(client, "https://evil.com")
        assert "access-control-allow-origin" not in r.headers

    def test_wildcard_allows_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
        client = TestClient(create_app())
        r = _preflight(client, "https://anything.com")
        assert r.headers.get("access-control-allow-origin") == "*"

    def test_simple_get_adds_allow_origin_when_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非 preflight 的简单请求也要带 allow-origin header (allowed 时)."""
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
        client = TestClient(create_app())
        r = client.get(
            "/health", headers={"Origin": "https://app.example.com"}
        )
        assert r.headers.get("access-control-allow-origin") == "https://app.example.com"
