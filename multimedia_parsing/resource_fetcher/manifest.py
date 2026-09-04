"""resource_fetcher.manifest — 统一 manifest JSON 解析 + 边界校验.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §5.2.

设计要点:
  - 两种 manifest:
      - ResourceParseManifest: 解析阶段, { batch_id, urls }.
      - ResourceFetchManifest: 下载/上传阶段, { batch_id, mode, download_dir?, oss?, items, ... }.
  - ManifestValidationError 跨包复用: 跟 image_fetcher / video_fetcher 同一个类
    (image_fetcher 已从 video_fetcher 导入, 路径一致; F0.5 Commit 3 移 OssUploader
    时保持此 re-export 不变).
  - mode 互斥沿用 image_fetcher 模式 + D3 决策的 'both' 新增 (本地 + OSS 都做).
  - ResourceFetchItem 是 frozen dataclass, 跟 video/image item 混发兼容 (item.resource_type 终判).
  - items[i].resource_type 白名单: video / image / audio / model (audio / model 是 D2 预留,
    本期 video_fetcher / image_fetcher 不接 audio/model 解析, 但 resource_type 字段允许,
    给前端预留).
  - batch_id / urls / download_dir 字段校验沿用 image_fetcher 三档错误消息风格
    (非 string / 空 string / 非 http(s) scheme) 便于上层精准提示.

不接受(2026-09-03 D1 决策):
  - YAML / TOML: 跟项目 data_settings.json / storage_settings.json 统一用 JSON.
  - pydantic: 项目无 pydantic 依赖, stdlib 优先.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# 跨包复用: image_fetcher 早已 re-export video_fetcher 的 ManifestValidationError,
# 这里直接 re-export image_fetcher 的版本 (F0.5 Commit 3 移 OssUploader 时仍兼容).
from multimedia_parsing.image_fetcher.manifest import (  # noqa: F401
    ManifestValidationError,
)
# OssConfig 也从 image_fetcher 拿 (image_fetcher 跨包从 video_fetcher 导入, 路径一致).
from multimedia_parsing.image_fetcher.manifest import ImageEntry  # noqa: F401  (参考类型, 本期不用)
from multimedia_parsing.video_fetcher.manifest import OssConfig


# ---------------------------------------------------------------------------
# 常量 + 资源类型白名单 (D2 决策)
# ---------------------------------------------------------------------------


VALID_MODES: frozenset[str] = frozenset({"local", "oss", "both"})

# 资源类型白名单 — video / image 是本期; audio / model 是 D2 预留, 允许在 item.resource_type
# 出现 (前端分类 tab 灰显 + 路由枚举有), 但 video_fetcher / image_fetcher 解析时会失败 (路由拒绝).
VALID_RESOURCE_TYPES: frozenset[str] = frozenset({"video", "image", "audio", "model"})


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceFetchItem:
    """单条资源记录 — ResourceFetchManifest.items 列表元素 (跨 video / image / 预留 audio/model).

    字段:
      - url: 资源 URL (video = source page, image = 直链).
      - source_url: 用户原始粘贴 URL (回查用, video 与 url 相同, image 是源页面).
      - resource_type: "video" / "image" / "audio" / "model".
      - item_id: 平台内资源标识 (video = "{platform}_{video_id}", image = uuid).
      - platform: 平台标识 (bilibili / xiaohongshu / weibo / ...).
      - title: 资源标题 (可选).
      - thumbnail: 缩略图 URL (可选).
      - meta: resolver-specific 扩展字段 (image: image_url / width / height, video: 暂无).
    """

    url: str
    source_url: str
    resource_type: str
    item_id: str
    platform: str
    title: str = ""
    thumbnail: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResourceParseManifest:
    """解析阶段 manifest — 1 batch_id + N 个源 URL.

    CLI 入口 (Commit 3): ``python -m publisher --resource-parse-manifest <path>``.
    """

    batch_id: str
    urls: List[str]

    def __post_init__(self) -> None:
        if not self.batch_id or not self.batch_id.strip():
            raise ManifestValidationError("batch_id must be a non-empty string")
        for i, url in enumerate(self.urls):
            _validate_http_url(url, f"urls[{i}]")


@dataclass(frozen=True)
class ResourceFetchManifest:
    """下载/上传阶段 manifest — 1 batch_id + mode + (download_dir or oss) + N 个 items.

    CLI 入口 (Commit 3): ``python -m publisher --resource-fetch-manifest <path>``.

    字段互斥 (``__post_init__`` 校验):
      - mode='local':  download_dir 必填 + oss 必 None
      - mode='oss':    oss 必填 + download_dir 必 None
      - mode='both':   download_dir + oss 都必填 (D3 决策, 新增合法 mode)
      - cookie_file / account_id: 可选 batch-level (video 类登录态用, image 类忽略).
    """

    batch_id: str
    mode: str
    items: List[ResourceFetchItem]
    download_dir: Optional[Path] = None  # mode=local/both 必填
    oss: Optional[OssConfig] = None  # mode=oss/both 必填
    cookie_file: Optional[Path] = None  # 可选 (video auth)
    account_id: Optional[str] = None  # 可选 (video auth)

    def __post_init__(self) -> None:
        if not self.batch_id or not self.batch_id.strip():
            raise ManifestValidationError("batch_id must be a non-empty string")
        if self.mode not in VALID_MODES:
            raise ManifestValidationError(
                f"mode must be one of {sorted(VALID_MODES)}, got {self.mode!r}"
            )
        if self.mode == "local":
            if self.download_dir is None:
                raise ManifestValidationError(
                    "download_dir is required when mode='local'"
                )
            if self.oss is not None:
                raise ManifestValidationError("oss must be null when mode='local'")
        elif self.mode == "oss":
            if self.oss is None:
                raise ManifestValidationError("oss is required when mode='oss'")
            if self.download_dir is not None:
                raise ManifestValidationError(
                    "download_dir must be null when mode='oss'"
                )
        else:  # both
            if self.download_dir is None:
                raise ManifestValidationError(
                    "download_dir is required when mode='both'"
                )
            if self.oss is None:
                raise ManifestValidationError("oss is required when mode='both'")
        if not self.items:
            raise ManifestValidationError("items must be a non-empty list")


# ---------------------------------------------------------------------------
# helpers (校验 + 解析)
# ---------------------------------------------------------------------------


def _validate_http_url(url: Any, field_name: str) -> None:
    """校验 URL 必须 http(s) — 拒 ftp / file / data: / javascript:.

    错误信息分三档 (沿用 image_fetcher 风格, 便于上层精准提示):
      - 非 string → "{field_name} must be a string, got {type}"
      - 空 string → "{field_name} must be non-empty"
      - 非 http(s) scheme → "{field_name} must start with http:// or https://, got {value}"
    """
    if not isinstance(url, str):
        raise ManifestValidationError(
            f"{field_name} must be a string, got {type(url).__name__}"
        )
    if not url.strip():
        raise ManifestValidationError(f"{field_name} must be non-empty")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ManifestValidationError(
            f"{field_name} must start with http:// or https://, got {url!r}"
        )


def _parse_optional_path(raw: Any, field_name: str) -> Optional[Path]:
    """校验 + 解析可选路径字段: None / 缺省 → None, 非空字符串 → Path."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestValidationError(
            f"{field_name} must be a non-empty string or null"
        )
    return Path(raw.strip())


