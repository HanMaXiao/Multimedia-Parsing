# AGENTS.md — Multimedia Parsing 服务工作约定

> 通用多媒体资源解析服务:video / image / (audio, model) 智能 URL 路由
> + 3-mode 下载(local / oss / both)+ SSE 事件流。FastAPI 实现。

## 项目背景

从 `Multi-Platform Automated Release` 项目抽出 — 该项目的 Tauri 桌面 app
需要一个 Python sidecar 来做多媒体资源解析下载。现在把它作为独立
HTTP 服务跑,供其他项目(包括原 Tauri app)调用。

**抽出版本** = Plan 2026-09-03-universal-resource-fetch 全 7 phases
收口后的代码:
- `resource_fetcher/` (7 文件) — 智能路由 + resolver 协议
- `oss/` (2 文件) — 跨包共享 OssUploader
- `video_fetcher/` (6 文件) — yt-dlp 包装,被 VideoResolver 调
- `image_fetcher/` (6 文件) — gallery-dl + BS4 包装,被 ImageResolver 调
- `event_emitter.py` — 9 状态机 + __EVENT__: 协议(适配到 SSE)

**不在范围**(明确剥离):
- 原 publisher package 的 dispatch / workflow_bindings / login_for_account
  / OSS 配置 UI — 那些是 Tauri app 自己的事
- 原项目的 Rust 端 commands / 事件桥接 — 服务化后由 HTTP 替代
- 原项目的前端 UI — 服务只暴露 API,UI 由调用方自实现

## Setup commands

- **Install**: `pip install -e ".[parsing,dev]"` (parsing 装 yt-dlp / gallery-dl 等,dev 装 pytest)
  - **Playwright 浏览器**: `playwright install chromium` (image_fetcher 的 JS 渲染路径需要)
- **Run dev server**: `multimedia-parsing-server --reload --port 8765`
  - 或 `python -m multimedia_parsing.server --reload --port 8765`
- **Test**: `pytest -m "not slow"` (~11s 跳过 5 个慢测试)
- **Full test**: `pytest` (~40s, 含 slow marker)
- **Smoke E2E**: `python scripts/smoke_e2e.py` (起 server, 跑 parse + fetch + cancel, 验 SSE)

> **PowerShell 提醒**:本仓库在 `win32` 平台。所有命令走 `pwsh`。**不要用** `&&` / `cp -r` / `rm -rf` —
> 改用 `;` / `Copy-Item -Recurse` / `mavis-trash`。

## Project layout

```
multimedia-parsing/                  # 服务根目录
├── pyproject.toml                    # 项目元数据 + 依赖 + 入口
├── AGENTS.md                         # 本文件
├── README.md                         # API 使用文档
├── multimedia_parsing/               # main package
│   ├── __init__.py
│   ├── server.py                     # FastAPI app + entry point
│   ├── event_emitter.py              # 9 状态机 + emit 函数(适配 SSE)
│   ├── resource_fetcher/             # 智能路由 + 统一接口
│   │   ├── base.py                   # ResourceType enum + dataclass + Protocol
│   │   ├── router.py                 # 域名 → Resolver 规则表
│   │   ├── manifest.py               # ResourceParseManifest / ResourceFetchManifest
│   │   ├── run_parse.py              # 解析阶段编排
│   │   ├── run_fetch.py              # 下载/上传阶段编排
│   │   └── resolvers/
│   │       ├── video.py              # VideoResolver (通用 — 包装 video_fetcher)
│   │       ├── image.py              # ImageResolver (通用 — 包装 image_fetcher)
│   │       ├── bilibili.py           # BilibiliResolver (薄扩展 over VideoResolver)
│   │       ├── youtube.py            # YouTubeResolver (薄扩展 over VideoResolver)
│   │       ├── xiaohongshu.py        # XiaohongshuResolver (F0.1 跨类型 dispatch)
│   │       ├── audio.py              # AudioResolver (新资源类型 — 真实现)
│   │       └── model.py              # ModelResolver (新资源类型 — D2 stub)
│   ├── oss/                          # 跨包共享 OssUploader
│   │   ├── __init__.py
│   │   └── oss_uploader.py
│   ├── video_fetcher/                # yt-dlp 包装层
│   │   ├── resolver.py
│   │   ├── downloader.py
│   │   ├── manifest.py
│   │   ├── run_fetch.py
│   │   ├── storage_state_adapter.py
│   │   └── __init__.py
│   └── image_fetcher/                # gallery-dl + BS4 包装层
│       ├── resolver.py
│       ├── downloader.py
│       ├── manifest.py
│       ├── run_fetch.py
│       ├── cache.py
│       ├── convert.py
│       └── __init__.py
├── tests/                            # pytest 测试(搬运原项目)
│   ├── test_resource_fetcher_*.py
│   ├── test_video_fetcher_*.py
│   ├── test_image_fetcher_*.py
│   └── test_event_emitter.py
├── scripts/
│   └── smoke_e2e.py                  # 端到端 smoke 测试
└── docs/
    ├── api.md                        # API 端点详细文档
    └── events.md                     # SSE event payload schema
```

