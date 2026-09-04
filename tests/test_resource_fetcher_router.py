"""test_resource_fetcher_router — URL 路由匹配矩阵 + 边界场景.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4 (F0.1).
phase: 2026-09-03-universal-resource-fetch Phase 1 Commit 1.
"""
from __future__ import annotations

import pytest

from multimedia_parsing.resource_fetcher.router import (
    RESOLVER_RULES,
    ResolverRule,
    resolve,
)
from multimedia_parsing.resource_fetcher.resolvers.audio import AudioResolver
from multimedia_parsing.resource_fetcher.resolvers.bilibili import BilibiliResolver
from multimedia_parsing.resource_fetcher.resolvers.image import ImageResolver
from multimedia_parsing.resource_fetcher.resolvers.video import VideoResolver
from multimedia_parsing.resource_fetcher.resolvers.xiaohongshu import (
    XiaohongshuResolver,
)
from multimedia_parsing.resource_fetcher.resolvers.youtube import YouTubeResolver


# ---------------------------------------------------------------------------
# 视频规则覆盖矩阵 — 平台特定 resolver (Phase 7+)
# ---------------------------------------------------------------------------

BILIBILI_URLS = [
    "https://www.bilibili.com/video/BV1xx",
    "https://bilibili.com/video/BV1xx",
    "https://b23.tv/abc",
    "https://t.bilibili.com/123",
    "https://space.bilibili.com/123",
]


@pytest.mark.parametrize("url", BILIBILI_URLS)
def test_resolve_bilibili_urls(url):
    assert resolve(url) is BilibiliResolver, f"Expected BilibiliResolver for {url!r}"


YOUTUBE_URLS = [
    "https://www.youtube.com/watch?v=foo",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=foo",
]


@pytest.mark.parametrize("url", YOUTUBE_URLS)
def test_resolve_youtube_urls(url):
    assert resolve(url) is YouTubeResolver, f"Expected YouTubeResolver for {url!r}"


# 通用视频规则覆盖矩阵 — 兜底 (x/twitter, kuaishou, douyin/tiktok)
GENERIC_VIDEO_URLS = [
    # X / Twitter
    "https://x.com/foo/status/123",
    "https://twitter.com/foo/status/123",
    # Kuaishou
    "https://www.kuaishou.com/short-video/3xxx",
    "https://v.kuaishou.com/abc",
    # Douyin / TikTok
    "https://www.douyin.com/video/7xxx",
    "https://www.tiktok.com/@foo/video/123",
]


@pytest.mark.parametrize("url", GENERIC_VIDEO_URLS)
def test_resolve_generic_video_urls(url):
    assert resolve(url) is VideoResolver, f"Expected VideoResolver for {url!r}"


# ---------------------------------------------------------------------------
# 音频规则覆盖矩阵 (Phase 7+)
# ---------------------------------------------------------------------------

AUDIO_URLS = [
    "https://soundcloud.com/artist/track",
    "https://www.soundcloud.com/artist/track",
    "https://snd.sc/abc",
]


@pytest.mark.parametrize("url", AUDIO_URLS)
def test_resolve_audio_urls(url):
    assert resolve(url) is AudioResolver, f"Expected AudioResolver for {url!r}"


# ---------------------------------------------------------------------------
# 图片规则覆盖矩阵
# ---------------------------------------------------------------------------

IMAGE_URLS = [
    "https://www.weibo.com/123/foo",
    "https://weibo.cn/123",
    "https://www.douban.com/photo/123",
    "https://movie.douban.com/subject/123",  # 子域名匹配
]


@pytest.mark.parametrize("url", IMAGE_URLS)
def test_resolve_image_urls(url):
    assert resolve(url) is ImageResolver, f"Expected ImageResolver for {url!r}"


# ---------------------------------------------------------------------------
# 无匹配 URL
# ---------------------------------------------------------------------------

UNKNOWN_URLS = [
    "https://example.com/foo",
    "https://github.com/user/repo",
    "https://news.ycombinator.com/item?id=1",
    "https://baidu.com/s?wd=foo",  # 不在注册表
    "https://www.qq.com/news",
]


@pytest.mark.parametrize("url", UNKNOWN_URLS)
def test_resolve_unknown_returns_none(url):
    assert resolve(url) is None, f"Expected None for {url!r}"


