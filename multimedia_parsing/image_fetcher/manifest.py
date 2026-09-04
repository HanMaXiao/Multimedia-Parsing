"""image_fetcher.manifest — manifest JSON 解析 + 边界校验。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §5。
Rust 侧 ``commands/image_fetch.rs::start_image_parse / start_image_fetch`` 写盘,
Python sidecar ``python -m publisher parse-images/fetch-images --manifest <path>`` 读盘。

设计要点:
  - 两种 manifest:``ImageParseManifest``(解析阶段,urls) + ``ImageFetchManifest``
    (下载/上传阶段,images + mode + 配置)。CLI 两个子命令对应两种 manifest 类型(spec §4.2)。
  - ``OssConfig`` / ``ManifestValidationError`` 跨包复用:从 ``video_fetcher.manifest``
    导入(spec F2.1 零复制原则的延伸),不在 image_fetcher 重复定义 5 字段 S3 兼容配置
    + 异常类,保证 video_fetcher.test_connection / OssUploader 抛出的异常可被
    image_fetcher.run_fetch 用同一 ``except`` 分支捕获。
  - 边界校验在反序列化层一次性兜底,与 video_fetcher 同款错误信息风格(分"非 string"
    / "空 string" / "非 http(s) scheme" 三档错误消息,便于上层精准提示)。
  - 路径字段全部解析成 ``pathlib.Path`` 避免下游 str/Path 混用。
  - 校验失败抛 ``ManifestValidationError``(``ValueError`` 子类),Phase 2 ``run_fetch``
    catch 一次性报给上层(走 ``image_fetch_status(..., status="failed", error=...)``)。

不接受:
  - YAML / TOML:跟项目 data_settings.json / storage_settings.json 统一用 JSON。
  - pydantic:项目无 pydantic 依赖(stdlib 优先),dataclass + 手写校验足够。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from multimedia_parsing.video_fetcher.manifest import (
    ManifestValidationError,  # 跨包复用同一个异常类(见 module docstring 零复制原则)
    OssConfig,
)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageEntry:
    """单条图片记录(在 ``ImageFetchManifest.images`` 列表中)。

    字段:
      - ``url``:解析出的图片直链(必填,http(s))。
      - ``source_url``:来源页面 URL(用户回查用,必填,http(s))。
      - ``width`` / ``height``:图片像素尺寸(可选,部分站点拿不到)。
    """

    url: str
    source_url: str
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass(frozen=True)
class ImageParseManifest:
    """解析阶段 manifest — 1 batch_id + N 个源 URL。

    CLI 入口:``python -m publisher parse-images --manifest <path>``。
    """

    batch_id: str
    urls: List[str]

    def __post_init__(self) -> None:
        if not self.batch_id or not self.batch_id.strip():
            raise ManifestValidationError("batch_id must be a non-empty string")
        for i, url in enumerate(self.urls):
            _validate_http_url(url, f"urls[{i}]")


@dataclass(frozen=True)
class ImageFetchManifest:
    """下载/上传阶段 manifest — 1 batch_id + mode + (download_dir or oss) + N 个图片。

    CLI 入口:``python -m publisher fetch-images --manifest <path>``。

    字段互斥(``__post_init__`` 校验):
      - ``mode='local'``:``download_dir`` 必填 + ``oss`` 必 None
      - ``mode='oss'``:``oss`` 必填 + ``download_dir`` 必 None
    """

    batch_id: str
    mode: str
    images: List[ImageEntry]
    download_dir: Optional[Path] = None  # mode=local 必填
    oss: Optional[OssConfig] = None  # mode=oss 必填(从 video_fetcher 导入)

    def __post_init__(self) -> None:
        if not self.batch_id or not self.batch_id.strip():
            raise ManifestValidationError("batch_id must be a non-empty string")
        if self.mode not in VALID_MODES:
            raise ManifestValidationError(
                f"mode must be one of {sorted(VALID_MODES)}, got {self.mode!r}"
            )
        # mode 互斥校验
        if self.mode == "local":
            if self.download_dir is None:
                raise ManifestValidationError(
                    "download_dir is required when mode='local'"
                )
            if self.oss is not None:
                raise ManifestValidationError(
                    "oss must be null when mode='local'"
                )
        elif self.mode == "oss":
            if self.oss is None:
                raise ManifestValidationError("oss is required when mode='oss'")
            if self.download_dir is not None:
                raise ManifestValidationError(
                    "download_dir must be null when mode='oss'"
                )
        # images 必填非空(ImageEntry 校验在 _parse_image_entries 内完成)
        if not self.images:
            raise ManifestValidationError("images must be a non-empty list")


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


VALID_MODES: frozenset[str] = frozenset({"local", "oss"})


# ---------------------------------------------------------------------------
# helpers(校验 + 解析)
# ---------------------------------------------------------------------------


def _validate_http_url(url: Any, field_name: str) -> None:
    """校验 URL 必须 http(s) — 拒 ftp / file / data: / javascript:。

    错误信息分三档(便于上层精准提示):
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
    """校验 + 解析可选路径字段:None / 缺省 → None,非空字符串 → Path。"""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestValidationError(
            f"{field_name} must be a non-empty string or null"
        )
    return Path(raw.strip())


