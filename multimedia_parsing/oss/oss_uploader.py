"""oss.oss_uploader — boto3 S3 兼容上传 + 直链推导.

权威定义: spec §4.1 oss_uploader 职责 + §6 OSS 对象与直链规则.
F0.5: 2026-09-03 从 video_fetcher/oss_uploader.py 上提到 publisher/oss/, 供 resource_fetcher
统一调用. 旧位置 publisher/video_fetcher/oss_uploader.py 留 deprecation shim.

设计要点:
  - boto3 顶层 lazy import — 跟 resolver / downloader 同一套 ImportError 兜底,
    测试环境无 boto3 也能 import,运行时缺包显式 raise(``_require_boto3``)。
  - boto3 client 构造 = single-shot,不缓存(每次 OssUploader 实例化新建)— boto3
    client 本身 stateless,无需 cache;client 由 caller 持有/复用,避免隐藏的
    长生命周期 resource leak。
  - 私有 bucket 直链 404 属用户配置责任,``OssUploader.test_connection()`` 提示
    "需开公共读或配置自定义域名";签名 URL 延后(spec §10)。
  - key 规则:``{path_prefix}/{yyyy-MM-dd}/{platform}_{video_id}.{ext}``(spec §6)。
  - 直链推导:``public_base_url`` 已配置 → ``{public_base_url}/{key}``;
    未配置 → ``https://{bucket}.{endpoint_host}/{key}``(virtual-host style,S3 公开直链惯例)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse

# boto3 顶层 lazy import — 测试无包能 import,runtime 缺包显式 raise。
try:
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config as BotoConfig  # type: ignore[import-untyped]
    from botocore.exceptions import (  # type: ignore[import-untyped]
        BotoCoreError,
        ClientError,
    )
except ImportError:  # pragma: no cover - 真实环境必有
    boto3 = None  # type: ignore[assignment]
    BotoConfig = None  # type: ignore[assignment,misc]
    BotoCoreError = Exception  # type: ignore[assignment,misc]
    ClientError = Exception  # type: ignore[assignment,misc]

# OssConfig 仍从 video_fetcher.manifest 拿 (跨包 dataclass, 唯一来源 — F2.1 零复制原则延伸)
from multimedia_parsing.video_fetcher.manifest import OssConfig


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class OssUploadError(RuntimeError):
    """OSS 上传失败(网络 / 凭据错 / bucket 不存在 / 权限不足)。"""


class OssConfigError(ValueError):
    """OSS 配置非法(endpoint 不可达 / bucket 私有直链 404 提示)— 校验阶段抛。"""


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OssResult:
    """上传结果视图。"""

    key: str
    url: str
    bucket: str
    size_bytes: int


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_boto3() -> None:
    if boto3 is None:
        raise ImportError(
            "boto3 is not installed. Install with: pip install boto3"
        )


def _strip_scheme(url: str) -> str:
    """``https://oss-cn-hangzhou.aliyuncs.com`` → ``oss-cn-hangzhou.aliyuncs.com``;
    ``minio.local:9000/api`` → ``minio.local:9000/api``(保留 port + path)。
    """
    p = urlparse(url if "://" in url else "http://" + url)
    host = p.netloc or (p.hostname or "")
    path = p.path or ""
    return (host + path).lstrip("/") or url


def _build_key(
    path_prefix: str,
    platform: str,
    video_id: str,
    ext: str,
    when: Optional[datetime] = None,
) -> str:
    """构造对象 key:``{path_prefix}/{yyyy-MM-dd}/{platform}_{video_id}.{ext}``(spec §6)。"""
    if not ext:
        ext = "mp4"
    ext = ext.lstrip(".")
    when = when or datetime.now(timezone.utc)
    date_part = when.strftime("%Y-%m-%d")
    prefix = (path_prefix or "").strip("/")
    parts: List[str] = []
    if prefix:
        parts.append(prefix)
    parts.append(date_part)
    parts.append(f"{platform}_{video_id}.{ext}")
    return "/".join(parts)


def _public_url(
    bucket: str, endpoint: str, key: str, public_base_url: Optional[str] = None
) -> str:
    """直链推导(spec §6):
      - ``public_base_url`` 已配置 → ``{public_base_url}/{key}``(CDN/自定义域名)
      - 未配置 → ``https://{bucket}.{endpoint_host}/{key}``(virtual-host style)
    """
    if public_base_url:
        base = public_base_url.rstrip("/")
        return f"{base}/{key}"
    host = _strip_scheme(endpoint)
    return f"https://{bucket}.{host}/{key}"


# ---------------------------------------------------------------------------
# 主体:OssUploader
# ---------------------------------------------------------------------------


class OssUploader:
    """boto3 S3 兼容上传器 — 单 instance 对应一份 OssConfig。"""

    def __init__(self, config: OssConfig) -> None:
        self.config = config
        self._client: Any = None  # 懒构造 — 第一次 upload / test_connection 才建

    # ----- lazy boto3 client -----

    def _get_client(self) -> Any:
        _require_boto3()
        if self._client is None:
            # 阿里云 OSS 等不支持 path-style (SecondLevelDomainForbidden),必须
            # virtual-hosted style;checksum 参数 = botocore 1.36+ 默认行为回退
            # (旧版本 botocore 的 Config 接受未知 kwargs 并忽略,向下兼容)。
            self._client = boto3.client(  # type: ignore[union-attr]
                "s3",
                endpoint_url=self._normalize_endpoint(self.config.endpoint),
                aws_access_key_id=self.config.access_key_id,
                aws_secret_access_key=self.config.secret_access_key,
                region_name=self.config.region,
                config=BotoConfig(
                    s3={"addressing_style": "virtual"},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        return self._client

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        """boto3 endpoint_url 必须是带 scheme 的 URL;用户可能写 ``oss-cn-hangzhou.aliyuncs.com``。"""
        if "://" not in endpoint:
            return f"https://{endpoint}"
        return endpoint

    # ----- 公开 API -----

    def build_key(self, platform: str, video_id: str, ext: str) -> str:
        """公开 build_key helper — Phase 3 run_fetch 在 upload 前调一下拿 key,便于测试。"""
        return _build_key(
            self.config.path_prefix, platform, video_id, ext
        )

    def build_url(self, key: str) -> str:
        """公开 build_url — 给 caller(测试 / 上层编排)在 upload 之前先看直链长啥样。"""
        return _public_url(
            self.config.bucket,
            self.config.endpoint,
            key,
            self.config.public_base_url,
        )

    def upload(
        self,
        local_path: Path,
        platform: str,
        video_id: str,
        ext: str,
    ) -> OssResult:
        """上传本地文件,返回 OssResult(key + url + bucket + size)。"""
        p = Path(local_path)
        if not p.exists():
            raise FileNotFoundError(f"local file not found: {p}")
        size = p.stat().st_size
        key = self.build_key(platform, video_id, ext)
        return self.upload_with_key(p, key, size=size)

    def upload_with_key(
        self,
        local_path: Path,
        key: str,
        *,
        size: Optional[int] = None,
    ) -> OssResult:
        """用调用方指定的 key 上传(图片模块专用,spec F2.1 零复制扩展)。

        视频模块的 key 规则是 ``{prefix}/{date}/{platform}_{video_id}.{ext}``(写死在
        ``build_key``);图片模块的 key 规则是 ``{prefix}/{batch_id}/{序号}_{文件名}``
        (spec §3 D3) — shape 不同,无法复用 ``build_key``。caller 在自己侧拼好 key 传进来,
        boto3 client / 直链推导 / 错误处理完全复用,零复制原则(F2.1)的延伸。
        """
        p = Path(local_path)
        if not p.exists():
            raise FileNotFoundError(f"local file not found: {p}")
        actual_size = p.stat().st_size
        client = self._get_client()
        try:
            client.upload_file(str(p), self.config.bucket, key)
        except (BotoCoreError, ClientError) as e:  # type: ignore[misc]
            raise OssUploadError(
                f"S3 upload failed (bucket={self.config.bucket!r}, key={key!r}): {e}"
            ) from e
        return OssResult(
            key=key,
            url=self.build_url(key),
            bucket=self.config.bucket,
            size_bytes=size if size is not None else actual_size,
        )

    def test_connection(self) -> Tuple[bool, str]:
        """轻量探测:列 bucket 中前 1 个对象(或空),验证 AK/SK + endpoint 配对可用。"""
        client = self._get_client()
        try:
            resp = client.list_objects_v2(
                Bucket=self.config.bucket, MaxKeys=1
            )
            count = resp.get("KeyCount", 0)
            if self.config.public_base_url is None:
                hint = (
                    f" 提示:未配置 public_base_url,默认走 virtual-host 直链 "
                    f"({self.build_url('<example-key>')});若 bucket 私有需开公共读或配置 CDN/自定义域名。"
                )
            else:
                hint = f" 已配置 public_base_url={self.config.public_base_url}"
            return True, f"ok (bucket reachable, {count} object(s) listed){hint}"
        except (BotoCoreError, ClientError) as e:  # type: ignore[misc]
            return False, f"OSS connection test failed: {e}"


# 模块级 alias(测试 import + Phase 3 run_fetch 用)
def build_key(
    path_prefix: str, platform: str, video_id: str, ext: str
) -> str:
    return _build_key(path_prefix, platform, video_id, ext)


def build_url(
    bucket: str, endpoint: str, key: str, public_base_url: Optional[str] = None
) -> str:
    return _public_url(bucket, endpoint, key, public_base_url)


__all__ = [
    "OssConfigError",
    "OssResult",
    "OssUploadError",
    "OssUploader",
    "build_key",
    "build_url",
]

