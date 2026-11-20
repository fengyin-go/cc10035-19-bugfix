"""页面快照领域服务：保存和比对页面状态快照。"""
from __future__ import annotations

import threading
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

COMPARISON_REGISTRY = "snapshot_comparisons"

# 对照记录交付（钉版）时的“查重 + 写入”必须原子化，避免并发批次重复登记。
_DELIVERY_LOCK = threading.Lock()


class PageSnapshotService(BaseModuleService):
    MODULE = "page_snapshot"
    ENTITY = "快照记录"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["快照ID", "页面URI", "DOM哈希", "文本哈希", "抓取时间", "页面大小", "是否变更"]
    REQUIRED_FIELDS = ["快照ID", "页面URI", "DOM哈希"]
    STATUS_ORDER = ["已保存", "已比对", "已过期"]
    ACTION_RULES = {"执行比对": "已保存", "标记过期": "已比对"}
    NEGATIVE_ACTIONS = []

    # ---------- 内部工具 ----------

    @staticmethod
    def _sort_key(row: dict[str, Any]) -> tuple[str, int]:
        """相邻采集按抓取时间排序；时间缺失时退化为登记序号。"""
        return (str(row.get("抓取时间") or ""), int(row.get("id", 0)))

    @staticmethod
    def _reference_no(previous_id: int | None, current_id: int) -> str:
        """比较对引用编号：质检报表与快照对照清单共用同一套编号。"""
        return f"CMP-{int(previous_id or 0):04d}-{int(current_id):04d}"

    def _page_group(
        self, page_uri: str, rows: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """同一页面的快照为一组；跨页面的哈希绝不混入本组比较。"""
        source = rows if rows is not None else store.rows(self.MODULE)
        group = [r for r in source if str(r.get("页面URI") or "") == page_uri]
        group.sort(key=self._sort_key)
        return group

    def _build_comparison(
        self,
        snapshot: dict[str, Any],
        previous: dict[str, Any] | None,
        page_uri: str,
    ) -> dict[str, Any]:
        """按相邻采集校验同类哈希：DOM 对 DOM、文本对文本。"""
        current_id = int(snapshot.get("id", 0))
        previous_id = int(previous.get("id", 0)) if previous is not None else None
        current_dom = str(snapshot.get("DOM哈希") or "")
        current_text = str(snapshot.get("文本哈希") or "")
        previous_dom = str(previous.get("DOM哈希") or "") if previous is not None else ""
        previous_text = str(previous.get("文本哈希") or "") if previous is not None else ""
        has_baseline = previous is not None
        # 首轮采集没有对照基线：不是“发生变化”，而是“缺证据”。
        dom_changed = (current_dom != previous_dom) if has_baseline else None
        text_changed = (current_text != previous_text) if has_baseline else None
        change_count = sum(1 for flag in (dom_changed, text_changed) if flag)
        recommend = bool(dom_changed)
        if not has_baseline:
            verdict = "缺证据"
        elif recommend:
            verdict = "需更新模板"
        else:
            verdict = "通过"
        return {
            "引用编号": self._reference_no(previous_id, current_id),
            "snapshot_id": current_id,
            "previous_snapshot_id": previous_id,
            "page_uri": page_uri,
            "dom_changed": dom_changed,
            "text_changed": text_changed,
            "change_count": change_count,
            "current_dom_hash": current_dom,
            "previous_dom_hash": previous_dom,
            "current_text_hash": current_text,
            "previous_text_hash": previous_text,
            "current_captured_at": str(snapshot.get("抓取时间") or ""),
            "previous_captured_at": str(previous.get("抓取时间") or "") if previous is not None else "",
            "evidence_status": "充分" if has_baseline else "首轮缺证据",
            "recommend_rule_update": recommend,
            "verdict": verdict,
        }

    def _fresh_comparison(
        self, snapshot: dict[str, Any], rows: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """按当前数据现算比较对：同页面、按抓取时间相邻的上一条采集作基线。"""
        page_uri = str(snapshot.get("页面URI") or "")
        group = self._page_group(page_uri, rows)
        current_id = int(snapshot.get("id", 0))
        index = next(
            (i for i, row in enumerate(group) if int(row.get("id", 0)) == current_id),
            None,
        )
        previous = group[index - 1] if index else None
        return self._build_comparison(snapshot, previous, page_uri)

    # ---------- 对照清单（已交付比较对） ----------

    @staticmethod
    def _registry() -> list[dict[str, Any]]:
        return store.registry(COMPARISON_REGISTRY)

    def get_comparison(self, reference_no: str) -> dict[str, Any] | None:
        """按引用编号查询已交付的比较对；补录历史快照不影响原判定依据。"""
        for record in self._registry():
            if record.get("引用编号") == reference_no:
                return dict(record)
        return None

    def latest_comparison_for(self, snapshot_id: int) -> dict[str, Any] | None:
        """该快照最近一次交付的比较对。"""
        for record in reversed(self._registry()):
            if int(record.get("snapshot_id", 0)) == int(snapshot_id):
                return dict(record)
        return None

    def list_comparisons(self) -> list[dict[str, Any]]:
        """快照对照清单：全部已交付比较对，引用编号与质检报表一致。"""
        return [dict(record) for record in self._registry()]

    # ---------- 比对与交付 ----------

    def compare_snapshots(self, snapshot_id: int) -> dict[str, Any]:
        """比对相邻两次采集的同类哈希，供质量检查和探针选择联动。

        已交付（钉版）的比较对优先返回原证据：值班员补录的中间快照
        不能暗中换掉报告发布时所依据的比较对。
        """
        snapshot = store.find(self.MODULE, snapshot_id)
        if snapshot is None:
            return {"ok": False, "reason": "快照不存在"}
        pinned = self.latest_comparison_for(snapshot_id)
        if pinned is not None:
            return {"ok": True, "pinned": True, **pinned}
        return {"ok": True, "pinned": False, **self._fresh_comparison(snapshot)}

    def deliver_comparison(
        self,
        snapshot_id: int,
        *,
        source: str = "action",
        batch_no: str | None = None,
        rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """交付（钉版）当前比较对：同一引用编号只登记一次，重复交付不重复留痕。"""
        if rows is None:
            snapshot = store.find(self.MODULE, snapshot_id)
        else:
            snapshot = next(
                (r for r in rows if int(r.get("id", 0)) == int(snapshot_id)), None
            )
        if snapshot is None:
            return {"ok": False, "reason": "快照不存在"}
        record = self._fresh_comparison(snapshot, rows)
        with _DELIVERY_LOCK:
            existing = next(
                (r for r in self._registry() if r.get("引用编号") == record["引用编号"]),
                None,
            )
            if existing is not None:
                return {"ok": True, "record": dict(existing), "deduplicated": True}
            record["delivered_by"] = source
            record["批次号"] = batch_no
            self._registry().append(record)
            return {"ok": True, "record": dict(record), "deduplicated": False}

    def deliver_hash_groups(
        self, rows: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """按页面交付哈希组，供质量检查批次计算变更证据质量报告。"""
        source = rows if rows is not None else store.rows(self.MODULE)
        pages: dict[str, list[dict[str, Any]]] = {}
        for row in source:
            pages.setdefault(str(row.get("页面URI") or ""), []).append(row)
        groups: list[dict[str, Any]] = []
        for page_uri, members in pages.items():
            ordered = sorted(members, key=self._sort_key)
            groups.append({
                "page_uri": page_uri,
                "snapshot_ids": [int(r.get("id", 0)) for r in ordered],
                "hash_groups": [
                    {
                        "snapshot_id": int(r.get("id", 0)),
                        "DOM哈希": str(r.get("DOM哈希") or ""),
                        "文本哈希": str(r.get("文本哈希") or ""),
                        "抓取时间": str(r.get("抓取时间") or ""),
                    }
                    for r in ordered
                ],
            })
        groups.sort(key=lambda g: g["snapshot_ids"][0] if g["snapshot_ids"] else 0)
        return groups

    def feed_quality_check(self, snapshot_id: int) -> dict[str, Any]:
        """将快照比对结果提供给质量检查。"""
        compare_result = self.compare_snapshots(snapshot_id)
        if not compare_result["ok"]:
            return compare_result

        has_baseline = compare_result["evidence_status"] == "充分"
        dom_passed = (not compare_result["dom_changed"]) if has_baseline else None
        text_passed = (not compare_result["text_changed"]) if has_baseline else None
        detail = "相邻采集同类哈希比对" if has_baseline else "首轮缺证据"
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "page_uri": compare_result["page_uri"],
            "引用编号": compare_result["引用编号"],
            "has_changes": compare_result["change_count"] > 0,
            "evidence_status": compare_result["evidence_status"],
            "check_items": [
                {"name": "DOM一致性", "passed": dom_passed, "detail": detail},
                {"name": "文本一致性", "passed": text_passed, "detail": detail},
            ],
            "recommend_rule_update": compare_result["recommend_rule_update"],
            "verdict": compare_result["verdict"],
        }

    # ---------- 通用骨架的领域扩展 ----------

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """快照详情附带变更证据：详情页看到的建议与报告所用比较对一致。"""
        entry = super().get_entry(entry_id)
        if entry is None:
            return None
        detail = dict(entry)
        comparison = self.compare_snapshots(entry_id)
        if comparison.get("ok"):
            detail["变更证据"] = comparison
        return detail

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is not None and action == "执行比对":
            delivery = self.deliver_comparison(entry_id, source="action")
            if delivery["ok"]:
                record = delivery["record"]
                entry["是否变更"] = (
                    "是" if record["change_count"] > 0
                    else "否" if record["evidence_status"] == "充分"
                    else "缺证据"
                )
                message = f"{message}，对照{record['引用编号']}：{record['verdict']}"
        return entry, message

    def compute_staleness(self) -> dict[str, Any]:
        """计算快照过时率。"""
        rows = store.rows(self.MODULE)
        total = len(rows)
        expired = sum(1 for r in rows if r.get("status") == "已过期")
        changed = sum(1 for r in rows if r.get("是否变更") == "是" or r.get("是否变更") == "true")
        return {
            "total_snapshots": total,
            "expired_count": expired,
            "changed_count": changed,
            "staleness_ratio": round(expired / total, 2) if total > 0 else 0.0,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["staleness"] = self.compute_staleness()
        base["delivered_comparisons"] = len(self._registry())
        return base
