"""★ 自研 Runtime 包（W28-D6，B5 PoC）——最小 agent loop 内核。

定位（ADR-011）：
- 框架抽象的税：LangGraph 的 checkpointer/interrupt 生态对 kb/ops 是主场（保留）；
- 自研内核的价值：data 域图单轮无状态、无 interrupt——证明"我随时能退出框架"；
- 双形态内核：图节点循环（data 域用）+ 原生 tool-calling 循环（w11/w12 路径回归）。
"""
