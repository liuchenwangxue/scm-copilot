"""SCM Copilot 共享层（W23 Day4 双域并入）——llm / rag / reliability / obs 公共模块。

设计：shared 只依赖 `app.shared.config` 与标准库/第三方库，不 import 任何域模块，
保证模块化单体中"域间不跨域 import 内部模块"的边界纪律（ADR-01）。
"""
