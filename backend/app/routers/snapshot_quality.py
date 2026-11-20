"""快照质检联动接口：批量变更证据质量报告、待复核清单与快照对照清单。

路由层只做分派，比较对计算、证据钉版和结论回写都在领域服务里。
"""
from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query

from app.services import service_for
from app.services.page_snapshot import PageSnapshotService
from app.services.quality_check import QualityCheckService

router = APIRouter(prefix="/api", tags=["快照质检"])


def _quality_service() -> QualityCheckService:
    service = service_for("quality_check")
    if service is None:
        raise HTTPException(status_code=404, detail="未知业务模块：quality_check")
    return cast(QualityCheckService, service)


def _snapshot_service() -> PageSnapshotService:
    service = service_for("page_snapshot")
    if service is None:
        raise HTTPException(status_code=404, detail="未知业务模块：page_snapshot")
    return cast(PageSnapshotService, service)


@router.post("/quality_check/batch")
def run_snapshot_quality_batch() -> dict[str, Any]:
    """质量检查入口：发布本批页面快照变更证据质量报告。"""
    return _quality_service().run_snapshot_quality_batch()


@router.get("/quality_check/report")
def get_snapshot_quality_report() -> dict[str, Any]:
    """待复核清单：已交付的质检结论，引用编号与快照对照清单一致。"""
    return _quality_service().quality_report()


@router.get("/page_snapshot/comparisons")
def list_snapshot_comparisons() -> dict[str, Any]:
    """快照对照清单：全部已交付比较对，历史判定依据可长期查询。"""
    items = _snapshot_service().list_comparisons()
    return {"ok": True, "total": len(items), "items": items}


@router.get("/page_snapshot/{snapshot_id}/comparison")
def get_snapshot_comparison(
    snapshot_id: int, ref: str | None = Query(default=None)
) -> dict[str, Any]:
    """快照详情复算：返回该快照的变更证据。

    指定 ref 时返回该次核对保存的原比较对，补录的快照不能暗中换掉
    报告所用的证据；未指定时返回最近一次交付的比较对，未交付则现算。
    """
    service = _snapshot_service()
    if ref:
        record = service.get_comparison(ref)
        if record is None or int(record.get("snapshot_id", 0)) != snapshot_id:
            raise HTTPException(
                status_code=404,
                detail=f"对照记录 {ref} 不存在或不属于快照 {snapshot_id}",
            )
        return {"ok": True, "pinned": True, **record}
    result = service.compare_snapshots(snapshot_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "快照不存在"))
    return result
