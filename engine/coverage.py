"""代码覆盖率分析（模拟）。

平台没有真实仓库，覆盖率由「模拟源码树 + 确定性伪随机」生成：每个项目
有一组固定的模拟模块（文件 → 行数），每次构建按 ``project_id + build_id``
作随机种子，计算每个文件具体覆盖到哪些行以及百分比。这样：

- 同一构建多次读取覆盖率**稳定一致**（种子确定）；
- 不同构建之间覆盖率有波动，可用于画出趋势；
- 覆盖率结果保留到行号，门禁可以把两次构建之间的模拟变更行单独聚合，
  得到「本次新增 / 修改代码覆盖率」；
- 覆盖率与构建通过率弱相关（通过率越高，平均覆盖率略高），
  体现「测试跑得越充分，覆盖越全」的直觉。
"""

from __future__ import annotations

import hashlib
import random
import time
from typing import Any, Optional

# 模拟源码树：模块路径 -> 行数（每个项目一致，保证跨项目口径可比）
_MODULES: list[tuple[str, int]] = [
    ("src/api/auth.py", 420),
    ("src/api/users.py", 368),
    ("src/api/projects.py", 290),
    ("src/core/executor.py", 512),
    ("src/core/scheduler.py", 448),
    ("src/core/validator.py", 236),
    ("src/core/report.py", 305),
    ("src/storage/lock.py", 188),
    ("src/storage/sharded.py", 402),
    ("src/utils/retry.py", 122),
    ("src/utils/log.py", 96),
    ("src/utils/http.py", 158),
]

# 模拟两次构建之间代码行发生变化的概率。
_CHANGED_LINE_RATE = 12


def _seed(*parts: Any) -> int:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(h[:12], 16)


class CoverageAnalyzer:
    """覆盖率分析器。"""

    def __init__(self, build_store_registry):
        self.builds = build_store_registry

    # -- 单次构建 ---------------------------------------------------------
    def generate(self, project_id: str, build_id: str,
                 passed_ratio: float = 1.0) -> dict:
        """生成并缓存一次构建的覆盖率报告。"""
        rng = random.Random(_seed(project_id, build_id, "coverage"))
        files = []
        total_lines = 0
        total_covered = 0
        for path, lines in _MODULES:
            # 通过率越高，覆盖率基线越高；叠加每文件独立的确定性波动
            base = 0.45 + 0.40 * max(0.0, min(1.0, passed_ratio))
            jitter = rng.uniform(-0.18, 0.18)
            ratio = max(0.05, min(0.99, base + jitter))
            covered_count = int(round(lines * ratio))
            covered_lines = sorted(rng.sample(range(1, lines + 1), covered_count))
            covered = len(covered_lines)
            total_lines += lines
            total_covered += covered
            files.append({
                "file": path,
                "lines": lines,
                "covered": covered,
                "covered_lines": covered_lines,
                "missed": lines - covered,
                "percent": round(covered / lines * 100, 1),
            })
        percent = round(total_covered / total_lines * 100, 1)
        coverage = {
            "project_id": project_id,
            "build_id": build_id,
            "percent": percent,
            "total_lines": total_lines,
            "covered_lines": total_covered,
            "missed_lines": total_lines - total_covered,
            "files": sorted(files, key=lambda f: f["percent"]),
            "generated_at": time.time(),
        }
        self.builds.for_project(project_id).write_coverage(build_id, coverage)
        return coverage

    def get(self, project_id: str, build_id: str) -> dict:
        cov = self.builds.for_project(project_id).read_coverage(build_id)
        if cov is None:
            build = self.builds.for_project(project_id).get(build_id)
            passed_ratio = 1.0
            if build:
                total = build.get("total", 0)
                passed = build.get("passed", 0)
                passed_ratio = (passed / total) if total else 1.0
            return self.generate(project_id, build_id, passed_ratio)
        return self._ensure_line_detail(project_id, build_id, cov)

    def _ensure_line_detail(self, project_id: str, build_id: str,
                            coverage: dict) -> dict:
        """兼容旧覆盖率缓存：缺少行号明细时重新生成当前确定性报告。"""
        if all("covered_lines" in item for item in coverage.get("files", [])):
            return coverage
        build = self.builds.for_project(project_id).get(build_id)
        passed_ratio = 1.0
        if build:
            total = build.get("total", 0)
            passed = build.get("passed", 0)
            passed_ratio = (passed / total) if total else 1.0
        return self.generate(project_id, build_id, passed_ratio)

    # -- 新增 / 修改代码 --------------------------------------------------
    def diff_coverage(self, project_id: str, build_id: str,
                      baseline_build_id: str,
                      coverage: Optional[dict] = None) -> dict:
        """对比两次构建，计算当前构建变更行的覆盖率。

        平台没有真实 Git diff，因此用 ``baseline_build_id + build_id +
        文件 + 行号`` 生成稳定的模拟 diff；同一组两次构建重复计算结果一致。
        """
        current = coverage or self.get(project_id, build_id)
        current_files = {f["file"]: set(f.get("covered_lines", []))
                         for f in current.get("files", [])}
        files = []
        changed_total = 0
        changed_covered = 0
        for path, line_count in _MODULES:
            changed_lines = []
            covered_lines = set(current_files.get(path, set()))
            for line in range(1, line_count + 1):
                if _seed(project_id, baseline_build_id, build_id, path, line,
                         "diff") % 100 < _CHANGED_LINE_RATE:
                    changed_lines.append(line)
            covered = sum(1 for line in changed_lines if line in covered_lines)
            total = len(changed_lines)
            changed_total += total
            changed_covered += covered
            files.append({
                "file": path,
                "changed_lines": total,
                "covered_lines": covered,
                "missed_lines": total - covered,
                "percent": round(covered / total * 100, 1) if total else 100.0,
                "lines": changed_lines,
            })
        return {
            "project_id": project_id,
            "build_id": build_id,
            "baseline_build_id": baseline_build_id,
            "percent": round(changed_covered / changed_total * 100, 1)
            if changed_total else 100.0,
            "changed_lines": changed_total,
            "covered_lines": changed_covered,
            "missed_lines": changed_total - changed_covered,
            "has_changes": changed_total > 0,
            "files": sorted(files, key=lambda f: (f["percent"], -f["changed_lines"])),
            "generated_at": time.time(),
        }

    # -- 趋势 -------------------------------------------------------------
    def trend(self, project_id: str, limit: int = 20) -> dict:
        store = self.builds.for_project(project_id)
        builds = store.list_builds()[:limit]
        points = []
        for b in reversed(builds):  # 时间正序
            cov = self.get(project_id, b["id"])
            gate = b.get("quality_gate") or {}
            points.append({
                "build_id": b["id"],
                "name": b.get("name") or b["id"],
                "status": b.get("status"),
                "percent": cov["percent"],
                "passed_ratio": (b.get("passed", 0) / b["total"]) if b.get("total") else 0,
                "quality_gate_status": b.get("quality_gate_status"),
                "release_allowed": b.get("release_allowed"),
                "new_code_percent": gate.get("new_code_coverage"),
                "finished_at": b.get("finished_at"),
            })
        return {"project_id": project_id, "points": points}
