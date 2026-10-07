"""覆盖率质量门禁。

构建执行完成后，门禁把「测试结果 + 总覆盖率 + 本次变更代码覆盖率」统一转成
可发布结论：

- 总覆盖率低于项目阈值：构建失败、禁止发布；
- 新增 / 修改代码覆盖率低于项目阈值：构建失败、禁止发布；
- 没有上一场构建可对比，或本次没有变更行：新增代码门禁跳过，不阻断发布；
- 门禁关闭：仍记录 ``skipped`` 结论，方便报告说明该构建没有被发布规则约束。

每次结论既写入构建目录下的 ``quality_gate.json``，也写入通用
``quality_gates`` 存储，用于跨项目检索和历史回溯。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .models import new_id

DEFAULT_COVERAGE_GATE = {
    "enabled": False,
    "overall_threshold": 80.0,
    "new_code_threshold": 85.0,
}


def normalize_coverage_gate(data: dict | None = None) -> dict:
    """读取并校验项目上的覆盖率门禁配置。"""
    gate = dict(DEFAULT_COVERAGE_GATE)
    if isinstance(data, dict):
        gate.update(data)

    gate["enabled"] = bool(gate.get("enabled", False))
    for key in ("overall_threshold", "new_code_threshold"):
        try:
            value = float(gate.get(key, DEFAULT_COVERAGE_GATE[key]))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是 0 到 100 之间的数字")
        if not 0 <= value <= 100:
            raise ValueError(f"{key} 必须在 0 到 100 之间")
        gate[key] = value
    return gate


class QualityGateEvaluator:
    """生成、持久化并检索覆盖率门禁结论。"""

    def __init__(self, registry, build_registry, coverage_analyzer):
        self.registry = registry
        self.builds = build_registry
        self.coverage = coverage_analyzer

    def get_project_config(self, project_id: str) -> dict:
        project = self.registry.store("projects").get(project_id)
        config = (project or {}).get("coverage_gate") or {}
        return normalize_coverage_gate(config)

    def evaluate_build(self, project_id: str, build_id: str,
                       coverage: Optional[dict] = None,
                       baseline_build_id: Optional[str] = None,
                       config: Optional[dict] = None) -> dict:
        """计算并保存一场构建的门禁结论。"""
        store = self.builds.for_project(project_id)
        build = store.get(build_id)
        if build is None:
            raise ValueError("构建不存在")

        config = normalize_coverage_gate(config) if config is not None \
            else self.get_project_config(project_id)
        coverage = coverage or self.coverage.get(project_id, build_id)
        status = build.get("test_status") or build.get("status")

        if not config.get("enabled", False):
            gate = self._base_result(project_id, build_id, build, coverage, config)
            gate.update({
                "gate_status": "skipped",
                "status": status,
                "release_allowed": status == "passed",
                "reason": "覆盖率门禁未启用",
                "checks": [],
            })
        elif status == "cancelled":
            gate = self._base_result(project_id, build_id, build, coverage, config)
            gate.update({
                "gate_status": "skipped",
                "status": status,
                "release_allowed": False,
                "reason": "构建已取消，未执行覆盖率门禁",
                "checks": [
                    {
                        "key": "tests",
                        "name": "测试执行",
                        "required": True,
                        "passed": False,
                        "actual": status,
                        "threshold": "passed",
                        "message": "构建已取消",
                    }
                ],
            })
        else:
            build_for_gate = dict(build)
            build_for_gate["status"] = status
            baseline_id, baseline, diff = self._resolve_baseline(
                project_id, build_id, baseline_build_id, coverage)
            gate = self._evaluate_enabled(
                project_id, build_id, build_for_gate, coverage, config,
                baseline_id, baseline, diff)

        gate["evaluated_at"] = time.time()
        store.write_quality_gate(build_id, gate)
        store.apply_quality_gate(build_id, gate)
        self._save_history(gate)
        return gate

    def _evaluate_enabled(self, project_id, build_id, build, coverage,
                          config, baseline_id, baseline, diff) -> dict:
        checks = []

        # 测试失败是最基础的发布门禁。
        test_passed = build.get("status") == "passed"
        checks.append({
            "key": "tests",
            "name": "测试执行",
            "required": True,
            "passed": test_passed,
            "actual": build.get("status"),
            "threshold": "passed",
            "message": "全部用例通过" if test_passed else "存在失败、错误或超时用例",
        })

        overall_percent = float(coverage.get("percent", 0.0))
        overall_threshold = float(config["overall_threshold"])
        checks.append({
            "key": "overall_coverage",
            "name": "总覆盖率",
            "required": True,
            "passed": overall_percent >= overall_threshold,
            "actual": overall_percent,
            "threshold": overall_threshold,
            "unit": "%",
            "message": f"{overall_percent}% / 阈值 {overall_threshold}%",
        })

        if baseline_id and diff.get("has_changes"):
            new_percent = float(diff.get("percent", 0.0))
            new_threshold = float(config["new_code_threshold"])
            checks.append({
                "key": "new_code_coverage",
                "name": "新增代码覆盖率",
                "required": True,
                "passed": new_percent >= new_threshold,
                "actual": new_percent,
                "threshold": new_threshold,
                "unit": "%",
                "changed_lines": diff.get("changed_lines", 0),
                "covered_lines": diff.get("covered_lines", 0),
                "message": f"{new_percent}% / 阈值 {new_threshold}%，"
                           f"变更 {diff.get('changed_lines', 0)} 行",
            })
        else:
            checks.append({
                "key": "new_code_coverage",
                "name": "新增代码覆盖率",
                "required": False,
                "passed": True,
                "skipped": True,
                "actual": diff.get("percent") if baseline_id else None,
                "threshold": float(config["new_code_threshold"]),
                "unit": "%",
                "changed_lines": diff.get("changed_lines", 0) if baseline_id else 0,
                "covered_lines": diff.get("covered_lines", 0) if baseline_id else 0,
                "message": "没有可对比的上一场构建，新增代码覆盖率跳过"
                if not baseline_id else "本次构建没有变更行，新增代码覆盖率跳过",
            })

        failed_checks = [c for c in checks if c.get("required") and not c.get("passed")]
        gate_passed = not failed_checks
        final_status = "passed" if gate_passed else "failed"
        reason = "通过：测试与总覆盖率满足要求" if gate_passed \
            else "；".join(c["message"] for c in failed_checks)
        if gate_passed and any(c.get("skipped") for c in checks):
            reason += "；无可对比构建或无变更行，新增代码覆盖率跳过"

        gate = self._base_result(project_id, build_id, build, coverage, config)
        gate.update({
            "gate_status": "passed" if gate_passed else "failed",
            "status": final_status,
            "release_allowed": gate_passed,
            "reason": reason,
            "checks": checks,
            "baseline_build_id": baseline_id,
            "baseline_name": (baseline or {}).get("name") or baseline_id,
            "diff_coverage": diff if baseline_id else None,
        })
        return gate

    def _resolve_baseline(self, project_id, build_id, requested_id, coverage):
        store = self.builds.for_project(project_id)
        build = store.get(build_id)
        if requested_id:
            baseline = store.get(requested_id)
            if baseline is None or requested_id == build_id:
                return None, None, self._empty_diff(project_id, build_id, None)
            diff = self.coverage.diff_coverage(
                project_id, build_id, requested_id, coverage)
            return requested_id, baseline, diff

        created_at = build.get("created_at", 0)
        candidates = [
            b for b in store.list_builds()
            if b.get("id") != build_id
            and b.get("created_at", 0) <= created_at
            and b.get("status") in ("passed", "failed")
        ]
        if not candidates:
            return None, None, self._empty_diff(project_id, build_id, None)
        baseline = sorted(
            candidates,
            key=lambda b: (b.get("created_at", 0), b.get("finished_at") or 0),
            reverse=True,
        )[0]
        baseline_id = baseline["id"]
        diff = self.coverage.diff_coverage(
            project_id, build_id, baseline_id, coverage)
        return baseline_id, baseline, diff

    @staticmethod
    def _empty_diff(project_id: str, build_id: str,
                    baseline_id: Optional[str]) -> dict:
        return {
            "project_id": project_id,
            "build_id": build_id,
            "baseline_build_id": baseline_id,
            "percent": None,
            "changed_lines": 0,
            "covered_lines": 0,
            "missed_lines": 0,
            "has_changes": False,
            "files": [],
        }

    def _base_result(self, project_id: str, build_id: str, build: dict,
                     coverage: dict, config: dict) -> dict[str, Any]:
        return {
            "id": new_id("gate"),
            "project_id": project_id,
            "build_id": build_id,
            "build_name": build.get("name") or build_id,
            "gate_status": None,
            "status": build.get("test_status") or build.get("status"),
            "test_status": build.get("test_status") or build.get("status"),
            "release_allowed": False,
            "reason": "",
            "thresholds": {
                "overall_coverage": float(config["overall_threshold"]),
                "new_code_coverage": float(config["new_code_threshold"]),
            },
            "coverage": {
                "percent": coverage.get("percent"),
                "total_lines": coverage.get("total_lines"),
                "covered_lines": coverage.get("covered_lines"),
                "missed_lines": coverage.get("missed_lines"),
            },
            "diff_coverage": None,
            "baseline_build_id": None,
            "baseline_name": None,
            "trigger": build.get("trigger"),
            "started_at": build.get("started_at"),
            "finished_at": build.get("finished_at"),
            "evaluated_at": None,
        }

    def _save_history(self, gate: dict) -> None:
        history_store = self.registry.store("quality_gates")
        record = dict(gate)
        record["project_name"] = (
            self.registry.store("projects").get(gate["project_id"]) or {}
        ).get("name", "")
        history_store.upsert_by("build_id", gate["build_id"], record)

    def get_for_build(self, project_id: str, build_id: str) -> Optional[dict]:
        return self.builds.for_project(project_id).read_quality_gate(build_id)

    def history(self, project_id: Optional[str] = None,
                limit: int = 50) -> list[dict]:
        store = self.registry.store("quality_gates")
        where = [("project_id", "eq", project_id)] if project_id else None
        return store.query(where=where, order_by="evaluated_at",
                           order="desc", limit=limit)
