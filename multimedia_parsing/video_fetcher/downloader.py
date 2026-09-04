"""video_fetcher.downloader — yt-dlp 视频下载 + progress hook。

权威定义:spec §4.1 downloader 职责 — yt-dlp ``YoutubeDL`` 配置 outtmpl / format
(``bv*+ba/b`` 优先 mp4) / ffmpeg 位置(``imageio_ffmpeg.get_ffmpeg_exe()``);
progress hook → 事件;返回本地文件路径。

设计要点:
  - format 选择:``bv*+ba/b`` 优先取最佳 mp4 视频流 + 最佳音频流,合并走 ffmpeg;
    退化路径(平台只给单流)→ ``b`` 直接下 best 单文件。
  - ffmpeg 来源:``imageio_ffmpeg`` pip 包自带静态二进制,``get_ffmpeg_exe()`` 拿
    路径,免用户装。spec D5 决策。
  - outtmpl:``{download_dir}/{platform}_{video_id}.%(ext)s`` —— 跟 spec §6 key 规则
    ``{path_prefix}/{yyyy-MM-dd}/{platform}_{video_id}.{ext}`` 一致前半段(后段 OSS
    上传时再加日期 + path_prefix,见 Phase 2 oss_uploader)。
  - progress hook:接受 ``Callable[[dict], None]`` —— 实际跑批时由 ``run_fetch`` 注入
    一个把 yt-dlp 进度 dict 转成 ``emit_platform_status(..., status='uploading',
    progress=N)`` 的 adapter(spec §5 加性扩展字段 ``progress``)。
  - lazy import yt-dlp / imageio-ffmpeg — 跟 resolver 同一套 ImportError 兜底。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# yt-dlp 顶层 lazy import — 跟 resolver 同一理由。
try:
    import yt_dlp  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    yt_dlp = None  # type: ignore[assignment]

# imageio-ffmpeg 顶层 lazy import — 没装时 ffmpeg 走 None,yt-dlp 会自己用内置
# 处理单流视频(无合并);但 B 站 / YouTube 双流场景必装。
try:
    import imageio_ffmpeg  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    imageio_ffmpeg = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class DownloaderError(RuntimeError):
    """yt-dlp 下载失败(网络 / 登录失效 / 格式不可用 / 磁盘)。"""


class FFmpegUnavailableError(RuntimeError):
    """需要 ffmpeg 合并但 ``imageio_ffmpeg`` 未装 / ``ffmpeg_location`` 无效。

    spec D5 决策:必装 ``imageio_ffmpeg``(pip 包),用户零安装体验;
    没装时本错误应 fail-fast,让用户 / 部署阶段立即看到,而不是默默下残缺文件。
    """


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DownloadedVideo:
    """yt-dlp 下载完成的本地文件视图。"""

    file_path: Path
    platform: str
    video_id: str
    title: str
    ext: str
    duration: Optional[float]


# progress hook 入参:yt-dlp 标准 dict 形如
#   {"status": "downloading" | "finished" | "error",
#    "downloaded_bytes": int, "total_bytes": int, "_percent_str": str, ...}
# 我们只读 ``_percent_str`` 提取 0-100 数字(或 ``total_bytes`` 推算)。
ProgressCallback = Callable[[Dict[str, Any]], None]


# ---------------------------------------------------------------------------
# 主体:download
# ---------------------------------------------------------------------------


def _require_yt_dlp():
    if yt_dlp is None:
        raise ImportError(
            "yt-dlp is not installed. Install with: pip install yt-dlp"
        )


def _resolve_ffmpeg_location() -> Optional[str]:
    """拿 ffmpeg 路径(``imageio_ffmpeg`` pip 包自带)。

    Returns:
        ffmpeg 可执行文件绝对路径;``imageio_ffmpeg`` 没装 → 返 None(yt-dlp 单流 fallback)。

    Raises:
        FFmpegUnavailableError: 必装 imageio_ffmpeg 才能合并双流,但 spec D5 决策
        要求零安装,这里**不强制** raise —— 留 None 让 yt-dlp 走单流 fallback。
        B 站 / YouTube 双流场景下用户没装会下到 .mkv / .webm(未合并)单流,这是
        用户责任;spec D5 明确 imageio_ffmpeg 应随项目装。
    """
    if imageio_ffmpeg is None:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # imageio_ffmpeg 罕见异常(权限 / 解压失败)
        return None


def _parse_percent_from_hook(d: Dict[str, Any]) -> Optional[float]:
    """从 yt-dlp progress hook dict 提取 0-100 浮点百分比。

    优先级:
      1. ``_percent_str`` 字段(yt-dlp 标准字段,形如 ``" 45.3%"``)
      2. ``downloaded_bytes`` + ``total_bytes`` 推算
      3. ``None``
    """
    pct_str = d.get("_percent_str")
    if isinstance(pct_str, str):
        cleaned = pct_str.strip().rstrip("%").strip()
        try:
            v = float(cleaned)
            if 0.0 <= v <= 100.0:
                return v
        except (ValueError, TypeError):
            pass
    downloaded = d.get("downloaded_bytes")
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    if isinstance(downloaded, (int, float)) and isinstance(total, (int, float)) and total > 0:
        ratio = downloaded / total
        return max(0.0, min(100.0, ratio * 100.0))
    return None


def _build_ydl_opts(
    outtmpl: str,
    cookie_file: Optional[str],
    progress_cb: Optional[ProgressCallback],
) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # format: 优先最佳视频 + 最佳音频(触发 ffmpeg 合并),退化到单 best 文件
        "format": "bv*+ba/b",
        "outtmpl": outtmpl,
        # 不让 yt-dlp 自动生成 .part 等中间文件名污染目录
        "nopart": False,  # 默认 False = 允许 .part(yt-dlp 内部原子 rename 走它)
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    ffmpeg_loc = _resolve_ffmpeg_location()
    if ffmpeg_loc:
        opts["ffmpeg_location"] = ffmpeg_loc
    if progress_cb is not None:
        # yt-dlp 接受 list of hooks;我们只接 downloading / finished 状态
        def _hook(d: Dict[str, Any]) -> None:
            status = d.get("status")
            if status in ("downloading", "finished"):
                progress_cb(d)

        opts["progress_hooks"] = [_hook]
    return opts


def _extract_downloaded_file(
    outtmpl: str, info: Dict[str, Any], download_dir: Path
) -> Path:
    """从 yt-dlp return 信息推算下载完成的本地文件路径。

    yt-dlp ``download`` 返 ``(info_dict, returned_code)``(Python API 模式),或
    返 ``info_dict``(有 ``requested_downloads`` 字段包含每个 file 信息)。

    优先级:
      1. ``requested_downloads[0].get("filepath")`` —— yt-dlp 2024+ 标准输出
      2. ``_filename`` —— 旧版 yt-dlp
      3. 手动拼 outtmpl(去掉 ``.%(ext)s`` → 用 ``info["ext"]`` 替换)
    """
    # 优先级 1:requested_downloads
    req = info.get("requested_downloads")
    if isinstance(req, list) and req:
        first = req[0]
        if isinstance(first, dict):
            fp = first.get("filepath")
            if isinstance(fp, str) and fp:
                return Path(fp)
    # 优先级 2:_filename
    fn = info.get("_filename")
    if isinstance(fn, str) and fn:
        return Path(fn)
    # 优先级 3:手动拼
    ext = info.get("ext") or "mp4"
    # outtmpl 形如 "{download_dir}/{platform}_{video_id}.%(ext)s"
    base = outtmpl.split(".%(ext)s")[0] if ".%(ext)s" in outtmpl else outtmpl
    return Path(base + "." + ext)


def download(
    url: str,
    out_dir: Path,
    *,
    platform: str,
    video_id: str,
    cookie_file: Optional[str] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> DownloadedVideo:
    """用 yt-dlp 下载单条视频到 ``out_dir``。

    Parameters
    ----------
    url:
        视频页面 URL(与 resolver 同一份)。
    out_dir:
        yt-dlp 输出目录(本模块 caller 一般是 ``runs/{batch_id}/downloads/``)。
    platform, video_id:
        用于构造 outtmpl —— ``{platform}_{video_id}.%(ext)s``。
    cookie_file:
        Netscape cookies.txt 路径(``storage_state_adapter`` 产出);``None`` = 无登录。
    progress_cb:
        yt-dlp progress hook 回调,接收完整 dict;caller(``run_fetch``)负责转
        ``emit_platform_status(..., status='uploading', progress=N)``。

    Returns
    -------
    DownloadedVideo:
        下载完成的本地文件视图(file_path / ext / title / duration)。

    Raises
    ------
    ImportError:
        yt-dlp 未装。
    DownloaderError:
        yt-dlp 抛异常 / 返回非 dict / 缺关键字段。
    """
    _require_yt_dlp()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    outtmpl = str(out_dir / f"{platform}_{video_id}.%(ext)s")
    opts = _build_ydl_opts(outtmpl, cookie_file, progress_cb)

    YDL = yt_dlp.YoutubeDL  # type: ignore[attr-defined]
    try:
        # 兼容新旧 yt-dlp:可能返 tuple,可能返 dict
        result = YDL(opts).download([url])
    except Exception as e:
        raise DownloaderError(f"yt-dlp download failed for {url!r}: {e}") from e

    # result 形如 (info_dict, return_code) 或 info_dict
    info: Any
    if isinstance(result, tuple) and len(result) >= 1:
        info = result[0]
    else:
        info = result

    if not isinstance(info, dict):
        # 兜底:再调一次 extract_info 拿元数据(只读 metadata,不下文件)
        try:
            YDL(_build_ydl_opts(outtmpl, cookie_file, None)).close()
            with YDL({**_build_ydl_opts(outtmpl, cookie_file, None), "skip_download": True}) as ydl_meta:
                info = ydl_meta.extract_info(url, download=False)
        except Exception:
            raise DownloaderError(
                f"yt-dlp returned non-dict info for {url!r}: {type(info).__name__}"
            )

    file_path = _extract_downloaded_file(outtmpl, info, out_dir)
    if not file_path.exists():
        raise DownloaderError(
            f"yt-dlp reported download but file not found: {file_path}"
        )

    ext = info.get("ext") or file_path.suffix.lstrip(".") or "mp4"
    title = info.get("title") or f"{platform}_{video_id}"
    duration = info.get("duration")
    if duration is not None and not isinstance(duration, (int, float)):
        duration = None

    return DownloadedVideo(
        file_path=file_path,
        platform=platform,
        video_id=video_id,
        title=title,
        ext=ext,
        duration=float(duration) if duration is not None else None,
    )


def parse_progress_percent(hook_data: Dict[str, Any]) -> Optional[float]:
    """公开 helper:从 yt-dlp progress hook dict 提取 0-100 浮点百分比。

    Phase 2 ``run_fetch`` 用这个把 hook 数据转成 ``emit_platform_status(..., progress=N)``。
    提前 public 出来便于单测。
    """
    return _parse_percent_from_hook(hook_data)


__all__ = [
    "DownloadedVideo",
    "DownloaderError",
    "FFmpegUnavailableError",
    "ProgressCallback",
    "download",
    "parse_progress_percent",
]

