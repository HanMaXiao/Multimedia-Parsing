"""image_fetcher.resolver — 两级图片 URL 解析器。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §4.1 + F1.1-F1.3。

设计要点:
  - **两级结构(F1.1)**:``parse_url()`` 先 ``gallery_dl.extractor.find(url)`` —
    命中 extractor 走库内嵌迭代拿图片直链;未命中 / 抛异常 → 回退 requests + BS4 通用解析。
  - **gallery-dl 库内嵌(F1.2)**:顶层 ``try: import gallery_dl except ImportError: gallery_dl = None``
    兜底,测试环境无包仍可 import;runtime 缺包 ``_require_gallery_dl()`` 显式 raise。
  - **通用解析过滤规则(F1.3)**:
      - 跳过 ``data:`` URI(防 base64 内嵌)
      - 跳过 ``.svg``(常用于图标 / 占位)
      - 已知宽高 < 100px 的缩略图变体 → 过滤
      - URL 去 query 参数归一后去重
      - ``srcset`` 取最大宽度 / 密度
      - 懒加载优先:``data-src`` / ``data-original`` 优先于 ``src``
      - ``<picture>/<source srcset>`` 纳入候选
      - ``og:image`` 兜底(无任何 img 时)
  - **网络**:requests + Referer 默认指源 URL(防盗链常见要求)。
  - 解析失败抛 ``ResolverError``(``RuntimeError`` 子类),Phase 2 ``run_fetch`` catch
    后一次性报给上层(走 ``image_fetch_status(..., status="failed", error=...)``)。

被拒(2026-09-02 D1 决策):
  - Playwright 渲染所有页面再解析 — 慢 + 登录态耦合,JS 渲染兜底延后(open Q1)。
  - 自研 extractor — gallery-dl 1000+ 站点覆盖,自研 = 重写整套。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

# 懒加载 — 测试环境无包仍可 import;runtime 缺包 _require_gallery_dl 显式 raise。
try:
    import gallery_dl  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 真实环境应有 publisher extra
    gallery_dl = None  # type: ignore[assignment]

import requests  # 已在项目根 requirements(test/lint)
from bs4 import BeautifulSoup  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from .cache import ImageParseCache


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class ResolverError(RuntimeError):
    """图片 URL 解析失败(gallery-dl 未装 / 网络失败 / 解析异常)。"""


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedImage:
    """单条解析出的图片记录。

    字段:
      - ``url``:图片直链(必填,http(s))。
      - ``width`` / ``height``:像素尺寸(可选,部分站点 / og:image 拿不到)。
    """

    url: str
    width: Optional[int] = None
    height: Optional[int] = None


# ---------------------------------------------------------------------------
# gallery-dl 集成
# ---------------------------------------------------------------------------


def _require_gallery_dl() -> None:
    """runtime gallery-dl 不可用 → 显式 ImportError(spec F1.2 镜像 yt-dlp 模式)。"""
    if gallery_dl is None:
        raise ImportError(
            "gallery-dl is not installed. Install with: pip install gallery-dl"
        )


def _gallery_dl_extract(url: str) -> Optional[List[str]]:
    """调 gallery_dl.extractor.find 拿图片 URL 列表。

    Returns:
        list[str] 命中且迭代出 N 张图;None 命中但 extractor 为空 / 抛异常 / find 返 None。
        上层据此决定 fallback BS4。

    Raises:
        不向上抛异常(内部 try/except 兜底)— 与设计原则"单 URL 失败不中断整批"一致。
    """
    try:
        extractor = gallery_dl.extractor.find(url)  # type: ignore[union-attr]
    except Exception:
        return None
    if extractor is None:
        return None
    out: List[str] = []
    try:
        for item in extractor:
            # gallery-dl 迭代器产出 (url, metadata_dict) 元组
            if isinstance(item, tuple) and len(item) >= 1:
                u = item[0]
                if isinstance(u, str) and u:
                    out.append(u)
            elif isinstance(item, str) and item:
                out.append(item)
    except Exception:
        return None if not out else out
    return out


# ---------------------------------------------------------------------------
# BS4 通用解析
# ---------------------------------------------------------------------------


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 解析缓存(plan followup 4)
# ---------------------------------------------------------------------------
# 模块级单例 — 默认不启用(None = 禁用缓存);CLI / 测试可注入。
# 设计取舍:不放在 parse_url 形参里(避免上层每处都传);用环境变量
# ``IMAGE_FETCHER_CACHE_DIR`` 启用,测试用 monkeypatch 注入。
import os as _os

_PARSE_CACHE: Optional["ImageParseCache"] = None
if _os.environ.get("IMAGE_FETCHER_CACHE_DIR"):
    from .cache import ImageParseCache
    _PARSE_CACHE = ImageParseCache(
        cache_dir=Path(_os.environ["IMAGE_FETCHER_CACHE_DIR"]),
    )


def set_parse_cache(cache: Optional["ImageParseCache"]) -> None:
    """设置/清空模块级缓存单例 — CLI 启用 / 测试 monkeypatch 入口。"""
    global _PARSE_CACHE
    _PARSE_CACHE = cache


# ---------------------------------------------------------------------------
# Playwright 渲染兜底(plan followup Open Q1)
# ---------------------------------------------------------------------------
# 懒加载 + 模块级标志 — 测试环境无 playwright 时静默跳过兜底,production 装好后
# 自动启用。同步 API(sync_playwright)够用 — 异步路径跟 spec 串行流水线冲突,无收益。
try:
    _playwright_module: Any = __import__("playwright.sync_api", fromlist=["sync_playwright"])
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - 真实环境装 playwright 后才有
    _PLAYWRIGHT_AVAILABLE = False
    _playwright_module = None  # type: ignore[assignment]


def _parse_with_playwright(url: str, *, timeout: float) -> List[ResolvedImage]:
    """Playwright 渲染兜底 — 拿执行 JS 后的最终 HTML,再走 BS4 抽取。

    Playwright 未装 → 返空(透明降级,不抛)。goto 超时 / 失败 → 返空(容错)。

    设计:
      - 拿 URL 后做 headless Chromium render
      - page.content() 拿完整 HTML(包含 JS 注入的 img)
      - 复用 _candidate_urls_from_html 抽取(BS4 抽取逻辑已在用)
      - 候选 url 走 _filter_and_dedupe 统一过滤
    """
    if not _PLAYWRIGHT_AVAILABLE or _playwright_module is None:
        return []
    try:
        with _playwright_module.sync_playwright() as pw:  # type: ignore[union-attr]
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                try:
                    page = context.new_page()
                    page.goto(url, timeout=int(timeout * 1000), wait_until="networkidle")
                    html = page.content()
                finally:
                    context.close()
            finally:
                browser.close()
    except Exception:
        return []
    # 复用 BS4 抽取 helper(纯函数,无副作用)
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    candidates = _candidate_urls_from_html(soup)
    return _filter_and_dedupe(
        [ResolvedImage(url=u, width=w, height=h) for (u, w, h) in candidates]
    )


def _fetch_html(url: str, *, timeout: float) -> str:
    """GET URL 拿 HTML 文本。失败抛 ``ResolverError``。"""
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                # 防盗链常见:Referer 设为源域(不是当前 URL 本身)
                "Referer": f"{urlparse(url).scheme}://{urlparse(url).netloc}/",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        raise ResolverError(f"failed to fetch {url!r}: {e}") from e


# srcset 单条候选形如 "url 1x" / "url 320w" / "url,url2 1x"(用逗号分隔多 url? 实际是空格)
# 也支持 srcset 单 URL 无描述符的退化场景(部分 <source> 标签)。
_SRCSET_CANDIDATE_WITH_DESC = re.compile(
    r"\s*"
    r"(?P<url>\S+)"
    r"\s+"
    r"(?P<descriptor>\d+[wx])"
    r"\s*"
    r"(?:,|$|\s)"
)
_SRCSET_CANDIDATE_BARE = re.compile(r"\s*(?P<url>\S+)\s*(?:,|$|\s)")


def _parse_srcset_candidate_size(desc: str) -> float:
    """srcset descriptor 解析:``2x`` → 2.0,``640w`` → 640.0。"""
    if desc.endswith("x"):
        return float(desc[:-1])
    if desc.endswith("w"):
        return float(desc[:-1])
    return 0.0


def _pick_largest_from_srcset(srcset: str) -> Optional[str]:
    """从 srcset 字符串挑出"最大"候选 URL。

    优先级:密度描述符(``1x`` / ``2x``)用密度数值;宽度描述符(``320w`` / ``640w``)用宽度。
    实际网页里 2x 通常意味着同尺寸更高分辨率,所以两者数值直接比大小 OK(1x ≈ 1 单位宽度)。

    也支持无描述符的单 URL 形式(``<source srcset="x.avif">`` 常见),视为 1x 候选。

    Returns:
        选中的 URL;空 srcset / 无法解析 → None。
    """
    if not srcset or not srcset.strip():
        return None
    best_url: Optional[str] = None
    best_size: float = -1.0
    for raw in srcset.split(","):
        s = raw.strip()
        if not s:
            continue
        m = _SRCSET_CANDIDATE_WITH_DESC.match(s)
        if m:
            u = m.group("url")
            d = m.group("descriptor")
            size = _parse_srcset_candidate_size(d)
        else:
            # 退化:无描述符 → 视作 URL 单条(1x 候选,size=1.0)
            m2 = _SRCSET_CANDIDATE_BARE.match(s)
            if not m2:
                continue
            u = m2.group("url")
            size = 1.0
        if size > best_size:
            best_size = size
            best_url = u
    return best_url


def _candidate_url_from_img_tag(tag: Any) -> Optional[str]:
    """从 ``<img>`` 标签挑出最佳 URL:懒加载属性 > srcset 最大 > src。"""
    # 懒加载属性优先(spec F1.3)
    for attr in ("data-src", "data-original", "data-lazy-src"):
        v = tag.get(attr)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # srcset 第二优先
    srcset = tag.get("srcset")
    if isinstance(srcset, str) and srcset.strip():
        picked = _pick_largest_from_srcset(srcset)
        if picked:
            return picked
    # src 兜底
    src = tag.get("src")
    if isinstance(src, str) and src.strip():
        return src.strip()
    return None


def _candidate_url_from_source_tag(tag: Any) -> Optional[str]:
    """从 ``<source>`` 标签(``<picture>`` 内)挑 URL:srcset 最大 → src。"""
    srcset = tag.get("srcset")
    if isinstance(srcset, str) and srcset.strip():
        picked = _pick_largest_from_srcset(srcset)
        if picked:
            return picked
    src = tag.get("src")
    if isinstance(src, str) and src.strip():
        return src.strip()
    return None


def _candidate_urls_from_html(soup: BeautifulSoup) -> List[Tuple[str, Optional[int], Optional[int]]]:
    """从 BS4 解析的 HTML 抽所有候选图片 URL + 可选宽高。

    Returns:
        list of (url, width, height) — width/height 可能为 None(og:image / 无 attrs img)。

    设计:
      - og:image 总是作为候选加入(不只是兜底) — 部分文章页用 og:image 显式封面,
        与正文 <img> 并存时,前端用户能看到两种来源的去重结果。后续 _filter_and_dedupe
        负责去重 + 过滤。
    """
    out: List[Tuple[str, Optional[int], Optional[int]]] = []

    # 1) <img> 标签
    for tag in soup.find_all("img"):
        u = _candidate_url_from_img_tag(tag)
        if not u:
            continue
        w = _safe_int(tag.get("width"))
        h = _safe_int(tag.get("height"))
        out.append((u, w, h))

    # 2) <source> 标签(<picture> 内的)
    for tag in soup.find_all("source"):
        u = _candidate_url_from_source_tag(tag)
        if not u:
            continue
        out.append((u, None, None))

    # 3) og:image 总是加入(与 img / source 并存时,dedupe 兜底处理)
    meta = soup.find("meta", attrs={"property": "og:image"})
    if meta and isinstance(meta.get("content"), str):
        out.append((meta["content"], None, None))

    return out


def _safe_int(v: Any) -> Optional[int]:
    """字符串/数字转 int,失败 → None(不抛)。"""
    if v is None:
        return None
    try:
        return int(str(v).rstrip("px").strip())
    except (ValueError, TypeError):
        return None


def _parse_with_bs4(url: str, *, timeout: float) -> List[ResolvedImage]:
    """通用解析:GET HTML + BS4 抽候选 + 过滤去重 → ResolvedImage list。"""
    html = _fetch_html(url, timeout=timeout)
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        # lxml 解析器异常时退化到 html.parser
        soup = BeautifulSoup(html, "html.parser")

    candidates = _candidate_urls_from_html(soup)
    return _filter_and_dedupe(
        [ResolvedImage(url=u, width=w, height=h) for (u, w, h) in candidates]
    )


# ---------------------------------------------------------------------------
# 过滤 + 去重
# ---------------------------------------------------------------------------


_SVG_RE = re.compile(r"\.svg(\?|$)", re.IGNORECASE)
_DATA_URI_RE = re.compile(r"^data:", re.IGNORECASE)
_MIN_DIMENSION_PX = 100


def _normalize_url_for_dedupe(url: str) -> str:
    """去 query 参数归一化(去重用)— 保留 scheme + netloc + path。

    例子:
      "https://example.com/img.jpg?v=1" → "https://example.com/img.jpg"
      "https://example.com/img.jpg#section" → "https://example.com/img.jpg"
    """
    try:
        p = urlparse(url)
    except (ValueError, TypeError):
        return url
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def _filter_and_dedupe(images: List[ResolvedImage]) -> List[ResolvedImage]:
    """应用过滤 + 去重规则(spec F1.3):

      1. 过滤 ``data:`` URI(防 base64 内嵌)
      2. 过滤 ``.svg``(常用于图标 / 占位)
      3. 过滤已知宽高 < 100px 的缩略图变体
      4. 去重:URL 去 query 参数后归一化,**优先保留无 query 的"canonical"版本**

    返回新 list,保序(按原列表相对顺序;canonical 候选就位时替换之前的非 canonical)。

    dedupe 策略:同 key 多候选时,优先选 ``urlparse(url).query == ""``(无 query 参数)
    的版本;若多个都无 query,选第一个出现的。
    """
    canonical: Dict[str, ResolvedImage] = {}  # key → canonical 版本
    order: List[str] = []  # 首次出现的 key 顺序
    for img in images:
        u = img.url
        if not isinstance(u, str) or not u.strip():
            continue
        if _DATA_URI_RE.match(u):
            continue
        if _SVG_RE.search(u):
            continue
        if (
            img.width is not None
            and img.height is not None
            and img.width < _MIN_DIMENSION_PX
            and img.height < _MIN_DIMENSION_PX
        ):
            continue
        key = _normalize_url_for_dedupe(u)
        if key in canonical:
            # 已见过 → 比较当前候选是否更 canonical(无 query 优先)
            existing = canonical[key]
            existing_has_q = bool(urlparse(existing.url).query)
            current_has_q = bool(urlparse(u).query)
            if existing_has_q and not current_has_q:
                # 当前无 query,比已存的 canonical 更好 → 替换
                canonical[key] = img
            # 否则保留已存的
            continue
        canonical[key] = img
        order.append(key)
    return [canonical[k] for k in order]


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------


def _extract_images_from_html(html: str, *, source_url: str) -> List[ResolvedImage]:
    """纯 HTML 字符串 → ResolvedImage list(无网络)。

    主要给测试用(本地 fixture HTML),也供 ``_parse_with_bs4`` 内部复用。
    `source_url` 保留为将来扩展(防盗链 Referer 推导等)用,本期未使用。
    """
    del source_url  # 当前实现不依赖 source_url,保留签名便于未来扩展
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    candidates = _candidate_urls_from_html(soup)
    return _filter_and_dedupe(
        [ResolvedImage(url=u, width=w, height=h) for (u, w, h) in candidates]
    )


def parse_url(
    url: str,
    *,
    timeout: float = 30.0,
    playwright_fallback: bool = True,
) -> List[ResolvedImage]:
    """单 URL 解析主入口 — gallery-dl 优先 + BS4 fallback。

    Parameters
    ----------
    url:
        待解析的网页 / 相册 URL(必须是 http(s),其他 scheme 在 manifest 层已拒)。
    timeout:
        requests.get 超时秒数(BS4 fallback 用),默认 30s。

    Returns
    -------
    list[ResolvedImage]:
        解析出的图片直链列表(已过滤 + 去重)。

    Raises
    ------
    ImportError:
        gallery-dl 未安装(spec F1.2 显式 raise,镜像 yt-dlp)。
    ResolverError:
        gallery-dl fallback 路径(网络 / 解析)也失败。

    Cache 行为(plan followup 4):
        模块级 ``_PARSE_CACHE`` 不为 None 时,先查 cache(命中 + 未过期)直接返,
        跳过 gallery-dl / requests。解析成功后回写 cache(下次命中)。
    """
    # 1) 缓存查询(plan followup 4)
    if _PARSE_CACHE is not None:
        cached = _PARSE_CACHE.get(url)
        if cached is not None:
            return [
                ResolvedImage(
                    url=item.get("url", ""),
                    width=item.get("width"),
                    height=item.get("height"),
                )
                for item in cached
                if item.get("url")
            ]

    _require_gallery_dl()
    gallery_dl_urls = _gallery_dl_extract(url)
    if gallery_dl_urls:
        images = _filter_and_dedupe(
            [ResolvedImage(url=u) for u in gallery_dl_urls]
        )
    else:
        # fallback:BS4 通用解析
        images = _parse_with_bs4(url, timeout=timeout)
        # Open Q1:BS4 拿不到(JS 懒加载) → Playwright 兜底
        if not images and playwright_fallback:
            images = _parse_with_playwright(url, timeout=timeout)

    # 2) 回写缓存
    if _PARSE_CACHE is not None and images:
        _PARSE_CACHE.set(
            url,
            [
                {"url": img.url, "width": img.width, "height": img.height}
                for img in images
            ],
        )
    return images


__all__ = [
    "ResolvedImage",
    "ResolverError",
    "_extract_images_from_html",
    "_filter_and_dedupe",
    "_pick_largest_from_srcset",
    "parse_url",
]

