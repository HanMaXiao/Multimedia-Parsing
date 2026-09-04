"""resolvers.image — 图片资源解析器 (Phase 1 Commit 2 真包装).

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.

设计要点:
  - 包装现有 image_fetcher.resolver.parse_url + image_fetcher.downloader.download_images +
    OssUploader.upload_with_key.
  - 模块级 import 别名 _parse_image_url / _download_images / _OssUploader — 测试用
    monkeypatch.setattr 替换, 不需要 DI 容器.
  - 协议翻译 ResolvedImage → ResourceItem: 每张图一个 ResourceItem, item_id = uuid4 短串
    (image URL 无稳定平台内 id).
  - 图片 fetch 走 _download_images 批量接口 (单图也走批量, 内部串行), downloader 自身
    处理取消 / 失败 / 文件名冲突.
  - both 模式 OSS 失败降级 (spec §9): 跟 video 同款.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import List, Optional

# 模块级 import 别名 — 测试用 monkeypatch 替换.
from multimedia_parsing.image_fetcher.downloader import download_images as _download_images
from multimedia_parsing.image_fetcher.manifest import ImageEntry
from multimedia_parsing.image_fetcher.resolver import parse_url as _parse_image_url
from multimedia_parsing.oss import OssUploader as _OssUploader  # F0.5 2026-09-03 上提

from ..base import (
    FetchDestination,
    FetchResult,
    ResourceItem,
)


def _build_image_entry(
    image_url: str,
    source_url: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> ImageEntry:
    """构造 image_fetcher.ImageEntry dataclass (downloader 期望的输入类型)."""
    return ImageEntry(
        url=image_url,
        source_url=source_url,
        width=width,
        height=height,
    )


class ImageResolver:
    """图片资源解析器 — 包装 image_fetcher (gallery-dl + BS4 链路).

    Parameters
    ----------
    无 batch-level 参数 (图片类不需要 cookie_file / account_id).
    """

    def parse(self, url: str) -> List[ResourceItem]:
        """单 URL 解析 — 调 _parse_image_url (gallery-dl + BS4), 翻译 ResolvedImage → ResourceItem.

        解析失败 (ResolverError) → 返空 list. 页面无图 → 返空 list. run_parse 层发相应事件.
        spec §4: 1 URL 解析可能产 0..N 条 (N = 页面图片数, 过滤 + 去重后).
        """
        try:
            resolved = _parse_image_url(url)
        except Exception:
            return []
        if not resolved:
            return []

        items: List[ResourceItem] = []
        for img in resolved:
            image_url = getattr(img, "url", None)
            if not isinstance(image_url, str) or not image_url:
                continue
            meta = {"image_url": image_url}
            width = getattr(img, "width", None)
            height = getattr(img, "height", None)
            if width is not None:
                meta["width"] = width
            if height is not None:
                meta["height"] = height
            items.append(
                ResourceItem(
                    item_id=uuid.uuid4().hex[:12],
                    resource_type="image",
                    platform=self._platform_from_url(url),
                    source_url=url,
                    meta=meta,
                )
            )
        return items

    def fetch(self, item: ResourceItem, dest: FetchDestination) -> FetchResult:
        """单张图下载 — 调 _download_images (单图 batch) + (可选) OssUploader.upload_with_key.

        mode 行为: 跟 VideoResolver 对齐 (spec §9 + D3 决策).
        """
        if item.resource_type != "image":
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error=f"ImageResolver cannot fetch resource_type={item.resource_type!r}",
            )

        image_url = item.meta.get("image_url", "")
        if not image_url:
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error="missing image_url in item.meta",
            )

        # 1) 下载 — 用单图 batch 调 _download_images
        out_dir = dest.download_dir or Path("./.resource_fetcher_tmp")
        # image_fetcher 用 ImageEntry (含 url / source_url / width / height)
        entry = _build_image_entry(
            image_url=image_url,
            source_url=item.source_url,
            width=item.meta.get("width"),
            height=item.meta.get("height"),
        )
        try:
            results = _download_images([entry], out_dir)
        except Exception as e:
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error=f"download: {e}",
            )

        if not results:
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error="download returned no results",
            )

        dl = results[0]
        if getattr(dl, "error", None):
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error=f"download: {dl.error}",
            )

        local_path = dl.file_path
        # 2) local 模式 — 直接 success
        if dest.mode == "local":
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                local_path=local_path,
            )

        # 3) oss / both 模式
        if dest.oss is None:
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                local_path=local_path,
                error="oss config missing in destination",
            )

        # OSS key 构造: {prefix}/{batch_id}/{item_id}_{filename}
        # 跟 image_fetcher.run_fetch._build_image_oss_key 一致 (F2.1 零复制)
        # 这里简化: key 不带 batch_id 上下文 (Commit 3 run_fetch 层补)
        try:
            oss = _OssUploader(dest.oss).upload_with_key(
                local_path,
                key=f"{item.item_id}_{Path(image_url).name or 'image'}",
            )
            oss_url = oss.url
            oss_error = None
        except Exception as e:
            oss_url = None
            oss_error = f"upload: {e}"
            if dest.mode == "oss":
                return FetchResult(
                    item_id=item.item_id,
                    resource_type=item.resource_type,
                    platform=item.platform,
                    local_path=local_path,
                    error=oss_error,
                )

        return FetchResult(
            item_id=item.item_id,
            resource_type=item.resource_type,
            platform=item.platform,
            local_path=local_path,
            oss_url=oss_url,
            oss_error=oss_error,
        )

    @staticmethod
    def _platform_from_url(url: str) -> str:
        """从 URL 拿平台标识. 简化版 — 真实场景 router 已经按域分好.

        本期 Resolver 在 router 调用时已经知道 platform, 这里用 urlparse 兜底.
        """
        from urllib.parse import urlparse

        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return "unknown"
        for prefix in ("www.", "m.", "mobile."):
            if host.startswith(prefix):
                host = host[len(prefix):]
                break
        # 简化: 取主域第一段
        return host.split(".")[0] if host else "unknown"


__all__ = ["ImageResolver"]

