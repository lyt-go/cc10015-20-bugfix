"""指标定义业务规则：状态流转、字段校验与筛选口径都收在这里。

V020 跨模块联动：指标定义模块读取同期群追踪留存矩阵中的指标键，并对每个
留存周期的分母做校验，校验逻辑只依赖矩阵载荷本身，避免与快照引擎循环导入。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "metric_def"
LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []

# V020：留存矩阵允许注册的指标键（同期群快照引擎产出的键必须在此登记）
V020_METRIC_KEYS = {"METR-V020-RET", "METR-V020-DEN"}
# 校验留存率允许的浮点误差
RATE_TOLERANCE = 1e-3


class MetricDefService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("指标编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标定义可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"

    # ---- V020 跨模块联动 ----------------------------------------------

    def v020_keys(self) -> list[str]:
        """V020 已注册指标键：内置键 + 指标定义表里带 V020 口径版本的生效键。"""
        keys = set(V020_METRIC_KEYS)
        for row in store.rows(MODULE):
            if row.get("status") == "已生效" and "V020" in str(row.get("口径版本", "")):
                keys.add(str(row.get("指标编号")))
        return sorted(keys)

    def validate_retention_matrix(self, matrix: dict[str, Any]) -> dict[str, Any]:
        """读取留存矩阵的指标键，并逐周期校验留存分母。

        校验项：
        1. 矩阵指标键已在 V020 注册；
        2. 每个周期都有分母且分母为正，并与矩阵级分母一致（筛选不能改变分母）；
        3. 留存人数不超过分母；
        4. rate 与 retained/denominator 重算结果一致。
        """
        errors: list[str] = []
        metric_key = matrix.get("metric_key")
        if not metric_key:
            errors.append("矩阵缺少指标键 metric_key")
        elif metric_key not in self.v020_keys():
            errors.append(f"指标键 {metric_key} 未在 V020 指标定义中注册")

        cells = matrix.get("cells") or []
        if not cells:
            errors.append("矩阵没有任何留存周期数据")
        matrix_denominator = matrix.get("denominator")
        checked = 0
        for cell in cells:
            period = cell.get("period", "?")
            denominator = cell.get("denominator")
            retained = cell.get("retained")
            rate = cell.get("rate")
            if not isinstance(denominator, int) or denominator <= 0:
                errors.append(f"周期 {period} 留存分母无效：{denominator}")
                continue
            if isinstance(matrix_denominator, int) and denominator != matrix_denominator:
                errors.append(
                    f"周期 {period} 分母 {denominator} 与矩阵分母 {matrix_denominator} 不一致，"
                    "疑似筛选改变了分母")
            if not isinstance(retained, int) or retained < 0 or retained > denominator:
                errors.append(f"周期 {period} 留存人数 {retained} 超出分母 {denominator}")
                continue
            if not isinstance(rate, (int, float)):
                errors.append(f"周期 {period} 留存率缺失")
            elif abs(rate - retained / denominator) > RATE_TOLERANCE:
                errors.append(
                    f"周期 {period} 留存率 {rate} 与 {retained}/{denominator} 重算结果不一致")
            checked += 1

        # 事件边界必须存在：群组快照与留存周期共用同一事件边界
        if not isinstance(matrix.get("event_boundary"), int):
            errors.append("矩阵缺少事件边界 event_boundary")
        return {
            "ok": not errors,
            "metric_key": metric_key,
            "checked_periods": checked,
            "denominator": matrix_denominator,
            "errors": errors,
        }

    def validate_cohort_matrices(self) -> list[dict[str, Any]]:
        """V020 调用链：批量校验同期群追踪当前确认矩阵。"""
        from app.services.cohort_track import CohortTrackService  # 延迟导入避免循环依赖

        cohort_service = CohortTrackService()
        payload = cohort_service.list_matrix(page=1, size=1_000_000)
        reports = []
        for row in payload["items"]:
            matrix = {
                "metric_key": row["metric_key"],
                "event_boundary": row["event_boundary"],
                "denominator": row["denominator"],
                "cells": row["cells"],
            }
            report = self.validate_retention_matrix(matrix)
            report["cohort_id"] = row["cohort_id"]
            reports.append(report)
        return reports
