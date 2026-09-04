"""resolvers.model — 模型资源解析器 (D2 决策 stub, 同事可参考扩展).

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.

设计要点 (同事参考):
  - **D2 决策**: 本期 model 类型路由枚举有, resolver 暂无 (同 audio 模式, 但 audio 已
    落地, model 是同事未来扩展点).
  - **实现 ResourceResolver 协议**: parse() + fetch() 两个方法必须实现 (Protocol + runtime
    checkable). 同事可继承本 stub 类, 替换 ``_platform_domains`` + 重写 parse/fetch.
  - **平台覆盖 (待同事填)**: huggingface.co / civitai.com / modelscope.cn 等.
  - **下载协议**: 跟 audio/video 不同, 模型通常是大文件 + 多文件 (权重 + config + tokenizer),
    需要走 git LFS / huggingface_hub / wget 之类. 同事按平台挑工具.

router 注册示例 (同事实现后):
    ResolverRule("model", ("huggingface.co",), ModelResolver),

扩展工作流 (同事可参考 ``resolver-extension`` skill):
  1. 复制本文件 → ``resolvers/{platform}.py``.
  2. 重写 ``_platform_domains`` 列表.
  3. 重写 ``parse()``: 调平台 API / huggingface_hub.list_repo_files 等.
  4. 重写 ``fetch()``: 走 huggingface_hub.snapshot_download / wget 等.
  5. 注册到 ``router.py`` RESOLVER_RULES.
  6. 加 tests/test_{platform}_resolver.py (parse / fetch / fetch_wrong_type / router).
  7. 跑 ``pytest -m "not slow"`` 全量验证.
  8. 跑 ``python scripts/smoke_e2e.py`` 端到端验证 (无 cookies 也能跑通基础路径).
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ..base import FetchDestination, FetchResult, ResourceItem


class ModelResolver:
    """模型资源解析器 — D2 决策 stub.

    同事可参考本类实现 huggingface / civitai / modelscope 等模型下载.
    parse() + fetch() 暂返空, 不抛错 (避免前端误报).
    """

    # 同事实现时填这个列表 (供 router 端做平台校验 / 调试日志)
    _platform_domains: tuple = ()  # type: ignore[type-arg]

    def __init__(
        self,
        *,
        cookie_file: Optional[Path] = None,
        account_id: Optional[str] = None,
    ) -> None:
        self.cookie_file = cookie_file
        self.account_id = account_id

    def parse(self, url: str) -> List[ResourceItem]:
        """解析模型 URL → 0 条 (未实现).

        同事实现时返 1..N 条 ResourceItem (resource_type='model'), item_id 推荐格式
        ``{platform}_{model_id}`` 跟 video/audio 对齐.
        """
        # D2 决策: 本期未实现. 同事扩展时实现 huggingface_hub.list_repo_files 等.
        return []

    def fetch(self, item: ResourceItem, dest: FetchDestination) -> FetchResult:
        """下载模型 → 当前返 ``not_implemented`` 错误.

        同事实现时:
          1. local 模式: 调 huggingface_hub.snapshot_download(repo_id=...) 落本地.
          2. oss 模式: 走 ``_OssUploader`` 递归上传整个 snapshot 目录.
          3. both 模式: 同 video/audio (本地 + oss, oss 失败降级).
        """
        if item.resource_type != "model":
            return FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error=f"ModelResolver cannot fetch resource_type={item.resource_type!r}",
            )

        return FetchResult(
            item_id=item.item_id,
            resource_type=item.resource_type,
            platform=item.platform,
            error=(
                "ModelResolver is a stub (D2 决策). "
                "同事参考 resolver-extension skill 实现 huggingface/civitai 等."
            ),
        )


__all__ = ["ModelResolver"]
