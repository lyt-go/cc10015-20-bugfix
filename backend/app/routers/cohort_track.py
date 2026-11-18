"""同期群追踪接口：同期群定义维护、留存矩阵、快照补录与 V020 重算。

约定：
- 列表与详情共用同一群组快照，矩阵接口支持稳定排序、翻页、分页与周期筛选；
- 补录生成独立修订版本；重算失败沿用当前完整矩阵，重试只作用于最后确认快照。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.cohort_snapshot import (
    PERIOD_IDS,
    ResidueConflictError,
    SnapshotValidationError,
)
from app.services.cohort_track import CohortTrackService

router = APIRouter(prefix="/api/cohort_track", tags=["同期群追踪"])
service = CohortTrackService()

LIST_FIELDS = ["群组编号", "群组名称", "首次行为", "时间基准", "追踪周期", "留存窗口", "分析指标", "对比群组"]
STATUSES = ["草稿", "待校验", "已生效", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页、排序等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


def parse_periods(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    """汇总卡片：取自已确认快照，同一群组条件刷新后数值不变。"""
    return {"metrics": service.summary()}


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按群组编号检索"),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """同期群定义列表：标准分页结构，留存矩阵走独立的 /matrix 接口。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/matrix")
def list_matrix(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    periods: str | None = Query(default=None, description="逗号分隔，如 P0,P1,P2"),
    sort_by: str = Query(default="cohort_id"),
    sort_order: str = Query(default="asc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """留存矩阵数据表：筛选/排序/翻页都基于同一群组快照，分母不随筛选变化。"""
    return service.list_matrix(
        filters=query_filters(request),
        keyword=keyword,
        status=status,
        periods=parse_periods(periods),
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        size=size,
    )


@router.get("/matrix/export")
def export_matrix(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    periods: str | None = Query(default=None),
    sort_by: str = Query(default="cohort_id"),
    sort_order: str = Query(default="asc"),
) -> StreamingResponse:
    """导出当前筛选/排序下的留存矩阵，列与页面周期选择保持一致。"""
    items = service.export_matrix(
        filters=query_filters(request), keyword=keyword, status=status,
        periods=parse_periods(periods), sort_by=sort_by, sort_order=sort_order)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    header = ["群组编号", "群组名称", "快照版本", "事件边界", "分母"]
    period_labels = items[0]["periods"] if items else list(PERIOD_IDS)
    header.extend(period_labels)
    writer.writerow(header)
    for item in items:
        rates = {cell["period"]: f'{cell["rate"]} ({cell["retained"]}/{cell["denominator"]})'
                 for cell in item["cells"]}
        writer.writerow([
            item.get("群组编号", ""), item.get("群组名称", ""),
            item["snapshot_revision"], item["event_boundary"], item["denominator"],
            *(rates.get(period, "") for period in period_labels),
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="cohort_track_matrix.csv"'})


@router.get("/audit")
def get_audit() -> dict[str, Any]:
    """V020 留档核对：快照确认、补录、回滚、重算全过程可追溯。"""
    return {"items": service.audit_log()}


@router.post("/run")
def run_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    return {"items": service.run_analysis(payload.get("values", {}) or {})}


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的同期群定义清单（不含矩阵，矩阵走 /matrix/export）。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="cohort_track.csv"'})


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="同期群定义已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/matrix")
def get_matrix(
    entry_id: int,
    periods: str | None = Query(default=None),
) -> dict[str, Any]:
    """详情页留存矩阵：与工作台列表使用同一快照、同一事件边界与同一批数据。"""
    try:
        return service.get_matrix(entry_id, parse_periods(periods))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{entry_id}/revisions")
def get_revisions(entry_id: int) -> dict[str, Any]:
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"同期群定义 {entry_id} 不存在")
    return {"items": service.revision_history(entry_id)}


@router.post("/{entry_id}/supplement")
def supplement_members(entry_id: int, payload: dict[str, Any]) -> ActionResult:
    """补录成员事件：生成独立修订版本，不覆盖筛选结果。

    - policy=fixed_snapshot：固定筛选前快照，保留原分母并标注补录差异（默认）
    - policy=latest_snapshot：采用补录后最新快照，重算受影响周期
    - residue=true：模拟补录环节发现未清残留，V020 触发事务回滚
    """
    try:
        count = int(payload.get("count", 0))
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="补录成员数必须为正整数")
    policy = str(payload.get("policy") or "fixed_snapshot")
    remark = str(payload.get("remark") or "")
    residue = bool(payload.get("residue", False))
    source = str(payload.get("source") or "supplement")
    try:
        result = service.supplement_members(
            entry_id, count, policy, remark=remark, residue=residue, source=source)
    except ResidueConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SnapshotValidationError as exc:
        return ActionResult(ok=False, message=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="补录已生成独立修订版本", entry=result)


@router.post("/{entry_id}/clear-residue")
def clear_residue(entry_id: int) -> ActionResult:
    try:
        result = service.clear_residue(entry_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ActionResult(ok=True, message=result["message"], entry=result)


@router.post("/{entry_id}/recompute")
def recompute_matrix(entry_id: int) -> ActionResult:
    """重算：先在 pending 修订上跑 V020 校验；失败回滚并沿用当前完整矩阵。"""
    try:
        result = service.recompute_matrix(entry_id)
    except SnapshotValidationError as exc:
        raise HTTPException(status_code=409, detail=f"重算失败，已沿用当前完整矩阵：{exc}") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ActionResult(ok=True, message="重算通过 V020 校验，矩阵已更新", entry=result)


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict[str, Any]:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"同期群定义 {entry_id} 不存在")
    return entry
