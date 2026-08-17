"""知识问答域（kb）——由 stage3-project-a 迁入（W23 Day4）。

承载：RAG 问答链路（混合检索/生成校验/CRAG/语义路由/语义缓存）+ 反馈闭环。
import 纪律：域内模块只依赖本域（app.domains.kb.*）与共享层（app.shared.*），
不 import 其他域（ADR-01 模块化单体边界）。
"""
