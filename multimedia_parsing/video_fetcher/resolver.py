"""video_fetcher.resolver — yt-dlp 元数据提取。

权威定义:spec §4.1 resolver 职责 — yt-dlp ``extract_info(download=False)`` 拿
title / duration / 视频_id / 画质列表,返回 ``ResolvedVideo`` dataclass。

设计要点:
  - 顶层 ``try: import yt_dlp except ImportError: yt_dlp = None`` 兜底 — 测试环境无
    yt-dlp 也能 import;运行时由 conftest 注入 fake 或 release 环境装真包。
  - ``resolve(url)`` 走 ``yt_dlp.YoutubeDL(quiet=True).extract_info(url, download=False)``。
  - platform 解析:从 URL 域名识别,跟 ``dispatch.py`` 已有的 17 个平台定义解耦 —
    我们只关心 yt-dlp 能否解析,D4 spec §4.1 列了 bilibili / youtube / x /
    xiaohongshu / kuaishou(理论支持),本模块不强校验,解析失败归 ``ResolverError``。

被拒(2026-09-02 D1 决策):
  - 自研各站 extractor — 成本不可接受,1000+ 站点。
  - spawn yt-dlp CLI 子进程 — stdout 行格式不稳定。
  - cobalt / pybalt — 服务端集中式,IP 封锁严重,引入 Node 依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# yt-dlp 顶层 lazy import — 测试环境无包仍可 import;运行时缺包显式 raise。
try:
    import yt_dlp  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 真实环境必有
    yt_dlp = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class ResolverError(RuntimeError):
    """yt-dlp 元数据提取失败(URL 不支持 / 网络 / 解析异常)。"""


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedVideo:
    """yt-dlp 解析后视图 — 仅保留跑批 + 表格展示需要的字段。

    字段:
      - ``url``: 原始 URL(回传给 downloader / 上传阶段做 article 标识)。
      - ``platform``: 从 URL 域名推导的站点标识(``bilibili`` / ``youtube`` / ``x`` 等),
        spec §5 用作 emit 事件的 ``platform`` 字段。
      - ``video_id``: yt-dlp 解析得到的视频 id(``BV1YM4m1z7nB`` / ``dQw4w9WgXcQ``)。
        spec §5 规定 ``article = "{platform}_{video_id}"``。
      - ``title``: 视频标题,日志 / 日后 UI 展示用。
      - ``duration``: 时长(秒),可选 — 一些平台 yt-dlp 拿不到(直播回放 / 限速)。
      - ``extractor``: yt-dlp extractor 名(``"youtube"`` / ``"BiliBili"``),调试用。
    """

    url: str
    platform: str
    video_id: str
    title: str
    duration: Optional[float]
    extractor: str


# ---------------------------------------------------------------------------
# platform 解析(从 URL 域名)
# ---------------------------------------------------------------------------

# 域名 → platform id 映射。spec §4.1 D1 描述的"理论支持"站点清单。
# 新增站点走这里加;yt-dlp 实际能不能抓是另一回事,本模块不强校验(解析失败归 ResolverError)。
_DOMAIN_TO_PLATFORM: Dict[str, str] = {
    "bilibili.com": "bilibili",
    "b23.tv": "bilibili",  # B 站短链
    "youtube.com": "youtube",
    "youtu.be": "youtube",  # YouTube 短链
    "x.com": "x",  # X(Twitter)主域
    "twitter.com": "x",  # 兼容老链接
    "xiaohongshu.com": "xiaohongshu",
    "xhslink.com": "xiaohongshu",  # 小红书短链
    "kuaishou.com": "kuaishou",
    "v.kuaishou.com": "kuaishou",  # 快手短链
}


def detect_platform(url: str) -> str:
    """从 URL 域名推导 platform id。无法识别 → ``"unknown"``(不抛错,留给 yt-dlp 兜底)。

    Examples
    --------
    >>> detect_platform("https://www.bilibili.com/video/BV1xx")
    'bilibili'
    >>> detect_platform("https://youtu.be/dQw4w9WgXcQ")
    'youtube'
    >>> detect_platform("https://example.com/foo")
    'unknown'
    """
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, TypeError):
        return "unknown"
    if not host:
        return "unknown"
    # 逐级匹配:先匹配完整 host,再剥前导子域(``www.`` / ``m.``)。
    if host in _DOMAIN_TO_PLATFORM:
        return _DOMAIN_TO_PLATFORM[host]
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            bare = host[len(prefix):]
            if bare in _DOMAIN_TO_PLATFORM:
                return _DOMAIN_TO_PLATFORM[bare]
    # 兜底:匹配后缀(``*.bilibili.com`` → ``bilibili``)。
    for domain, platform in _DOMAIN_TO_PLATFORM.items():
        if host.endswith("." + domain):
            return platform
    return "unknown"


# ---------------------------------------------------------------------------
# 主体:resolve
# ---------------------------------------------------------------------------


def _build_ydl_opts(cookie_file: Optional[str]) -> Dict[str, Any]:
    """yt-dlp ``YoutubeDL`` 入参。quiet=True 抑制 stderr 噪音(进度由 downloader 的 hook 接管)。"""
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,  # resolver 阶段绝不下文件
        "noplaylist": True,  # 不展开播放列表(用户粘贴单视频)
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts


def _require_yt_dlp():
    """运行时 yt-dlp 不可用 → 显式 ImportError(用户/Phase 7 装包时一眼看到)。"""
    if yt_dlp is None:
        raise ImportError(
            "yt-dlp is not installed. Install with: pip install yt-dlp"
        )


def resolve(url: str, *, cookie_file: Optional[str] = None) -> ResolvedVideo:
    """调用 yt-dlp 提取元数据。

    Parameters
    ----------
    url:
        视频页面 URL(支持 spec §4.1 列的 bilibili / youtube / x / xiaohongshu /
        kuaishou,及其他 yt-dlp 1000+ 站点)。
    cookie_file:
        Netscape cookies.txt 路径(``storage_state_adapter`` 产出);``None`` = 无登录。

    Returns
    -------
    ResolvedVideo:
        解析后的不可变视图。

    Raises
    ------
    ImportError:
        yt-dlp 未安装(只会在生产路径触发;测试环境用 mock 注入 fake module)。
    ResolverError:
        yt-dlp 抛异常 / 返回非 dict / 缺关键字段 ``id`` / ``title`` / ``extractor``。
    """
    _require_yt_dlp()
    # yt-dlp 类型注解不完整(MyPy ignore)
    YDL = yt_dlp.YoutubeDL  # type: ignore[attr-defined]
    opts = _build_ydl_opts(cookie_file)
    try:
        with YDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        # yt-dlp 自定义 DownloadError 等都包成 ResolverError,Phase 3 编排层好 catch
        raise ResolverError(f"yt-dlp extract_info failed for {url!r}: {e}") from e

    if not isinstance(info, dict):
        raise ResolverError(
            f"yt-dlp returned non-dict info for {url!r}: {type(info).__name__}"
        )

    video_id = info.get("id")
    title = info.get("title")
    extractor = info.get("extractor")
    duration = info.get("duration")
    if not isinstance(video_id, str) or not video_id:
        raise ResolverError(
            f"yt-dlp info missing 'id' for {url!r}: {info!r}"
        )
    if not isinstance(title, str) or not title:
        raise ResolverError(
            f"yt-dlp info missing 'title' for {url!r}: {info!r}"
        )
    if not isinstance(extractor, str) or not extractor:
        raise ResolverError(
            f"yt-dlp info missing 'extractor' for {url!r}: {info!r}"
        )
    if duration is not None and not isinstance(duration, (int, float)):
        # duration 是 None 合法(直播回放 / 限速),但类型错 → 视为 None
        duration = None

    return ResolvedVideo(
        url=url,
        platform=detect_platform(url),
        video_id=video_id,
        title=title,
        duration=float(duration) if duration is not None else None,
        extractor=extractor,
    )


__all__ = [
    "DOMAIN_TO_PLATFORM",
    "ResolvedVideo",
    "ResolverError",
    "detect_platform",
    "resolve",
]


# 把内部 _DOMAIN_TO_PLATFORM 用作 PUBLIC_ALIAS(给 Phase 2 run_fetch 做平台过滤对照)
DOMAIN_TO_PLATFORM = _DOMAIN_TO_PLATFORM

