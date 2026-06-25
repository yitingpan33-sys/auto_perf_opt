import os
import argparse
from common.config import load_config
from common.logger import logger
from static_analysis.ast_parser import ASTParser
from static_analysis.graph_generator import GraphGenerator
from dynamic_analysis.executable_runner import ExecutableRunner
from dynamic_analysis.hotspot_analyzer import HotspotAnalyzer
from dynamic_analysis.source_extractor import SourceExtractor


def main():
    parser = argparse.ArgumentParser(description="C++自动性能优化工具")
    parser.add_argument("-c", "--config", required=True, help="项目配置文件路径(YAML)")
    parser.add_argument("-o", "--output", default="output", help="输出目录")
    parser.add_argument("--skip-static", action="store_true", help="跳过静态分析")
    parser.add_argument("--skip-dynamic", action="store_true", help="跳过动态分析")
    parser.add_argument("--llm", action="store_true", help="启用 LLM 性能优化分析（使用配置文件中的 llm 段）")

    args = parser.parse_args()

    # 加载配置
    config = load_config(args.config)
    logger.info(f"加载项目配置成功: {config.project_name}")

    # 创建输出目录
    output_dir = os.path.abspath(args.output)
    static_output = os.path.join(output_dir, "static_analysis")
    dynamic_output = os.path.join(output_dir, "dynamic_analysis")
    os.makedirs(output_dir, exist_ok=True)

    ast_data = None

    # ── 静态分析 ──
    if not args.skip_static:
        logger.info("=== 开始静态分析 ===")
        ast_parser = ASTParser(config)
        ast_data = ast_parser.parse_project()

        graph_gen = GraphGenerator(static_output)
        graph_gen.generate_call_graph(ast_data["call_graph"], ast_data["functions"])
        graph_gen.generate_class_inheritance_graph(ast_data["classes"])
        logger.info("静态分析完成")

    # ── 动态分析 ──
    if not args.skip_dynamic:
        logger.info("=== 开始动态分析 ===")
        runner = ExecutableRunner(config)

        # 运行可执行文件并采集 perf 数据
        perf_data = runner.run_and_profile(dynamic_output)

        # 分析热点（多事件 + 调用树）
        analyzer = HotspotAnalyzer(dynamic_output, config, ast_data)
        report = analyzer.analyze(perf_data)

        # ── 提取性能问题源码 ──
        if report.get("top_hotspots"):
            extractor = SourceExtractor(config, ast_data)

            # 新方式：提取完整函数体，保存到独立目录（供 LLM 分析）
            hotspot_sources_dir = os.path.join(dynamic_output, "hotspot_sources")
            extractor.save_to_directory(
                report["top_hotspots"][:10], hotspot_sources_dir
            )

            # 旧方式：保留上下文片段追加到 Markdown 报告中
            source_snippets = extractor.extract_for_functions(
                report["top_hotspots"][:10], context_lines=40
            )

            # 将源码追加到 Markdown 报告中
            if source_snippets:
                md_path = os.path.join(dynamic_output, "hotspot_report.md")
                with open(md_path, "a") as f:
                    f.write("\n## 9. 性能瓶颈函数源码\n\n")
                    for snip in source_snippets:
                        f.write(
                            f"### `{os.path.basename(snip['file'])}`"
                            f" (行 {snip['func_line']})\n\n"
                        )
                        m = snip.get("metrics", {})
                        if m:
                            f.write(
                                f"CPU: {m.get('cpu_pct', '?')}% | "
                                f"IPC: {m.get('ipc', '?')} | "
                                f"CacheMiss: {m.get('cache_miss_rate', '?')}% | "
                                f"BranchMiss: {m.get('branch_miss_rate', '?')}%\n\n"
                            )
                        f.write(f"```cpp\n{snip['code']}\n```\n\n")
                logger.info(f"已追加 {len(source_snippets)} 个函数的源码到报告")

        # ── 渲染动态调用图 PNG ──
        dot_path = os.path.join(dynamic_output, "dynamic_call_graph.dot")
        if os.path.exists(dot_path):
            graph_gen = GraphGenerator(dynamic_output)
            png_path = graph_gen.render_dynamic_call_graph(dot_path)
            if png_path:
                logger.info(f"动态调用图: {png_path}")

        # ── 打印摘要 ──
        logger.info("\n" + "=" * 60)
        logger.info("=== 性能分析摘要 ===")
        logger.info("=" * 60)

        s = report.get("summary", {})
        logger.info(f"分析函数: {s.get('total_functions', 0)} 个")
        logger.info(f"总采样数: {s.get('total_samples', 0):,}")

        top = report.get("top_hotspots", [])
        if top:
            logger.info(f"\n--- Top 5 CPU热点函数 ---")
            for i, h in enumerate(top[:5], 1):
                logger.info(
                    f"  {i}. {h['function'][:50]} "
                    f"| CPU: {h.get('cpu_pct', 0)}% "
                    f"| IPC: {h['ipc']} "
                    f"| CacheMiss: {h['cache_miss_rate']}%"
                )

        # 事件分布
        eb = s.get("event_breakdown", {})
        if eb:
            logger.info(f"\n--- 事件分布 ---")
            for evt, cnt in sorted(eb.items(), key=lambda x: -x[1]):
                logger.info(f"  {evt}: {cnt:,}")

        # perf stat 聚合
        ps = s.get("perf_stat")
        if ps:
            logger.info(f"\n--- 程序级聚合指标 (perf stat) ---")
            if "cycles" in ps and "instructions" in ps and ps["cycles"] > 0:
                ipc = ps["instructions"] / ps["cycles"]
                logger.info(f"  程序级 IPC: {ipc:.3f}")
            if "cache-references" in ps and "cache-misses" in ps and ps["cache-references"] > 0:
                rate = ps["cache-misses"] / ps["cache-references"] * 100
                logger.info(f"  Cache Miss 率: {rate:.2f}%")
            if "branch-instructions" in ps and "branch-misses" in ps and ps["branch-instructions"] > 0:
                rate = ps["branch-misses"] / ps["branch-instructions"] * 100
                logger.info(f"  Branch Miss 率: {rate:.2f}%")

        logger.info(f"\n报告文件: {dynamic_output}/hotspot_report.md")
        logger.info(f"JSON数据: {dynamic_output}/hotspot_report.json")

    # ── LLM 性能优化分析 ──
    if args.llm:
        from llm_analysis import run_llm_analysis
        hotspot_sources_dir = os.path.join(dynamic_output, "hotspot_sources")
        run_llm_analysis(hotspot_sources_dir, config.llm)

    logger.info(f"\n所有分析完成，结果保存在: {output_dir}")


if __name__ == "__main__":
    main()
