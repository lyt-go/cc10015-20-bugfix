"""同期群群组快照与留存矩阵（V020）核心规则测试。

覆盖：
- 同一群组快照下列表筛选/周期筛选/刷新不改变留存分母与留存率；
- 工作台列表与详情页业务数据一致；
- 排序、翻页、分页定位稳定；
- 补录固定/最新两种冲突策略、独立修订版本；
- 未清残留触发事务回滚并留档；
- 重算失败沿用当前完整矩阵，重试只作用于最后确认快照；
- metric_def V020 调用链读取矩阵指标键并逐周期校验分母。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.cohort_snapshot import (  # noqa: E402
    POLICY_FIXED,
    POLICY_LATEST,
    REV_CONFIRMED,
    REV_ROLLED_BACK,
    REV_STANDBY,
    REV_SUPERSEDED,
    ResidueConflictError,
    SnapshotValidationError,
    snapshot_repo,
)
from app.services.cohort_track import CohortTrackService  # noqa: E402
from app.services.metric_def import MetricDefService  # noqa: E402
from app.seed import SEED_ROWS  # noqa: E402
from app.store import store  # noqa: E402

COHORT_ID = 1


@pytest.fixture()
def service():
    # 其他测试会清空共享内存 store，这里恢复种子数据并重置快照仓库，
    # 保证本模块测试互不依赖、可任意顺序运行。
    store._tables = {name: [dict(row) for row in rows]
                     for name, rows in SEED_ROWS.items()}
    snapshot_repo.reset()
    snapshot_repo.set_validator(MetricDefService().validate_retention_matrix)
    svc = CohortTrackService()
    yield svc
    snapshot_repo.reset()


def _matrix_row(svc, cohort_id):
    return next(
        row for row in svc.list_matrix(page=1, size=1_000)["items"]
        if row["cohort_id"] == cohort_id
    )


def test_snapshot_is_deterministic_and_filter_keeps_denominator(service):
    """同一群组条件下，筛选与刷新都不会改变矩阵分母和留存率。"""
    first = service.list_matrix(page=1, size=1_000)
    second = service.list_matrix(page=1, size=1_000)
    assert first == second

    row = _matrix_row(service, COHORT_ID)
    filtered = service.list_matrix(
        filters={"群组名称": row["群组名称"]}, page=1, size=1_000)
    assert len(filtered["items"]) == 1
    target = filtered["items"][0]
    assert target["denominator"] == row["denominator"]
    assert target["cells"] == row["cells"]
    assert target["event_boundary"] == row["event_boundary"]

    # 周期筛选只裁剪列，分母不变
    partial = service.list_matrix(periods=["P1", "P3"], page=1, size=1_000)
    partial_row = next(r for r in partial["items"] if r["cohort_id"] == COHORT_ID)
    assert [c["period"] for c in partial_row["cells"]] == ["P1", "P3"]
    assert partial_row["denominator"] == row["denominator"]


def test_detail_matches_workbench(service):
    """详情页与工作台列表必须展示相同业务数据。"""
    for row in service.list_matrix(page=1, size=1_000)["items"]:
        detail = service.get_matrix(row["cohort_id"])
        assert detail["denominator"] == row["denominator"]
        assert detail["cells"] == row["cells"]
        assert detail["event_boundary"] == row["event_boundary"]
        assert detail["metric_key"] == row["metric_key"]
        # 详情页携带成员事件与修订历史，且成员事件共用同一事件边界
        assert detail["member_events"]
        assert all(m["event_boundary"] == row["event_boundary"]
                   for m in detail["member_events"])
        assert detail["revisions"]


def test_sorting_and_pagination_are_stable(service):
    """排序翻页拼接必须等于全量排序，且无重复遗漏。"""
    page1 = [r["cohort_id"] for r in service.list_matrix(
        sort_by="P2", sort_order="desc", page=1, size=2)["items"]]
    page2 = [r["cohort_id"] for r in service.list_matrix(
        sort_by="P2", sort_order="desc", page=2, size=2)["items"]]
    full = [r["cohort_id"] for r in service.list_matrix(
        sort_by="P2", sort_order="desc", page=1, size=1_000)["items"]]
    assert page1 + page2 == full
    assert len(set(full)) == len(full)

    asc_rows = service.list_matrix(
        sort_by="denominator", sort_order="asc", page=1, size=1_000)["items"]
    denominators = [r["denominator"] for r in asc_rows]
    assert denominators == sorted(denominators)


def test_asc_and_desc_orders_are_reversed_with_stable_tie_break(service):
    """同值行按 cohort_id 升序兜底，升序结果必须恰好是降序结果的整体反转。"""
    asc = [r["cohort_id"] for r in service.list_matrix(
        sort_by="P2", sort_order="asc", page=1, size=1_000)["items"]]
    desc = [r["cohort_id"] for r in service.list_matrix(
        sort_by="P2", sort_order="desc", page=1, size=1_000)["items"]]
    assert asc == list(reversed(desc))
    # 翻页拼接与全量排序一致，定位结果稳定
    paged = []
    for page_no in (1, 2):
        paged.extend(r["cohort_id"] for r in service.list_matrix(
            sort_by="P2", sort_order="desc", page=page_no, size=2)["items"])
    assert paged == desc


def test_supplement_fixed_policy_keeps_denominator_and_marks_diff(service):
    """固定筛选前快照：现网矩阵不变，补录以独立修订留档并标注差异。"""
    before = service.get_matrix(COHORT_ID)
    service.supplement_members(COHORT_ID, 12, POLICY_FIXED, remark="补录12人")
    after = service.get_matrix(COHORT_ID)

    assert after["denominator"] == before["denominator"]
    assert after["snapshot_revision"] == before["snapshot_revision"]
    assert after["event_boundary"] == before["event_boundary"]
    diff = after["supplement_diff"]
    assert diff["added_members"] == 12
    assert diff["fixed_denominator"] == before["denominator"]
    assert diff["latest_denominator"] == before["denominator"] + 12

    statuses = [r["status"] for r in service.revision_history(COHORT_ID)]
    assert REV_STANDBY in statuses
    confirmed = snapshot_repo.confirmed(COHORT_ID)
    assert confirmed.matrix["denominator"] == before["denominator"]


def test_supplement_latest_policy_recomputes_with_new_denominator(service):
    """补录后最新快照：分母生效，受影响周期重算，事件边界保持一致。"""
    before = service.get_matrix(COHORT_ID)
    service.supplement_members(COHORT_ID, 20, POLICY_LATEST)
    after = service.get_matrix(COHORT_ID)

    assert after["denominator"] == before["denominator"] + 20
    assert after["event_boundary"] == before["event_boundary"]
    assert after["snapshot_revision"] > before["snapshot_revision"]
    assert all(cell["denominator"] == after["denominator"] for cell in after["cells"])
    assert snapshot_repo.confirmed(COHORT_ID).status == REV_CONFIRMED


def test_residue_triggers_transaction_rollback_and_archive(service):
    """V020：补录发现未清残留 → 事务回滚，现网矩阵不变，留档可核对。"""
    before = service.get_matrix(COHORT_ID)
    with pytest.raises(ResidueConflictError):
        service.supplement_members(COHORT_ID, 5, POLICY_FIXED, residue=True)

    current = service.get_matrix(COHORT_ID)
    assert current["denominator"] == before["denominator"]
    assert current["snapshot_revision"] == before["snapshot_revision"]
    rolled = [r for r in service.revision_history(COHORT_ID)
              if r["status"] == REV_ROLLED_BACK]
    assert len(rolled) == 1
    assert rolled[0]["event_boundary"] == before["event_boundary"]
    assert any(a.get("rolled_back") for a in service.audit_log())

    # 清残留后可继续补录
    result = service.clear_residue(COHORT_ID)
    assert result["action"] == "clear_residue"
    service.supplement_members(COHORT_ID, 3, POLICY_LATEST)
    assert service.get_matrix(COHORT_ID)["denominator"] == before["denominator"] + 3


def test_recompute_failure_keeps_full_matrix_and_retry_uses_last_confirmed(service):
    """重算失败沿用当前完整矩阵；恢复后重试只作用于最后确认快照。"""
    service.supplement_members(COHORT_ID, 15, POLICY_LATEST)
    stable = service.get_matrix(COHORT_ID)

    snapshot_repo.set_validator(
        lambda matrix: {"ok": False, "errors": ["模拟V020分母校验失败"],
                        "checked_periods": 0})
    with pytest.raises(SnapshotValidationError):
        service.recompute_matrix(COHORT_ID)

    current = service.get_matrix(COHORT_ID)
    assert current["denominator"] == stable["denominator"]
    assert current["snapshot_revision"] == stable["snapshot_revision"]
    assert snapshot_repo.pending(COHORT_ID) is None
    assert any(r["status"] == REV_ROLLED_BACK
               for r in service.revision_history(COHORT_ID))

    snapshot_repo.set_validator(MetricDefService().validate_retention_matrix)
    service.recompute_matrix(COHORT_ID)
    retried = service.get_matrix(COHORT_ID)
    assert retried["denominator"] == stable["denominator"]
    assert retried["cells"] == stable["cells"]
    assert retried["event_boundary"] == stable["event_boundary"]


def test_consecutive_fixed_supplements_accumulate_without_overwriting(service):
    """连续固定快照补录：现网分母不变，待并入口径累计，各修订独立留档。"""
    before = service.get_matrix(COHORT_ID)
    service.supplement_members(COHORT_ID, 5, POLICY_FIXED)
    service.supplement_members(COHORT_ID, 7, POLICY_FIXED)

    current = service.get_matrix(COHORT_ID)
    assert current["denominator"] == before["denominator"]
    diff = current["supplement_diff"]
    assert diff["added_members"] == 7  # 最新修订自身的补录数
    assert diff["latest_denominator"] == before["denominator"] + 12  # 累计待并入
    assert diff["fixed_denominator"] == before["denominator"]

    standby = [r for r in service.revision_history(COHORT_ID) if r["status"] == REV_STANDBY]
    assert [r["supplement_count"] for r in standby] == [5, 7]


def test_latest_snapshot_absorbs_prior_standby_revisions(service):
    """最新快照吸收此前待并入修订：旧 standby 变 superseded 留档，分母累计生效。"""
    before = service.get_matrix(COHORT_ID)
    service.supplement_members(COHORT_ID, 5, POLICY_FIXED)
    service.supplement_members(COHORT_ID, 7, POLICY_FIXED)
    service.supplement_members(COHORT_ID, 3, POLICY_LATEST)

    after = service.get_matrix(COHORT_ID)
    assert after["denominator"] == before["denominator"] + 15
    assert after["supplement_diff"] is None  # 已无待并入修订
    statuses = [r["status"] for r in service.revision_history(COHORT_ID)]
    assert statuses.count(REV_SUPERSEDED) == 2
    assert REV_STANDBY not in statuses
    # 留档完整可核对
    assert sum(r["supplement_count"] for r in service.revision_history(COHORT_ID)) >= 15


def test_concurrent_filter_and_supplement_keeps_filtered_matrix(service):
    """补录与列表筛选并发（固定策略）：筛选结果不变，仅追加补录差异标注。"""
    row = _matrix_row(service, COHORT_ID)
    before = service.list_matrix(
        filters={"群组名称": row["群组名称"]}, page=1, size=1_000)
    service.supplement_members(COHORT_ID, 7, POLICY_FIXED, remark="并发补录")
    after = service.list_matrix(
        filters={"群组名称": row["群组名称"]}, page=1, size=1_000)

    assert before["items"][0]["cells"] == after["items"][0]["cells"]
    assert before["items"][0]["denominator"] == after["items"][0]["denominator"]
    assert after["items"][0]["supplement_diff"]["added_members"] == 7


def test_v020_metric_def_validates_matrix_keys_and_denominators(service):
    """V020 调用链：指标定义读取矩阵指标键，逐周期校验留存分母。"""
    metric_service = MetricDefService()
    reports = metric_service.validate_cohort_matrices()
    assert len(reports) == len(store.rows("cohort_track"))
    assert all(report["ok"] for report in reports)
    assert all(report["checked_periods"] == 6 for report in reports)
    assert all(report["metric_key"] in metric_service.v020_keys()
               for report in reports)

    good_matrix = service.get_matrix(COHORT_ID)
    matrix_payload = {
        "metric_key": good_matrix["metric_key"],
        "event_boundary": good_matrix["event_boundary"],
        "denominator": good_matrix["denominator"],
        "cells": good_matrix["cells"],
    }
    assert metric_service.validate_retention_matrix(matrix_payload)["ok"]

    # 分母被筛选改变（周期分母与矩阵分母不一致）必须被拦截
    tampered = dict(matrix_payload)
    bad_cells = [dict(c) for c in matrix_payload["cells"]]
    bad_cells[0]["denominator"] = matrix_payload["denominator"] - 1
    tampered["cells"] = bad_cells
    report = metric_service.validate_retention_matrix(tampered)
    assert not report["ok"]
    assert any("不一致" in err for err in report["errors"])

    # 未注册指标键必须被拦截
    unknown = dict(matrix_payload, metric_key="METR-UNKNOWN")
    report = metric_service.validate_retention_matrix(unknown)
    assert not report["ok"]
    assert any("未在 V020" in err for err in report["errors"])


def test_create_cohort_generates_confirmed_snapshot(service):
    """新建同期群立即拥有确认快照，列表与详情可取到同一份矩阵。"""
    entry, missing = service.create_entry({
        "群组编号": "COHO-NEW",
        "群组名称": "新增群组",
        "首次行为": "注册",
    })
    assert missing == [] and entry is not None
    new_id = int(entry["id"])
    matrix = service.get_matrix(new_id)
    assert matrix["denominator"] > 0
    assert matrix["snapshot_revision"] == 1
    listed = _matrix_row(service, new_id)
    assert listed["cells"] == matrix["cells"]
    assert snapshot_repo.confirmed(new_id) is not None