def _parse_oss(raw: Any) -> OssConfig:
    """校验 + 解析 oss 字段块 — 委托给 video_fetcher._parse_oss (零复制原则)."""
    # 避免循环 import: 走模块级 import
    from multimedia_parsing.video_fetcher.manifest import _parse_oss as _vf_parse_oss  # noqa: PLC0415

    return _vf_parse_oss(raw)


def _parse_resource_fetch_item(raw: Any, index: int) -> ResourceFetchItem:
    """校验 + 解析单条 ResourceFetchItem."""
    if not isinstance(raw, dict):
        raise ManifestValidationError(
            f"items[{index}] must be a JSON object, got {type(raw).__name__}"
        )
    # 必填字段
    required = ["url", "source_url", "resource_type", "item_id", "platform"]
    for field_name in required:
        if raw.get(field_name) is None:
            raise ManifestValidationError(
                f"items[{index}].{field_name} is required"
            )
    # url / source_url 必须是 http(s)
    _validate_http_url(raw["url"], f"items[{index}].url")
    _validate_http_url(raw["source_url"], f"items[{index}].source_url")
    # resource_type 必须在白名单
    if raw["resource_type"] not in VALID_RESOURCE_TYPES:
        raise ManifestValidationError(
            f"items[{index}].resource_type must be one of {sorted(VALID_RESOURCE_TYPES)}, "
            f"got {raw['resource_type']!r}"
        )
    # item_id / platform / title 字符串非空
    for s_field in ("item_id", "platform", "title", "thumbnail"):
        v = raw.get(s_field, "")
        if not isinstance(v, str):
            raise ManifestValidationError(
                f"items[{index}].{s_field} must be a string, got {type(v).__name__}"
            )
    # meta 可选 dict
    meta = raw.get("meta", {})
    if not isinstance(meta, dict):
        raise ManifestValidationError(
            f"items[{index}].meta must be a JSON object, got {type(meta).__name__}"
        )

    return ResourceFetchItem(
        url=raw["url"],
        source_url=raw["source_url"],
        resource_type=raw["resource_type"],
        item_id=raw["item_id"],
        platform=raw["platform"],
        title=raw.get("title", ""),
        thumbnail=raw.get("thumbnail", ""),
        meta=meta,
    )


