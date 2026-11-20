"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from app.routers import resource, snapshot_quality

# 快照质检的固定路径必须先于通用 /{module}/{entry_id} 注册，
# 否则 /api/quality_check/report 等会被动态路径抢先匹配。
ROUTERS = [snapshot_quality, resource]
