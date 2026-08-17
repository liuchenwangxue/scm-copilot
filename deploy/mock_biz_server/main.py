"""mock 业务系统（W19 Day2，W23 Day6 随 SCM Copilot 部署复制）：接口契约真实 + 可注入故障。

- FastAPI，默认端口 8794（支持 --port / PORT 环境变量覆盖，测试脚本用随机端口）
- 接口：订单列表/详情、更新金额交期、取消、报表（库存/对账）
- 幂等：Idempotency-Key 头 + 内存存储，同 key 同路径重复请求返回首次成功结果
- 故障注入：BIZ_FAIL_RATE / BIZ_LATENCY_MS / BIZ_500_MODE / BIZ_429_MODE（见 faults.py）

运行：python mock_biz_server\\main.py  （或带 --port 8794）
"""
import argparse
import os
import sys
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from faults import maybe_latency, should_fail

app = FastAPI(title="mock 业务系统", version="biz-v1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---- 统一错误体：{ "error": { "code", "message", "detail" } } ----
class ApiError(Exception):
    def __init__(self, code: int, message: str, detail=None):
        self.code = code
        self.message = message
        self.detail = detail


@app.exception_handler(ApiError)
async def _handle_api_error(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.code,
        content={"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
    )


# ---- 幂等存储：{ "METHOD:path:key": (status_code, body) }，仅缓存 2xx 成功结果 ----
_IDEM_STORE: dict[str, tuple[int, dict]] = {}


def _apply_fault(path: str) -> None:
    """故障注入检查（在幂等命中检查之后调用：幂等命中不触发故障——不重复执行）。"""
    maybe_latency()
    kind = should_fail(path)
    if kind == "429":
        raise ApiError(429, "rate limited (fault injected)")
    if kind == "500":
        raise ApiError(500, "internal error (fault injected)")


def _idem_key(request: Request, method: str) -> str:
    """写操作幂等键：HEADER Idempotency-Key（Agent 侧生成，契约强制缺失 → 400）。"""
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise ApiError(400, "missing Idempotency-Key header")
    return f"{method}:{request.url.path}:{key}"


def _idem_hit(store_key: str):
    """幂等命中：同 key 重复请求直接返回首次成功结果（不重复执行）。"""
    cached = _IDEM_STORE.get(store_key)
    if cached is not None:
        return JSONResponse(status_code=cached[0], content=cached[1])
    return None


def _idem_save(store_key: str, status_code: int, body: dict) -> None:
    """仅缓存 2xx 成功结果；失败响应不缓存（同 key 可安全重试）。"""
    if 200 <= status_code < 300:
        _IDEM_STORE[store_key] = (status_code, body)


# ================= 接口 =================

@app.get("/health")
def health():
    return {"status": "ok", "service": "mock_biz_server"}


@app.get("/api/v1/orders")
def list_orders(status: str | None = None,
                page: int = Query(1, ge=1),
                page_size: int = Query(10, ge=1, le=100)):
    path = "/api/v1/orders"
    _apply_fault(path)
    if status is not None and status not in db.ORDER_STATUS:
        raise ApiError(400, f"invalid status: {status}")
    all_items = db.list_orders(status)
    total = len(all_items)
    total_pages = (total + page_size - 1) // page_size if total else 0
    start = (page - 1) * page_size
    items = all_items[start:start + page_size]
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "total_pages": total_pages}


@app.get("/api/v1/orders/{order_id}")
def get_order(order_id: str):
    _apply_fault(f"/api/v1/orders/{order_id}")
    order = db.get_order(order_id)
    if order is None:
        raise ApiError(404, f"order not found: {order_id}")
    return order


@app.patch("/api/v1/orders/{order_id}")
async def patch_order(order_id: str, request: Request):
    store_key = _idem_key(request, "PATCH")
    hit = _idem_hit(store_key)
    if hit is not None:
        return hit
    _apply_fault(f"/api/v1/orders/{order_id}")

    order = db.get_order(order_id)
    if order is None:
        raise ApiError(404, f"order not found: {order_id}")

    try:
        body = await request.json()
    except Exception:
        raise ApiError(400, "invalid JSON body")

    amount = body.get("amount")
    delivery_date = body.get("delivery_date")
    if amount is None and delivery_date is None:
        raise ApiError(400, "no updatable field: provide amount or delivery_date")

    if amount is not None:
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
            raise ApiError(400, f"invalid amount: {amount} (must be > 0)")
    if delivery_date is not None:
        if not _valid_date(delivery_date):
            raise ApiError(400, f"invalid delivery_date: {delivery_date} (expect YYYY-MM-DD)")

    # 状态机：仅 draft/approving/ordered 可改
    if order["status"] not in db.EDITABLE_STATUSES:
        raise ApiError(409, f"order {order_id} is {order['status']}, cannot modify")

    updated = db.update_order(order_id, amount=amount, delivery_date=delivery_date)
    _idem_save(store_key, 200, updated)
    return updated


@app.post("/api/v1/orders/{order_id}/cancel")
async def cancel_order(order_id: str, request: Request):
    store_key = _idem_key(request, "POST")
    hit = _idem_hit(store_key)
    if hit is not None:
        return hit
    _apply_fault(f"/api/v1/orders/{order_id}/cancel")

    order = db.get_order(order_id)
    if order is None:
        raise ApiError(404, f"order not found: {order_id}")

    try:
        body = await request.json()
    except Exception:
        raise ApiError(400, "invalid JSON body")
    reason = (body or {}).get("reason")
    if not reason or not str(reason).strip():
        raise ApiError(400, "reason is required")

    # 状态机：仅 draft/approving/ordered 可取消；shipped 需退货流程 / closed 终态
    if order["status"] not in db.CANCELLABLE_STATUSES:
        raise ApiError(409, f"order {order_id} is {order['status']}, cannot cancel")

    updated = db.cancel_order(order_id)
    _idem_save(store_key, 200, updated)
    return updated


@app.get("/api/v1/reports/{report_type}")
def report(report_type: str,
           from_: str | None = Query(None, alias="from"),
           to: str | None = Query(None)):
    _apply_fault(f"/api/v1/reports/{report_type}")

    if report_type not in ("inventory", "reconciliation"):
        raise ApiError(404, f"report type not found: {report_type}")
    for d in (from_, to):
        if d is not None and not _valid_date(d):
            raise ApiError(400, f"invalid date: {d} (expect YYYY-MM-DD)")
    if from_ and to and from_ > to:
        raise ApiError(400, "from must be <= to")

    from datetime import datetime, timezone
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if report_type == "inventory":
        rows = db.list_inventory()
        low = [r for r in rows if r["low_stock"]]
        return {
            "report_type": "inventory",
            "generated_at": generated_at,
            "summary": {"total_items": len(rows), "low_stock": len(low),
                        "low_stock_total_qty": sum(r["qty"] for r in low)},
            "rows": rows,
        }

    rows, order_count, total_amount = db.reconciliation(from_, to)
    return {
        "report_type": "reconciliation",
        "generated_at": generated_at,
        "period": {"from": from_, "to": to},
        "summary": {"order_count": order_count, "total_amount": total_amount},
        "rows": rows,
    }


def _valid_date(s: str) -> bool:
    from datetime import date
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def main():
    parser = argparse.ArgumentParser(description="mock 业务系统（W19 Day2）")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8794")))
    args = parser.parse_args()
    db.init_data()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
