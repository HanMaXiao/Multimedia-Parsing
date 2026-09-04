"""test_image_fetcher_playwright.py — JS 渲染兜底(plan followup Open Q1)。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md Open Q1:
部分文章页图片懒加载靠 JS 注入,requests 拿不到 → Playwright 渲染兜底分支。

设计要点:
  - ``_parse_with_playwright(url, timeout) -> List[ResolvedImage]``:懒加载 Playwright,
    打开 headless Chromium 渲染 → 拿最终 HTML → 走 BS4 抽取
  - ``parse_url`` 调用流程:gallery-dl → BS4(返空 / 数量 < 阈值)→ Playwright 兜底
  - 透明降级:Playwright 未装 → 跳过兜底(spec:不强求装)
  - Chromium 未下载 → playwright 安装时显式 raise("run playwright install chromium")
  - 兜底本身可关:``playwright_fallback=False`` 参数

被拒(2026-09-03 followup Open Q1):
  - 始终走 Playwright — 慢(秒级)+ 资源消耗大,本场景 BS4 90% 够
  - 截图比对(headless render 前后 DOM diff)— 复杂,JS 注入检测本身不稳
  - 反爬 / cookie 注入 — 那是 followup 1 的事,这里只做"渲染后取 DOM"
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from multimedia_parsing.image_fetcher import resolver as res_mod
from multimedia_parsing.image_fetcher.resolver import (
    ResolvedImage,
    _candidate_urls_from_html,
    _parse_with_playwright,
    parse_url,
)


# ---------------------------------------------------------------------------
# _parse_with_playwright 单测
# ---------------------------------------------------------------------------


def test_parse_with_playwright_uses_injected_browser(monkeypatch, tmp_path: Path):
    """用 fake playwright browser 拿 HTML,返回 BS4 解析的图片列表。"""
    # 准备 fake HTML 文件路径
    fake_html = (
        "<html><body>"
        '<img src="https://cdn/lazy.jpg" data-src="https://cdn/real.jpg" />'
        '<img src="https://cdn/og-fallback.jpg" />'
        "</body></html>"
    )
    # fake sync_playwright context manager
    class _FakePage:
        def __init__(self, html):
            self.html = html
            self.url = "https://example.com/article/1"

        def goto(self, url, **kwargs):
            self.url = url
            return None

        def content(self):
            return self.html

    class _FakeContext:
        def __init__(self, html):
            self.page = _FakePage(html)

        def new_page(self):
            return self.page

        def close(self):
            pass

    class _FakeBrowser:
        def __init__(self, html):
            self.context_obj = _FakeContext(html)

        def new_context(self):
            return self.context_obj

        def close(self):
            pass

    class _FakePlaywright:
        def __init__(self, html):
            self.chromium = MagicMock(launch=lambda **kwargs: _FakeBrowser(html))

    def fake_sync_playwright():
        class _CM:
            def __init__(self, html):
                self.pw = _FakePlaywright(html)

            def __enter__(self):
                return self.pw

            def __exit__(self, *args):
                return False

        return _CM(fake_html)

    # 注入 fake module:模拟 ``from playwright.sync_api import sync_playwright``
    fake_pw_module = MagicMock()
    fake_pw_module.sync_playwright = fake_sync_playwright
    monkeypatch.setattr(res_mod, "_PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(res_mod, "_playwright_module", fake_pw_module)

    images = _parse_with_playwright("https://example.com/article/1", timeout=10.0)
    # 2 张 img(data-src 优先 → real.jpg;og → og-fallback.jpg)
    urls = {img.url for img in images}
    assert "https://cdn/real.jpg" in urls
    assert "https://cdn/og-fallback.jpg" in urls


def test_parse_with_playwright_no_playwright_returns_empty(monkeypatch):
    """Playwright 未装 → 返空 list(透明降级,不抛)。"""
    monkeypatch.setattr(res_mod, "_PLAYWRIGHT_AVAILABLE", False)
    images = _parse_with_playwright("https://example.com/article/1", timeout=10.0)
    assert images == []


def test_parse_with_playwright_timeout_returns_empty(monkeypatch):
    """Playwright goto 超时 → 返空(spec 容错,不抛)。"""
    class _FakePage:
        def goto(self, url, **kwargs):
            raise RuntimeError("Timeout exceeded")

        def content(self):
            return "<html></html>"

    class _FakeContext:
        def new_page(self):
            return _FakePage()

        def close(self):
            pass

    class _FakeBrowser:
        def new_context(self):
            return _FakeContext()

        def close(self):
            pass

    class _FakePlaywright:
        chromium = MagicMock(launch=lambda **kwargs: _FakeBrowser())

    class _CM:
        def __enter__(self):
            return _FakePlaywright()

        def __exit__(self, *args):
            return False

    fake_pw_module = MagicMock(sync_playwright=lambda: _CM())
    monkeypatch.setattr(res_mod, "_PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(res_mod, "_playwright_module", fake_pw_module)

    images = _parse_with_playwright("https://example.com/article/1", timeout=10.0)
    assert images == []


# ---------------------------------------------------------------------------
# parse_url 集成 — gallery-dl 0 + BS4 0 + Playwright 兜底
# ---------------------------------------------------------------------------


def test_parse_url_falls_back_to_playwright_when_bs4_empty(monkeypatch):
    """gallery-dl 返 0 + BS4 返 0 + Playwright 兜底拿图 → 返 Playwright 结果。"""
    # 1) gallery-dl find 返 None
    class _EmptyExtractor:
        def __iter__(self):
            return iter([])

    fake_gd = MagicMock()
    fake_gd.extractor.find = MagicMock(return_value=None)
    monkeypatch.setattr(res_mod, "gallery_dl", fake_gd)

    # 2) requests.get 返空 HTML(BS4 拿不到图)
    def fake_get(url, **kwargs):
        return MagicMock(status_code=200, text="<html><body>JS 待渲染</body></html>")
    monkeypatch.setattr(res_mod.requests, "get", fake_get)

    # 3) Playwright 返真实图
    fake_html = '<html><body><img src="https://cdn/js-rendered.jpg" /></body></html>'

    class _FakePage:
        def goto(self, url, **kwargs):
            pass

        def content(self):
            return fake_html

    class _FakeContext:
        def new_page(self):
            return _FakePage()

        def close(self):
            pass

    class _FakeBrowser:
        def new_context(self):
            return _FakeContext()

        def close(self):
            pass

    class _FakePlaywright:
        chromium = MagicMock(launch=lambda **kwargs: _FakeBrowser())

    class _CM:
        def __enter__(self):
            return _FakePlaywright()

        def __exit__(self, *args):
            return False

    fake_pw_module = MagicMock(sync_playwright=lambda: _CM())
    monkeypatch.setattr(res_mod, "_PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(res_mod, "_playwright_module", fake_pw_module)

    # 关掉 cache(避免上次跑污染)
    monkeypatch.setattr(res_mod, "_PARSE_CACHE", None)

    images = parse_url("https://example.com/js-only-page", timeout=5.0)
    urls = [img.url for img in images]
    assert "https://cdn/js-rendered.jpg" in urls


def test_parse_url_skips_playwright_when_bs4_has_results(monkeypatch):
    """gallery-dl 0 + BS4 拿到 N 张 → 不调 Playwright(快路径优先)。"""
    fake_gd = MagicMock()
    fake_gd.extractor.find = MagicMock(return_value=None)
    monkeypatch.setattr(res_mod, "gallery_dl", fake_gd)

    # BS4 返 1 张图
    bs4_html = '<html><body><img src="https://cdn/static.jpg" /></body></html>'

    def fake_get(url, **kwargs):
        return MagicMock(status_code=200, text=bs4_html)
    monkeypatch.setattr(res_mod.requests, "get", fake_get)

    # Playwright 故意 throw(若被调用就 fail)
    pw_calls: list = []
    def fake_pw(*args, **kwargs):
        pw_calls.append((args, kwargs))
        raise RuntimeError("should not be called when BS4 has results")
    fake_pw_module = MagicMock(sync_playwright=fake_pw)
    monkeypatch.setattr(res_mod, "_PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(res_mod, "_playwright_module", fake_pw_module)
    monkeypatch.setattr(res_mod, "_PARSE_CACHE", None)

    images = parse_url("https://example.com/static-page", timeout=5.0)
    assert len(pw_calls) == 0
    urls = [img.url for img in images]
    assert "https://cdn/static.jpg" in urls


def test_parse_url_playwright_fallback_disabled(monkeypatch):
    """``parse_url(playwright_fallback=False)`` → 不调 Playwright。"""
    fake_gd = MagicMock()
    fake_gd.extractor.find = MagicMock(return_value=None)
    monkeypatch.setattr(res_mod, "gallery_dl", fake_gd)

    def fake_get(url, **kwargs):
        return MagicMock(status_code=200, text="<html><body>JS only</body></html>")
    monkeypatch.setattr(res_mod.requests, "get", fake_get)

    pw_calls: list = []
    fake_pw_module = MagicMock(sync_playwright=MagicMock(side_effect=lambda *a, **k: pw_calls.append((a, k))))
    monkeypatch.setattr(res_mod, "_PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(res_mod, "_playwright_module", fake_pw_module)
    monkeypatch.setattr(res_mod, "_PARSE_CACHE", None)

    images = parse_url(
        "https://example.com/js-only", timeout=5.0, playwright_fallback=False
    )
    assert pw_calls == []
    assert images == []

