"""resource_fetcher.router — URL → Resolver 智能路由 (spec §4 两步判定).

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.

设计要点:
  - **初判 (选 resolver)**: 按域名规则表匹配, 返回 type[ResourceResolver]. 规则按
    "最具体优先" — 完整 host 匹配 > 泛后缀匹配 (`bilibili.com` 匹配 t.bilibili.com).
  - **终判 (定类型)**: resolver.parse() 产出的每个 ResourceItem 自带 resource_type,
    前端卡片类型徽标以解析结果为准. 初判仅供选择 resolver.
  - **歧义域名**: xiaohongshu 同时可能注册在 video + image 规则中 (本期注册表只放 video,
    实际类型由 yt-dlp 二次判定). 注册表顺序敏感 — 同 host 第一次匹配就返, 不 fallback.
  - **无匹配**: 返回 None, 由 run_parse 层 (Commit 3) 发 failed 事件.
  - **小写归一**: URL host 大小写不敏感 (DNS 标准), 比较前先 .lower().

host 匹配规则 (简化):
  1. 完整 host 等于 rule.domain (e.g. "bilibili.com" == "bilibili.com")
  2. host 以 "." + rule.domain 结尾 (e.g. "t.bilibili.com" 匹配 "bilibili.com")
  不做:
  - 前缀匹配 (e.g. "xxbilibili.com" 不应匹配 "bilibili.com")
  - 通配符 (本期硬编码, 未来可扩)
  - 子串匹配 (e.g. "bilibili" 不应匹配 "evil-bilibili.com")
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple, Type
from urllib.parse import urlparse

from .base import ResourceResolver
from .resolvers.audio import AudioResolver
from .resolvers.bilibili import BilibiliResolver
from .resolvers.image import ImageResolver
from .resolvers.video import VideoResolver
from .resolvers.xiaohongshu import XiaohongshuResolver
from .resolvers.youtube import YouTubeResolver


# ---------------------------------------------------------------------------
# 规则 + 注册表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolverRule:
    """单条路由规则 — 域名集合 → resolver class.

    字段:
      - resource_type: 提示类型 ("video" / "image"), 仅调试用. 实际类型由
                       resolver.parse() 二次判定 (F0.1 终判).
      - domains: 完整域名 tuple. 支持泛后缀: "bilibili.com" 自动匹配 *.bilibili.com.
      - resolver: Resolver class (不是 instance — router 只选类型, parse 时再实例化).
    """

    resource_type: str
    domains: Tuple[str, ...]
    resolver: Type[ResourceResolver]


# 注册表:顺序敏感 — 同 host 第一次匹配就返.
# 视频规则优先 (D2 决策 — 小红书默认走视频规则, 实际类型由 yt-dlp 决定).
RESOLVER_RULES: List[ResolverRule] = [
    # 视频规则 — 平台特定 (Phase 7+ 2026-09-04): 优先于通用 VideoResolver.
    # 同事扩展新平台时, 在这里加一行 ResolverRule 即可 (常见 case).
    ResolverRule("video", ("bilibili.com", "b23.tv"), BilibiliResolver),
    ResolverRule("video", ("youtube.com", "youtu.be"), YouTubeResolver),
    ResolverRule("video", ("x.com", "twitter.com"), VideoResolver),
    ResolverRule("video", ("kuaishou.com", "v.kuaishou.com"), VideoResolver),
    ResolverRule("video", ("douyin.com", "tiktok.com"), VideoResolver),
    # 小红书用 XiaohongshuResolver (F0.1 智能 dispatch: 视频 / 图集都能解析)
    ResolverRule("mixed", ("xiaohongshu.com", "xhslink.com"), XiaohongshuResolver),
    # 音频规则 — Phase 7+ 同事示例: soundcloud 音频走 AudioResolver.
    ResolverRule("audio", ("soundcloud.com", "snd.sc"), AudioResolver),
    # 图片规则
    ResolverRule("image", ("weibo.com", "weibo.cn"), ImageResolver),
    ResolverRule("image", ("douban.com",), ImageResolver),
]


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


# 解析时用于过滤 — 必须 http(s) scheme
_VALID_SCHEMES = frozenset({"http", "https"})


def _extract_host(url: str) -> Optional[str]:
    """从 URL 拿 host, 小写. 失败 / 非 http(s) / 空 → None.

    urlparse 对各种 malformed input 容忍度很高, 这里再加 scheme + hostname 双重过滤.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        p = urlparse(url)
    except (ValueError, TypeError, AttributeError):
        return None
    if p.scheme.lower() not in _VALID_SCHEMES:
        return None
    host = (p.hostname or "").lower().strip()
    if not host:
        return None
    return host


def _match_host_to_rule(host: str, rule: ResolverRule) -> bool:
    """host 是否匹配 rule.domains.

    匹配规则:
      1. host 完全等于 domain (e.g. "bilibili.com" == "bilibili.com")
      2. host 以 "." + domain 结尾 (e.g. "t.bilibili.com" 匹配 "bilibili.com")

    不做:
      - 前缀匹配 (e.g. "xxbilibili.com" 不匹配 "bilibili.com")
      - 通配符 (本期硬编码)
    """
    for domain in rule.domains:
        d = domain.lower()
        if host == d:
            return True
        if host.endswith("." + d):
            return True
    return False


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------


def resolve(url: str) -> Optional[Type[ResourceResolver]]:
    """按 URL 选 resolver. 无匹配 → None (由 run_parse 层发 failed 事件, Commit 3).

    Parameters
    ----------
    url:
        用户粘贴的链接, 容忍 scheme 缺失 / 大小写混用 / 带端口 / 带 query.

    Returns
    -------
    type[ResourceResolver] | None:
        第一个匹配 ResolverRule 的 resolver class. 注册表里没的域名 → None.
        非字符串 / 非 http(s) / 空字符串 / 解析失败 → None.

    Examples
    --------
    >>> resolve("https://www.bilibili.com/video/BV1xx")
    <class 'publisher.resource_fetcher.resolvers.video.VideoResolver'>
    >>> resolve("https://example.com/foo")
    None
    >>> resolve("not-a-url")
    None
    """
    host = _extract_host(url)
    if host is None:
        return None
    for rule in RESOLVER_RULES:
        if _match_host_to_rule(host, rule):
            return rule.resolver
    return None


__all__ = [
    "RESOLVER_RULES",
    "ResolverRule",
    "resolve",
]

