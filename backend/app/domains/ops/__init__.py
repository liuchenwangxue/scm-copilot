"""业务操作域（ops）——由 stage3-project-b 迁入（W23 Day4）。

承载：业务 Agent（意图识别/工具链/HITL 审批/报表）+ 可靠层（幂等/熔断/缓存/预算）。
import 纪律：域内模块只依赖本域（app.domains.ops.*）与共享层（app.shared.*），
不 import 其他域（ADR-01 模块化单体边界）。
"""
