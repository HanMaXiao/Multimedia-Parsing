"""tests/conftest.py — pytest 配置 + sys.modules 注入.

解决的问题:
  原 publisher 项目用 `import event_emitter` (cwd-based pythonpath=.)。
  新项目 event_emitter 在 `multimedia_parsing.event_emitter` 包内, 顶层
  `import event_emitter` 找不到。

修法: 在 conftest 启动时, 把 `multimedia_parsing.event_emitter` 同步注册到
`sys.modules["event_emitter"]` 别名, 让所有 `import event_emitter` 解析到
multimedia_parsing.event_emitter。

pytest 启动 conftest 在 import 测试文件之前, 所以能保证 local import
`import event_emitter` 找到别名。

tradeoff: 跟其他独立 module 冲突时, 别名会被覆盖 (实际项目里没冲突).
"""

import sys

import multimedia_parsing.event_emitter as _event_emitter

# 注册别名, 让 `import event_emitter` 在测试代码里能解析
sys.modules.setdefault("event_emitter", _event_emitter)