## Architecture

```mermaid
flowchart LR
    Client[HTTP Client] -->|POST /parse| Server[FastAPI server.py]
    Client -->|GET /events/{batch_id}| SSE[SSE stream]
    Server -->|threading.Event cancel| RunParse[resource_fetcher.run_parse]
    Server -->|threading.Event cancel| RunFetch[resource_fetcher.run_fetch]
    RunParse --> Router[router.resolve]
    Router --> VideoR[VideoResolver]
    Router --> ImageR[ImageResolver]
    VideoR --> VideoF[video_fetcher]
    ImageR --> ImageF[image_fetcher]
    RunParse -->|emit event| EventQ[asyncio.Queue per batch_id]
    RunFetch -->|emit event| EventQ
    EventQ -->|SSE| Client
```

**关键设计**:
- **FIFO event 队列**: 每个 batch_id 一个 `asyncio.Queue`,run_parse / run_fetch 线程
  push 事件 → server async loop 消费 → SSE emit
- **cancel via threading.Event**: run_parse / run_fetch 内部 cancel_event 参数
  检查 → server 收到 cancel 请求时 set
- **3-mode 下载 (D3)**: `local` / `oss` / `both` (本地 + OSS, OSS 失败降级 spec §9)
- **9 状态机 (SPEC §3.3)**: 4 非终态 (pending / logging_in / uploading / processing)
  + 5 终态 (success / draft / failed / skipped / cancelled)

## API 端点

| Method | Path | Body | 返回 |
|---|---|---|---|
| `GET` | `/health` | - | `{status: "ok", version: "0.1.0"}` |
| `POST` | `/parse` | `{batch_id?, urls: [...]}` | `{batch_id: "..."}` |
| `POST` | `/fetch` | `{mode, items: [...], download_dir?, oss?, batch_id?, cookie_file?, account_id?}` | `{batch_id: "..."}` |
| `POST` | `/cancel/{batch_id}` | - | `{cancelled: true/false}` |
| `GET` | `/events/{batch_id}` | - | SSE stream (`text/event-stream`) |
| `GET` | `/batches` | - | `[{batch_id, stage, status, total, done, ...}, ...]` |

详细 schema 见 `docs/api.md`,SSE event payload 见 `docs/events.md`。

## Code style

- **Python 3.10+** (union syntax, match-case)
- **PEP 8** + `black` formatter (line-length=88)
- **Type hints** 强制 (`from __future__ import annotations`)
- **Frozen dataclass** for immutable manifest/item types
- **Protocol + @runtime_checkable** for resolver / fetcher contracts
- **Pydantic v2** for FastAPI request/response models (mirror resource_fetcher dataclasses)
- **Async/await** for FastAPI handlers, **threading** for run_parse / run_fetch (yt-dlp is sync)
- **asyncio.Queue** for cross-thread event passing to SSE

## Test strategy

- 搬运原项目 12+ 个 `test_*.py` 文件
- 跑 `pytest -m "not slow"` 验证单测全过(目标 1512+ passed)
- 跑 `scripts/smoke_e2e.py` 验证 e2e(server 起 → 解析 → 取消 → SSE 收到)
- 保留 pre-existing baseline failure: `test_agent_framework_langgraph::test_run_agent_does_not_run_tools_the_model_did_not_choose`
  (从原项目继承,跟 resource_fetcher 无关)

## Security

- **no-auth by default** (内网服务, 信任调用方)
- 后续可加 API key middleware: `API_KEY=xxx` env var → 校验 `X-API-Key` header
- **CORS**: 允许所有 origin (内网) — 生产环境收紧
- **rate limit**: 待定 (先 functional, 不加限流)
- **input validation**: Pydantic 严格 mode (extra=forbid) 防止恶意 manifest

