"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import conflict_diff, resource

# 冲突比对别名（/api/iceberg_commit/conflicts、/api/data_diff/conflicts）必须排在
# 通用 /api/{module} 之前，否则会被动态路径当成 entry_id。
ROUTERS = [conflict_diff, resource]

ALIAS_ROUTERS = [conflict_diff.iceberg_alias, conflict_diff.data_diff_alias]