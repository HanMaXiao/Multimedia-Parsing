"""video_fetcher.manifest — manifest JSON 解析 + 边界校验。

权威定义:spec §4.1 描述,Rust ``commands/video_fetch.rs::start_video_fetch`` 写盘,
Python sidecar ``python -m publisher fetch --manifest <path>`` 读盘。

设计要点:
  - 边界校验在反序列化层一次性兜底:URL 非空 / OSS 字段齐全 / 必填目录存在或可建。
  - 路径字段全部解析成 ``pathlib.Path``(不存字符串)避免下游 str/Path 混用。
  - 校验失败抛 ``ManifestValidationError``(``ValueError`` 子类),Phase 2 ``run_fetch`` 入口
    catch 一次性报给上层(走 ``emit_platform_status(..., status="failed", error=...)``)。

不接受:
  - YAML / TOML:跟项目 data_settings.json / storage_settings.json 统一用 JSON。
  - pydantic:项目无 pydantic 依赖(stdlib 优先),dataclass + 手写校验足够。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class ManifestValidationError(ValueError):
    """manifest 解析 / 校验失败。

    继承 ``ValueError`` 方便上层 ``except ValueError`` 一并处理。
    错误信息面向开发者(英文,简短),Phase 2 编排层 catch 后做中文/用户友好包装。
    """


# ---------------------------------------------------------------------------
# Provider 白名单
# ---------------------------------------------------------------------------

# 跟 D3 决策一致:阿里云 OSS / 腾讯 COS / R2 / MinIO 一套 S3 协议(boto3)。
# 新增 provider 走枚举扩展,不允许前端传入未识别字符串。
VALID_PROVIDERS: frozenset[str] = frozenset(
    {"aliyun_oss", "tencent_cos", "cloudflare_r2", "minio", "custom"}
)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OssConfig:
    """OSS 上传配置 — boto3 client 入参 + 直链推导所需字段。"""

    provider: str
    endpoint: str
    region: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    path_prefix: str = ""
    public_base_url: Optional[str] = None

    def __post_init__(self) -> None:
        if self.provider not in VALID_PROVIDERS:
            raise ManifestValidationError(
                f"oss.provider must be one of {sorted(VALID_PROVIDERS)}, "
                f"got {self.provider!r}"
            )
        if not self.endpoint.strip():
            raise ManifestValidationError("oss.endpoint must be non-empty")
        if not self.bucket.strip():
            raise ManifestValidationError("oss.bucket must be non-empty")
        if not self.access_key_id.strip():
            raise ManifestValidationError("oss.access_key_id must be non-empty")
        if not self.secret_access_key.strip():
            raise ManifestValidationError("oss.secret_access_key must be non-empty")


@dataclass(frozen=True)
class VideoFetchManifest:
    """解析 + 校验后的 manifest 不可变视图。

    字段:
      - ``batch_id``: 批次唯一 id,作为 ``runs/{batch_id}/`` 目录名(由 Rust 侧生成)。
      - ``urls``: 用户批量粘贴的视频链接列表,逐条 resolve → download → (upload?)。
      - ``oss``: 上传目标配置(见 ``OssConfig``)。**F5:Optional** —
        ``None`` 表示本地下载模式(Plan follow-up 5/6,用户没配 OSS 时跑批):
        Python 端跳过 upload,success 状态用 `file://` 本地路径。
      - ``download_dir``: yt-dlp ``outtmpl`` 根目录,Rust 侧填 ``runs/{batch_id}/downloads/``。
      - ``cookie_file``: storage_state_adapter 输出的 Netscape cookies.txt 临时文件路径;
        ``None`` 表示无登录态(≤480p 降级)。
      - ``account_id``: 用户账号 id,决定 ``storage_state`` 来源路径(``None`` 跳过登录态)。
      - ``platform_filter``: 限定哪些平台纳入处理;空 list = 不过滤(默认)。
    """

    batch_id: str
    urls: List[str]
    oss: Optional[OssConfig] = None  # F5:None = 本地下载模式
    download_dir: Path = field(default_factory=Path)
    cookie_file: Optional[Path] = None
    account_id: Optional[str] = None
    platform_filter: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.batch_id.strip():
            raise ManifestValidationError("batch_id must be non-empty")
        if not self.urls:
            raise ManifestValidationError("urls must be a non-empty list")
        for i, url in enumerate(self.urls):
            if not isinstance(url, str) or not url.strip():
                raise ManifestValidationError(
                    f"urls[{i}] must be a non-empty string, got {url!r}"
                )
        if not isinstance(self.download_dir, Path):
            # frozen dataclass + 类型注解,理论上构造时就该是 Path;此处防御
            raise ManifestValidationError(
                f"download_dir must be a Path, got {type(self.download_dir).__name__}"
            )


# ---------------------------------------------------------------------------
# 校验 / 解析
# ---------------------------------------------------------------------------


_REQUIRED_OSS_KEYS = {
    "provider",
    "endpoint",
    "region",
    "bucket",
    "access_key_id",
    "secret_access_key",
}


def _parse_oss(raw: Any) -> OssConfig:
    """校验 + 解析 oss 字段块。失败抛 ``ManifestValidationError``。

    区分"缺字段(None)"和"给了但类型错"两种失败 — 给用户的错误信息更精准。
    """
    if raw is None:
        raise ManifestValidationError("oss is required (missing field)")
    if not isinstance(raw, dict):
        raise ManifestValidationError(
            f"oss must be a JSON object, got {type(raw).__name__}"
        )
    missing = _REQUIRED_OSS_KEYS - set(raw.keys())
    if missing:
        raise ManifestValidationError(
            f"oss missing required fields: {sorted(missing)}"
        )
    return OssConfig(
        provider=raw["provider"],
        endpoint=raw["endpoint"],
        region=raw["region"],
        bucket=raw["bucket"],
        access_key_id=raw["access_key_id"],
        secret_access_key=raw["secret_access_key"],
        path_prefix=raw.get("path_prefix", "") or "",
        public_base_url=raw.get("public_base_url"),
    )


def _parse_urls(raw: Any) -> List[str]:
    """校验 urls 字段:必须是字符串 list,可去重但保序。

    区分"缺字段(None)"和"给了但类型错"两种失败 — 给用户的错误信息更精准。
    """
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
        if not isinstance(item, str):
            raise ManifestValidationError(
                f"urls[{i}] must be a string, got {type(item).__name__}"
            )
        if not item.strip():
            raise ManifestValidationError(f"urls[{i}] must be non-empty")
        out.append(item)
    return out


def _parse_path(raw: Any, field_name: str) -> Path:
    """校验 + 解析路径字段:非空字符串 → Path。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestValidationError(f"{field_name} must be a non-empty string")
    return Path(raw.strip())