## Naming

- **package**: `multimedia_parsing` (root) — 不用 `mpx` 之类的简写(避免 import 混淆)
- **batch_id**: 前缀 `parse_<unix_ms>` / `fetch_<unix_ms>` (跟原 project 一致)
- **events**: type=`resource_fetch_status` 单一 type (跟原 project 统一,spec §5.1)

## 扩展新平台 / 新资源类型

项目按 `ResourceResolver` 协议 + `RESOLVER_RULES` 注册表扩展。**同事加新平台拿到
[resolver-extension skill](C:/Users/xiaoge/.minimax/skills/resolver-extension/SKILL.md)
直接照着做**,4 种模板选一种 (薄扩展 / 平台 dispatch / 新资源类型 / stub),8 步
工作流 ~30min-1.5h 收口。

### 当前已实现的 resolver

| Resolver | 类型 | 模板 | 注册域名 | 备注 |
|---|---|---|---|---|
| `VideoResolver` | video | 通用 | x.com / twitter / kuaishou / douyin / tiktok (兜底) | 包装 video_fetcher (yt-dlp) |
| `ImageResolver` | image | 通用 | weibo / douban | 包装 image_fetcher (gallery-dl + BS4) |
| `BilibiliResolver` | video | 薄扩展 (继承 Video) | bilibili.com / b23.tv | 加 `is_bilibili` meta |
| `YouTubeResolver` | video | 薄扩展 (继承 Video) | youtube.com / youtu.be | 加 `is_youtube` meta |
| `XiaohongshuResolver` | mixed | 平台 dispatch (F0.1) | xiaohongshu.com / xhslink.com | 跨 video/image 终判 |
| `AudioResolver` | audio | 新资源类型 (真实现) | soundcloud.com / snd.sc | yt-dlp audio-only + FFmpegExtractAudio |
| `ModelResolver` | model | stub (D2 决策) | (未注册) | 接口就位, 等同事扩展 huggingface/civitai |

### 同事典型场景

- **加 wechat 公众号文章图** → Template 0,只改 `router.py` 一行注册
- **加 twitter Spaces 音频** → Template 0 注册到 `AudioResolver`
- **加 微信视频号** → Template 1 (薄扩展 `VideoResolver`,加 wechat 特定 meta)
- **加 B站 收藏夹 (混合视频+图文)** → Template 2 (dispatch)
- **加 civitai 模型下载** → Template 4 (改 `ModelResolver` 即可)

**关键约束**(同事必读,详见 skill):
1. `parse()` 失败返 `[]`,不抛 — 让 `run_parse` 层发 failed 事件
2. `fetch()` 必须返 `FetchResult`,不抛 — 错误编码进 `result.error` 字段
3. `cookie_file` 传 `str(self.cookie_file) if self.cookie_file else None` (yt-dlp 不接受 Path)
4. `item_id` 格式 `{platform}_{native_id}` (OSS key / frontend 卡片都靠它)
5. parametrize 别用 `list(frozenset)` (xdist ERROR),用 `sorted(...)`



## Known limitations

- **单进程**: 当前 server 是单进程 FastAPI。run_parse / run_fetch 用后台线程,
  不能跨进程共享 batch 状态。后续如需多 worker,改用 Redis pub/sub
- **OSS 配置**: 走 env var 临时传 (Plan D6 storage settings),没持久化
- **playwright**: 需要装 chromium (`playwright install chromium`),不在默认依赖
- **单 batch 单 stage**: 一个 batch_id 只对应一个 stage (parse 或 fetch),不混合
- **9 状态 vs SSE**: SSE event payload 沿用原 project 的 resource_fetch_status type
  (前端兼容,跟 Rust emit 协议一致)

## References

- 原 project: `D:\XiaoProject\Multi-Platform Automated Release`
- 原 spec: `Multi-Platform Automated Release/docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md`
- 原 plan: `Multi-Platform Automated Release/docs/superpowers/plans/2026-09-03-universal-resource-fetch/`
- 9 状态机: `Multi-Platform Automated Release/.harness/docs/event-protocol.md`
- **Resolver 扩展 skill**: `C:\Users\xiaoge\.minimax\skills\resolver-extension\SKILL.md`
  (同事拿到这个 skill 可以快速加新平台,4 种模板 + 8 步工作流 + 7 个关键约束)
