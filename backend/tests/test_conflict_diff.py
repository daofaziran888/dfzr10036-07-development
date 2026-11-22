"""冲突比对索引与提交失败恢复的回归测试。

用标准库 unittest 直接驱动领域服务：不依赖 httpx/TestClient，
把“快照身份精确、旧索引只提示刷新、受理范围单调、重复派发幂等”固定下来。
"""
from __future__ import annotations

import copy
import unittest

from app.services.conflict_diff import conflict_diff_service, normalize_position
from app.store import store

TABLE = "ods.payment_log_iceberg"
S1 = "IC-0002"
POS1 = "binlog.000042:158320"
S2 = "IC-0003"
POS2 = "binlog.000043:99001"


class ConflictDiffTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tables_backup = copy.deepcopy(store._tables)
        self._ledger_backup = copy.deepcopy(conflict_diff_service._ledger)

    def tearDown(self) -> None:
        store._tables = self._tables_backup
        conflict_diff_service._ledger = self._ledger_backup

    def query(self, table=TABLE, snapshot=S1, position=POS1):
        return conflict_diff_service.query(
            target_table=table, snapshot=snapshot, log_position=position)


class QueryIdentityTest(ConflictDiffTestBase):
    def test_query_hits_only_exact_snapshot(self) -> None:
        result = self.query()
        self.assertTrue(result["ok"])
        self.assertEqual(result["index"]["冲突快照"], S1)
        self.assertEqual(result["index"]["日志位点对"], POS1)
        self.assertEqual(result["index"]["待修键"], ["pay-0001", "pay-0002"])
        self.assertEqual(result["候选快照"]["快照编号"], S1)
        self.assertEqual(result["源日志"]["位点对"], POS1)

    def test_unknown_snapshot_does_not_fall_back_to_same_table(self) -> None:
        result = self.query(snapshot="IC-9999")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SNAPSHOT_NOT_FOUND")
        self.assertIsNone(result["index"])
        self.assertEqual(result["差异明细"], [])

    def test_snapshot_required(self) -> None:
        result = conflict_diff_service.query(target_table=TABLE, snapshot="", log_position=POS1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SNAPSHOT_REQUIRED")

    def test_log_position_mismatch_does_not_fall_back(self) -> None:
        result = self.query(position="binlog.000042:999999")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "LOG_POSITION_MISMATCH")
        self.assertIsNone(result["index"])

    def test_log_position_accepts_object_form(self) -> None:
        result = conflict_diff_service.query(
            target_table=TABLE, snapshot=S1,
            log_position={"文件": "binlog.000042", "偏移": "158320"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["index"]["日志位点对"], POS1)

    def test_normalize_position(self) -> None:
        self.assertEqual(
            normalize_position({"binlog文件": "binlog.000042", "offset": " 158320 "}),
            POS1,
        )
        self.assertEqual(normalize_position(["binlog.000042", "158320"]), POS1)
        self.assertEqual(normalize_position("binlog.000042：158320"), POS1)


class RefreshAndRecoveryTest(ConflictDiffTestBase):
    def test_two_conflicts_same_table_are_independent(self) -> None:
        s2 = conflict_diff_service.register(
            target_table=TABLE, snapshot=S2, log_position=POS2,
            keys=["pay-0001", "pay-0099"],
        )
        self.assertTrue(s2["ok"])
        self.assertNotEqual(s2["index"]["索引令牌"], self.query()["index"]["索引令牌"])
        # S1 依旧只关联自己的候选快照和源日志
        s1 = self.query()
        self.assertEqual(s1["候选快照"]["快照编号"], S1)
        self.assertEqual(s1["源日志"]["位点对"], POS1)

    def test_stale_index_requires_refresh_and_accepts_nothing(self) -> None:
        old_index = copy.deepcopy(self.query()["index"])
        conflict_diff_service.confirm(old_index, ["pay-0001"])
        conflict_diff_service.register(
            target_table=TABLE, snapshot=S2, log_position=POS2, keys=["pay-0001"])

        stale = conflict_diff_service.recover(old_index, "待修")
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["code"], "STALE_INDEX")
        self.assertTrue(stale["need_refresh"])
        self.assertEqual(stale["accepted"], [])
        self.assertEqual(stale["current_index"]["待修键"], ["pay-0002"])

        fresh = self.query()
        self.assertEqual(fresh["index"]["待修键"], ["pay-0002"])
        self.assertEqual(fresh["index"]["已确认键"], ["pay-0001"])

    def test_duplicate_dispatch_is_idempotent(self) -> None:
        index = copy.deepcopy(self.query()["index"])
        first = conflict_diff_service.recover(index, "待修")
        self.assertTrue(first["ok"])
        self.assertEqual(first["accepted"], ["pay-0001", "pay-0002"])

        second = conflict_diff_service.recover(index, "待修")
        self.assertTrue(second["ok"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["accepted"], [])
        self.assertEqual(second["上次受理键"], ["pay-0001", "pay-0002"])

        current = self.query()["index"]
        self.assertEqual(current["受理键"], ["pay-0001", "pay-0002"])
        self.assertEqual(current["待修键"], [])
        self.assertEqual(current["受理键区间"], [
            {"起键": "pay-0001", "止键": "pay-0002", "键数": 2},
        ])

    def test_confirmed_key_cannot_be_accepted_again(self) -> None:
        index = copy.deepcopy(self.query()["index"])
        confirmed = conflict_diff_service.confirm(index, ["pay-0001"])
        self.assertEqual(confirmed["confirmed"], ["pay-0001"])

        again = conflict_diff_service.confirm(confirmed["index"], ["pay-0001"])
        self.assertEqual(again["confirmed"], [])
        self.assertEqual(again["ignored"], ["pay-0001"])

        recovered = conflict_diff_service.recover(self.query()["index"], "待修")
        self.assertEqual(recovered["accepted"], ["pay-0002"])

    def test_recheck_mode_accepts_nothing(self) -> None:
        index = copy.deepcopy(self.query()["index"])
        result = conflict_diff_service.recover(index, "复核")
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "复核")
        self.assertEqual(result["accepted"], [])
        current = self.query()["index"]
        self.assertEqual(current["待修键"], ["pay-0001", "pay-0002"])

    def test_index_of_other_snapshot_cannot_drive_this_snapshot(self) -> None:
        conflict_diff_service.register(
            target_table=TABLE, snapshot=S2, log_position=POS2, keys=["pay-0099"])
        index = copy.deepcopy(self.query()["index"])
        tampered = dict(index, 冲突快照=S2)
        result = conflict_diff_service.recover(tampered, "待修")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_INDEX")
        self.assertEqual(self.query(snapshot=S2, position=POS2)["index"]["受理键"], [])

    def test_index_with_other_candidate_snapshot_is_rejected(self) -> None:
        index = copy.deepcopy(self.query()["index"])
        index["候选快照ID"] = "cand-forged"
        result = conflict_diff_service.recover(index, "待修")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_INDEX")
        self.assertEqual(self.query()["index"]["受理键"], [])

    def test_unknown_token_is_rejected(self) -> None:
        index = copy.deepcopy(self.query()["index"])
        index["索引令牌"] = "CDX-forged"
        result = conflict_diff_service.recover(index, "待修")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_INDEX")

    def test_confirm_with_stale_index_writes_nothing(self) -> None:
        old_index = copy.deepcopy(self.query()["index"])
        conflict_diff_service.confirm(old_index, ["pay-0001"])
        result = conflict_diff_service.confirm(old_index, ["pay-0002"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "STALE_INDEX")
        self.assertEqual(result["accepted"], [])
        current = self.query()["index"]
        self.assertEqual(current["已确认键"], ["pay-0001"])
        self.assertEqual(current["待修键"], ["pay-0002"])
        self.assertEqual(current["受理键"], [])

    def test_generic_action_goes_through_versioned_recovery(self) -> None:
        entry = conflict_diff_service.get_entry(1)
        self.assertIsNotNone(entry)
        entry, message = conflict_diff_service.run_action(1, "待修范围受理")
        self.assertIsNotNone(entry)
        self.assertIn("待修范围受理", message)
        current = self.query()["index"]
        self.assertEqual(current["受理键"], ["pay-0001", "pay-0002"])

        rejected, message = conflict_diff_service.run_action(1, "直接完成")
        self.assertIsNone(rejected)
        self.assertIn("不属于", message)

    def test_generic_confirm_action_is_bounded_by_pending(self) -> None:
        entry, message = conflict_diff_service.run_action(1, "确认修复", ["pay-0001", "pay-9999"])
        self.assertIsNotNone(entry)
        self.assertIn("确认修复", message)
        current = self.query()["index"]
        self.assertEqual(current["已确认键"], ["pay-0001"])
        self.assertEqual(current["待修键"], ["pay-0002"])
        self.assertEqual(current["受理键"], [])

    def test_generic_create_keeps_index_identity(self) -> None:
        entry, missing = conflict_diff_service.create_entry({
            "目标表": TABLE, "冲突快照": S2, "日志位点对": POS2, "待修键": "k1,k2",
        })
        self.assertEqual(missing, [])
        self.assertIsNotNone(entry)
        self.assertEqual(entry["待修键"], ["k1", "k2"])
        entry, missing = conflict_diff_service.create_entry({
            "目标表": TABLE, "冲突快照": S1, "日志位点对": "binlog.000099:1",
        })
        self.assertIsNone(entry)
        self.assertTrue(missing)


class SeedStateIsolationTest(unittest.TestCase):
    def test_runtime_changes_do_not_mutate_seed_templates(self) -> None:
        from app.seed import SEED_ROWS

        before = copy.deepcopy(SEED_ROWS["conflict_diff"])
        index = copy.deepcopy(conflict_diff_service.query(
            target_table=TABLE, snapshot=S1, log_position=POS1)["index"])
        conflict_diff_service.recover(index, "待修")
        self.assertEqual(SEED_ROWS["conflict_diff"], before)


if __name__ == "__main__":
    unittest.main()
