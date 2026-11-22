"""冲突比对服务：按“目标表 + 冲突快照 + 日志位点对”定位比对数据索引。

设计约束（与提交失败恢复联动）：
- 快照号必须精确命中，绝不按目标表回退到同一张表的其他冲突。
- 每次样本状态变化都会推进版本并重发索引令牌；旧索引只能得到“需刷新”，
  不会基于过期范围受理任何键。
- 复核/受理只作用于该快照当前仍未确认、未受理的键，重复派发不改动已写数据。
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def normalize_position(value: Any) -> str:
    """把日志位点对归一成 ``文件:偏移``，兼容字符串、对象和二元组。"""
    if value is None:
        return ""
    if isinstance(value, dict):
        file_part = (
            value.get("文件") or value.get("binlog文件") or value.get("file")
            or value.get("log_file") or value.get("日志文件")
        )
        offset = (
            value.get("偏移") or value.get("位点") or value.get("offset")
            or value.get("position") or value.get("log_offset") or value.get("偏移量")
        )
        if file_part is not None and offset is not None:
            return f"{str(file_part).strip()}:{str(offset).strip()}"
        parts = [str(item).strip() for item in value.values() if str(item).strip()]
        return ":".join(parts)
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return ":".join(parts)
    return str(value).strip().replace("：", ":")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _key_list(value: Any) -> list[str]:
    """待修键支持列表、逗号/顿号/空格分隔的字符串。"""
    if value is None:
        return []
    if isinstance(value, str):
        for separator in ("、", ",", "，", ";", "；", "\n"):
            value = value.replace(separator, " ")
        raw = value.split()
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    keys: list[str] = []
    for item in raw:
        key = _text(item)
        if key and key not in keys:
            keys.append(key)
    return keys


def _position_parts(position: str) -> tuple[str, str]:
    if ":" in position:
        file_part, offset = position.rsplit(":", 1)
        return file_part, offset
    if "/" in position:
        file_part, offset = position.split("/", 1)
        return file_part, offset
    return position, ""


class ConflictDiffService(BaseModuleService):
    MODULE = "conflict_diff"
    ENTITY = "冲突比对样本"
    LIST_FIELDS = ["样本编号", "目标表", "冲突快照", "日志位点对", "待修键数", "已确认键数", "受理键数"]
    REQUIRED_FIELDS = ["目标表", "冲突快照", "日志位点对"]
    # 通用动作不能绕过版本校验直接改状态，这里只保留查询与登记。
    STATUS_ORDER = ["待修复", "部分修复", "已受理", "已完成"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    def __init__(self) -> None:
        # 令牌台账：token -> {sample_id, version, applied, result}
        self._ledger: dict[str, dict[str, Any]] = {}

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """列表检索：身份字段（目标表/冲突快照/日志位点对）精确匹配，其余包含匹配。"""
        rows = list(store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            if field not in self.LIST_FIELDS:
                continue
            if field == "日志位点对":
                wanted = normalize_position(expected)
                rows = [
                    row for row in rows
                    if normalize_position(row.get("日志位点对")) == wanted
                ]
            elif field in {"目标表", "冲突快照", "样本编号"}:
                rows = [
                    row for row in rows
                    if _text(row.get(field)).casefold() == expected.casefold()
                ]
            else:
                rows = [
                    row for row in rows
                    if expected.casefold() in _text(row.get(field)).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in _text(row.get(field)).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return [self._row_view(row) for row in rows[start:start + size]], total

    def _row_view(self, sample: dict[str, Any]) -> dict[str, Any]:
        row = copy.deepcopy(sample)
        row["索引"] = self._view(sample)["index"]
        return row

    # ---- 查询 ----

    def query(
        self,
        *,
        target_table: Any = None,
        snapshot: Any = None,
        log_position: Any = None,
        log_file: Any = None,
        log_offset: Any = None,
    ) -> dict[str, Any]:
        """按复合键检索冲突比对数据索引。

        目标表与冲突快照必填；日志位点对可选，但一旦给出就必须精确匹配。
        任何一项对不上都返回未命中，不会退回到同表的其他冲突快照。
        """
        table = _text(target_table)
        snapshot_no = _text(snapshot)
        if not table:
            return self._miss("TARGET_TABLE_REQUIRED", "必须提供目标表，不能跨表检索冲突比对")
        if not snapshot_no:
            return self._miss(
                "SNAPSHOT_REQUIRED",
                "必须提供冲突快照编号，不能只按目标表退回到其他冲突",
            )
        if log_position is None and (log_file is not None or log_offset is not None):
            log_position = {"文件": log_file, "偏移": log_offset}
        position = normalize_position(log_position)

        same_snapshot = [
            row for row in store.rows(self.MODULE)
            if _text(row.get("目标表")).casefold() == table.casefold()
            and _text(row.get("冲突快照")).casefold() == snapshot_no.casefold()
        ]
        if not same_snapshot:
            return self._miss(
                "SNAPSHOT_NOT_FOUND",
                f"目标表 {table} 下不存在冲突快照 {snapshot_no}，未退回同表其他冲突",
                target_table=table,
                snapshot=snapshot_no,
            )
        if position:
            matched = [
                row for row in same_snapshot
                if normalize_position(row.get("日志位点对")) == position
            ]
            if not matched:
                return self._miss(
                    "LOG_POSITION_MISMATCH",
                    f"快照 {snapshot_no} 与日志位点对 {position} 不匹配，未退回同表其他比对样本",
                    target_table=table,
                    snapshot=snapshot_no,
                )
        elif len(same_snapshot) == 1:
            matched = same_snapshot
        else:
            return self._miss(
                "LOG_POSITION_REQUIRED",
                f"快照 {snapshot_no} 存在多个日志位点，必须提供日志位点对",
                target_table=table,
                snapshot=snapshot_no,
            )

        return self._view(matched[0])

    # ---- 登记 ----

    def register(
        self,
        *,
        target_table: Any,
        snapshot: Any,
        log_position: Any,
        keys: Any = None,
        diff_details: Any = None,
        candidate_snapshot_id: Any = None,
        source_endpoint: Any = None,
    ) -> dict[str, Any]:
        table = _text(target_table)
        snapshot_no = _text(snapshot)
        position = normalize_position(log_position)
        missing = [
            label for label, value in (
                ("目标表", table), ("冲突快照", snapshot_no), ("日志位点对", position),
            ) if not value
        ]
        if missing:
            return {
                "ok": False,
                "code": "MISSING_FIELDS",
                "message": f"缺少必填字段：{'、'.join(missing)}",
                "missing": missing,
            }

        exact = self._find_exact(table, snapshot_no, position)
        if exact is not None:
            payload = self._view(exact)
            payload["created"] = False
            payload["message"] = "同一快照与日志位点的冲突比对样本已存在，返回原索引"
            return payload

        sibling = [
            row for row in store.rows(self.MODULE)
            if _text(row.get("目标表")).casefold() == table.casefold()
            and _text(row.get("冲突快照")).casefold() == snapshot_no.casefold()
        ]
        if sibling:
            return {
                "ok": False,
                "code": "SNAPSHOT_POSITION_CONFLICT",
                "message": f"快照 {snapshot_no} 已登记在其他日志位点，不能合并为同一冲突",
            }

        pending = _key_list(keys)
        details = self._normalise_details(diff_details, pending)
        rows = store.rows(self.MODULE)
        new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        sample = {
            "id": new_id,
            "status": "待修复",
            "pending": True,
            "abnormal": True,
            "样本编号": f"CDIFF-{new_id:04d}",
            "目标表": table,
            "冲突快照": snapshot_no,
            "日志位点对": position,
            "源端": _text(source_endpoint) or "CDC实时流",
            "快照版本": 1,
            "待修键": list(pending),
            "已确认键": [],
            "受理键": [],
            "受理键区间": [],
            "差异明细": details,
            "候选快照": self._default_candidate(
                table, snapshot_no, candidate_snapshot_id),
            "受理记录": [],
            "索引令牌": None,
        }
        self._refresh_counts(sample)
        rows.append(sample)
        payload = self._view(sample)
        payload["created"] = True
        payload["message"] = f"冲突比对样本 {sample['样本编号']} 已登记"
        return payload

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """通用登记入口转交到复合键登记，避免生成没有索引的残缺样本。"""
        result = self.register(
            target_table=values.get("目标表"),
            snapshot=values.get("冲突快照"),
            log_position=values.get("日志位点对"),
            keys=values.get("待修键") or values.get("待修键列表"),
            diff_details=values.get("差异明细"),
        )
        if not result.get("ok"):
            missing = result.get("missing")
            if missing:
                return None, [str(field) for field in missing]
            return None, [str(result.get("message", result.get("code", "登记失败")))]
        sample = self.get_entry(int(result["index"]["样本ID"]))
        return sample, []

    def run_action(
        self, entry_id: int, action: str, keys: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """通用动作入口映射到确认/复核/受理，仍走同一套版本校验与范围裁剪。"""
        sample = self.get_entry(entry_id)
        if sample is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        index = self._view(sample)["index"]
        if action in {"确认修复", "确认已修复"}:
            result = self.confirm(index, keys or [])
        else:
            mode = {
                "复核": "复核", "recheck": "复核",
                "待修范围受理": "待修", "受理": "待修", "recover": "待修",
            }.get(action)
            if mode is None:
                return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
            result = self.recover(index, mode)
        if not result.get("ok"):
            return None, str(result.get("message", "索引校验失败"))
        return self.get_entry(entry_id), f"{self.ENTITY}已{action}"

    # ---- 确认修复 ----

    def confirm(self, index: Any, keys: Any) -> dict[str, Any]:
        located = self._locate(index)
        if not located["ok"]:
            return located
        sample = located["sample"]
        ledger = located["ledger"]
        if ledger.get("applied"):
            return self._duplicate(sample, ledger)
        if ledger["version"] != int(sample.get("快照版本", 1)):
            return self._stale(sample, index, ledger)

        requested = _key_list(keys)
        if not requested:
            return {
                "ok": False,
                "code": "NO_KEYS",
                "message": "必须指定要确认修复的键",
                "accepted": [],
                "受理键": [],
            }
        pending = _text_list(sample.get("待修键"))
        confirmed = _text_list(sample.get("已确认键"))
        # 只有当前仍待修的键才可确认；已经受理的键不能再被“确认修复”。
        confirmable = [key for key in requested if key in pending]
        ignored = [key for key in requested if key not in pending]
        if confirmable:
            for key in confirmable:
                if key not in confirmed:
                    confirmed.append(key)
            sample["已确认键"] = confirmed
            sample["待修键"] = [key for key in pending if key not in confirmed]
            self._bump(sample)
            sample["受理记录"].append({
                "动作": "复核确认",
                "键": list(confirmable),
                "索引令牌": ledger["token"],
                "快照版本": int(sample["快照版本"]) - 1,
            })
            self._refresh_counts(sample)
            ledger["result"] = {
                "ok": True,
                "confirmed": list(confirmable),
                "confirmed_keys": list(confirmable),
                "已确认键": list(confirmable),
                "ignored": ignored,
                "accepted": [],
                "受理键": [],
                "message": f"快照 {sample['冲突快照']} 已确认修复 {len(confirmable)} 个键",
            }
        payload = self._view(sample)
        payload.update({
            "ok": True,
            "confirmed": list(confirmable),
            "confirmed_keys": list(confirmable),
            "已确认键": list(confirmable),
            "ignored": ignored,
            "accepted": [],
            "受理键": [],
            "message": (
                f"快照 {sample['冲突快照']} 已确认修复 {len(confirmable)} 个键"
                if confirmable else "没有仍处于待修范围的键可确认"
            ),
        })
        return payload

    # ---- 提交失败恢复：复核 / 待修范围受理 ----

    def recover(self, index: Any, mode: Any = "待修") -> dict[str, Any]:
        mode_text = _text(mode) or "待修"
        located = self._locate(index)
        if not located["ok"]:
            return located
        sample = located["sample"]
        ledger = located["ledger"]

        # 已受理过的令牌必须原样返回结果，绝不再次写数据。
        if ledger.get("applied"):
            return self._duplicate(sample, ledger)
        if ledger["version"] != int(sample.get("快照版本", 1)):
            return self._stale(sample, index, ledger)

        if mode_text.casefold() in {"复核", "recheck", "check", "review"}:
            payload = self._view(sample)
            payload.update({
                "ok": True,
                "mode": "复核",
                "accepted": [],
                "受理键": [],
                "duplicate": False,
                "message": f"快照 {sample['冲突快照']} 复核通过，未受理键",
            })
            return payload

        pending = _text_list(sample.get("待修键"))
        indexed_pending = index.get("待修键") if isinstance(index, dict) else None
        if indexed_pending is not None:
            # 只信任索引声明的范围与当前未确认范围的交集，防止旧索引扩大受理面。
            scope = set(_text_list(indexed_pending))
            pending = [key for key in pending if key in scope]
        accepted = [key for key in pending if key not in _text_list(sample.get("受理键"))]

        if not accepted:
            payload = self._view(sample)
            payload.update({
                "ok": True,
                "mode": "待修受理",
                "accepted": [],
                "受理键": [],
                "duplicate": True,
                "message": f"快照 {sample['冲突快照']} 没有可新受理的键，未改动已写数据",
            })
            ledger["applied"] = True
            ledger["result"] = {
                "accepted": [], "受理键": [], "受理键区间": [],
                "message": payload["message"],
            }
            return payload

        previous_version = int(sample.get("快照版本", 1))
        sample["受理键"] = _text_list(sample.get("受理键")) + accepted
        sample["待修键"] = [key for key in _text_list(sample.get("待修键")) if key not in accepted]
        self._bump(sample)
        sample["受理键区间"] = self._ranges(_text_list(sample.get("受理键")))
        sample["受理记录"].append({
            "动作": "待修范围受理",
            "键": list(accepted),
            "受理键区间": copy.deepcopy(sample["受理键区间"]),
            "索引令牌": ledger["token"],
            "快照版本": previous_version,
        })
        self._refresh_counts(sample)

        payload = self._view(sample)
        payload.update({
            "ok": True,
            "mode": "待修受理",
            "accepted": list(accepted),
            "accepted_keys": list(accepted),
            "受理键": list(accepted),
            "受理键区间": copy.deepcopy(sample["受理键区间"]),
            "duplicate": False,
            "written_back": True,
            "message": f"快照 {sample['冲突快照']} 已受理 {len(accepted)} 个待修键并写回索引",
        })
        ledger["applied"] = True
        ledger["result"] = {
            "accepted": list(accepted),
            "受理键": list(accepted),
            "受理键区间": copy.deepcopy(sample["受理键区间"]),
            "message": payload["message"],
        }
        return payload

    # ---- 内部工具 ----

    def _find_exact(self, table: str, snapshot: str, position: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if (
                _text(row.get("目标表")).casefold() == table.casefold()
                and _text(row.get("冲突快照")).casefold() == snapshot.casefold()
                and normalize_position(row.get("日志位点对")) == position
            ):
                return row
        return None

    def _locate(self, index: Any) -> dict[str, Any]:
        if not isinstance(index, dict):
            return self._invalid("索引必须是包含索引令牌与快照身份的完整对象")
        token = _text(
            index.get("索引令牌") or index.get("索引") or index.get("index_token")
            or index.get("token")
        )
        if not token:
            return self._invalid("索引缺少索引令牌，无法校验快照版本")
        ledger = self._ledger.get(token)
        if ledger is None:
            return self._invalid("索引令牌未签发或已失效，请重新查询快照索引", token=token)

        sample = store.find(self.MODULE, int(ledger["sample_id"]))
        if sample is None:
            return self._invalid("索引对应的冲突比对样本已不存在", token=token)

        identity_checks = (
            ("目标表", index.get("目标表") or index.get("target_table"),
             sample.get("目标表")),
            ("冲突快照", index.get("冲突快照") or index.get("snapshot"), sample.get("冲突快照")),
            ("样本编号", index.get("样本编号") or index.get("sample_no"), sample.get("样本编号")),
        )
        for label, claimed, actual in identity_checks:
            if claimed is not None and _text(claimed).casefold() != _text(actual).casefold():
                return self._invalid(
                    f"索引中的{label} {claimed} 与令牌所属快照 {actual} 不一致，未回退到其他冲突",
                    token=token,
                )
        claimed_position = index.get("日志位点对") or index.get("log_position")
        if claimed_position is not None and normalize_position(claimed_position) != normalize_position(
            sample.get("日志位点对")
        ):
            return self._invalid(
                "索引中的日志位点对与令牌所属快照不一致，未回退到其他比对样本",
                token=token,
            )
        claimed_candidate = index.get("候选快照ID") or index.get("candidate_snapshot_id")
        if claimed_candidate is not None and _text(claimed_candidate) != _text(
            self._candidate_snapshot(sample).get("候选快照ID")
        ):
            return self._invalid(
                "索引中的候选快照与令牌所属快照不一致，未回退到其他冲突",
                token=token,
            )
        claimed_version = index.get("快照版本") or index.get("version")
        if claimed_version is not None:
            try:
                if int(claimed_version) != int(ledger["version"]):
                    return self._invalid("索引版本与令牌版本不一致，索引可能被篡改", token=token)
            except (TypeError, ValueError):
                return self._invalid("索引版本格式不正确", token=token)
        return {"ok": True, "sample": sample, "ledger": ledger, "token": token}

    def _view(self, sample: dict[str, Any]) -> dict[str, Any]:
        token = self._issue(sample)
        version = int(sample.get("快照版本", 1))
        pending = _text_list(sample.get("待修键"))
        confirmed = _text_list(sample.get("已确认键"))
        accepted = _text_list(sample.get("受理键"))
        candidate = self._candidate_snapshot(sample)
        source_log = self._source_log(sample)
        index = {
            "索引令牌": token,
            "索引": token,
            "index_token": token,
            "样本ID": int(sample.get("id", 0)),
            "样本编号": _text(sample.get("样本编号")),
            "目标表": _text(sample.get("目标表")),
            "冲突快照": _text(sample.get("冲突快照")),
            "日志位点对": normalize_position(sample.get("日志位点对")),
            "候选快照ID": _text(candidate.get("候选快照ID")),
            "源日志位点对": _text(source_log.get("位点对")),
            "源端": _text(sample.get("源端")),
            "快照版本": version,
            "version": version,
            "待修键": pending,
            "已确认键": confirmed,
            "受理键": accepted,
            "受理键区间": copy.deepcopy(sample.get("受理键区间", [])),
            "状态": _text(sample.get("status")),
        }
        details = copy.deepcopy(sample.get("差异明细", []))
        for detail in details:
            if isinstance(detail, dict):
                detail.setdefault("冲突快照", index["冲突快照"])
                detail.setdefault("日志位点对", index["日志位点对"])
                detail["候选快照ID"] = index["候选快照ID"]
                detail["样本版本"] = version
                detail["修复资格"] = _text(detail.get("键")) in pending
        payload = {
            "ok": True,
            "index": index,
            "索引": copy.deepcopy(index),
            "差异明细": details,
            "diff_details": copy.deepcopy(details),
            "候选快照": candidate,
            "candidate_snapshot": copy.deepcopy(candidate),
            "源日志": source_log,
            "source_log": copy.deepcopy(source_log),
            "快照版本": version,
            "version": version,
            "状态": index["状态"],
        }
        return payload

    def _issue(self, sample: dict[str, Any]) -> str:
        version = int(sample.get("快照版本", 1))
        identity = "|".join((
            str(sample.get("id", "")),
            _text(sample.get("目标表")),
            _text(sample.get("冲突快照")),
            normalize_position(sample.get("日志位点对")),
            str(version),
        ))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        token = f"CDX-{int(sample.get('id', 0))}-v{version}-{digest}"
        ledger = self._ledger.get(token)
        if ledger is None:
            self._ledger[token] = {
                "token": token,
                "sample_id": int(sample.get("id", 0)),
                "version": version,
                "applied": False,
                "result": None,
            }
        sample["索引令牌"] = token
        return token

    def _bump(self, sample: dict[str, Any]) -> None:
        sample["快照版本"] = int(sample.get("快照版本", 1)) + 1
        sample["索引令牌"] = None
        pending = _text_list(sample.get("待修键"))
        confirmed = _text_list(sample.get("已确认键"))
        accepted = _text_list(sample.get("受理键"))
        if pending:
            sample["status"] = "部分修复" if (confirmed or accepted) else "待修复"
        elif accepted:
            sample["status"] = "已受理"
        else:
            sample["status"] = "已完成"
        sample["pending"] = bool(pending)
        sample["abnormal"] = bool(pending)

    def _refresh_counts(self, sample: dict[str, Any]) -> None:
        sample["待修键数"] = len(_text_list(sample.get("待修键")))
        sample["已确认键数"] = len(_text_list(sample.get("已确认键")))
        sample["受理键数"] = len(_text_list(sample.get("受理键")))

    def _candidate_snapshot(self, sample: dict[str, Any]) -> dict[str, Any]:
        table = _text(sample.get("目标表"))
        snapshot_no = _text(sample.get("冲突快照"))
        stored = sample.get("候选快照")
        candidate: dict[str, Any] = copy.deepcopy(stored) if isinstance(stored, dict) else {}
        # 只在快照号与目标表同时精确匹配时关联提交快照，绝不能拿同表其他冲突顶替。
        for row in store.rows("iceberg_commit"):
            if (
                _text(row.get("快照编号")).casefold() == snapshot_no.casefold()
                and _text(row.get("目标表")).casefold() == table.casefold()
            ):
                for field in ("快照编号", "目标表", "数据文件", "清单文件", "提交时间"):
                    candidate.setdefault(field, row.get(field))
                candidate.setdefault("候选快照ID", row.get("快照ID"))
                break
        candidate.setdefault("快照编号", snapshot_no)
        candidate.setdefault("目标表", table)
        candidate.setdefault(
            "候选快照ID",
            f"cand-{snapshot_no}-{_text(sample.get('样本编号')) or sample.get('id')}",
        )
        candidate.setdefault("关联日志位点对", normalize_position(sample.get("日志位点对")))
        candidate["关联样本版本"] = int(sample.get("快照版本", 1))
        return candidate

    def _source_log(self, sample: dict[str, Any]) -> dict[str, Any]:
        position = normalize_position(sample.get("日志位点对"))
        file_part, offset = _position_parts(position)
        stored = sample.get("源日志")
        source: dict[str, Any] = copy.deepcopy(stored) if isinstance(stored, dict) else {}
        source.setdefault("位点对", position)
        source.setdefault("日志文件", file_part)
        source.setdefault("偏移", offset)
        source.setdefault("源端", _text(sample.get("源端")) or "CDC实时流")
        source.setdefault("关联样本版本", int(sample.get("快照版本", 1)))
        return source

    def _default_candidate(
        self, table: str, snapshot_no: str, candidate_id: Any,
    ) -> dict[str, Any]:
        candidate = {
            "快照编号": snapshot_no,
            "目标表": table,
            "候选快照ID": _text(candidate_id) or f"cand-{snapshot_no}",
        }
        for row in store.rows("iceberg_commit"):
            if (
                _text(row.get("快照编号")).casefold() == snapshot_no.casefold()
                and _text(row.get("目标表")).casefold() == table.casefold()
            ):
                for field in ("数据文件", "清单文件", "提交时间"):
                    candidate.setdefault(field, row.get(field))
                if not _text(candidate_id):
                    candidate["候选快照ID"] = row.get("快照ID")
                break
        return candidate

    def _normalise_details(self, details: Any, keys: list[str]) -> list[dict[str, Any]]:
        if isinstance(details, list) and details:
            normalised: list[dict[str, Any]] = []
            for item in details:
                if isinstance(item, dict):
                    normalised.append(copy.deepcopy(item))
                else:
                    normalised.append({"键": _text(item)})
            return normalised
        return [
            {
                "键": key,
                "差异类型": "数据不一致",
                "修复建议": "按源日志重放",
            }
            for key in keys
        ]

    def _ranges(self, keys: list[str]) -> list[dict[str, Any]]:
        """把受理键折叠成可检索的区间；数字后缀相邻且位宽一致的键合并为一个区间。"""
        buckets: dict[tuple[str, int], list[int]] = {}
        other: list[str] = []
        for key in sorted(set(keys)):
            matched = re.match(r"^(.*?)(\d+)$", key)
            if matched:
                digits = matched.group(2)
                buckets.setdefault((matched.group(1), len(digits)), []).append(int(digits))
            else:
                other.append(key)

        def format_key(prefix: str, width: int, number: int) -> str:
            return f"{prefix}{number:0{width}d}"

        ranges: list[dict[str, Any]] = []
        for prefix, width in sorted(buckets):
            numbers = sorted(set(buckets[(prefix, width)]))
            start = previous = numbers[0]
            for number in numbers[1:]:
                if number == previous + 1:
                    previous = number
                    continue
                ranges.append({
                    "起键": format_key(prefix, width, start),
                    "止键": format_key(prefix, width, previous),
                    "键数": previous - start + 1,
                })
                start = previous = number
            ranges.append({
                "起键": format_key(prefix, width, start),
                "止键": format_key(prefix, width, previous),
                "键数": previous - start + 1,
            })
        for key in other:
            ranges.append({"起键": key, "止键": key, "键数": 1})
        return ranges

    def _miss(self, code: str, message: str, **extra: Any) -> dict[str, Any]:
        payload = {
            "ok": False,
            "code": code,
            "message": message,
            "index": None,
            "差异明细": [],
            "diff_details": [],
            "候选快照": None,
            "candidate_snapshot": None,
            "源日志": None,
            "source_log": None,
        }
        payload.update(extra)
        return payload

    def _invalid(self, message: str, **extra: Any) -> dict[str, Any]:
        payload = {
            "ok": False,
            "code": "INVALID_INDEX",
            "message": message,
            "accepted": [],
            "受理键": [],
        }
        payload.update(extra)
        return payload

    def _stale(self, sample: dict[str, Any], index: Any, ledger: dict[str, Any]) -> dict[str, Any]:
        current = self._view(sample)
        indexed_pending = _text_list(index.get("待修键")) if isinstance(index, dict) else []
        indexed_confirmed = _text_list(index.get("已确认键")) if isinstance(index, dict) else []
        current_index = current["index"]
        changes = {
            "待修键": {
                "索引": indexed_pending,
                "当前": current_index["待修键"],
            },
            "已确认键": {
                "索引": indexed_confirmed,
                "当前": current_index["已确认键"],
            },
            "受理键": {
                "索引": _text_list(index.get("受理键")) if isinstance(index, dict) else [],
                "当前": current_index["受理键"],
            },
        }
        message = (
            f"索引已过期：快照 {current_index['冲突快照']} 从版本 {ledger['version']} "
            f"推进到 {current_index['快照版本']}，未受理任何键，请使用新索引后重试"
        )
        payload = {
            "ok": False,
            "code": "STALE_INDEX",
            "need_refresh": True,
            "refresh_required": True,
            "message": message,
            "refresh_reason": message,
            "索引版本": int(ledger["version"]),
            "当前版本": current_index["快照版本"],
            "变化": changes,
            "accepted": [],
            "受理键": [],
            "duplicate": False,
            "current_index": current["index"],
            "index": current["index"],
            "差异明细": current["差异明细"],
            "diff_details": current["diff_details"],
            "候选快照": current["候选快照"],
            "candidate_snapshot": current["candidate_snapshot"],
            "源日志": current["源日志"],
            "source_log": current["source_log"],
        }
        return payload

    def _duplicate(self, sample: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
        current = self._view(sample)
        result = copy.deepcopy(ledger.get("result") or {})
        accepted = _text_list(result.get("accepted"))
        payload = {
            "ok": True,
            "code": "DUPLICATE_REQUEST",
            "duplicate": True,
            "message": "该索引请求已受理过，本次未改动任何已写数据",
            "accepted": [],
            "accepted_keys": [],
            "受理键": [],
            "上次受理键": accepted,
            "受理键区间": result.get("受理键区间", current["index"]["受理键区间"]),
            "index": current["index"],
            "差异明细": current["差异明细"],
            "diff_details": current["diff_details"],
            "候选快照": current["候选快照"],
            "candidate_snapshot": current["candidate_snapshot"],
            "源日志": current["源日志"],
            "source_log": current["source_log"],
        }
        return payload


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _key_list(value)
    return [_text(item) for item in value if _text(item)]


conflict_diff_service = ConflictDiffService()
