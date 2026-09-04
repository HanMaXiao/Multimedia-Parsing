"""test_image_fetcher_resolver.py — image_fetcher/resolver.py 单元测试。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §4.1 + F1.1-F1.3。

覆盖范围(全部 mock 驱动,不打真站):
  - parse_url 主体:gallery-dl 命中 / 未命中 / 异常 → fallback BS4
  - gallery-dl 未装 → ImportError 显式抛(spec F1.2)
  - _extract_images_from_html:src / data-src / srcset / picture / og:image
  - 过滤:data: URI / .svg / < 100px 缩略图
  - 去重:URL 去 query 后 + 原始双 key
  - srcset:取最大宽度或最高密度
  - 集成场景:文章页 URL → 通用解析 → 过滤 + 去重 → 列表
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from multimedia_parsing.image_fetcher import resolver as res_mod
from multimedia_parsing.image_fetcher.resolver import (
    ResolvedImage,
    ResolverError,
    _extract_images_from_html,
    _filter_and_dedupe,
    _pick_largest_from_srcset,
    parse_url,
)


# ---------------------------------------------------------------------------
# Fake gallery-dl(模拟 gallery_dl.extractor.find)
# ---------------------------------------------------------------------------


class _FakeGalleryDLExtractor:
    """最小化 gallery-dl extractor:__iter__ 产出 (url, keywords) 元组。"""

    def __init__(self, urls: List[str]):
        self._urls = urls

    def __iter__(self):
        for u in self._urls:
            yield (u, {})


class _FakeGalleryDLExtractorModule:
    """``gallery_dl.extractor`` 模块替身 — 暴露 ``find`` 方法。"""

    def __init__(self, mapping: Dict[str, Any]):
        # mapping: url → extractor instance | None
        self._mapping = mapping
        self.find_calls: List[str] = []

    def find(self, url: str) -> Optional[_FakeGalleryDLExtractor]:
        self.find_calls.append(url)
        return self._mapping.get(url)


class _FakeGalleryDLModule:
    """``gallery_dl`` 模块替身 — 带 .extractor 属性。"""

    def __init__(self, extractor_module: _FakeGalleryDLExtractorModule):
        self.extractor = extractor_module


def _patch_gallery_dl(monkeypatch, fake_module):
    """把 fake 注入到 resolver 模块的 gallery_dl 名字。"""
    monkeypatch.setattr(res_mod, "gallery_dl", fake_module)


# ---------------------------------------------------------------------------
# BS4 集成测试:fake requests response(避免打真站)
# ---------------------------------------------------------------------------


class _FakeResponse:
    """模拟 requests.get 返回 — status_code + text + content。"""

    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.content = text.encode("utf-8")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_requests_get(monkeypatch, html: str, status_code: int = 200):
    """把 requests.get 替换成返 fake response。"""
    captured: Dict[str, Any] = {"call_count": 0, "last_url": None}

    def fake_get(url: str, **kwargs):
        captured["call_count"] += 1
        captured["last_url"] = url
        return _FakeResponse(html, status_code)

    monkeypatch.setattr(res_mod.requests, "get", fake_get)
    return captured


# ---------------------------------------------------------------------------
# parse_url — gallery-dl 路径
# ---------------------------------------------------------------------------


def test_parse_url_uses_gallery_dl_when_extractor_returns_images(monkeypatch):
    """gallery-dl 命中 → 走 extractor 列表,不走 BS4。"""
    extractor = _FakeGalleryDLExtractor(
        [
            "https://i.pximg.net/img-original/x1.jpg",
            "https://i.pximg.net/img-original/x2.jpg",
        ]
    )
    extractor_mod = _FakeGalleryDLExtractorModule(
        {"https://www.pixiv.net/artworks/123": extractor}
    )
    _patch_gallery_dl(monkeypatch, _FakeGalleryDLModule(extractor_mod))
    requests_capture = _patch_requests_get(monkeypatch, "<html>should not see this</html>")

    images = parse_url("https://www.pixiv.net/artworks/123")
    assert len(images) == 2
    assert all(isinstance(img, ResolvedImage) for img in images)
    assert images[0].url == "https://i.pximg.net/img-original/x1.jpg"
    assert images[1].url == "https://i.pximg.net/img-original/x2.jpg"
    # gallery-dl 命中 → requests 0 调用
    assert requests_capture["call_count"] == 0
    assert extractor_mod.find_calls == ["https://www.pixiv.net/artworks/123"]


def test_parse_url_falls_back_to_bs4_when_extractor_not_found(monkeypatch):
    """gallery-dl find() 返 None(不识别 URL)→ fallback BS4。"""
    extractor_mod = _FakeGalleryDLExtractorModule({})
    _patch_gallery_dl(monkeypatch, _FakeGalleryDLModule(extractor_mod))
    _patch_requests_get(
        monkeypatch,
        '<html><body><img src="https://example.com/img1.jpg"></body></html>',
    )

    images = parse_url("https://example.com/article/1")
    assert len(images) == 1
    assert images[0].url == "https://example.com/img1.jpg"


def test_parse_url_falls_back_to_bs4_when_extractor_returns_empty(monkeypatch):
    """gallery-dl find() 命中但 extractor 迭代产 0 条 → fallback BS4。"""
    extractor = _FakeGalleryDLExtractor([])  # 空 list
    extractor_mod = _FakeGalleryDLExtractorModule(
        {"https://example.com/empty": extractor}
    )
    _patch_gallery_dl(monkeypatch, _FakeGalleryDLModule(extractor_mod))
    _patch_requests_get(
        monkeypatch,
        '<html><body><img src="https://example.com/img1.jpg"></body></html>',
    )

    images = parse_url("https://example.com/empty")
    assert len(images) == 1
    assert images[0].url == "https://example.com/img1.jpg"


def test_parse_url_falls_back_to_bs4_when_extractor_raises(monkeypatch):
    """gallery-dl extractor 迭代抛异常 → fallback BS4(graceful degradation)。"""
    class _RaisingExtractor:
        def __iter__(self):
            raise RuntimeError("gallery-dl 内部 extractor 错")
            yield  # 让它成为 generator,实际不会到这行

    extractor_mod = _FakeGalleryDLExtractorModule(
        {"https://example.com/broken": _RaisingExtractor()}
    )
    _patch_gallery_dl(monkeypatch, _FakeGalleryDLModule(extractor_mod))
    _patch_requests_get(
        monkeypatch,
        '<html><body><img src="https://example.com/img1.jpg"></body></html>',
    )

    images = parse_url("https://example.com/broken")
    assert len(images) == 1
    assert images[0].url == "https://example.com/img1.jpg"


def test_parse_url_raises_import_error_when_gallery_dl_not_installed(monkeypatch):
    """gallery-dl 未装 → ImportError 显式抛(spec F1.2 镜像 yt-dlp)。"""
    monkeypatch.setattr(res_mod, "gallery_dl", None)
    _patch_requests_get(
        monkeypatch,
        '<html><body><img src="https://example.com/img1.jpg"></body></html>',
    )
    with pytest.raises(ImportError, match="gallery-dl is not installed"):
        parse_url("https://example.com/article/1")


# ---------------------------------------------------------------------------
# _extract_images_from_html — 各类 HTML 元素覆盖
# ---------------------------------------------------------------------------


def test_extract_img_tag_with_src():
    """基本 <img src> 抽取。"""
    html = '<html><body><img src="https://example.com/a.jpg" /></body></html>'
    images = _extract_images_from_html(html, source_url="https://example.com/page")
    assert len(images) == 1
    assert images[0].url == "https://example.com/a.jpg"


def test_extract_img_tag_with_data_src_lazy_load():
    """懒加载 data-src / data-original 优先于 src(spec F1.3)。"""
    html = (
        '<html><body>'
        '<img src="https://example.com/placeholder.gif" '
        'data-src="https://example.com/real.jpg" />'
        '<img data-original="https://example.com/orig.jpg" />'
        '</body></html>'
    )
    images = _extract_images_from_html(html, source_url="https://example.com/page")
    urls = [i.url for i in images]
    assert "https://example.com/real.jpg" in urls
    assert "https://example.com/orig.jpg" in urls
    # 占位图 placeholder.gif 应被过滤(后续 _filter_and_dedupe 处理)


def test_extract_img_tag_with_srcset_picks_largest():
    """srcset 多个候选 → 取最大宽度(2x > 1x)。"""
    html = (
        '<html><body>'
        '<img srcset="'
        'https://example.com/a.jpg 1x, '
        'https://example.com/a@2x.jpg 2x, '
        'https://example.com/a@3x.jpg 3x'
        '" />'
        '</body></html>'
    )
    images = _extract_images_from_html(html, source_url="https://example.com/page")
    assert len(images) == 1
    assert images[0].url == "https://example.com/a@3x.jpg"


def test_extract_img_tag_with_srcset_width_descriptors():
    """srcset 宽度描述符:100w / 200w → 取最大宽度。"""
    html = (
        '<html><body>'
        '<img srcset="'
        'https://example.com/small.jpg 320w, '
        'https://example.com/med.jpg 640w, '
        'https://example.com/big.jpg 1280w'
        '" />'
        '</body></html>'
    )
    images = _extract_images_from_html(html, source_url="https://example.com/page")
    assert len(images) == 1
    assert images[0].url == "https://example.com/big.jpg"


def test_extract_picture_source_tag():
    """<picture><source srcset> 抽取。"""
    html = (
        '<html><body>'
        '<picture>'
        '<source srcset="https://example.com/webp.avif" type="image/avif" />'
        '<source srcset="https://example.com/webp.webp" type="image/webp" />'
        '<img src="https://example.com/fallback.jpg" />'
        '</picture>'
        '</body></html>'
    )
    images = _extract_images_from_html(html, source_url="https://example.com/page")
    urls = [i.url for i in images]
    assert "https://example.com/webp.avif" in urls
    assert "https://example.com/webp.webp" in urls
    assert "https://example.com/fallback.jpg" in urls


def test_extract_og_image_fallback():
    """og:image meta 标签兜底(spec F1.3 通用解析规则)。"""
    html = (
        '<html><head>'
        '<meta property="og:image" content="https://example.com/og.jpg" />'
        '</head><body>正文无图</body></html>'
    )
    images = _extract_images_from_html(html, source_url="https://example.com/page")
    assert len(images) == 1
    assert images[0].url == "https://example.com/og.jpg"


def test_extract_no_images_returns_empty():
    """完全无图的 HTML → 空 list。"""
    html = "<html><body>纯文字</body></html>"
    images = _extract_images_from_html(html, source_url="https://example.com/page")
    assert images == []


# ---------------------------------------------------------------------------
# 过滤与去重
# ---------------------------------------------------------------------------


def test_filter_drops_data_uri():
    """data: URI(防 base64 内嵌)— 直接过滤。"""
    candidates = [
        ResolvedImage(url="https://example.com/real.jpg"),
        ResolvedImage(url="data:image/png;base64,iVBORw0KGgo="),
    ]
    out = _filter_and_dedupe(candidates)
    urls = [i.url for i in out]
    assert "https://example.com/real.jpg" in urls
    assert not any(u.startswith("data:") for u in urls)


def test_filter_drops_svg():
    """.svg(常用于图标 / 占位)— 过滤。"""
    candidates = [
        ResolvedImage(url="https://example.com/photo.jpg"),
        ResolvedImage(url="https://example.com/icon.svg"),
    ]
    out = _filter_and_dedupe(candidates)
    urls = [i.url for i in out]
    assert "https://example.com/photo.jpg" in urls
    assert "https://example.com/icon.svg" not in urls


def test_filter_drops_small_thumbnails_when_dimensions_known():
    """宽高已知且 < 100px → 过滤(缩略图变体)。"""
    candidates = [
        ResolvedImage(url="https://example.com/big.jpg", width=1920, height=1080),
        ResolvedImage(url="https://example.com/thumb.jpg", width=64, height=64),
    ]
    out = _filter_and_dedupe(candidates)
    urls = [i.url for i in out]
    assert "https://example.com/big.jpg" in urls
    assert "https://example.com/thumb.jpg" not in urls


def test_filter_keeps_small_thumbnails_when_dimensions_unknown():
    """宽高未知(None)→ 保留(避免误伤缩略图尺寸未抓到的真图)。"""
    candidates = [
        ResolvedImage(url="https://example.com/x.jpg", width=None, height=None),
    ]
    out = _filter_and_dedupe(candidates)
    assert len(out) == 1


def test_dedupes_by_normalized_url():
    """同 URL 不同 query 参数 → 去重。"""
    candidates = [
        ResolvedImage(url="https://example.com/img.jpg?v=1"),
        ResolvedImage(url="https://example.com/img.jpg?v=2"),
        ResolvedImage(url="https://example.com/img.jpg"),  # 无 query
    ]
    out = _filter_and_dedupe(candidates)
    assert len(out) == 1
    # 保留首次出现的(原 URL 无 query)
    assert out[0].url == "https://example.com/img.jpg"


def test_dedupes_by_raw_url():
    """不同 path 即使 query 一致 → 不去重。"""
    candidates = [
        ResolvedImage(url="https://example.com/a.jpg"),
        ResolvedImage(url="https://example.com/b.jpg"),
    ]
    out = _filter_and_dedupe(candidates)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# _pick_largest_from_srcset
# ---------------------------------------------------------------------------


def test_pick_largest_density_descriptor():
    """1x / 2x / 3x → 3x。"""
    out = _pick_largest_from_srcset(
        "https://a.com/x.jpg 1x, https://a.com/x@2x.jpg 2x, https://a.com/x@3x.jpg 3x"
    )
    assert out == "https://a.com/x@3x.jpg"


def test_pick_largest_width_descriptor():
    """320w / 640w / 1280w → 1280w。"""
    out = _pick_largest_from_srcset(
        "https://a.com/s.jpg 320w, https://a.com/m.jpg 640w, https://a.com/b.jpg 1280w"
    )
    assert out == "https://a.com/b.jpg"


def test_pick_largest_empty_returns_none():
    assert _pick_largest_from_srcset("") is None
    assert _pick_largest_from_srcset("   ") is None


def test_pick_largest_single_candidate():
    out = _pick_largest_from_srcset("https://a.com/x.jpg 2x")
    assert out == "https://a.com/x.jpg"


# ---------------------------------------------------------------------------
# 集成场景
# ---------------------------------------------------------------------------


def test_parse_url_article_page_full_flow(monkeypatch):
    """集成:文章页 URL → gallery-dl 找不到 → BS4 解析 → 过滤+去重。

    HTML 同时含:
      - 1 个真图(<img src>)
      - 1 个懒加载图(<img data-src>)
      - 1 个 srcset 3 选 1
      - 1 个 data: URI 噪声
      - 1 个 .svg 噪声
      - 1 个 og:image 兜底
    """
    extractor_mod = _FakeGalleryDLExtractorModule({})
    _patch_gallery_dl(monkeypatch, _FakeGalleryDLModule(extractor_mod))

    html = """
    <html>
    <head>
        <meta property="og:image" content="https://example.com/og.jpg" />
    </head>
    <body>
        <img src="https://example.com/photo1.jpg" width="800" height="600" />
        <img src="https://example.com/placeholder.gif" data-src="https://example.com/photo2.jpg" />
        <img srcset="https://example.com/s.jpg 320w, https://example.com/m.jpg 640w" />
        <img src="data:image/png;base64,iVBORw0KGgo=" />
        <img src="https://example.com/icon.svg" />
        <img src="https://example.com/photo1.jpg?x=1" />  <!-- 重复 -->
    </body>
    </html>
    """
    _patch_requests_get(monkeypatch, html)

    images = parse_url("https://example.com/article/123")

    urls = [i.url for i in images]
    # 期望保留:photo1(2 次去重为 1) + photo2 + srcset 最大 m + og
    assert "https://example.com/photo1.jpg" in urls
    assert "https://example.com/photo2.jpg" in urls
    assert "https://example.com/m.jpg" in urls
    assert "https://example.com/og.jpg" in urls
    # 过滤掉的
    assert "https://example.com/placeholder.gif" not in urls  # 已被 data-src 替代
    assert not any(u.startswith("data:") for u in urls)
    assert "https://example.com/icon.svg" not in urls
    # photo1.jpg 去重后只有 1 条
    assert urls.count("https://example.com/photo1.jpg") == 1

