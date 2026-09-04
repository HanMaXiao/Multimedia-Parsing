"""resolvers.video — 视频资源解析器 (Phase 1 Commit 2 真包装).

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.

设计要点:
  - 包装现有 video_fetcher.resolver.resolve + video_fetcher.downloader.download +
    OssUploader (Commit 3 移到 publisher.oss/, 本期用现有路径).
  - 模块级 import 别名 _resolve_video / _download_video / _OssUploader —
    测试用 monkeypatch.setattr 替换, 不依赖 pytest fixtures.
  - 协议翻译 ResolvedVideo ↔ ResourceItem (item_id = "{platform}_{video_id}".
  - both 模式 OSS 失败降级 (spec §9): 本地已成功 → 整体 success, oss_error 字段记录.

拒绝(2026-09-03 D1 + F0.2 决策):
  - 重新实现 yt-dlp 链路 → 重复且易回归.
  - asyncio.gather 并发解析/下载 → spec §4 串行更稳, 单用户场景复杂度无收益.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

# 模块级 import 别名 — 测试用 monkeypatch 替换即可, 不需要 DI 容器.
from multimedia_parsing.video_fetcher.downloader import download as _download_video
from multimedia_parsing.oss import OssUploader as _OssUploader  # F0.5 2026-09-03 上提
from multimedia_parsing.video_fetcher.resolver import resolve as _resolve_video

from ..base import (
    FetchDestination,
    FetchResult,
    ResourceItem,
)


class VideoResolver:
    """视频资源解析器 — 包装 video_fetcher (yt-dlp 链路).

    Parameters
    ----------
    cookie_file:
        Netscape cookies.txt 路径 (storage_state_adapter 产出). 透传给 _resolve_video +
        _download_video. None = 无登录态 (≤480p 降级).
    account_id:
        用户账号 id, 记录用. 跟 video_fetcher manifest 字段对齐 (本期未直接消费).
    """

    def __init__(
        self,
        *,
        cookie_file: Optional[Path] = None,
        account_id: Optional[str] = None,
    ) -> None:
        self.cookie_file = cookie_file
        self.account_id = account_id

    def parse(self, url: str) -> List[ResourceItem]:
        """单 URL 解析 — 调 _resolve_video (yt-dlp), 翻译 ResolvedVideo → ResourceItem.

        解析失败 (ResolverError) → 返空 list. run_parse 层 (Commit 3) 发 failed 事件.
        spec §4 + F0.1: 1 URL 解析可能产 1 条 (单视频) 或 0 条 (不支持 / 失败).
        """
        try:
            rv = _resolve_video(
                url,
                cookie_file=str(self.cookie_file) if self.cookie_file else None,
            )
        except Exception:
            # ResolverError / 其他 — 透传给 run_parse 层做 failed 事件
            return []

        return [
            ResourceItem(
                item_id=f"{rv.platform}_{rv.video_id}",
                resource_type="video",
                platform=rv.platform,
                source_url=url,
                title=rv.title or "",
                meta={
                    "video_id": rv.video_id,
                    "duration": rv.duration,
                    "extractor": rv.extractor,
                },
            )
        ]

    def fetch(self, item: ResourceItem, dest: FetchDestination) -> FetchResult:
        """单条目下载 — 调 _download_video + (可选) _OssUploader.upload.

        mode 行为 (spec §9 + D3 决策):
          - local: 下载到 dest.download_dir, success 返 local_path.
          - oss:   下载到 dest.download_dir (临时) + 上传, success 返 oss_url (+ local_path).
          - both:  下载到 dest.download_dir + 上传, success 返 oss_url + local_path.
                    OSS 失败时, 降级: 仍 success (本地已有) + oss_error 字段记录.
                    oss-only 模式 OSS 失败 = 整体 failed (无本地兜底).
        """
        if item.resource_type != "video":
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error=f"VideoResolver cannot fetch resource_type={item.resource_type!r}",
            )

        video_id = item.meta.get("video_id", "")
        if not video_id:
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error="missing video_id in item.meta",
            )

        # 1) 下载
        out_dir = dest.download_dir or Path("./.resource_fetcher_tmp")
        try:
            dv = _download_video(
                item.source_url,
                out_dir,
                platform=item.platform,
                video_id=video_id,
                cookie_file=str(self.cookie_file) if self.cookie_file else None,
            )
        except Exception as e:
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error=f"download: {e}",
            )

        local_path = dv.file_path
        # 2) local 模式 — 直接 success
        if dest.mode == "local":
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                local_path=local_path,
            )

        # 3) oss / both 模式 — 需要 uploader
        if dest.oss is None:
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                local_path=local_path,
                error="oss config missing in destination",
            )

        # 4) 上传 — both 模式失败降级, oss-only 模式失败 = 整体失败
        try:
            oss = _OssUploader(dest.oss).upload(
                local_path,
                platform=item.platform,
                video_id=video_id,
                ext=dv.ext,
            )
            oss_url = oss.url
            oss_error: Optional[str] = None
        except Exception as e:
            oss_url = None
            oss_error = f"upload: {e}"
            if dest.mode == "oss":
                # oss-only: 无本地兜底, 整体失败
                return FetchResult(
                    item_id=item.item_id,
                    resource_type=item.resource_type,
                    platform=item.platform,
                    local_path=local_path,
                    error=oss_error,
                )
            # both 模式: 本地已有, 降级 success + oss_error 字段

        return FetchResult(
            item_id=item.item_id,
            resource_type=item.resource_type,
            platform=item.platform,
            local_path=local_path,
            oss_url=oss_url,
            oss_error=oss_error,
        )


__all__ = ["VideoResolver"]

