"""publisher.resource_fetcher — 通用资源解析下载模块.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md.

按 URL 智能路由到 video / image(本期)/ audio(预留, D2)/ model(预留, D2)解析器,
统一预览网格 + 统一下载模式(local / oss / both, D3).

子模块:
  - base:     ResourceType enum + ResourceItem / FetchDestination / FetchResult dataclass
              + ResourceResolver Protocol + ResourceFetchError
  - router:   URL → Resolver 智能路由(初判, spec §4 两步判定第一步)
  - resolvers/: video / image 的 ResourceResolver 实现(包装现有 video_fetcher / image_fetcher)
  - manifest: 统一 manifest JSON 校验 (ResourceParseManifest / ResourceFetchManifest)
  - run_parse / run_fetch: (Commit 3) 编排层
"""
from __future__ import annotations

from .base import (
    VALID_MODES,
    FetchDestination,
    FetchResult,
    ResourceFetchError,
    ResourceItem,
    ResourceResolver,
    ResourceType,
)
from .manifest import (
    ManifestValidationError,
    ResourceFetchItem,
    ResourceFetchManifest,
    ResourceParseManifest,
    load_resource_fetch_manifest,
    load_resource_parse_manifest,
    parse_resource_fetch_manifest_dict,
    parse_resource_parse_manifest_dict,
)
from .router import (
    RESOLVER_RULES,
    ResolverRule,
    resolve,
)

__all__ = [
    # base
    "VALID_MODES",
    "FetchDestination",
    "FetchResult",
    "ResourceFetchError",
    "ResourceItem",
    "ResourceResolver",
    "ResourceType",
    # manifest
    "ManifestValidationError",
    "ResourceFetchItem",
    "ResourceFetchManifest",
    "ResourceParseManifest",
    "load_resource_fetch_manifest",
    "load_resource_parse_manifest",
    "parse_resource_fetch_manifest_dict",
    "parse_resource_parse_manifest_dict",
    # router
    "RESOLVER_RULES",
    "ResolverRule",
    "resolve",
]

