"""同期群群组快照与留存矩阵引擎（V020）。

核心约束：
- 群组快照（成员事件）与留存周期共用同一个「事件边界」，边界在快照构建时一次性
  确定，列表筛选、周期筛选都不会再改变分母。
- 成员事件与留存率全部由确定性随机源生成：同一群组、同一修订版本，无论调用多少次、
  经过什么筛选，矩阵完全一致，不再出现列表与矩阵两次随机结果对不上的问题。
- 补录成员生成独立修订版本，不覆盖已确认快照；重算先在 pending 修订上跑 V020 校验，
  失败则丢弃 pending、沿用当前完整矩阵（事务回滚），全过程留档可核对。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.store import store

COHORT_MODULE = "cohort_track"
# V020 指标键，同时在 metric_def 服务里登记为内置生效键
METRIC_KEY_RETENTION = "METR-V020-RET"
METRIC_KEY_DENOMINATOR = "METR-V020-DEN"

PERIOD_COUNT = 6
PERIOD_LABELS = [f"第{idx}周期" for idx in range(PERIOD_COUNT)]
PERIOD_IDS = [f"P{idx}" for idx in range(PERIOD_COUNT)]
RETENTION_BASE = 0.94
RETENTION_DECAY = 0.07
MIN_RETENTION = 0.35
DEFAULT_MEMBER_BASE = 80
MEMBER_CAP = 240

# 修订版本状态
REV_CONFIRMED = "confirmed"      # 已确认，矩阵对外可见
REV_PENDING = "pending"          # 重算中，尚未通过 V020 校验
REV_ROLLED_BACK = "rolled_back"  # 事务回滚，仅留档
REV_STANDBY = "standby"          # 补录独立修订（固定快照策略下不覆盖现网）
REV_SUPERSEDED = "superseded"    # 待并入修订被后续最新快照吸收，仅留档

# 补录与筛选并发时的版本冲突策略
POLICY_FIXED = "fixed_snapshot"   # 固定筛选前快照：保留筛选前分母，标注补录差异
POLICY_LATEST = "latest_snapshot"  # 补录后最新快照：使用新分母并重算受影响周期
VALID_POLICIES = {POLICY_FIXED, POLICY_LATEST}


class ResidueConflictError(RuntimeError):
    """处理矩阵时发现未清残留，V020 要求触发事务回滚。"""


class SnapshotValidationError(RuntimeError):
    """重算结果未通过 V020 校验，pending 修订必须丢弃。"""


@dataclass
class MemberEvent:
    """单个同期群成员在事件边界内的行为事件。"""

    user_id: str
    active_periods: list[int]
    event_boundary: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "active_periods": list(self.active_periods),
            "event_boundary": self.event_boundary,
        }


@dataclass
class SnapshotRevision:
    """群组的一个不可变快照修订版本。"""

    revision: int
    cohort_id: int
    status: str
    source: str
    event_boundary: int
    metric_key: str
    members: list[MemberEvent]
    matrix: dict[str, Any]
    parent_revision: int | None = None
    remark: str = ""
    residue: bool = False
    supplement_count: int = 0
    created_at: float = field(default_factory=time.time)
    confirmed_at: float | None = None

    def to_archive(self) -> dict[str, Any]:
        """留档视图：包含成员事件与完整矩阵，供 V020 核对。"""
        return {
            "revision": self.revision,
            "cohort_id": self.cohort_id,
            "status": self.status,
            "source": self.source,
            "event_boundary": self.event_boundary,
            "metric_key": self.metric_key,
            "parent_revision": self.parent_revision,
            "remark": self.remark,
            "residue": self.residue,
            "supplement_count": self.supplement_count,
            "member_count": len(self.members),
            "members": [m.to_dict() for m in self.members],
            "matrix": self.matrix,
            "created_at": self.created_at,
            "confirmed_at": self.confirmed_at,
        }


def _stable_seed(cohort_id: int, boundary: int, tag: str) -> str:
    # 种子只依赖群组与事件边界：同一边界下任何修订重建都得到相同成员事件，
    # 补录仅在尾部追加成员，重算则逐成员复现，矩阵因此可稳定复现。
    return f"cohort-snapshot|cohort={cohort_id}|boundary={boundary}|{tag}"


def make_event_boundary(cohort_id: int, revision: int = 1) -> int:
    """事件边界：快照与留存周期共用的唯一序数日边界。

    边界只由群组决定，保证同一群组下任何时刻重建结果一致。
    """
    rng = random.Random(f"cohort-snapshot|boundary-init|cohort={cohort_id}")
    return 1000 + rng.randrange(0, 900)


def _retention_curve(snapshot_rng: random.Random) -> list[float]:
    jitter = snapshot_rng.uniform(-0.02, 0.02)
    return [
        round(max(MIN_RETENTION, RETENTION_BASE - idx * RETENTION_DECAY + jitter), 4)
        for idx in range(PERIOD_COUNT)
    ]


def build_snapshot(
    cohort_id: int,
    revision: int,
    *,
    source: str,
    extra_members: int = 0,
    parent: SnapshotRevision | None = None,
    residue: bool = False,
) -> SnapshotRevision:
    """按同一事件边界构建成员事件与留存矩阵。

    成员是否纳入、各周期事件是否落在边界内，都使用本函数算出的同一个
    ``event_boundary``，从根上避免「快照一套边界、周期另一套边界」。

    事件边界是群组属性：同一群组的所有修订（补录、重算）都沿用首个快照
    确定的边界，只有群组的第一个修订才会新建边界，保证快照与留存周期始终
    共用同一事件边界，分母变化只来自补录成员。
    """
    boundary = (
        parent.event_boundary
        if parent is not None
        else make_event_boundary(cohort_id, revision)
    )
    # 分母由事件边界确定性推导，补录人数在其后追加；筛选不会触达这里。
    base_size = DEFAULT_MEMBER_BASE + (boundary % 5) * 10
    # 重算只重算成员事件、不改分母：直接继承父修订的成员规模
    if parent is not None and source == "recompute":
        size = parent.matrix["denominator"]
    else:
        size = min(MEMBER_CAP, base_size + max(0, extra_members))

    # 曲线与成员种子都只依赖（群组、事件边界），重算/补录/初始构建在同一边界下
    # 复现完全一致的成员事件与留存率。
    snapshot_rng = random.Random(_stable_seed(cohort_id, boundary, "curve"))
    curve = _retention_curve(snapshot_rng)

    members: list[MemberEvent] = []
    retained_by_period = [0] * PERIOD_COUNT
    for idx in range(size):
        member_rng = random.Random(_stable_seed(cohort_id, boundary, f"member={idx}"))
        # 首次行为固定落在第 0 周期，且必须在事件边界内才会进入群组
        active = [0]
        for period in range(1, PERIOD_COUNT):
            if member_rng.random() < curve[period]:
                active.append(period)
        for period in active:
            retained_by_period[period] += 1
        # 成员身份由群组、事件边界、成员序号决定，跨修订稳定，重算可复现
        members.append(MemberEvent(
            user_id=f"U{cohort_id:03d}-B{boundary}-{idx:04d}",
            active_periods=active,
            event_boundary=boundary,
        ))

    cells = []
    for period in range(PERIOD_COUNT):
        retained = retained_by_period[period]
        cells.append({
            "period": PERIOD_IDS[period],
            "label": PERIOD_LABELS[period],
            "denominator": size,
            "retained": retained,
            "rate": round(retained / size, 4) if size else 0.0,
        })
    matrix = {
        "metric_key": METRIC_KEY_RETENTION,
        "event_boundary": boundary,
        "periods": list(PERIOD_IDS),
        "denominator": size,
        "cells": cells,
    }
    return SnapshotRevision(
        revision=revision,
        cohort_id=cohort_id,
        status=REV_PENDING,
        source=source,
        event_boundary=boundary,
        metric_key=METRIC_KEY_RETENTION,
        members=members,
        matrix=matrix,
        residue=residue,
        supplement_count=max(0, extra_members),
    )


class CohortSnapshotRepository:
    """内存修订版本库：管理每个群组的快照修订、补录、重算与留档。"""

    def __init__(self, validator: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self._revisions: dict[int, list[SnapshotRevision]] = {}
        self._audit: list[dict[str, Any]] = []
        # V020 校验器延迟注入，避免 cohort_snapshot 与 metric_def 循环导入
        self._validator = validator

    def reset(self) -> None:
        self._revisions.clear()
        self._audit.clear()

    def set_validator(self, validator: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._validator = validator

    # ---- 初始化与读取 -------------------------------------------------

    def ensure_cohort(self, cohort_id: int) -> SnapshotRevision:
        existing = self._revisions.get(cohort_id)
        if not existing:
            revision = build_snapshot(cohort_id, 1, source="initial")
            revision.status = REV_CONFIRMED
            revision.confirmed_at = time.time()
            self._revisions[cohort_id] = [revision]
            self._audit.append(self._audit_entry(revision, "初始快照已确认"))
            return revision
        return self.confirmed(cohort_id) or existing[0]

    def ensure_all(self) -> None:
        for row in store.rows(COHORT_MODULE):
            self.ensure_cohort(int(row["id"]))

    def confirmed(self, cohort_id: int) -> SnapshotRevision | None:
        for revision in reversed(self._revisions.get(cohort_id, [])):
            if revision.status == REV_CONFIRMED:
                return revision
        return None

    def pending(self, cohort_id: int) -> SnapshotRevision | None:
        for revision in reversed(self._revisions.get(cohort_id, [])):
            if revision.status == REV_PENDING:
                return revision
        return None

    def history(self, cohort_id: int) -> list[dict[str, Any]]:
        self.ensure_cohort(cohort_id)
        return [revision.to_archive() for revision in self._revisions.get(cohort_id, [])]

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit)

    # ---- 矩阵组装（列表与详情共用） -----------------------------------

    def matrix_row(self, cohort_row: dict[str, Any]) -> dict[str, Any]:
        cohort_id = int(cohort_row["id"])
        revision = self.ensure_cohort(cohort_id)
        standby = self._standby_diff(cohort_id)
        return {
            "cohort_id": cohort_id,
            "群组编号": cohort_row.get("群组编号"),
            "群组名称": cohort_row.get("群组名称"),
            "首次行为": cohort_row.get("首次行为"),
            "时间基准": cohort_row.get("时间基准"),
            "snapshot_revision": revision.revision,
            "event_boundary": revision.event_boundary,
            "metric_key": revision.metric_key,
            "denominator": revision.matrix["denominator"],
            "cells": [dict(cell) for cell in revision.matrix["cells"]],
            "supplement_diff": standby,
        }

    def _standby_diff(self, cohort_id: int) -> dict[str, Any] | None:
        confirmed = self.confirmed(cohort_id)
        for revision in reversed(self._revisions.get(cohort_id, [])):
            if revision.status == REV_STANDBY:
                return {
                    "standby_revision": revision.revision,
                    "added_members": revision.supplement_count,
                    "fixed_denominator": confirmed.matrix["denominator"] if confirmed else None,
                    "latest_denominator": revision.matrix["denominator"],
                    "remark": revision.remark,
                }
        return None

    # ---- 补录（成员事件修订） -----------------------------------------

    def supplement(
        self,
        cohort_id: int,
        count: int,
        policy: str,
        *,
        remark: str = "",
        residue: bool = False,
        source: str = "supplement",
    ) -> SnapshotRevision:
        cohort = store.find(COHORT_MODULE, cohort_id)
        if cohort is None:
            raise KeyError(f"同期群定义 {cohort_id} 不存在")
        if policy not in VALID_POLICIES:
            raise ValueError(f"未知快照冲突策略：{policy}")
        if count <= 0:
            raise ValueError("补录成员数必须为正整数")
        if self.pending(cohort_id) is not None:
            raise SnapshotValidationError("群组存在重算中的修订，请先完成或回滚后再补录")

        # V020：补录环节发现未清残留 → 事务回滚。回滚修订不生效，只做留档，
        # 因此以 rolled_back 状态写入修订历史与审计日志，现网确认矩阵保持不变。
        if residue:
            base = self.ensure_cohort(cohort_id)
            cumulative = self._pending_supplement_total(cohort_id) + count
            failed = build_snapshot(
                cohort_id, self._next_revision(cohort_id),
                source=source, extra_members=cumulative, residue=True, parent=base,
            )
            failed.supplement_count = count
            failed.status = REV_ROLLED_BACK
            failed.remark = remark or "补录时发现未清残留"
            self._revisions.setdefault(cohort_id, []).append(failed)
            self._audit.append(self._audit_entry(
                failed, "V020 事务回滚：检测到未清残留", rolled_back=True))
            raise ResidueConflictError("检测到未清残留，补录事务已回滚，请先清残留再重试")

        base = self.ensure_cohort(cohort_id)
        # 已有待并入补录时，本次修订按「累计待并入 + 本次」构建分母，
        # 避免连续固定快照补录互相覆盖。
        pending_total = self._pending_supplement_total(cohort_id)
        cumulative = pending_total + count
        revision = build_snapshot(
            cohort_id, self._next_revision(cohort_id),
            source=source, extra_members=cumulative, parent=base,
        )
        revision.parent_revision = base.revision
        revision.remark = remark
        # 修订自身记录本次补录人数；矩阵分母反映累计待并入规模
        revision.supplement_count = count

        if policy == POLICY_FIXED:
            # 固定筛选前快照：现网矩阵（含分母）原封不动，补录作为独立修订留档
            revision.status = REV_STANDBY
            revision.confirmed_at = None
            self._revisions.setdefault(cohort_id, []).append(revision)
            self._audit.append(self._audit_entry(
                revision,
                f"固定筛选前快照：保留分母 {base.matrix['denominator']}，"
                f"本次补录 {count} 人（累计待并入 {cumulative} 人）独立留档（修订 {revision.revision}）",
            ))
            return revision

        # 最新快照策略：补录后分母生效，受影响周期在 build_snapshot 内已重算；
        # 此前的待并入修订已被本快照吸收，标记为 superseded，仅保留留档。
        self._absorb_standby(cohort_id)
        self._promote(revision, f"采用补录后最新快照：分母 {base.matrix['denominator']} → "
                                f"{revision.matrix['denominator']}，受影响周期已重算")
        return revision

    def _pending_supplement_total(self, cohort_id: int) -> int:
        return sum(
            revision.supplement_count
            for revision in self._revisions.get(cohort_id, [])
            if revision.status == REV_STANDBY
        )

    def _absorb_standby(self, cohort_id: int) -> None:
        for revision in self._revisions.get(cohort_id, []):
            if revision.status == REV_STANDBY:
                revision.status = REV_SUPERSEDED

    def clear_residue(self, cohort_id: int) -> dict[str, Any]:
        cohort = store.find(COHORT_MODULE, cohort_id)
        if cohort is None:
            raise KeyError(f"同期群定义 {cohort_id} 不存在")
        record = {
            "cohort_id": cohort_id,
            "action": "clear_residue",
            "cleared_at": time.time(),
            "message": "残留标记已清除，可按最后确认的群组快照重试",
        }
        self._audit.append(record)
        return record

    # ---- 重算（事务：先 pending 校验，失败回滚） ----------------------

    def recompute(self, cohort_id: int) -> SnapshotRevision:
        cohort = store.find(COHORT_MODULE, cohort_id)
        if cohort is None:
            raise KeyError(f"同期群定义 {cohort_id} 不存在")
        confirmed = self.confirmed(cohort_id)
        if confirmed is None:
            raise SnapshotValidationError("群组没有已确认快照，无法重算")
        if self.pending(cohort_id) is not None:
            raise SnapshotValidationError("已有重算中的修订，重试只能作用于最后确认的群组快照")

        # 重试只允许基于最后确认的群组快照：继承同一事件边界与成员事件
        revision = build_snapshot(
            cohort_id, self._next_revision(cohort_id),
            source="recompute", parent=confirmed,
        )
        revision.parent_revision = confirmed.revision
        revision.remark = "基于最后确认快照重算"
        self._revisions.setdefault(cohort_id, []).append(revision)

        validation = self._run_validation(revision)
        if not validation.get("ok"):
            # 事务回滚：丢弃 pending，confirmed 完整矩阵保持不变
            revision.status = REV_ROLLED_BACK
            self._audit.append(self._audit_entry(
                revision,
                "重算未通过 V020 校验，已回滚并沿用当前完整矩阵："
                + "；".join(validation.get("errors", [])),
                rolled_back=True,
                validation=validation,
            ))
            raise SnapshotValidationError("；".join(validation.get("errors", ["V020 校验失败"])))

        before = confirmed.matrix["denominator"]
        self._promote(revision, f"重算通过 V020 校验，分母保持 {before}，矩阵一致")
        return revision

    def _run_validation(self, revision: SnapshotRevision) -> dict[str, Any]:
        if self._validator is None:
            return {"ok": True, "errors": [], "checked_periods": PERIOD_COUNT,
                    "metric_key": revision.metric_key}
        return self._validator(revision.matrix)

    def _promote(self, revision: SnapshotRevision, message: str) -> None:
        revision.status = REV_CONFIRMED
        revision.confirmed_at = time.time()
        self._revisions.setdefault(revision.cohort_id, []).append(revision)
        self._audit.append(self._audit_entry(revision, message, validation=None))

    # ---- 内部工具 -----------------------------------------------------

    def _next_revision(self, cohort_id: int) -> int:
        revisions = self._revisions.get(cohort_id, [])
        return max((r.revision for r in revisions), default=0) + 1

    def _audit_entry(
        self,
        revision: SnapshotRevision,
        message: str,
        *,
        rolled_back: bool = False,
        validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "cohort_id": revision.cohort_id,
            "revision": revision.revision,
            "status": revision.status,
            "source": revision.source,
            "event_boundary": revision.event_boundary,
            "metric_key": revision.metric_key,
            "denominator": revision.matrix["denominator"],
            "rolled_back": rolled_back,
            "validation": validation,
            "message": message,
            "archived_at": time.time(),
        }


# 单例：与内存 store 同生命周期
snapshot_repo = CohortSnapshotRepository()