# ---------------------------------------------------------------------------
# 边界场景 — invalid URL / non-http scheme / 非字符串
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not-a-url",
        "ftp://example.com/foo",  # 非 http(s)
        "javascript:alert(1)",
        "data:text/plain,foo",
        "file:///etc/passwd",
        "https://",  # 无 host
        "//example.com/foo",  # 无 scheme (relative URL)
    ],
)
def test_resolve_invalid_returns_none(url):
    assert resolve(url) is None, f"Expected None for invalid {url!r}"


@pytest.mark.parametrize("url", [None, 123, [], {}, object()])
def test_resolve_non_string_returns_none(url):
    assert resolve(url) is None, f"Expected None for non-string {url!r}"


# ---------------------------------------------------------------------------
# 大小写 + 子域 + 端口
# ---------------------------------------------------------------------------


def test_resolve_host_is_case_insensitive():
    """URL host 大小写不敏感."""
    assert resolve("HTTPS://WWW.BILIBILI.COM/video/BV1xx") is BilibiliResolver
    assert resolve("https://Www.Bilibili.Com/video/BV1xx") is BilibiliResolver


def test_resolve_with_port():
    """URL 带端口号应能正确匹配 host 部分."""
    assert resolve("https://bilibili.com:443/video/BV1xx") is BilibiliResolver
    assert resolve("https://weibo.com:8080/123") is ImageResolver


def test_resolve_with_query_and_path():
    """URL 带 query / path 也能匹配 host 部分."""
    assert resolve("https://www.bilibili.com/video/BV1xx?p=1&spm=foo") is BilibiliResolver
    assert resolve("https://weibo.com/u/123?from=feed") is ImageResolver


# ---------------------------------------------------------------------------
# 歧义域名 — F0.1 二次判定
# ---------------------------------------------------------------------------


def test_resolve_xiaohongshu_uses_mixed_resolver():
    """F0.1 + spec §4: 小红书在 router 阶段走 XiaohongshuResolver (mixed).

    实际类型由 XiaohongshuResolver.parse() 二次判定 (yt-dlp note_info →
    video.media.stream or imageList, dispatch 到 VideoResolver / ImageResolver).
    """
    assert (
        resolve("https://www.xiaohongshu.com/explore/abc") is XiaohongshuResolver
    )
    assert resolve("https://xhslink.com/abc") is XiaohongshuResolver


# ---------------------------------------------------------------------------
# 注册表结构
# ---------------------------------------------------------------------------


def test_resolver_rule_dataclass_frozen():
    """ResolverRule 是 frozen dataclass — 不可变."""
    rule = ResolverRule("video", ("bilibili.com",), VideoResolver)
    with pytest.raises((AttributeError, Exception)):
        rule.domains = ("other.com",)  # type: ignore[misc]


def test_resolver_rules_not_empty():
    assert len(RESOLVER_RULES) >= 2  # 至少 video + image
    # 每个 rule 都有 resolver + 域名都是字符串
    for r in RESOLVER_RULES:
        assert r.resolver is not None
        assert isinstance(r.domains, tuple)
        assert all(isinstance(d, str) and d for d in r.domains)
        assert r.resource_type in {"video", "image", "audio", "model", "mixed"}


def test_resolver_rules_have_unique_domains_within_type():
    """同一 resource_type 内, 同一域名不应重复出现在不同规则 (避免歧义).

    例: 'bilibili.com' 同时出现在 video 和 image 规则里会让人迷惑 — router
    选 video 后无法 fallback 到 image 二次尝试. 当前设计靠注册表顺序 +
    解析器二次判定, 此处只防御 typo.
    """
    seen: dict = {}
    for r in RESOLVER_RULES:
        for d in r.domains:
            key = (r.resource_type, d)
            assert key not in seen, f"Domain {d!r} duplicated under {r.resource_type}"
            seen[key] = r.resolver


def test_resolver_rules_video_covers_common_platforms():
    """视频规则覆盖 spec §4 列出的核心平台."""
    video_domains = set()
    for r in RESOLVER_RULES:
        if r.resource_type == "video":
            video_domains.update(r.domains)
    for required in ["bilibili.com", "youtube.com", "x.com"]:
        assert required in video_domains, (
            f"video rule missing domain {required!r}"
        )