def _parse_oss(raw: Any) -> OssConfig:
    """校验 + 解析 oss 字段块。失败抛 ``ManifestValidationError``。

    与 video_fetcher._parse_oss 行为一致(白名单 provider / 必填字段),这里直接调
    video_fetcher 的实现(零复制原则的延伸 — OssConfig 单一来源)。
    """
    # 避免循环 import:走模块级 import 而非顶层 from,这样测试 mock 也好处理
    from multimedia_parsing.video_fetcher.manifest import _parse_oss as _vf_parse_oss  # noqa: PLC0415

    return _vf_parse_oss(raw)


def _parse_image_entry(raw: Any, index: int) -> ImageEntry:
    """校验 + 解析单条 ImageEntry。"""
    if not isinstance(raw, dict):
        raise ManifestValidationError(
            f"images[{index}] must be a JSON object, got {type(raw).__name__}"
        )
    url = raw.get("url")
    source_url = raw.get("source_url")
    if url is None:
        raise ManifestValidationError(f"images[{index}].url is required")
    if source_url is None:
        raise ManifestValidationError(f"images[{index}].source_url is required")

    # URL 字段必须 http(s) — 与 urls 解析共用 _validate_http_url 防御 data: / ftp: / file: 等
    _validate_http_url(url, f"images[{index}].url")
    _validate_http_url(source_url, f"images[{index}].source_url")

    width = _parse_optional_positive_int(raw.get("width"), f"images[{index}].width")
    height = _parse_optional_positive_int(
        raw.get("height"), f"images[{index}].height"
    )

    return ImageEntry(
        url=url,
        source_url=source_url,
        width=width,
        height=height,
    )


def _parse_optional_positive_int(raw: Any, field_name: str) -> Optional[int]:
    """校验可选正整数:None / 缺省 → None,否则必须为正 int。"""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        # bool 是 int 的子类,显式排除
        raise ManifestValidationError(
            f"{field_name} must be a positive integer or null, got {type(raw).__name__}"
        )
    if raw <= 0:
        raise ManifestValidationError(
            f"{field_name} must be a positive integer, got {raw}"
        )
    return raw


def _parse_images(raw: Any) -> List[ImageEntry]:
    """校验 images 字段:必须是 dict list,逐条解析为 ImageEntry。"""
    if raw is None:
        raise ManifestValidationError("images is required (missing field)")
    if not isinstance(raw, list):
        raise ManifestValidationError(
            f"images must be a JSON array, got {type(raw).__name__}"
        )
    if not raw:
        raise ManifestValidationError("images must be a non-empty array")
    return [_parse_image_entry(item, i) for i, item in enumerate(raw)]


# ---------------------------------------------------------------------------
# 解析 ImageParseManifest
# ---------------------------------------------------------------------------


def _parse_urls(raw: Any) -> List[str]:
    """校验 urls 字段:必须是 http(s) string list。"""
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


def parse_parse_manifest_dict(raw: Dict[str, Any]) -> ImageParseManifest:
    """从 dict 解析 + 校验 ImageParseManifest(不走 JSON 文件 I/O,方便单测)。"""
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

    return ImageParseManifest(
        batch_id=batch_id.strip(),
        urls=urls,
    )


def load_parse_manifest(path: Path) -> ImageParseManifest:
    """从 JSON 文件加载 + 解析 ImageParseManifest。

    Raises:
        ManifestValidationError: 缺字段 / 类型错 / 边界不通过。
        FileNotFoundError: 文件不存在。
        json.JSONDecodeError: JSON 不合法。
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return parse_parse_manifest_dict(raw)


# ---------------------------------------------------------------------------
# 解析 ImageFetchManifest
# ---------------------------------------------------------------------------


def parse_fetch_manifest_dict(raw: Dict[str, Any]) -> ImageFetchManifest:
    """从 dict 解析 + 校验 ImageFetchManifest(不走 JSON 文件 I/O,方便单测)。"""
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

    images = _parse_images(raw.get("images"))

    # mode 互斥的字段解析:先都读,__post_init__ 校验互斥
    download_dir = _parse_optional_path(raw.get("download_dir"), "download_dir")
    oss = _parse_oss(raw.get("oss")) if raw.get("oss") is not None else None

    return ImageFetchManifest(
        batch_id=batch_id.strip(),
        mode=mode,
        images=images,
        download_dir=download_dir,
        oss=oss,
    )


def load_fetch_manifest(path: Path) -> ImageFetchManifest:
    """从 JSON 文件加载 + 解析 ImageFetchManifest。

    Raises:
        ManifestValidationError: 缺字段 / 类型错 / 边界不通过。
        FileNotFoundError: 文件不存在。
        json.JSONDecodeError: JSON 不合法。
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return parse_fetch_manifest_dict(raw)


__all__ = [
    "ImageEntry",
    "ImageFetchManifest",
    "ImageParseManifest",
    "ManifestValidationError",
    "VALID_MODES",
    "load_fetch_manifest",
    "load_parse_manifest",
    "parse_fetch_manifest_dict",
    "parse_parse_manifest_dict",
]

