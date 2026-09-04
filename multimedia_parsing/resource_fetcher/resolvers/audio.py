"""resolvers.audio — 音频资源解析器 (Phase 7+ 音频类型真实现).

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.

设计要点 (同事参考):
  - **复用 video_fetcher 链路**: parse() 直接调 ``_resolve_video`` 拿 metadata (yt-dlp
    自动根据 URL extractor 选 audio/video formats, 解析阶段两者一致).
  - **下载用 audio-only opts**: fetch() 自己用 yt-dlp ``format='bestaudio/best'`` +
    ``FFmpegExtractAudio`` postprocessor, 不走 video_fetcher.downloader (那个强制
    ``bv*+ba/b`` 视频流).
  - **OSS 上传复用 _OssUploader**: 跟 video 一样, 产 ext = 'mp3'/'m4a'/'opus'.
  - **平台覆盖**: soundcloud / bilibili 音频 / x 语音 / podcast (yt-dlp 通用).
  - **何时用 audio vs video**: 用户明确要"只要音频" → audio; 默认 → video.

router 注册示例:
    ResolverRule("audio", ("soundcloud.com", "snd.sc"), AudioResolver),
    ResolverRule("audio", ("bilibili.com", "b23.tv"), AudioResolver),  # 可选覆盖
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

# 复用 video_fetcher 已有基础设施 (跟 VideoResolver 同套)
from multimedia_parsing.oss import OssUploader as _OssUploader
from multimedia_parsing.video_fetcher.downloader import DownloaderError
from multimedia_parsing.video_fetcher.resolver import resolve as _resolve_audio_meta
from multimedia_parsing.video_fetcher.resolver import ResolverError

from ..base import FetchDestination, FetchResult, ResourceItem


class AudioResolver:
    """音频资源解析器 — 走 yt-dlp 音频提取 (soundcloud / podcast / B 站 audio 等).

    Parameters
    ----------
    cookie_file:
        Netscape cookies.txt 路径 (storage_state_adapter 产出). 透传给 yt-dlp.
        None = 无登录态 (soundcloud 公开音频可用, 平台登录音频拿不到).
    account_id:
        用户账号 id, 记录用.
    audio_format:
        目标音频格式 (``"mp3"`` / ``"m4a"`` / ``"opus"`` / ``"wav"``). 走 ffmpeg 转码.
        默认 ``"mp3"`` (兼容性最广).
    """

    def __init__(
        self,
        *,
        cookie_file: Optional[Path] = None,
        account_id: Optional[str] = None,
        audio_format: str = "mp3",
    ) -> None:
        self.cookie_file = cookie_file
        self.account_id = account_id
        self.audio_format = audio_format

    def parse(self, url: str) -> List[ResourceItem]:
        """解析音频 URL → 1 条 ResourceItem (audio type).

        复用 ``_resolve_audio_meta`` (= video_fetcher.resolver.resolve), yt-dlp 自动
        选 extractor (Soundcloud / generic audio / B 站 audio track / X voice tweet 等).
        失败 → 返空 list, run_parse 层发 failed 事件.
        """
        try:
            rv = _resolve_audio_meta(
                url,
                cookie_file=str(self.cookie_file) if self.cookie_file else None,
            )
        except (ResolverError, Exception):
            return []

        return [
            ResourceItem(
                item_id=f"{rv.platform}_{rv.video_id}",
                resource_type="audio",
                platform=rv.platform,
                source_url=url,
                title=rv.title or "",
                meta={
                    "audio_id": rv.video_id,
                    "duration": rv.duration,
                    "extractor": rv.extractor,
                    "audio_format": self.audio_format,
                },
            )
        ]

    def fetch(self, item: ResourceItem, dest: FetchDestination) -> FetchResult:
        """单条目下载 — yt-dlp audio-only mode + (可选) OSS 上传.

        实现:
          1. 调 yt-dlp ``format='bestaudio/best'`` + ``FFmpegExtractAudio`` postprocessor
             拿 audio_format 文件 (mp3/m4a/opus).
          2. local 模式 → 直接 success 返 local_path.
          3. oss / both 模式 → 上传 (同 video 上传路径, ext = audio_format).
        """
        if item.resource_type != "audio":
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error=f"AudioResolver cannot fetch resource_type={item.resource_type!r}",
            )

        audio_id = item.meta.get("audio_id", "")
        if not audio_id:
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error="missing audio_id in item.meta",
            )

        out_dir = dest.download_dir or Path("./.resource_fetcher_tmp")
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1) 下载 (audio-only)
        try:
            local_path, ext = self._download_audio(item.source_url, out_dir)
        except DownloaderError as e:
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error=f"download: {e}",
            )
        except Exception as e:  # pragma: no cover - 兜底
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error=f"download: {e}",
            )

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

        # 4) 上传 — both 模式失败降级, oss-only 模式失败 = 整体失败
        try:
            oss = _OssUploader(dest.oss).upload(
                local_path,
                platform=item.platform,
                video_id=audio_id,  # 复用 video_id 字段 (OssUploader 不区分 type)
                ext=ext,
            )
            oss_url = oss.url
            oss_error: Optional[str] = None
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

    def _download_audio(self, url: str, out_dir: Path) -> tuple[Path, str]:
        """yt-dlp audio-only download + ffmpeg 转码. 返 (local_path, ext).

        Raises:
            DownloaderError: yt-dlp 失败 / ffmpeg 不可用.
        """
        try:
            import yt_dlp  # type: ignore[import-untyped]
        except ImportError as exc:
            raise DownloaderError(
                "yt-dlp is not installed. Install with: pip install yt-dlp"
            ) from exc

        outtmpl = str(out_dir / f"%(id)s.%(ext)s")
        ydl_opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": self.audio_format,
                }
            ],
        }
        if self.cookie_file:
            ydl_opts["cookiefile"] = str(self.cookie_file)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise DownloaderError(f"yt-dlp returned None for {url!r}")
            # FFmpegExtractAudio 写入 {id}.{audio_format}, yt-dlp 会改 filepath
            file_id = info.get("id") or Path(url).stem or "audio"
            local_path = out_dir / f"{file_id}.{self.audio_format}"
            if not local_path.exists():
                # 兜底: yt-dlp 可能用了原 ext, scan out_dir
                candidates = sorted(out_dir.glob(f"{file_id}.*"))
                if not candidates:
                    raise DownloaderError(
                        f"audio file not found after download: {local_path}"
                    )
                local_path = candidates[0]
            return local_path, self.audio_format


__all__ = ["AudioResolver"]
