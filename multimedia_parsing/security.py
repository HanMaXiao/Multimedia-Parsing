"""multimedia_parsing.security — P1 安全加固 (API Key auth + CORS 白名单).

ROADMAP P1:
  #4 API Key middleware — `API_KEY` env → 校验 `X-API-Key` header;
      env 未设置时保持 no-auth (向后兼容内网信任环境)
  #5 CORS 收紧 — `CORS_ALLOW_ORIGINS` env (逗号分隔 origin 白名单);
      默认空列表 = 未配置 origin 的跨域请求被拒

配置全部走 env var (12-factor), app 工厂 create_app() 在构造时读取。
"""

from __future__ import annotations

import hmac
import os
from typing import List, Mapping, Optional

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

API_KEY_ENV = "API_KEY"
API_KEY_HEADER = "X-API-Key"
CORS_ORIGINS_ENV = "CORS_ALLOW_ORIGINS"

# 免认证端点 — 探活 / API 文档 (监控和浏览器调试需要)
AUTH_EXEMPT_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})


def load_api_key(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """从 env 读 `API_KEY`, 空白/未设置返回 None (= no-auth 模式)."""
    source = os.environ if env is None else env
    key = source.get(API_KEY_ENV, "").strip()
    return key or None


def load_cors_allow_origins(
    env: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """从 env 读 `CORS_ALLOW_ORIGINS` (逗号分隔白名单).

    默认 (未设置/空白) 返回 [] — 全部跨域拒绝 (P1 #5 收紧语义).
    显式 `*` 返回 ["*"] = 全放行 (等效旧的内网全放行行为).
    """
    source = os.environ if env is None else env
    raw = source.get(CORS_ORIGINS_ENV, "").strip()
    if not raw:
        return []
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return ["*"] if origins == ["*"] else origins


class APIKeyMiddleware(BaseHTTPMiddleware):
    """校验 `X-API-Key` header — api_key=None 时透传 (no-auth)."""

    def __init__(self, app: object, api_key: Optional[str]) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._api_key = api_key

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if self._api_key is None:
            return await call_next(request)
        if request.url.path in AUTH_EXEMPT_PATHS:
            return await call_next(request)
        provided = request.headers.get(API_KEY_HEADER)
        if provided is None or not hmac.compare_digest(provided, self._api_key):
            return JSONResponse(
                status_code=401,
                content={"detail": f"missing or invalid API key (header: {API_KEY_HEADER})"},
            )
        return await call_next(request)
