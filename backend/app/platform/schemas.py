"""平台基座 pydantic 模型（W23 Day3 认证链路）。

设计要点（对应《02》4 节 API 一览）：
- LoginIn：登录请求体（username + password，bcrypt 校验前限制长度防 silent truncate）
- TokenOut：双令牌返回体（15min access + 24h refresh）
- RefreshIn：refresh 请求体
- UserOut：用户信息视图（供受保护端点回显当前用户）
"""

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    """登录请求体。

    密码长度限制在 bcrypt 72 字节输入上限内（手册坑：超长会 silent truncate，
    登录校验前先拒绝，避免"短密码能过、长密码被截断成同一 hash"的时序风险）。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=72)


class TokenOut(BaseModel):
    """双令牌返回：短效 access + 长效 refresh（泄露窗口 vs 体验的平衡）。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access 有效期秒数，供前端定时刷新


class RefreshIn(BaseModel):
    """refresh 请求体。"""

    refresh_token: str


class UserOut(BaseModel):
    """受保护端点回显当前用户（权限来自 JWT claims，不查库）。"""

    id: int
    username: str
    tenant_id: str
    permissions: list[str]
    model_config = {"from_attributes": True}
