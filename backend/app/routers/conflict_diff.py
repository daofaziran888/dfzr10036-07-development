"""冲突比对专用路由：按目标表/冲突快照/日志位点对检索索引并驱动提交失败恢复。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request

from app.services.conflict_diff import conflict_diff_service

router = APIRouter(prefix="/api/conflict_diff", tags=["冲突比对"])


def _pick(source: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in source and source[name] not in (None, ""):
            return source[name]
    return None


def _query_fields(source: dict[str, Any]) -> dict[str, Any]:
    log_position = _pick(source, "日志位点对", "log_position", "position", "位点对")
    log_file = _pick(source, "日志文件", "log_file")
    log_offset = _pick(source, "日志偏移", "log_offset", "offset")
    if log_position is None and (log_file is not None or log_offset is not None):
        log_position = {"文件": log_file, "偏移": log_offset}
    return {
        "target_table": _pick(source, "目标表", "target_table", "table"),
        "snapshot": _pick(source, "冲突快照", "snapshot", "snapshot_no", "快照编号"),
        "log_position": log_position,
    }


@router.get("/query")
def query_conflict_diff(request: Request) -> dict[str, Any]:
    """GET 形态：目标表 + 冲突快照 + 日志位点对，精确命中一个比对样本。"""
    return conflict_diff_service.query(**_query_fields(dict(request.query_params)))


@router.get("/lookup")
def lookup_conflict_diff(request: Request) -> dict[str, Any]:
    """查询入口别名，语义与 /query 一致。"""
    return conflict_diff_service.query(**_query_fields(dict(request.query_params)))


@router.post("/query")
def query_conflict_diff_post(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    """POST 形态：允许日志位点对以对象或“文件:偏移”字符串传入。"""
    return conflict_diff_service.query(**_query_fields(payload or {}))


@router.post("/index/query")
def query_conflict_diff_index(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    """显式按“冲突比对数据索引”检索的别名入口。"""
    return conflict_diff_service.query(**_query_fields(payload or {}))


@router.post("/register")
def register_conflict_diff(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    """登记冲突比对样本：同一目标表可以并存多个快照，但快照身份必须唯一。"""
    values = payload or {}
    result = conflict_diff_service.register(
        target_table=_pick(values, "目标表", "target_table", "table"),
        snapshot=_pick(values, "冲突快照", "snapshot", "snapshot_no", "快照编号"),
        log_position=_pick(values, "日志位点对", "log_position", "position"),
        keys=_pick(values, "待修键", "keys", "diff_keys"),
        diff_details=_pick(values, "差异明细", "diff_details", "details"),
        candidate_snapshot_id=_pick(values, "候选快照ID", "candidate_snapshot_id"),
        source_endpoint=_pick(values, "源端", "source_endpoint"),
    )
    return result


@router.post("/confirm")
def confirm_conflict_diff(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    """复核某个快照中已修复的键；只接受仍未确认、未受理的键。"""
    values = payload or {}
    index = values.get("index") if isinstance(values.get("index"), dict) else values
    return conflict_diff_service.confirm(index, _pick(values, "keys", "键", "已修复键"))


@router.post("/recover")
def recover_conflict_diff(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    """提交失败恢复：用冲突比对数据索引发起复核或待修范围受理。"""
    values = payload or {}
    index = values.get("index") if isinstance(values.get("index"), dict) else values
    mode = _pick(values, "mode", "模式", "action") or "待修"
    return conflict_diff_service.recover(index, mode)


# ---- 只读别名：从湖仓提交 / 数据比对视角检索同一份冲突比对索引 ----
# 必须注册在通用资源路由之前，否则 /api/data_diff/query 会被当成 entry_id。

def _alias_query(request: Request) -> dict[str, Any]:
    return conflict_diff_service.query(**_query_fields(dict(request.query_params)))


iceberg_alias = APIRouter(prefix="/api/iceberg_commit/conflicts", tags=["冲突比对"])
data_diff_alias = APIRouter(prefix="/api/data_diff/conflicts", tags=["冲突比对"])


@iceberg_alias.get("/query")
def query_from_commit(request: Request) -> dict[str, Any]:
    """从湖仓提交视角按目标表 + 快照 + 位点对检索冲突比对索引。"""
    return _alias_query(request)


@iceberg_alias.post("/query")
def query_from_commit_post(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return conflict_diff_service.query(**_query_fields(payload or {}))


@data_diff_alias.get("/query")
def query_from_diff(request: Request) -> dict[str, Any]:
    """从数据比对视角检索同一份冲突比对索引。"""
    return _alias_query(request)


@data_diff_alias.post("/query")
def query_from_diff_post(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return conflict_diff_service.query(**_query_fields(payload or {}))
