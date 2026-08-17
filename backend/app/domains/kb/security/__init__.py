"""kb 域安全模块（W23 Day4 平台化后收编）。

平台化说明：
- JWT 认证 / RBAC / 审计 / 限流 已统一到平台基座（app.platform.auth / rbac / audit，
  Day3 落地），本域不再保留双份实现（手册 Day4"冲突清理"）。
- 本域保留 kb 特有的输入防护：规则消毒（InputSanitizer）+ 模型层注入判断（ModelGuard）。
"""
from .input_sanitizer import InputSanitizer  # noqa: F401
from .model_guard import ModelGuard  # noqa: F401

__all__ = ["InputSanitizer", "ModelGuard"]
