"""覆盖率门禁测试。

覆盖：新增代码覆盖率（两次构建对比）、门禁配置校验、构建后评估与拦截、
发布判定、历史结论回溯，以及调度器集成（门禁失败把构建标记为不通过）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (CoverageAnalyzer, CoverageGate, DefectManager,
                    EnvironmentManager, NotificationManager, ReportGenerator,
                    Scheduler, TestExecutor)
from engine.gate import release_decision, validate_gate_config
from storage import BuildStoreRegistry, StoreRegistry


def _make_env(data_root):
    registry = StoreRegistry(os.path.join(data_root, "store"), shard_size=50)
    builds = BuildStoreRegistry(os.path.join(data_root, "builds"))
    coverage = CoverageAnalyzer(builds)
    gate = CoverageGate(registry, builds, coverage)
    return registry, builds, coverage, gate


def _make_build(builds, pid, bid, passed=10, total=10, status="passed"):
    store = builds.for_project(pid)
    store.create(bid)
    store.set_total(bid, total)
    for i in range(total):
        store.record_result(bid, {
            "case_id": f"c{i}", "case_name": f"用例{i}", "group": "g",
            "priority": "P2", "duration": 0.01, "logs": [],
            "status": "passed" if i < passed else "failed",
        })
    store.finish(bid, status)
    return store.get(bid)


class TestCoverageDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry, self.builds, self.coverage, self.gate = _make_env(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_build_all_lines_are_new(self):
        _make_build(self.builds, "p1", "b1")
        diff = self.coverage.diff("p1", "b1")
        self.assertIsNone(diff["base_build_id"])
        self.assertIn("首次构建", diff["note"])
        cov = self.coverage.get("p1", "b1")
        # 全量代码视为新增：新增覆盖率 == 总覆盖率
        self.assertEqual(diff["changed_lines"], cov["total_lines"])
        self.assertEqual(diff["percent"], cov["percent"])

    def test_diff_against_previous_build(self):
        _make_build(self.builds, "p1", "b1")
        time.sleep(0.01)
        _make_build(self.builds, "p1", "b2")
        diff = self.coverage.diff("p1", "b2")
        self.assertEqual(diff["base_build_id"], "b1")
        self.assertGreater(diff["changed_lines"], 0)
        self.assertLess(diff["changed_lines"], self.coverage.get("p1", "b2")["total_lines"])
        self.assertLessEqual(diff["covered_lines"], diff["changed_lines"])
        self.assertIsNotNone(diff["percent"])
        # 与基线的总覆盖率差值
        self.assertEqual(diff["base_percent"], self.coverage.get("p1", "b1")["percent"])
        self.assertAlmostEqual(
            diff["delta_percent"],
            round(self.coverage.get("p1", "b2")["percent"] - diff["base_percent"], 1))

    def test_diff_is_deterministic(self):
        _make_build(self.builds, "p1", "b1")
        time.sleep(0.01)
        _make_build(self.builds, "p1", "b2")
        d1 = self.coverage.diff("p1", "b2")
        d2 = self.coverage.diff("p1", "b2")
        self.assertEqual(d1["changed_lines"], d2["changed_lines"])
        self.assertEqual(d1["percent"], d2["percent"])
        self.assertEqual(d1["files"], d2["files"])

    def test_diff_explicit_base_unions_intermediate_changes(self):
        _make_build(self.builds, "p1", "b1")
        time.sleep(0.01)
        _make_build(self.builds, "p1", "b2")
        time.sleep(0.01)
        _make_build(self.builds, "p1", "b3")
        direct = self.coverage.diff("p1", "b3")               # 基线 = b2
        spanning = self.coverage.diff("p1", "b3", base_build_id="b1")  # 基线 = b1
        self.assertEqual(spanning["base_build_id"], "b1")
        # 跨两次构建的改动并集不少于单次的改动
        self.assertGreaterEqual(spanning["changed_lines"], direct["changed_lines"])

    def test_diff_invalid_base(self):
        _make_build(self.builds, "p1", "b1")
        time.sleep(0.01)
        _make_build(self.builds, "p1", "b2")
        self.assertIn("error", self.coverage.diff("p1", "b2", base_build_id="nope"))
        # 基线不能比当前构建更新
        self.assertIn("error", self.coverage.diff("p1", "b1", base_build_id="b2"))

    def test_trend_includes_new_code_percent(self):
        _make_build(self.builds, "p1", "b1")
        time.sleep(0.01)
        _make_build(self.builds, "p1", "b2")
        points = self.coverage.trend("p1")["points"]
        self.assertEqual(len(points), 2)
        self.assertIn("new_code_percent", points[0])
        self.assertIsNotNone(points[1]["new_code_percent"])


class TestGateConfig(unittest.TestCase):
    def test_validate(self):
        cfg, err = validate_gate_config({"enabled": True, "min_total_percent": 70,
                                         "min_new_code_percent": 80})
        self.assertIsNone(err)
        self.assertEqual(cfg["min_total_percent"], 70.0)
        _, err = validate_gate_config({"min_total_percent": 120})
        self.assertIsNotNone(err)
        _, err = validate_gate_config({"min_total_percent": "abc"})
        self.assertIsNotNone(err)
        cfg, err = validate_gate_config({"enabled": False})
        self.assertIsNone(err)
        self.assertIsNone(cfg["min_total_percent"])

    def test_save_and_get(self):
        with tempfile.TemporaryDirectory() as d:
            registry, builds, coverage, gate = _make_env(d)
            pid = registry.store("projects").insert({"name": "P"})
            result = gate.save_config(pid, {"enabled": True, "min_total_percent": 60})
            self.assertIn("config", result)
            self.assertTrue(gate.get_config(pid)["enabled"])
            self.assertEqual(gate.get_config(pid)["min_total_percent"], 60.0)
            self.assertIn("error", gate.save_config(pid, {"min_total_percent": -1}))
            self.assertIn("error", gate.save_config("missing", {"enabled": True}))


class TestGateEvaluate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry, self.builds, self.coverage, self.gate = _make_env(self.tmp.name)
        self.pid = self.registry.store("projects").insert({"name": "P"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_gate_does_not_block(self):
        _make_build(self.builds, self.pid, "b1")
        conclusion = self.gate.evaluate(self.pid, "b1")
        self.assertEqual(conclusion["status"], "disabled")
        build = self.builds.for_project(self.pid).get("b1")
        self.assertEqual(build["status"], "passed")  # 不拦截
        self.assertEqual(build["gate_status"], "disabled")

    def test_passing_gate(self):
        self.gate.save_config(self.pid, {"enabled": True, "min_total_percent": 0,
                                         "min_new_code_percent": 0})
        _make_build(self.builds, self.pid, "b1")
        conclusion = self.gate.evaluate(self.pid, "b1")
        self.assertEqual(conclusion["status"], "passed")
        self.assertEqual(conclusion["release"], "allow")
        self.assertEqual(len(conclusion["checks"]), 2)
        build = self.builds.for_project(self.pid).get("b1")
        self.assertEqual(build["status"], "passed")

    def test_failing_gate_marks_build_failed(self):
        # 阈值 100%：模拟覆盖率最高 99%，必然不达标
        self.gate.save_config(self.pid, {"enabled": True, "min_total_percent": 100})
        _make_build(self.builds, self.pid, "b1")
        conclusion = self.gate.evaluate(self.pid, "b1")
        self.assertEqual(conclusion["status"], "failed")
        self.assertEqual(conclusion["release"], "block")
        failed_checks = [c for c in conclusion["checks"] if not c["ok"]]
        self.assertEqual(len(failed_checks), 1)
        self.assertEqual(failed_checks[0]["metric"], "total")

        build = self.builds.for_project(self.pid).get("b1")
        self.assertEqual(build["status"], "failed")       # 构建被标记为不通过
        self.assertEqual(build["test_status"], "passed")  # 原始测试结果保留
        self.assertTrue(build["gate_failed"])

        # 结论随构建落盘，可回读
        stored = self.builds.for_project(self.pid).read_gate("b1")
        self.assertEqual(stored["id"], conclusion["id"])

        # 构建日志里有门禁说明
        logs = self.builds.for_project(self.pid).read_logs("b1")
        self.assertTrue(any("门禁" in line for line in logs["lines"]))

    def test_new_code_threshold_uses_diff(self):
        self.gate.save_config(self.pid, {"enabled": True, "min_new_code_percent": 100})
        _make_build(self.builds, self.pid, "b1")
        time.sleep(0.01)
        _make_build(self.builds, self.pid, "b2")
        conclusion = self.gate.evaluate(self.pid, "b2")
        check = next(c for c in conclusion["checks"] if c["metric"] == "new_code")
        diff = self.coverage.diff(self.pid, "b2")
        self.assertEqual(check["actual"], diff["percent"])
        self.assertEqual(conclusion["new_code"]["base_build_id"], "b1")
        self.assertEqual(conclusion["status"], "failed")

    def test_history_is_traceable(self):
        self.gate.save_config(self.pid, {"enabled": True, "min_total_percent": 50})
        for bid in ("b1", "b2"):
            _make_build(self.builds, self.pid, bid)
            self.gate.evaluate(self.pid, bid)
            time.sleep(0.01)
        history = self.gate.history(self.pid)
        self.assertEqual(len(history), 2)
        # 倒序：最新的在前
        self.assertEqual(history[0]["build_id"], "b2")
        self.assertEqual(history[1]["build_id"], "b1")
        for record in history:
            self.assertIn("evaluated_at", record)
            self.assertIn("thresholds", record)

    def test_report_contains_gate_and_release(self):
        self.gate.save_config(self.pid, {"enabled": True, "min_total_percent": 100})
        _make_build(self.builds, self.pid, "b1")
        self.gate.evaluate(self.pid, "b1")
        report = ReportGenerator(self.builds).build_report(self.pid, "b1", force=True)
        self.assertEqual(report["gate"]["status"], "failed")
        self.assertEqual(report["release"]["decision"], "block")
        self.assertTrue(any("门禁" in r for r in report["release"]["reasons"]))
        # 缓存的报告也带门禁结论
        cached = self.builds.for_project(self.pid).read_report("b1")
        self.assertEqual(cached["release"]["decision"], "block")


class TestReleaseDecision(unittest.TestCase):
    def test_allow_when_all_passed(self):
        d = release_decision({"status": "passed"}, {"status": "passed", "checks": []})
        self.assertEqual(d["decision"], "allow")

    def test_block_on_test_failure(self):
        d = release_decision({"status": "failed"}, None)
        self.assertEqual(d["decision"], "block")

    def test_block_on_gate_failure_with_reason(self):
        gate = {"status": "failed", "checks": [
            {"name": "总覆盖率", "actual": 62.0, "expected": 70.0, "ok": False}]}
        d = release_decision({"status": "failed", "test_status": "passed"}, gate)
        self.assertEqual(d["decision"], "block")
        self.assertTrue(any("62.0" in r for r in d["reasons"]))


class TestGateSchedulerIntegration(unittest.TestCase):
    """端到端：调度器收尾时自动评估门禁，失败则构建不通过并进报告。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.registry = StoreRegistry(os.path.join(root, "store"), shard_size=50)
        self.builds = BuildStoreRegistry(os.path.join(root, "builds"))
        env_mgr = EnvironmentManager(self.registry, root)
        coverage = CoverageAnalyzer(self.builds)
        self.gate = CoverageGate(self.registry, self.builds, coverage)
        report = ReportGenerator(self.builds)
        defects = DefectManager(self.registry)
        notify = NotificationManager(self.registry)
        self.sched = Scheduler(self.registry, self.builds, TestExecutor(), env_mgr,
                               report, coverage, defects, notify,
                               max_build_workers=2, max_case_workers=4,
                               tick_seconds=0.2, gate=self.gate)
        self.env_mgr = env_mgr

    def tearDown(self):
        self.sched.shutdown()
        self.tmp.cleanup()

    def _setup_project(self, gate_config):
        pid = self.registry.store("projects").insert(
            {"name": "P", "coverage_gate": gate_config})
        env = self.env_mgr.create(pid, {"name": "dev",
                                        "config": {"latency_ms": 0, "fail_rate": 0.0}})
        cases_store = self.registry.store("cases")
        ids = []
        for i in range(6):
            ids.append(cases_store.insert({
                "id": f"case_{i}", "project_id": pid, "name": f"用例{i}",
                "priority": "P2", "tags": ["g"], "timeout": 30,
                "steps": [
                    {"action": "request", "method": "GET", "url": "/api/health"},
                    {"action": "assert", "type": "status",
                     "actual": "${resp.status}", "expected": 200},
                ],
            }))
        suite = {"id": "suite_1", "project_id": pid, "name": "冒烟",
                 "env_id": env["id"], "case_ids": ids}
        self.registry.store("suites").insert(suite)
        return pid, suite

    def _wait_build(self, pid, build_id, timeout=20):
        """等构建收尾完成：终态 + 门禁已评估 + 报告已落盘。"""
        deadline = time.time() + timeout
        store = self.builds.for_project(pid)
        while time.time() < deadline:
            build = store.get(build_id)
            if build and build["status"] in ("passed", "failed", "cancelled", "error") \
                    and build.get("gate_status") \
                    and store.read_report(build_id) is not None:
                return build
            time.sleep(0.05)
        return store.get(build_id)

    def test_gate_blocks_release_when_coverage_below_threshold(self):
        pid, suite = self._setup_project({"enabled": True, "min_total_percent": 100})
        result = self.sched.submit_build(pid, suite["id"])
        build = self._wait_build(pid, result["id"])
        # 测试本身全过，但覆盖率门禁把构建标记为不通过
        self.assertEqual(build["passed"], 6)
        self.assertEqual(build["status"], "failed")
        self.assertEqual(build["test_status"], "passed")
        self.assertEqual(build["gate_status"], "failed")

        # 报告含门禁结论与发布判定
        report = self.builds.for_project(pid).read_report(result["id"])
        self.assertEqual(report["gate"]["status"], "failed")
        self.assertEqual(report["release"]["decision"], "block")

        # 历史可回溯
        history = self.gate.history(pid)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["build_id"], result["id"])

    def test_gate_passes_when_thresholds_met(self):
        pid, suite = self._setup_project({"enabled": True, "min_total_percent": 0})
        result = self.sched.submit_build(pid, suite["id"])
        build = self._wait_build(pid, result["id"])
        self.assertEqual(build["status"], "passed")
        self.assertEqual(build["gate_status"], "passed")
        report = self.builds.for_project(pid).read_report(result["id"])
        self.assertEqual(report["release"]["decision"], "allow")


if __name__ == "__main__":
    unittest.main()
