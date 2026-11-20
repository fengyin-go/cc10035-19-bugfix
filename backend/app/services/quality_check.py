"""质量检查领域服务：对抽取结果进行多维度质量把关。"""
from __future__ import annotations

import threading
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

REPORT_REGISTRY = "snapshot_quality_report"

# 批次发布串行化：同一批保存的比较对与质量结论必须来自同一次核对。
_BATCH_LOCK = threading.Lock()


class QualityCheckService(BaseModuleService):
    MODULE = "quality_check"
    ENTITY = "检查规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则名称", "检查字段", "检查类型", "阈值设置", "告警动作", "适用模块", "是否阻断"]
    REQUIRED_FIELDS = ["规则名称", "检查字段", "检查类型"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用规则": "草稿", "停用规则": "已启用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def check_entity_quality(self, entity_id: str) -> dict[str, Any]:
        """对单个实体抽取结果进行质量评分。"""
        entry = store.find("entity_extract", int(entity_id) if entity_id.isdigit() else 0)
        if entry is None:
            alt = [r for r in store.rows("entity_extract") if r.get("实体ID") == entity_id]
            if not alt:
                return {"ok": False, "reason": "实体不存在"}
            entry = alt[0]

        confidence_str = entry.get("置信度", "0.5")
        try:
            confidence = float(confidence_str)
        except (ValueError, TypeError):
            confidence = 0.5

        has_id = bool(entry.get("实体ID", "").strip())
        has_type = bool(entry.get("实体类型", "").strip())
        has_name = bool(entry.get("实体名称", "").strip())
        has_source = bool(entry.get("页面来源", "").strip())
        has_snippet = bool(entry.get("原文片段", "").strip())

        checks = {
            "confidence_threshold": confidence >= 0.5,
            "entity_id_present": has_id,
            "entity_type_present": has_type,
            "entity_name_present": has_name,
            "source_page_present": has_source,
            "original_snippet_present": has_snippet,
        }
        passed = sum(1 for v in checks.values() if v)
        total = len(checks)
        score = round(passed / total, 2)

        quality_rules = [
            r for r in store.rows(self.MODULE)
            if r.get("status") == "已启用" and r.get("适用模块") in ("entity_extract", "*", "全部")
        ]
        return {
            "ok": True,
            "entity_id": entity_id,
            "quality_score": score,
            "checks": checks,
            "applied_rules": len(quality_rules),
            "passed_all": passed == total,
            "result": "通过" if passed == total else "预警",
        }

    def run_quality_pipeline(self) -> dict[str, Any]:
        """对所有已启用检查规则的跨模块批量质量审计。"""
        from app.services.page_snapshot import PageSnapshotService
        pss = PageSnapshotService()
        rules = [r for r in store.rows(self.MODULE) if r.get("status") == "已启用"]
        results: list[dict[str, Any]] = []
        for rule in rules:
            applicable_module = rule.get("适用模块", "")
            if applicable_module in ("entity_extract", "*"):
                target_rows = store.rows("entity_extract")
                for row in target_rows:
                    results.append({
                        "rule_name": rule.get("规则名称"),
                        "target_module": "entity_extract",
                        "target_id": row.get("id"),
                        "check_type": rule.get("检查类型"),
                        "threshold": rule.get("阈值设置"),
                        "blocking": rule.get("是否阻断") == "是",
                        "passed": self.check_entity_quality(row.get("实体ID", str(row.get("id", ""))))["passed_all"],
                    })
            if applicable_module in ("page_snapshot", "*"):
                target_rows = store.rows("page_snapshot")
                for row in target_rows:
                    # 与单条判定共用同一比较核心：相邻采集、同类哈希，
                    # 批量结果不再丢失 DOM 变化。
                    comparison = pss.compare_snapshots(int(row.get("id", 0)))
                    results.append({
                        "rule_name": rule.get("规则名称"),
                        "target_module": "page_snapshot",
                        "target_id": row.get("id"),
                        "check_type": rule.get("检查类型"),
                        "threshold": rule.get("阈值设置"),
                        "blocking": rule.get("是否阻断") == "是",
                        "passed": bool(
                            comparison.get("ok")
                            and comparison.get("evidence_status") == "充分"
                            and not comparison.get("dom_changed")
                        ),
                        "引用编号": comparison.get("引用编号"),
                        "conclusion": comparison.get("verdict"),
                    })

        total = len(results)
        passed = sum(1 for r in results if r.get("passed"))
        return {
            "ok": True,
            "total_checks": total,
            "passed_checks": passed,
            "pass_ratio": round(passed / total, 2) if total > 0 else 0.0,
            "results": results[:20],
        }

    def check_snapshot_consistency(self, snapshot_id: int) -> dict[str, Any]:
        """校验页面快照一致性，联动质量检查→探针→快照的验证链路。"""
        from app.services.page_snapshot import PageSnapshotService
        pss = PageSnapshotService()
        feed = pss.feed_quality_check(snapshot_id)
        if not feed["ok"]:
            return feed

        rule_count = len([r for r in store.rows(self.MODULE)
                          if r.get("status") == "已启用"
                          and r.get("适用模块") in ("page_snapshot", "*")])
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "引用编号": feed["引用编号"],
            "has_changes": feed["has_changes"],
            "evidence_status": feed["evidence_status"],
            "recommend_rule_update": feed["recommend_rule_update"],
            "active_check_rules": rule_count,
            "verdict": feed["verdict"],
        }

    def run_snapshot_quality_batch(self) -> dict[str, Any]:
        """页面快照质检批次：按快照模块交付的页面和哈希组计算变更证据质量报告。

        批次开始即锁定本次核对的快照集合（一致性读）：核对期间值班员
        补录的中间快照不会混入本批，发布的结论与保存的比较对必然来自
        同一次核对。结论回写到待复核清单，重复提交本批检查不会重复
        登记质量结论。
        """
        from app.services.page_snapshot import PageSnapshotService
        pss = PageSnapshotService()
        with _BATCH_LOCK:
            rows_snapshot = [dict(row) for row in store.rows("page_snapshot")]
            groups = pss.deliver_hash_groups(rows_snapshot)
            batch_no = f"BATCH-{store.next_counter('snapshot_quality_batch'):04d}"
            registry = store.registry(REPORT_REGISTRY)
            entries: list[dict[str, Any]] = []
            new_conclusions = 0
            for group in groups:
                hash_groups = group["hash_groups"]
                if not hash_groups:
                    continue
                current_id = hash_groups[-1]["snapshot_id"]
                delivery = pss.deliver_comparison(
                    current_id, source="batch", batch_no=batch_no, rows=rows_snapshot)
                if not delivery["ok"]:
                    continue
                record = delivery["record"]
                existing = next(
                    (e for e in registry if e.get("引用编号") == record["引用编号"]),
                    None,
                )
                if existing is None:
                    existing = {
                        "引用编号": record["引用编号"],
                        "批次号": batch_no,
                        "页面URI": record["page_uri"],
                        "当前快照ID": record["snapshot_id"],
                        "对照快照ID": record["previous_snapshot_id"],
                        "当前DOM哈希": record["current_dom_hash"],
                        "对照DOM哈希": record["previous_dom_hash"],
                        "当前文本哈希": record["current_text_hash"],
                        "对照文本哈希": record["previous_text_hash"],
                        "DOM变化": record["dom_changed"],
                        "文本变化": record["text_changed"],
                        "证据状态": record["evidence_status"],
                        "结论": record["verdict"],
                        "建议更新模板": record["recommend_rule_update"],
                        "复核状态": "待复核",
                    }
                    registry.append(existing)
                    new_conclusions += 1
                entries.append(dict(existing))
                # 结论回写快照清单，值班员在快照列表即可看到本批判定。
                row = store.find("page_snapshot", current_id)
                if row is not None:
                    row["是否变更"] = (
                        "是" if record["change_count"] > 0
                        else "否" if record["evidence_status"] == "充分"
                        else "缺证据"
                    )
            return {
                "ok": True,
                "批次号": batch_no,
                "report_name": "页面快照变更证据质量报告",
                "total_pages": len(entries),
                "changed_pages": sum(1 for e in entries if e["结论"] == "需更新模板"),
                "insufficient_evidence_pages": sum(1 for e in entries if e["结论"] == "缺证据"),
                "new_conclusions": new_conclusions,
                "entries": entries,
            }

    def quality_report(self) -> dict[str, Any]:
        """待复核清单：已交付的变更证据质量结论，引用编号与快照对照清单一致。"""
        entries = [dict(e) for e in store.registry(REPORT_REGISTRY)]
        return {
            "ok": True,
            "report_name": "页面快照变更证据质量报告",
            "total": len(entries),
            "pending_review": sum(1 for e in entries if e.get("复核状态") == "待复核"),
            "entries": entries,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rules = store.rows(self.MODULE)
        base["quality_stats"] = {
            "total_rules": len(rules),
            "active_rules": sum(1 for r in rules if r.get("status") == "已启用"),
            "blocking_rules": sum(1 for r in rules if r.get("是否阻断") == "是"),
        }
        base["snapshot_report"] = {
            "delivered_conclusions": len(store.registry(REPORT_REGISTRY)),
        }
        return base
