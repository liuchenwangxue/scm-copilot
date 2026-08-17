"""ops 域安全模块（W23 Day4 平台化后收编）。

平台化说明：
- JWT 认证 / RBAC / 限流 已统一到平台基座（app.platform.auth / rbac，Day3 落地），
  本域不再保留双份实现（手册 Day4"冲突清理"）。
- 本域保留业务特有组件：高危审批 HITL（ApprovalService）+ 业务事件审计（AuditLogger，
  文件级 JSON lines；HTTP 级审计由平台中间件统一落 audit_logs）。
"""
from .approval import ApprovalService  # noqa: F401
from .audit import AuditLogger  # noqa: F401

__all__ = ["ApprovalService", "AuditLogger"]

