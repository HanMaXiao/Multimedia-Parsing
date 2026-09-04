"""publisher.oss — 通用 OSS 上传模块 (Phase 1 Commit 3 上提).

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §5.2 + F0.5.

F0.5: OssUploader 从 publisher/video_fetcher/oss_uploader.py 上提到 publisher/oss/, 供
video / image / 未来 audio / model 共用. 旧 video_fetcher/oss_uploader.py 留 deprecation
shim (re-export + 警告), Phase 6 旧模块退役时一并删.

公共 API (跟旧 video_fetcher/oss_uploader.py 一致):
  - OssUploader: boto3 S3 兼容上传器
  - OssResult: 上传结果视图 (key / url / bucket / size_bytes)
  - OssUploadError: 上传失败异常
  - OssConfigError: 配置非法异常
  - build_key / build_url: 公开 helper
"""
from __future__ import annotations

from .oss_uploader import (
    OssConfigError,
    OssResult,
    OssUploadError,
    OssUploader,
    build_key,
    build_url,
)

__all__ = [
    "OssConfigError",
    "OssResult",
    "OssUploadError",
    "OssUploader",
    "build_key",
    "build_url",
]

