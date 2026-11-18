"""同期群追踪服务：同期群定义维护 + 群组快照/留存矩阵统一出口。

列表页与详情页都从本服务取数，且所有留存率都来自同一个已确认群组快照，
不再各自调用随机数，因此筛选、翻页、刷新前后留存率与分母保持一致。
"""
from __future__ import annotations

import functools
import time
from typing import Any

from app.services.cohort_snapshot import (
    PERIOD_IDS,
    POLICY_FIXED,
    VALID_POLICIES,
    ResidueConflictError,
    SnapshotValidationError,
    snapshot_repo,
)
from app.store import store

MODULE = "cohort_track"
LIST_FIELDS = ["群组编号", "群组名称", "首次行为", "时间基准", "追踪周期", "留存窗口", "分析指标", "对比群组"]
REQUIRED_FIELDS = ["群组编号", "群组名称", "首次行为"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用群组": "已停用"}
NEGATIVE_ACTIONS = ["停用群组"]

SORTABLE_COLUMNS = {"cohort_id", "群组编号", "群组名称", "denominator", *PERIOD_IDS}


class CohortTrackService:
    # ---- 同期群定义 ---------------------------------------------------

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("群组编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return [dict(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["created_at"] = time.time()
        store.rows(MODULE).append(entry)
        store.bump_version(MODULE)
        # 建群即生成首个确认快照，保证列表与详情立刻有同一份矩阵
        snapshot_repo.ensure_cohort(int(entry["id"]))
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同期群定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于同期群追踪可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.bump_version(MODULE)
        return entry, f"同期群已{action}"

    # ---- 汇总与运行分析（确定性，刷新不变） ---------------------------

    def summary(self) -> list[dict[str, Any]]:
        snapshot_repo.ensure_all()
        rows = store.rows(MODULE)
        boundaries = [snapshot_repo.ensure_cohort(int(r["id"])).event_boundary for r in rows]
        rates = [
            cell["rate"]
            for row in rows
            for cell in snapshot_repo.ensure_cohort(int(row["id"])).matrix["cells"]
        ]
        latest_rate = round(sum(rates) / len(rates), 3) if rates else 0.0
        abnormal = sum(1 for row in rows if row.get("abnormal"))
        return [
            {"label": "覆盖数据源", "value": len(set(boundaries)), "trend": len(rows)},
            {"label": "最新指标值", "value": latest_rate, "trend": 2},
            {"label": "异常记录", "value": abnormal, "trend": -1 if abnormal else 0},
        ]

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """保留运行分析形态（KPI/柱状图/数据表共用），数据取自已确认快照。"""
        count = int(params.get("样本范围", params.get("并行数量", "6")) or "6")
        snapshot_repo.ensure_all()
        rows = store.rows(MODULE)[:count]
        results: list[dict[str, Any]] = []
        for idx, row in enumerate(rows):
            revision = snapshot_repo.ensure_cohort(int(row["id"]))
            first_cell = revision.matrix["cells"][0]
            last_cell = revision.matrix["cells"][-1]
            results.append({
                "id": row["id"],
                "分组": row.get("群组名称") or f"分组{idx + 1}",
                "指标值": last_cell["rate"],
                "对比基准": first_cell["rate"],
                "差异": round(last_cell["rate"] - first_cell["rate"], 3),
            })
        return results

    # ---- 留存矩阵：列表与详情唯一数据源 -------------------------------

    def list_matrix(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        periods: list[str] | None = None,
        sort_by: str = "cohort_id",
        sort_order: str = "asc",
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        snapshot_repo.ensure_all()
        rows, total_cohorts = self.list_entries(
            filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)

        selected_periods = self._normalize_periods(periods)
        matrix_rows = [snapshot_repo.matrix_row(row) for row in rows]
        # 周期筛选只裁剪展示列，分母与留存率来自同一快照，不随筛选改变
        for matrix_row in matrix_rows:
            matrix_row["cells"] = [
                cell for cell in matrix_row["cells"] if cell["period"] in selected_periods
            ]
            matrix_row["periods"] = list(selected_periods)

        self._sort_matrix(matrix_rows, sort_by, sort_order)
        total = len(matrix_rows)
        start = (page - 1) * size
        page_rows = matrix_rows[start:start + size]
        return {
            "items": page_rows,
            "total": total,
            "page": page,
            "size": size,
            "total_cohorts": total_cohorts,
            "periods": list(selected_periods),
            "sort_by": sort_by,
            "sort_order": sort_order,
        }

    def get_matrix(self, entry_id: int, periods: list[str] | None = None) -> dict[str, Any]:
        entry = self.get_entry(entry_id)
        if entry is None:
            raise KeyError(f"同期群定义 {entry_id} 不存在")
        selected_periods = self._normalize_periods(periods)
        row = snapshot_repo.matrix_row(entry)
        row["cells"] = [cell for cell in row["cells"] if cell["period"] in selected_periods]
        row["periods"] = list(selected_periods)
        # 详情页同时返回成员事件与修订历史，业务数据与工作台完全一致
        revision = snapshot_repo.ensure_cohort(entry_id)
        row["member_events"] = [member.to_dict() for member in revision.members]
        row["revisions"] = snapshot_repo.history(entry_id)
        return row

    def export_matrix(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        periods: list[str] | None = None,
        sort_by: str = "cohort_id",
        sort_order: str = "asc",
    ) -> list[dict[str, Any]]:
        payload = self.list_matrix(
            filters=filters, keyword=keyword, status=status, periods=periods,
            sort_by=sort_by, sort_order=sort_order, page=1, size=1_000_000)
        return payload["items"]

    # ---- 补录 / 重算 / 留档 -------------------------------------------

    def supplement_members(
        self,
        entry_id: int,
        count: int,
        policy: str | None,
        *,
        remark: str = "",
        residue: bool = False,
        source: str = "supplement",
    ) -> dict[str, Any]:
        chosen = policy if policy in VALID_POLICIES else POLICY_FIXED
        revision = snapshot_repo.supplement(
            entry_id, count, chosen, remark=remark, residue=residue, source=source)
        return {
            "ok": True,
            "cohort_id": entry_id,
            "revision": revision.to_archive(),
            "matrix": self.get_matrix(entry_id),
            "policy": chosen,
        }

    def recompute_matrix(self, entry_id: int) -> dict[str, Any]:
        revision = snapshot_repo.recompute(entry_id)
        return {
            "ok": True,
            "cohort_id": entry_id,
            "revision": revision.to_archive(),
            "matrix": self.get_matrix(entry_id),
        }

    def clear_residue(self, entry_id: int) -> dict[str, Any]:
        return snapshot_repo.clear_residue(entry_id)

    def revision_history(self, entry_id: int) -> list[dict[str, Any]]:
        return snapshot_repo.history(entry_id)

    def audit_log(self) -> list[dict[str, Any]]:
        return snapshot_repo.audit_log()

    # ---- 内部工具 -----------------------------------------------------

    @staticmethod
    def _normalize_periods(periods: list[str] | None) -> list[str]:
        if not periods:
            return list(PERIOD_IDS)
        selected = [period for period in periods if period in PERIOD_IDS]
        # 去重并保持标准周期顺序，翻页/排序结果稳定
        seen: set[str] = set()
        ordered = []
        for period in PERIOD_IDS:
            if period in selected and period not in seen:
                ordered.append(period)
                seen.add(period)
        return ordered or list(PERIOD_IDS)

    @staticmethod
    def _sort_matrix(rows: list[dict[str, Any]], sort_by: str, sort_order: str) -> None:
        column = sort_by if sort_by in SORTABLE_COLUMNS else "cohort_id"
        reverse = sort_order == "desc"

        def primary_key(row: dict[str, Any]) -> Any:
            if column in PERIOD_IDS:
                cell = next((c for c in row["cells"] if c["period"] == column), None)
                return cell["rate"] if cell else -1.0
            if column == "denominator":
                return row["denominator"]
            if column in ("群组编号", "群组名称"):
                return str(row.get(column, ""))
            return row["cohort_id"]

        # 复合排序：主列按 sort_order 决定方向，cohort_id 始终升序兜底。
        # 这样同值行在升序/降序下相对顺序一致，翻页定位不漂移，
        # 且升序结果恰好是降序结果的整体反转。
        def comparator(left: dict[str, Any], right: dict[str, Any]) -> int:
            lv, rv = primary_key(left), primary_key(right)
            if lv != rv:
                result = -1 if lv < rv else 1
                return -result if reverse else result
            return (left["cohort_id"] > right["cohort_id"]) - (left["cohort_id"] < right["cohort_id"])

        rows.sort(key=functools.cmp_to_key(comparator))


__all__ = [
    "CohortTrackService",
    "ResidueConflictError",
    "SnapshotValidationError",
]
