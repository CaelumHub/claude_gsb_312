"""代码覆盖率分析（模拟）。

平台没有真实仓库，覆盖率由「模拟源码树 + 确定性伪随机」生成：每个项目
有一组固定的模拟模块（文件 → 行数），每次构建按 ``build_id`` 作随机种子
计算每个文件覆盖的行数与百分比。这样：

- 同一构建多次读取覆盖率**稳定一致**（种子确定）；
- 不同构建之间覆盖率有波动，可用于画出趋势；
- 覆盖率与构建通过率弱相关（通过率越高，平均覆盖率略高），
  体现「测试跑得越充分，覆盖越全」的直觉。

新增代码覆盖率（对比两次构建）
------------------------------
每次构建相对其父构建「改动了哪些行」同样用确定性伪随机模拟
（种子 = 项目 + 构建）：:func:`CoverageAnalyzer.changed_ranges` 给出每个
文件被改动的行区间。对比任意两次构建时，把基线之后、本次之前（含本次）
所有构建的改动区间按文件求并集，再用本次构建的覆盖行集合（同样确定性
可重算）统计「改动行里有多少被覆盖」，即新增代码覆盖率。
"""

from __future__ import annotations

import hashlib
import random
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


def _seed(*parts: Any) -> int:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(h[:12], 16)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠 / 相邻的行区间（闭区间），返回按起点排序的不相交区间。"""
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


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
            covered = int(round(lines * ratio))
            total_lines += lines
            total_covered += covered
            files.append({
                "file": path,
                "lines": lines,
                "covered": covered,
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
            "generated_at": __import__("time").time(),
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
        return cov

    # -- 趋势 -------------------------------------------------------------
    def trend(self, project_id: str, limit: int = 20) -> dict:
        store = self.builds.for_project(project_id)
        builds = store.list_builds()[:limit]
        points = []
        for b in reversed(builds):  # 时间正序
            cov = self.get(project_id, b["id"])
            diff = self.diff(project_id, b["id"])
            points.append({
                "build_id": b["id"],
                "name": b.get("name") or b["id"],
                "status": b.get("status"),
                "percent": cov["percent"],
                "new_code_percent": diff.get("percent"),
                "passed_ratio": (b.get("passed", 0) / b["total"]) if b.get("total") else 0,
                "finished_at": b.get("finished_at"),
            })
        return {"project_id": project_id, "points": points}

    # -- 新增代码覆盖率（对比两次构建） ------------------------------------
    def changed_ranges(self, project_id: str, build_id: str) -> dict:
        """本次构建相对其父构建改动的行区间（确定性模拟）。

        返回 ``{文件路径: [(起始行, 结束行), ...]}``（闭区间、已合并）。
        同一构建多次调用结果一致，因此任意两次构建之间的「改动集合」
        可以把中间每次构建的改动区间求并集得到。
        """
        rng = random.Random(_seed(project_id, build_id, "diff"))
        changed: dict[str, list] = {}
        for path, lines in _MODULES:
            if rng.random() >= 0.55:  # 该文件本次未改动
                continue
            ranges = []
            for _ in range(rng.randint(1, 3)):
                length = max(1, int(lines * rng.uniform(0.03, 0.25)))
                start = rng.randint(1, max(1, lines - length + 1))
                ranges.append((start, min(lines, start + length - 1)))
            changed[path] = _merge_ranges(ranges)
        return changed

    def _covered_line_set(self, project_id: str, build_id: str, path: str,
                          lines: int, covered: int) -> set:
        """某文件被覆盖的具体行号集合（确定性抽样，可重算，不落盘）。"""
        rng = random.Random(_seed(project_id, build_id, "covered-lines", path))
        return set(rng.sample(range(1, lines + 1), max(0, min(covered, lines))))

    def diff(self, project_id: str, build_id: str,
             base_build_id: Optional[str] = None) -> dict:
        """对比两次构建，算出「本次改动涉及的那部分代码」被覆盖了多少。

        - ``base_build_id`` 为空时默认取当前构建的上一次构建；
        - 改动集合 = 基线之后、本次之前（含本次）所有构建改动区间的并集；
        - 首次构建（无更早构建）时全量代码视为新增；
        - 覆盖判定以**本次构建**的覆盖行为准。
        """
        store = self.builds.for_project(project_id)
        current = self.get(project_id, build_id)
        builds = store.list_builds()  # 按创建时间倒序，下标越小越新
        order = {b["id"]: i for i, b in enumerate(builds)}
        if build_id not in order:
            return {"error": "构建不存在"}
        current_idx = order[build_id]

        note = ""
        if base_build_id is None:
            if current_idx + 1 < len(builds):
                base_build_id = builds[current_idx + 1]["id"]
            else:
                note = "首次构建，全量代码视为新增"
        elif base_build_id not in order:
            return {"error": "基线构建不存在"}
        elif order[base_build_id] <= current_idx:
            return {"error": "基线构建必须早于当前构建"}

        # 1) 改动集合：基线之后到本次构建的改动区间并集（首次构建 = 全部行）
        union: dict[str, list] = {}
        if base_build_id is None:
            for path, lines in _MODULES:
                union[path] = [(1, lines)]
        else:
            base_idx = order[base_build_id]
            for b in builds[current_idx:base_idx]:
                for path, ranges in self.changed_ranges(project_id, b["id"]).items():
                    union[path] = _merge_ranges(union.get(path, []) + ranges)

        # 2) 用本次构建的覆盖行集合统计改动行的覆盖情况
        covered_by_file = {f["file"]: f for f in current.get("files", [])}
        files = []
        total_changed = 0
        total_covered = 0
        for path, lines in _MODULES:
            ranges = union.get(path)
            if not ranges:
                continue
            changed = sum(end - start + 1 for start, end in ranges)
            info = covered_by_file.get(path, {})
            covered_set = self._covered_line_set(
                project_id, build_id, path, lines, info.get("covered", 0))
            covered = sum(1 for start, end in ranges
                          for line in range(start, end + 1)
                          if line in covered_set)
            total_changed += changed
            total_covered += covered
            files.append({
                "file": path,
                "changed": changed,
                "covered": covered,
                "missed": changed - covered,
                "percent": round(covered / changed * 100, 1) if changed else None,
            })

        # 3) 总体覆盖率相对基线的变化
        base_percent = None
        delta = None
        if base_build_id is not None:
            base_percent = self.get(project_id, base_build_id)["percent"]
            delta = round(current["percent"] - base_percent, 1)

        return {
            "project_id": project_id,
            "build_id": build_id,
            "base_build_id": base_build_id,
            "changed_lines": total_changed,
            "covered_lines": total_covered,
            "missed_lines": total_changed - total_covered,
            "percent": round(total_covered / total_changed * 100, 1)
            if total_changed else None,
            "files": sorted(files, key=lambda f: (f["percent"] is None, f["percent"])),
            "total_percent": current["percent"],
            "base_percent": base_percent,
            "delta_percent": delta,
            "note": note,
        }