def _parse_optional_path(raw: Any, field_name: str) -> Optional[Path]:
    """校验 + 解析可选路径字段:None / 缺省 → None,非空字符串 → Path。"""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestValidationError(
            f"{field_name} must be a non-empty string or null"
        )
    return Path(raw.strip())


def _parse_optional_str(raw: Any, field_name: str) -> Optional[str]:
    """校验可选字符串字段:None / 缺省 → None,否则 strip 后非空。"""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ManifestValidationError(
            f"{field_name} must be a string or null, got {type(raw).__name__}"
        )
    s = raw.strip()
    return s or None


def _parse_platform_filter(raw: Any) -> List[str]:
    """校验 platform_filter:list[str],空 list / 缺省 = 不过滤。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestValidationError(
            f"platform_filter must be a JSON array, got {type(raw).__name__}"
        )
    out: List[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ManifestValidationError(
                f"platform_filter[{i}] must be a non-empty string"
            )
        out.append(item.strip())
    return out


def parse_manifest_dict(raw: Dict[str, Any]) -> VideoFetchManifest:
    """从 dict 解析 + 校验 manifest(不走 JSON 文件 I/O,方便单测)。

    Raises:
        ManifestValidationError: 缺字段 / 类型错 / 边界不通过。
    """
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
    # F5:oss 变 optional — None / 缺省 = 本地下载模式
    oss_raw = raw.get("oss")
    oss = _parse_oss(oss_raw) if oss_raw is not None else None

    download_dir_raw = raw.get("download_dir")
    if download_dir_raw is None:
        raise ManifestValidationError("download_dir is required")
    download_dir = _parse_path(download_dir_raw, "download_dir")

    cookie_file = _parse_optional_path(raw.get("cookie_file"), "cookie_file")
    account_id = _parse_optional_str(raw.get("account_id"), "account_id")
    platform_filter = _parse_platform_filter(raw.get("platform_filter"))

    return VideoFetchManifest(
        batch_id=batch_id.strip(),
        urls=urls,
        oss=oss,
        download_dir=download_dir,
        cookie_file=cookie_file,
        account_id=account_id,
        platform_filter=platform_filter,
    )


def load_manifest(path: Path) -> VideoFetchManifest:
    """从 JSON 文件加载 + 解析 manifest。

    Raises:
        ManifestValidationError: 缺字段 / 类型错 / 边界不通过。
        FileNotFoundError: 文件不存在。
        json.JSONDecodeError: JSON 不合法。
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return parse_manifest_dict(raw)


__all__ = [
    "ManifestValidationError",
    "OssConfig",
    "VALID_PROVIDERS",
    "VideoFetchManifest",
    "load_manifest",
    "parse_manifest_dict",
]

