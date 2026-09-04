"""video_fetcher.run_fetch — 逐 URL resolve → download → upload 编排器。

权威定义:spec §4.1 run_fetch 职责 + §5 事件协议语义映射 + §6 错误处理。

设计要点:
  - ``run_fetch(manifest, *, event_cb=None) -> List[CellResult]``:
    - 逐 URL 走 resolve → download → upload 流水线。
    - 单 URL 失败不中断整批(spec §8 显式:与现有发布批次语义一致)。
    - 整批取消通过 ``cancel_event`` threading.Event 触发,每 URL 入口 + 每阶段
      progress hook 检查一次。
  - 事件协议:走 ``event_emitter.emit_platform_status``(spec §5 复用),但通过
    ``event_cb`` 注入,方便测试 capture — 不直接 print,让 Phase 3 publisher CLI
    入口负责把 event_cb 接到 ``make_event_cb``(stdout 双流通道)。
  - 阶段状态映射(spec §5 表格):
    - resolve 入口   → ``logging_in`` (解析元数据中)
    - download 入口  → ``uploading`` (下载中)
    - download 进度  → ``uploading`` + progress 字段(0-100)
    - upload 入口    → ``processing`` (OSS 上传中)
    - success        → 终态 + url
    - failed         → 终态 + error(``resolve:`` / ``download:`` / ``upload:`` 前缀)
  - 不支持域名     → ``skipped``(spec §5: "不支持的域名 — 预告未收录平台")
  - 取消           → 已有 cell 标 ``cancelled``,未启动的 cell 不发事件
  - article 标识   = ``{platform}_{video_id}``(spec §5 末段)

被拒(2026-09-02 D2 决策):
  - 新建独立 store — 拒绝:复用 publishStore + cellsByEntry。
  - 异步 asyncio.gather 并发 — 拒绝:yt-dlp 自身是 sync,各平台登录态独立,
    串行更稳;并发收益不抵复杂度(单用户场景)。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .downloader import DownloaderError, DownloadedVideo, download, parse_progress_percent
from .manifest import VideoFetchManifest
from multimedia_parsing.oss import OssResult, OssUploader, OssUploadError  # F0.5 2026-09-03 上提; 旧 .oss_uploader 留 deprecation shim
from .resolver import ResolvedVideo, ResolverError, resolve as resolve_video


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CellResult:
    """单 URL 跑完的终态视图(与 event payload 字段对齐)。"""

    article: str  # {platform}_{video_id}
    platform: str
    status: str  # 终态:success / failed / skipped / cancelled
    url: Optional[str] = None
    error: Optional[str] = None
    source_url: Optional[str] = None  # 原 URL(spec §5 加性字段)


# event_cb 签名:跟 event_emitter.emit_platform_status 兼容。
# 接受 keyword args(article / platform / status / url / error / screenshot / source_url / progress)。
EventCallback = Callable[..., None]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _article_id(platform: str, video_id: str) -> str:
    """spec §5:cell 行标识 = ``{platform}_{video_id}``。"""
    return f"{platform}_{video_id}"


def _emit(
    event_cb: Optional[EventCallback],
    *,
    article: str,
    platform: str,
    status: str,
    url: Optional[str] = None,
    error: Optional[str] = None,
    source_url: Optional[str] = None,
    progress: Optional[float] = None,
    screenshot: Optional[str] = None,
) -> None:
    """包 emit_platform_status 的 event_cb 调用,event_cb 缺失时静默。"""
    if event_cb is None:
        return
    # spec §5 加性字段:source_url / progress(Rust 透传不解析,前端可选消费)
    kwargs: Dict[str, Any] = {
        "url": url,
        "error": error,
        "screenshot": screenshot,
    }
    if source_url is not None:
        kwargs["source_url"] = source_url
    if progress is not None:
        kwargs["progress"] = progress
    event_cb(article, platform, status, **kwargs)


def _build_progress_cb(
    event_cb: Optional[EventCallback],
    article: str,
    platform: str,
    source_url: str,
) -> Callable[[Dict[str, Any]], None]:
    """yt-dlp progress hook 适配:从 dict 抽 progress,转 emit_platform_status(..., status='uploading', progress=N)。

    progress 事件也带 ``source_url``(spec §5 加性字段)— 跟前后的 uploading/processing
    事件保持一致,前端做事件流归约时不会缺字段。
    """

    def _hook(d: Dict[str, Any]) -> None:
        status = d.get("status")
        if status == "finished":
            _emit(
                event_cb,
                article=article,
                platform=platform,
                status="uploading",
                source_url=source_url,
                progress=100.0,
            )
            return
        if status == "downloading":
            pct = parse_progress_percent(d)
            if pct is not None:
                _emit(
                    event_cb,
                    article=article,
                    platform=platform,
                    status="uploading",
                    source_url=source_url,
                    progress=pct,
                )

    return _hook


# ---------------------------------------------------------------------------
# 单 URL 流水线
# ---------------------------------------------------------------------------


def _fetch_one(
    url: str,
    *,
    manifest: VideoFetchManifest,
    uploader: Optional[OssUploader],
    event_cb: Optional[EventCallback],
    cancel_event: Optional[threading.Event] = None,
) -> CellResult:
    """单 URL 全流水线。失败返 CellResult(status='failed'),不抛(spec §8:不中断整批)。

    F5:``uploader`` 变 Optional — ``None`` = 本地下载模式,跳过 upload 步骤,
    success 状态用 ``file://`` 本地路径。OSS 模式上传后走 oss.url 透出。
    """
    # 1) 解析元数据
    platform: Optional[str] = None
    try:
        if cancel_event is not None and cancel_event.is_set():
            return CellResult(
                article="", platform="", status="cancelled", source_url=url
            )
        _emit(event_cb, article="", platform="", status="logging_in", source_url=url)
        rv: ResolvedVideo = resolve_video(
            url, cookie_file=str(manifest.cookie_file) if manifest.cookie_file else None
        )
        platform = rv.platform
        article = _article_id(platform, rv.video_id)

        # 2) 下载
        if cancel_event is not None and cancel_event.is_set():
            return CellResult(
                article=article, platform=platform, status="cancelled", source_url=url
            )
        _emit(
            event_cb,
            article=article,
            platform=platform,
            status="uploading",
            source_url=url,
        )
        progress_cb = _build_progress_cb(event_cb, article, platform, url)
        dv: DownloadedVideo = download(
            url,
            manifest.download_dir,
            platform=platform,
            video_id=rv.video_id,
            cookie_file=str(manifest.cookie_file) if manifest.cookie_file else None,
            progress_cb=progress_cb,
        )

        # 3) F5 分叉:有 uploader → OSS 模式上传;无 uploader → 本地模式直接 success
        if uploader is None:
            # 本地下载模式 — 不上传,success 状态用 file:// 绝对路径
            local_url = f"file://{dv.file_path.as_posix()}"
            _emit(
                event_cb,
                article=article,
                platform=platform,
                status="success",
                url=local_url,
                source_url=url,
            )
            return CellResult(
                article=article,
                platform=platform,
                status="success",
                url=local_url,
                source_url=url,
            )

        # OSS 模式:上传 → success
        if cancel_event is not None and cancel_event.is_set():
            return CellResult(
                article=article, platform=platform, status="cancelled", source_url=url
            )
        _emit(
            event_cb,
            article=article,
            platform=platform,
            status="processing",
            source_url=url,
        )
        oss: OssResult = uploader.upload(
            dv.file_path, platform=platform, video_id=rv.video_id, ext=dv.ext
        )

        # 4) 成功
        _emit(
            event_cb,
            article=article,
            platform=platform,
            status="success",
            url=oss.url,
            source_url=url,
        )
        return CellResult(
            article=article,
            platform=platform,
            status="success",
            url=oss.url,
            source_url=url,
        )

    except ResolverError as e:
        # 未识别域名 → 走 skipped(spec §5 + spec §8:不支持的域名预告)
        # 但 resolver 自己不抛"unknown domain"异常(它只是 detect_platform 返 "unknown")。
        # yt-dlp 不支持的 URL 会抛 DownloadError / ExtractorError 等,我们包成 ResolverError。
        # 这里走 failed 路径(error 前缀 resolve:),caller 看 status 自己判断。
        article_fallback = _article_id(platform or "unknown", "unknown") if platform else ""
        err = f"resolve: {e}"
        _emit(
            event_cb,
            article=article_fallback,
            platform=platform or "unknown",
            status="failed",
            error=err,
            source_url=url,
        )
        return CellResult(
            article=article_fallback,
            platform=platform or "unknown",
            status="failed",
            error=err,
            source_url=url,
        )

    except DownloaderError as e:
        article_fallback = (
            _article_id(platform or "unknown", "unknown") if platform else ""
        )
        err = f"download: {e}"
        _emit(
            event_cb,
            article=article_fallback,
            platform=platform or "unknown",
            status="failed",
            error=err,
            source_url=url,
        )
        return CellResult(
            article=article_fallback,
            platform=platform or "unknown",
            status="failed",
            error=err,
            source_url=url,
        )

    except OssUploadError as e:
        article_fallback = (
            _article_id(platform or "unknown", "unknown") if platform else ""
        )
        err = f"upload: {e}"
        _emit(
            event_cb,
            article=article_fallback,
            platform=platform or "unknown",
            status="failed",
            error=err,
            source_url=url,
        )
        return CellResult(
            article=article_fallback,
            platform=platform or "unknown",
            status="failed",
            error=err,
            source_url=url,
        )


# ---------------------------------------------------------------------------
# 主体:run_fetch
# ---------------------------------------------------------------------------


_UNSET = object()  # 哨兵:区分"未传"和"显式 None"(F5 本地下载模式)


def run_fetch(
    manifest: VideoFetchManifest,
    *,
    event_cb: Optional[EventCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    uploader: Any = _UNSET,
) -> List[CellResult]:
    """逐 URL 跑 resolve → download → (upload?) 流水线。

    Parameters
    ----------
    manifest:
        已校验的 manifest(``parse_manifest_dict`` 或 ``load_manifest`` 产出)。
    event_cb:
        接受 ``(article, platform, status, **kwargs)`` 的可调用对象。None 时静默
        (测试场景 / 离线预览)。Phase 3 接 ``make_event_cb``(stdout 双流通道)。
    cancel_event:
        ``threading.Event``;set 后当前 URL 收尾 → 返 cancelled,未启动的 URL 跳过。
        None = 不可取消。
    uploader:
        注入 OssUploader 实例(测试用 fake;默认行为按 manifest.oss 决定):
          - 未传(默认) + ``manifest.oss is None`` → 本地下载模式,不构造 OssUploader
          - 未传 + ``manifest.oss`` 有值 → 构造默认 OssUploader(走 OSS 模式)
          - 显式传 None → 强制本地下载模式,无视 manifest.oss(测试 / 离线预览场景)
          - 显式传 OssUploader 实例 → 用它(测试 fake)

    Returns
    -------
    List[CellResult]:
        长度 = len(manifest.urls);顺序与 urls 一致。每条 cell 必带 status 终态
        (success / failed / cancelled) 或 skipped(本期不分支,暂未实现)。
    """
    # F5:本地下载模式(Plan follow-up 5/6)
    # 哨兵 _UNSET 区分"未传"和"显式传 None":
    #   - 未传 + manifest.oss=None → 本地模式(uploader 保持 None)
    #   - 未传 + manifest.oss=有 → 默认 OssUploader
    #   - 显式 None → 强制本地模式(覆盖 manifest.oss)
    if uploader is _UNSET:
        uploader = OssUploader(manifest.oss) if manifest.oss is not None else None

    results: List[CellResult] = []
    for url in manifest.urls:
        # 整批取消检查(URL 启动前)
        if cancel_event is not None and cancel_event.is_set():
            results.append(
                CellResult(
                    article="", platform="", status="cancelled", source_url=url
                )
            )
            continue
        cell = _fetch_one(
            url,
            manifest=manifest,
            uploader=uploader,
            event_cb=event_cb,
            cancel_event=cancel_event,
        )
        results.append(cell)

    return results


__all__ = [
    "CellResult",
    "EventCallback",
    "run_fetch",
]

