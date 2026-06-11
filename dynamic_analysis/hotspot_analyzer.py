import json
import os
import re
from datetime import datetime
from collections import defaultdict
from common.logger import logger


class HotspotAnalyzer:
    """多事件性能分析器 — 解析 perf script 并生成综合性能报告"""

    # 事件名 -> 归一化类别
    EVENT_CATEGORIES = {
        "cycles": "cycles",
        "instructions": "instructions",
        "cache-references": "cache_references",
        "cache-misses": "cache_misses",
        "branch-instructions": "branch_instructions",
        "branch-misses": "branch_misses",
        "L1-dcache-loads": "L1_dcache_loads",
        "L1-dcache-load-misses": "L1_dcache_load_misses",
        "LLC-loads": "LLC_loads",
        "LLC-load-misses": "LLC_load_misses",
    }

    def __init__(self, output_dir, project_config, ast_data=None):
        self.output_dir = output_dir
        self.config = project_config
        self.ast_data = ast_data  # 可选，来自 AST 解析的 {functions, classes, call_graph}
        os.makedirs(output_dir, exist_ok=True)

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    def analyze(self, perf_data):
        """主入口：解析 perf 数据，生成报告"""
        logger.info("开始分析perf数据（多事件 + 调用树）...")

        # 1) 解析 perf script — 提取所有指标
        func_metrics, total_event_counts, call_edges = self._parse_perf_script(
            perf_data["perf_script"]
        )

        # 2) 解析 perf stat（如果存在）
        perf_stat_data = None
        if perf_data.get("perf_stat"):
            perf_stat_data = self._parse_perf_stat(perf_data["perf_stat"])

        # 3) 生成报告
        report = self._generate_report(
            func_metrics, total_event_counts, call_edges, perf_stat_data
        )
        return report

    # ──────────────────────────────────────────────
    # Perf script 单次遍历解析
    # ──────────────────────────────────────────────

    def _parse_perf_script(self, perf_script_path):
        """
        单次遍历 perf.script，同时提取：
        - 每个函数的各事件采样数
        - 调用树边（caller -> callee）
        - 各事件总采样数
        """
        func_metrics = {}          # func_name -> {file, events: {...}, callers: {...}, callees: {...}}
        total_event_counts = defaultdict(int)
        call_edges = defaultdict(lambda: defaultdict(int))  # caller -> {callee -> count}

        current_event_cat = None
        current_stack_frames = []   # 当前样本的调用栈帧（叶子在前）

        # Header 行正则：匹配 "comm pid ts: count event_name: "
        header_re = re.compile(
            r"^\S+\s+\d+\s+[\d.]+:\s+\d+\s+[\w/_-]+/([\w/-]+)/?\s*:?\s*$"
        )
        # 栈帧行正则：匹配 "\taddr func+0xoffset (file)"
        frame_re = re.compile(
            r"^\s+[0-9a-f]+\s+(.+)\+0x[0-9a-f]+\s+\(([^)]+)\)\s*$"
        )

        project_path = self.config.project_path
        executable_name = os.path.basename(self.config.executable.path)
        source_roots = self.config.code_scope.source_roots

        with open(perf_script_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # ── 尝试匹配 Header 行 ──
                hdr_match = header_re.match(line)
                if hdr_match:
                    # 先处理上一个样本的调用栈
                    if current_event_cat is not None and current_stack_frames:
                        self._process_sample(
                            current_stack_frames, current_event_cat,
                            func_metrics, total_event_counts, call_edges,
                            project_path, executable_name, source_roots
                        )
                    raw_event = hdr_match.group(1)
                    current_event_cat = self._normalize_event(raw_event)
                    current_stack_frames = []
                    continue

                # ── 尝试匹配栈帧行 ──
                frame_match = frame_re.match(line)
                if frame_match and current_event_cat is not None:
                    func_name = frame_match.group(1).strip()
                    file_path = frame_match.group(2).strip()
                    current_stack_frames.append((func_name, file_path))
                    continue

            # 处理最后一个样本
            if current_event_cat is not None and current_stack_frames:
                self._process_sample(
                    current_stack_frames, current_event_cat,
                    func_metrics, total_event_counts, call_edges,
                    project_path, executable_name, source_roots
                )

        logger.info(
            f"解析完成: {len(func_metrics)} 个函数, "
            f"{sum(total_event_counts.values())} 个采样"
        )
        return func_metrics, dict(total_event_counts), dict(call_edges)

    def _normalize_event(self, raw_event):
        """将原始事件名归一化到类别"""
        raw_lower = raw_event.lower().strip("/")
        # 处理如 "cpu_core/cycles" -> "cycles"
        for key, cat in self.EVENT_CATEGORIES.items():
            if raw_lower == key or raw_lower.endswith("/" + key):
                return cat
        # 兼容 cpu_core/cache-references/ 等带尾部斜杠的
        for key, cat in self.EVENT_CATEGORIES.items():
            if key in raw_lower:
                return cat
        return raw_lower

    def _is_user_code(self, file_path, project_path, executable_name, source_roots):
        """判断路径是否属于用户代码"""
        if file_path == "[unknown]":
            return False
        if "/usr/" in file_path or "/lib/" in file_path:
            return False
        if project_path and project_path in file_path:
            return True
        if executable_name and file_path.endswith(executable_name):
            return True
        for root in source_roots:
            if root in file_path:
                return True
        return False

    def _process_sample(self, stack_frames, event_cat, func_metrics,
                        total_event_counts, call_edges,
                        project_path, executable_name, source_roots):
        """
        处理一个采样样本：
        - stack_frames: [(func_name, file_path), ...] 叶子在前
        - event_cat: 归一化后的事件类别
        """
        total_event_counts[event_cat] += 1

        if not stack_frames:
            return

        # ── 叶子函数计入该事件的采样数 ──
        leaf_func, leaf_file = stack_frames[0]

        if not self._is_user_code(leaf_file, project_path, executable_name, source_roots):
            # 叶子不是用户代码时，向上找第一个用户代码帧
            found = None
            for func, fpath in stack_frames:
                if self._is_user_code(fpath, project_path, executable_name, source_roots):
                    found = (func, fpath)
                    break
            if found is None:
                return  # 整个栈帧都没有用户代码
            leaf_func, leaf_file = found

        # 初始化/更新函数指标
        if leaf_func not in func_metrics:
            func_metrics[leaf_func] = {
                "file": leaf_file,
                "events": defaultdict(int),
                "callers": defaultdict(int),
                "callees": defaultdict(int),
            }
        func_metrics[leaf_func]["events"][event_cat] += 1

        # ── 构建调用关系 ──
        # stack_frames: [leaf, ..., root]
        # 相邻对 (stack[i+1], stack[i]) 表示 caller -> callee
        for i in range(len(stack_frames) - 1):
            callee_func, callee_file = stack_frames[i]
            caller_func, caller_file = stack_frames[i + 1]

            # 双方至少有一方是用户代码才记录
            callee_is_user = self._is_user_code(
                callee_file, project_path, executable_name, source_roots
            )
            caller_is_user = self._is_user_code(
                caller_file, project_path, executable_name, source_roots
            )
            if not callee_is_user and not caller_is_user:
                continue

            call_edges[caller_func][callee_func] += 1

            # 更新 caller/callee 计数
            if leaf_func in func_metrics:
                func_metrics[leaf_func]["callers"][caller_func] += 1
                if caller_func not in func_metrics:
                    func_metrics[caller_func] = {
                        "file": caller_file,
                        "events": defaultdict(int),
                        "callers": defaultdict(int),
                        "callees": defaultdict(int),
                    }
                func_metrics[caller_func]["callees"][callee_func] += 1

    # ──────────────────────────────────────────────
    # Perf stat 解析
    # ──────────────────────────────────────────────

    def _parse_perf_stat(self, perf_stat_path):
        """解析 perf stat 输出，提取聚合事件计数"""
        if not perf_stat_path or not os.path.exists(perf_stat_path):
            return None

        data = {}
        with open(perf_stat_path, "r") as f:
            for line in f:
                # 格式："    123,456      cycles"
                match = re.match(r"\s*([\d,]+)\s+(.+)", line.strip())
                if match:
                    count = int(match.group(1).replace(",", ""))
                    event_name = match.group(2).strip()
                    data[event_name] = count

        logger.info(f"perf stat 解析完成: {len(data)} 个事件")
        return data

    # ──────────────────────────────────────────────
    # 派生指标计算
    # ──────────────────────────────────────────────

    def _compute_derived_metrics(self, func_metrics):
        """为每个函数计算派生指标（采样近似值，相对排名有效）"""
        enriched = []
        for func_name, info in func_metrics.items():
            events = info["events"]
            cycles = events.get("cycles", 0)
            instructions = events.get("instructions", 0)
            cache_refs = events.get("cache_references", 0)
            cache_miss = events.get("cache_misses", 0)
            br_inst = events.get("branch_instructions", 0)
            br_miss = events.get("branch_misses", 0)

            # IPC: 采样近似值，夹紧到 [0, 10]
            ipc = round(instructions / cycles, 3) if cycles > 0 else 0
            ipc = min(ipc, 10.0)  # 合理的上限

            # Cache Miss Rate: 采样近似，夹紧到 [0, 100]
            cmr = round(cache_miss / cache_refs * 100, 2) if cache_refs > 0 else 0
            cmr = min(cmr, 100.0)

            # Branch Miss Rate: 采样近似，夹紧到 [0, 100]
            bmr = round(br_miss / br_inst * 100, 2) if br_inst > 0 else 0
            bmr = min(bmr, 100.0)

            enriched.append({
                "function": func_name,
                "file": info["file"],
                "events": dict(events),
                "callers": dict(info["callers"]),
                "callees": dict(info["callees"]),
                "cycles": cycles,
                "instructions": instructions,
                "ipc": ipc,
                "cache_references": cache_refs,
                "cache_misses": cache_miss,
                "cache_miss_rate": cmr,
                "branch_instructions": br_inst,
                "branch_misses": br_miss,
                "branch_miss_rate": bmr,
                "total_samples": sum(events.values()),
            })
        return enriched

    # ──────────────────────────────────────────────
    # 报告生成
    # ──────────────────────────────────────────────

    def _generate_report(self, func_metrics, total_event_counts,
                         call_edges, perf_stat_data):
        enriched = self._compute_derived_metrics(func_metrics)
        total_cycles = sum(f["cycles"] for f in enriched)

        # 按 cycles 排序
        enriched.sort(key=lambda x: x["cycles"], reverse=True)
        for f in enriched:
            f["cpu_pct"] = round(f["cycles"] / total_cycles * 100, 2) if total_cycles > 0 else 0

        # ── 按各指标排序的列表 ──
        # 注意：采样指标为近似值，低采样数的函数比例可能不准确
        top_cycles = sorted(enriched, key=lambda x: x["cycles"], reverse=True)[:20]
        top_ipc_low = sorted(
            [f for f in enriched if f["instructions"] > 100 and f["cycles"] > 100],
            key=lambda x: x["ipc"]
        )[:10]
        top_ipc_high = sorted(
            [f for f in enriched if f["instructions"] > 100 and f["cycles"] > 100],
            key=lambda x: x["ipc"], reverse=True
        )[:10]
        top_cache_miss = sorted(
            [f for f in enriched if f["cache_references"] > 100],
            key=lambda x: x["cache_miss_rate"], reverse=True
        )[:10]
        top_branch_miss = sorted(
            [f for f in enriched if f["branch_instructions"] > 100],
            key=lambda x: x["branch_miss_rate"], reverse=True
        )[:10]

        # ── 构建调用树（取 Top 10 热点函数的调用树） ──
        call_trees = {}
        for func in top_cycles[:10]:
            call_trees[func["function"]] = self._build_call_tree_for_func(
                func["function"], func_metrics, call_edges, depth=3
            )

        # ── 构建报告数据 ──
        report_data = {
            "report_version": "2.0",
            "generated_at": datetime.now().isoformat(),
            "executable": self.config.executable.path,
            "run_duration": self.config.executable.run_duration,
            "summary": {
                "total_functions": len(enriched),
                "total_samples": sum(total_event_counts.values()),
                "total_cycles": total_cycles,
                "perf_stat": perf_stat_data,
                "event_breakdown": total_event_counts,
            },
            "top_hotspots": top_cycles,
            "top_cache_miss": top_cache_miss,
            "top_branch_miss": top_branch_miss,
            "ipc_lowest": top_ipc_low,
            "ipc_highest": top_ipc_high,
            "call_trees": call_trees,
            "call_edges": {
                f"{c} -> {e}": cnt
                for c, targets in call_edges.items()
                for e, cnt in sorted(targets.items(), key=lambda x: -x[1])[:5]
                if cnt > 10
            },
            "all_functions": enriched,
        }

        # 保存 JSON
        json_path = os.path.join(self.output_dir, "hotspot_report.json")
        with open(json_path, "w") as f:
            json.dump(report_data, f, indent=2, default=str)

        # 保存 Markdown
        md_path = os.path.join(self.output_dir, "hotspot_report.md")
        self._write_markdown_report(report_data, md_path)

        # 保存调用图 DOT 文件
        dot_path = os.path.join(self.output_dir, "dynamic_call_graph.dot")
        self._write_call_graph_dot(enriched[:30], call_edges, dot_path)

        logger.info(f"综合报告已生成: {json_path} 和 {md_path}")
        return report_data

    def _build_call_tree_for_func(self, func_name, func_metrics, call_edges, depth):
        """为指定函数构建展开的调用树"""
        tree = {"function": func_name, "callers": [], "callees": []}

        # 谁调用了此函数
        if func_name in func_metrics:
            callers = sorted(
                func_metrics[func_name]["callers"].items(),
                key=lambda x: -x[1]
            )[:5]
            for c_name, cnt in callers:
                tree["callers"].append({"function": c_name, "samples": cnt})

        # 此函数调用了谁
        if func_name in func_metrics:
            callees = sorted(
                func_metrics[func_name]["callees"].items(),
                key=lambda x: -x[1]
            )[:5]
            for c_name, cnt in callees:
                callee_node = {"function": c_name, "samples": cnt}
                if depth > 1 and c_name in func_metrics:
                    sub_tree = self._build_call_tree_for_func(
                        c_name, func_metrics, call_edges, depth - 1
                    )
                    callee_node["sub_callees"] = sub_tree["callees"][:3]
                tree["callees"].append(callee_node)

        return tree

    def _write_markdown_report(self, d, md_path):
        """生成综合 Markdown 报告"""
        with open(md_path, "w") as f:
            f.write("# 性能热点分析报告\n\n")
            f.write(f"**生成时间**: {d['generated_at']}\n\n")
            f.write(f"**可执行文件**: {d['executable']}\n\n")
            f.write(f"**运行时长**: {d['run_duration']}秒\n\n")
            f.write("> **注**: IPC、Cache Miss率、Branch Miss率等指标为 perf sampling 近似值，相对排名有效但绝对值可能与硬件计数器有偏差。\n\n")

            # ── 概要 ──
            s = d["summary"]
            f.write("## 概要\n\n")
            f.write(f"- 分析函数总数: {s['total_functions']}\n")
            f.write(f"- 总采样数: {s['total_samples']:,}\n")
            f.write(f"- 事件分布:\n")
            for evt, cnt in sorted(s.get("event_breakdown", {}).items(),
                                   key=lambda x: -x[1]):
                f.write(f"  - `{evt}`: {cnt:,}\n")
            f.write("\n")

            # ── perf stat 聚合数据 ──
            if s.get("perf_stat"):
                f.write("## 程序级聚合指标 (perf stat)\n\n")
                f.write("| 事件 | 计数 |\n")
                f.write("|------|------|\n")
                for evt, cnt in s["perf_stat"].items():
                    f.write(f"| {evt} | {cnt:,} |\n")

                ps = s["perf_stat"]
                if "cycles" in ps and "instructions" in ps and ps["cycles"] > 0:
                    total_ipc = ps["instructions"] / ps["cycles"]
                    f.write(f"\n**程序级 IPC**: {total_ipc:.3f}\n")
                if ("cache-references" in ps and "cache-misses" in ps
                        and ps["cache-references"] > 0):
                    rate = ps["cache-misses"] / ps["cache-references"] * 100
                    f.write(f"\n**程序级 Cache Miss 率**: {rate:.2f}%\n")
                if ("branch-instructions" in ps and "branch-misses" in ps
                        and ps["branch-instructions"] > 0):
                    rate = ps["branch-misses"] / ps["branch-instructions"] * 100
                    f.write(f"\n**程序级 Branch Miss 率**: {rate:.2f}%\n")
                f.write("\n")

            # ── 1. CPU 热点 Top 20 ──
            f.write("## 1. CPU 热点函数 Top 20\n\n")
            f.write("| 排名 | 函数名 | 所属文件 | CPU% | Cycles | 采样数 |\n")
            f.write("|------|--------|----------|------|--------|--------|\n")
            for i, func in enumerate(d["top_hotspots"][:20], 1):
                f.write(
                    f"| {i} | `{func['function'][:60]}` "
                    f"| {os.path.basename(func['file'])} "
                    f"| {func.get('cpu_pct', 0)}% "
                    f"| {func['cycles']:,} "
                    f"| {func['total_samples']:,} |\n"
                )
            f.write("\n")

            # ── 2. 调用树（Top 10 热点） ──
            f.write("## 2. 热点函数调用树 (Top 10)\n\n")
            call_trees = d.get("call_trees", {})
            for func_name, tree in list(call_trees.items())[:10]:
                f.write(f"### `{func_name}`\n\n")
                # callers
                if tree["callers"]:
                    f.write("**← 调用者 (callers)**:\n\n")
                    for c in tree["callers"]:
                        f.write(f"- `{c['function'][:50]}` ({c['samples']} samples)\n")
                # callees
                if tree["callees"]:
                    f.write("\n**→ 被调用 (callees)**:\n\n")
                    for c in tree["callees"]:
                        indent = ""
                        f.write(f"- `{c['function'][:50]}` ({c['samples']} samples)\n")
                        for sc in c.get("sub_callees", []):
                            f.write(f"  - `{sc['function'][:50]}` ({sc['samples']} samples)\n")
                f.write("\n")

            # ── 3. IPC 分析 ──
            f.write("## 3. IPC 分析 (Instructions Per Cycle)\n\n")
            f.write("### IPC 最低的函数（可能受内存延迟限制）\n\n")
            f.write("| 函数名 | IPC | Instructions | Cycles |\n")
            f.write("|--------|-----|-------------|--------|\n")
            for func in d["ipc_lowest"][:10]:
                f.write(
                    f"| `{func['function'][:50]}` "
                    f"| {func['ipc']} "
                    f"| {func['instructions']:,} "
                    f"| {func['cycles']:,} |\n"
                )
            f.write("\n### IPC 最高的函数（计算密集）\n\n")
            f.write("| 函数名 | IPC | Instructions | Cycles |\n")
            f.write("|--------|-----|-------------|--------|\n")
            for func in d["ipc_highest"][:10]:
                f.write(
                    f"| `{func['function'][:50]}` "
                    f"| {func['ipc']} "
                    f"| {func['instructions']:,} "
                    f"| {func['cycles']:,} |\n"
                )
            f.write("\n")

            # ── 4. Cache Miss 分析 ──
            f.write("## 4. Cache Miss 分析\n\n")
            f.write("| 函数名 | Miss率(%) | Misses | References |\n")
            f.write("|--------|-----------|--------|------------|\n")
            for func in d["top_cache_miss"][:10]:
                f.write(
                    f"| `{func['function'][:50]}` "
                    f"| {func['cache_miss_rate']}% "
                    f"| {func['cache_misses']:,} "
                    f"| {func['cache_references']:,} |\n"
                )
            f.write("\n")

            # ── 5. L1/LLC Cache 聚合 ──
            f.write("## 5. L1 / LLC Cache 分析\n\n")
            if s.get("perf_stat"):
                ps = s["perf_stat"]
                l1_keys = [k for k in ps if "L1" in k.upper() or "l1" in k]
                llc_keys = [k for k in ps if "LLC" in k.upper() or "llc" in k]
                if l1_keys:
                    f.write("### L1 数据缓存\n\n")
                    f.write("| 事件 | 计数 |\n|------|------|\n")
                    for k in l1_keys:
                        f.write(f"| {k} | {ps[k]:,} |\n")
                    f.write("\n")
                if llc_keys:
                    f.write("### LLC (Last Level Cache)\n\n")
                    f.write("| 事件 | 计数 |\n|------|------|\n")
                    for k in llc_keys:
                        f.write(f"| {k} | {ps[k]:,} |\n")
                    f.write("\n")
            else:
                f.write("> 需要启用 perf stat 以获取 L1/LLC 数据\n\n")

            # ── 6. 分支预测分析 ──
            f.write("## 6. 分支预测分析\n\n")
            f.write("| 函数名 | Miss率(%) | Branch Misses | Branch Inst |\n")
            f.write("|--------|-----------|---------------|-------------|\n")
            for func in d["top_branch_miss"][:10]:
                f.write(
                    f"| `{func['function'][:50]}` "
                    f"| {func['branch_miss_rate']}% "
                    f"| {func['branch_misses']:,} "
                    f"| {func['branch_instructions']:,} |\n"
                )
            f.write("\n")

            # ── 7. 调用关系 ──
            f.write("## 7. 调用关系 (Dynamic Call Edges)\n\n")
            edges = d.get("call_edges", {})
            if edges:
                f.write("| Caller → Callee | Samples |\n")
                f.write("|-----------------|--------|\n")
                for edge, cnt in list(edges.items())[:20]:
                    f.write(f"| {edge} | {cnt} |\n")
            f.write("\n")

            # ── 8. 有性能问题的源码 ──
            f.write("## 8. 性能瓶颈函数列表\n\n")
            f.write("> 以下是建议重点优化的函数（CPU占比 > 1%):\n\n")
            f.write("| 函数 | CPU% | Cycles | IPC | CacheMiss% | BranchMiss% |\n")
            f.write("|------|------|--------|-----|------------|-------------|\n")
            for func in d["top_hotspots"]:
                if func.get("cpu_pct", 0) > 1.0:
                    f.write(
                        f"| `{func['function'][:40]}` "
                        f"| {func.get('cpu_pct', 0)}% "
                        f"| {func['cycles']:,} "
                        f"| {func['ipc']} "
                        f"| {func['cache_miss_rate']}% "
                        f"| {func['branch_miss_rate']}% |\n"
                    )
            f.write("\n")

    def _write_call_graph_dot(self, enriched, call_edges, dot_path):
        """生成基于 perf 数据的加权调用图（DOT 格式）"""
        top_funcs = set(f["function"] for f in enriched[:30])

        with open(dot_path, "w") as f:
            f.write("digraph dynamic_call_graph {\n")
            f.write('  rankdir=LR;\n')
            f.write('  node [fontsize=10, shape=box];\n')

            for func in enriched:
                name = func["function"]
                if name not in top_funcs:
                    continue
                pct = func.get("cpu_pct", 0)
                # 节点颜色从绿到红
                if pct > 5:
                    color = "red"
                elif pct > 2:
                    color = "orange"
                else:
                    color = "lightblue"
                label = f"{name[:30]}\\n{pct:.1f}%"
                f.write(
                    f'  "{name}" [label="{label}", style=filled, fillcolor={color}];\n'
                )

            # 边
            max_edge = max(
                (cnt for caller_targets in call_edges.values() for cnt in caller_targets.values()),
                default=1,
            )
            for caller, targets in call_edges.items():
                if caller not in top_funcs:
                    continue
                for callee, cnt in targets.items():
                    if callee not in top_funcs:
                        continue
                    penwidth = max(1, int(5 * cnt / max_edge))
                    f.write(
                        f'  "{caller}" -> "{callee}" [penwidth={penwidth}, label="{cnt}"];\n'
                    )

            f.write("}\n")
        logger.info(f"动态调用图 DOT 已生成: {dot_path}")