def _parse_items(raw: Any) -> List[ResourceFetchItem]:
    """校验 items 字段: 必须是 dict list, 逐条解析为 ResourceFetchItem."""
    if raw is None:
        raise ManifestValidationError("items is required (missing field)")
    if not isinstance(raw, list):
        raise ManifestValidationError(
            f"items must be a JSON array, got {type(raw).__name__}"
        )
    if not raw:
        raise ManifestValidationError("items must be a non-empty array")
    return [_parse_resource_fetch_item(item, i) for i, item in enumerate(raw)]


def _parse_urls(raw: Any) -> List[str]:
    """校验 urls 字段: 必须是 http(s) string list (沿用 image_fetcher 风格)."""
    if raw is None:
        raise ManifestValidationError("urls is required (missing field)")
    if not isinstance(raw, list):
        raise ManifestValidationError(
            f"urls must be a JSON array, got {type(raw).__name__}"
        )
    if not raw:
        raise ManifestValidationError("urls must be a non-empty array")
    out: List[str] = []
    for i, item in enumerate(raw):
        _validate_http_url(item, f"urls[{i}]")
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 解析 ResourceParseManifest
# ---------------------------------------------------------------------------


def parse_resource_parse_manifest_dict(raw: Dict[str, Any]) -> ResourceParseManifest:
    """从 dict 解析 + 校验 ResourceParseManifest (不走 JSON 文件 I/O, 方便单测)."""
    if not isinstance(raw, dict):
        raise ManifestValidationError(
            f"manifest must be a JSON object, got {type(raw).__name__}"
        )
    batch_id = raw.get("batch_id")
    if batch_id is None:
        raise ManifestValidationError("batch_id is required (missing field)")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ManifestValidationError("batch_id must be a non-empty string")

    urls = _parse_urls(raw.get("urls"))

    return ResourceParseManifest(
        batch_id=batch_id.strip(),
        urls=urls,
    )


def load_resource_parse_manifest(path: Path) -> ResourceParseManifest:
    """从 JSON 文件加载 + 解析 ResourceParseManifest.

    Raises:
        ManifestValidationError: 缺字段 / 类型错 / 边界不通过.
        FileNotFoundError: 文件不存在.
        json.JSONDecodeError: JSON 不合法.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return parse_resource_parse_manifest_dict(raw)


# ---------------------------------------------------------------------------
# 解析 ResourceFetchManifest
# ---------------------------------------------------------------------------


def parse_resource_fetch_manifest_dict(raw: Dict[str, Any]) -> ResourceFetchManifest:
    """从 dict 解析 + 校验 ResourceFetchManifest (不走 JSON 文件 I/O, 方便单测)."""
    if not isinstance(raw, dict):
        raise ManifestValidationError(
            f"manifest must be a JSON object, got {type(raw).__name__}"
        )
    batch_id = raw.get("batch_id")
    if batch_id is None:
        raise ManifestValidationError("batch_id is required (missing field)")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ManifestValidationError("batch_id must be a non-empty string")

    mode = raw.get("mode")
    if mode is None:
        raise ManifestValidationError("mode is required (missing field)")
    if not isinstance(mode, str) or mode not in VALID_MODES:
        raise ManifestValidationError(
            f"mode must be one of {sorted(VALID_MODES)}, got {mode!r}"
        )

    items = _parse_items(raw.get("items"))

    download_dir = _parse_optional_path(raw.get("download_dir"), "download_dir")
    oss = _parse_oss(raw.get("oss")) if raw.get("oss") is not None else None
    cookie_file = _parse_optional_path(raw.get("cookie_file"), "cookie_file")
    account_id_raw = raw.get("account_id")
    if account_id_raw is not None and not isinstance(account_id_raw, str):
        raise ManifestValidationError(
            f"account_id must be a string or null, got {type(account_id_raw).__name__}"
        )
    account_id = (
        account_id_raw.strip() if isinstance(account_id_raw, str) and account_id_raw.strip() else None
    )

    return ResourceFetchManifest(
        batch_id=batch_id.strip(),
        mode=mode,
        items=items,
        download_dir=download_dir,
        oss=oss,
        cookie_file=cookie_file,
        account_id=account_id,
    )


def load_resource_fetch_manifest(path: Path) -> ResourceFetchManifest:
    """从 JSON 文件加载 + 解析 ResourceFetchManifest.

    Raises:
        ManifestValidationError: 缺字段 / 类型错 / 边界不通过.
        FileNotFoundError: 文件不存在.
        json.JSONDecodeError: JSON 不合法.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return parse_resource_fetch_manifest_dict(raw)


__all__ = [
    "ManifestValidationError",  # re-export (跨包复用)
    "OssConfig",  # re-export
    "VALID_MODES",
    "VALID_RESOURCE_TYPES",
    "ResourceFetchItem",
    "ResourceParseManifest",
    "ResourceFetchManifest",
    "parse_resource_parse_manifest_dict",
    "parse_resource_fetch_manifest_dict",
    "load_resource_parse_manifest",
    "load_resource_fetch_manifest",
]

