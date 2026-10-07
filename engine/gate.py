"""覆盖率门禁：阈值配置、构建后评估、结论落盘与发布判定。

把覆盖率从「展示指标」变成「质量门禁」：

1. **阈值配置**：项目级 ``coverage_gate`` 配置（总覆盖率阈值、新增代码
   覆盖率阈值、是否启用），随项目记录持久化；
2. **构建后评估**：构建结束（且覆盖率已生成）后，:meth:`CoverageGate.evaluate`
   逐项检查阈值，得出门禁结论（passed / failed / disabled）；
3. **不达标即拦截**：门禁失败时把构建标记为不通过（原始测试结果保留在
   ``test_status``），结论写入构建目录 ``gate.json`` 并进入报告与
   发布判定；
4. **历史可回溯**：每次评估同步写入项目级 ``gate_history`` 存储，
   支持按项目查询历次门禁结论。
"""

from __future__ import annotations

import time
from typing import Optional

from .models import new_id

# 门禁结论状态
GATE_STATUSES = ["passed", "failed", "disabled"]

# 项目门禁配置的默认值（未配置时不拦截）
DEFAULT_GATE_CONFIG = {
    "enabled": False,
    "min_total_percent": None,
    "min_new_code_percent": None,
}

_THRESHOLD_KEYS = ("min_total_percent", "min_new_code_percent")


def validate_gate_config(payload: dict) -> tuple[Optional[dict], Optional[str]]:
    """校验并规范化门禁配置，返回 ``(配置, 错误信息)``。"""
    if payload is None:
        return None, "配置不能为空"
    config = {"enabled": bool(payload.get("enabled", False))}
    for key in _THRESHOLD_KEYS:
        value = payload.get(key)
        if value is None or value == "":
            config[key] = None
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None, f"{key} 必须是 0~100 的数字"
        if not 0 <= value <= 100:
            return None, f"{key} 必须在 0~100 之间"
        config[key] = round(value, 1)
    return config, None


def release_decision(build: dict, gate: Optional[dict]) -> dict:
    """发布判定：综合测试结果与门禁结论，给出「允许 / 阻止发布」。"""
    reasons = []
    test_status = build.get("test_status") or build.get("status")
    if test_status == "cancelled":
        reasons.append("构建已取消")
    elif test_status != "passed":
        reasons.append("测试未全部通过")
    if gate and gate.get("status") == "failed":
        for check in gate.get("checks", []):
            if not check.get("ok"):
                reasons.append(
                    f"覆盖率门禁未通过：{check['name']} {check['actual']}% "
                    f"低于阈值 {check['expected']}%")
    return {"decision": "block" if reasons else "allow", "reasons": reasons}


class CoverageGate:
    """覆盖率门禁。"""

    def __init__(self, registry, build_registry, coverage_analyzer):
        self.registry = registry
        self.builds = build_registry
        self.coverage = coverage_analyzer

    # -- 配置 -------------------------------------------------------------
    def get_config(self, project_id: str) -> dict:
        project = self.registry.store("projects").get(project_id)
        config = dict(DEFAULT_GATE_CONFIG)
        if project and isinstance(project.get("coverage_gate"), dict):
            config.update({k: project["coverage_gate"].get(k, config[k])
                           for k in config})
        return config

    def save_config(self, project_id: str, payload: dict) -> dict:
        """校验并保存项目门禁配置，返回 ``{"config": ...}`` 或 ``{"error": ...}``。"""
        if self.registry.store("projects").get(project_id) is None:
            return {"error": "项目不存在"}
        config, err = validate_gate_config(payload)
        if err:
            return {"error": err}
        self.registry.store("projects").update(project_id,
                                               {"coverage_gate": config})
        return {"config": config}

    # -- 评估 -------------------------------------------------------------
    def evaluate(self, project_id: str, build_id: str) -> dict:
        """评估一次构建的覆盖率门禁，落盘结论并按需标记构建不通过。"""
        store = self.builds.for_project(project_id)
        build = store.get(build_id)
        if build is None:
            return {"error": "构建不存在"}

        config = self.get_config(project_id)
        cov = self.coverage.get(project_id, build_id)
        diff = self.coverage.diff(project_id, build_id)
        if "error" in diff:
            return diff

        checks = []
        if config["enabled"]:
            if config["min_total_percent"] is not None:
                checks.append({
                    "name": "总覆盖率",
                    "metric": "total",
                    "actual": cov["percent"],
                    "expected": config["min_total_percent"],
                    "ok": cov["percent"] >= config["min_total_percent"],
                })
            if config["min_new_code_percent"] is not None:
                if not diff["changed_lines"]:
                    checks.append({
                        "name": "新增代码覆盖率",
                        "metric": "new_code",
                        "actual": None,
                        "expected": config["min_new_code_percent"],
                        "ok": True,
                        "note": "本次无新增代码，自动通过",
                    })
                else:
                    checks.append({
                        "name": "新增代码覆盖率",
                        "metric": "new_code",
                        "actual": diff["percent"],
                        "expected": config["min_new_code_percent"],
                        "ok": diff["percent"] >= config["min_new_code_percent"],
                    })

        if not config["enabled"]:
            status = "disabled"
        else:
            status = "failed" if any(not c["ok"] for c in checks) else "passed"

        conclusion = {
            "id": new_id("gate"),
            "project_id": project_id,
            "build_id": build_id,
            "build_name": build.get("name") or build_id,
            "status": status,
            "enabled": config["enabled"],
            "checks": checks,
            "thresholds": {k: config[k] for k in _THRESHOLD_KEYS},
            "total_percent": cov["percent"],
            "new_code_percent": diff["percent"],
            "new_code": {
                "base_build_id": diff["base_build_id"],
                "changed_lines": diff["changed_lines"],
                "covered_lines": diff["covered_lines"],
            },
            "release": "block" if status == "failed" else "allow",
            "evaluated_at": time.time(),
        }

        # 结论随构建落盘 + 写入项目级历史（可回溯）
        store.write_gate(build_id, conclusion)
        self.registry.store("gate_history").insert(conclusion)

        # 不达标 → 标记构建不通过（原始测试结果保留在 test_status）
        patch = {"gate_status": status}
        if status == "failed":
            patch["gate_failed"] = True
            if build.get("status") == "passed":
                patch["test_status"] = build["status"]
                patch["status"] = "failed"
        store.update(build_id, patch)

        if status == "failed":
            failed_checks = [c for c in checks if not c["ok"]]
            detail = "；".join(f"{c['name']} {c['actual']}% < 阈值 {c['expected']}%"
                               for c in failed_checks)
            store.append_log(build_id, f"覆盖率门禁未通过：{detail}，构建标记为不通过")
        elif status == "passed":
            store.append_log(build_id, "覆盖率门禁通过")
        return conclusion

    # -- 查询 -------------------------------------------------------------
    def get_conclusion(self, project_id: str, build_id: str) -> Optional[dict]:
        return self.builds.for_project(project_id).read_gate(build_id)

    def history(self, project_id: str, limit: int = 50) -> list[dict]:
        """项目历次门禁结论（按评估时间倒序，可回溯）。"""
        return self.registry.store("gate_history").query(
            where=[("project_id", "eq", project_id)],
            order_by="evaluated_at", order="desc", limit=limit)
