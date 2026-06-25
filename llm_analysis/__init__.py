"""LLM 性能优化模块

用法:
    from llm_analysis import run_llm_analysis

    # 离线模式（默认）：生成 prompt 文件，手动复制给大模型
    run_llm_analysis("particle_output/dynamic_analysis/hotspot_sources")

    # 在线模式：从配置文件读取 API 地址，直接调大模型
    run_llm_analysis("particle_output/dynamic_analysis/hotspot_sources", llm_config)
"""

import os
from common.logger import logger
from llm_analysis.prompt_builder import PromptBuilder
from llm_analysis.llm_client import LLMClient
from llm_analysis.patch_writer import PatchWriter


def run_llm_analysis(hotspot_sources_dir, llm_config=None, output_dir=None):
    """运行 LLM 性能优化分析

    Args:
        hotspot_sources_dir: hotspot_sources 目录路径
        llm_config: LLMConfig 对象（从 project_config.yaml 读取）
            - 如果 base_url 为空 → 离线模式，只生成 prompt 文件
            - 如果 base_url 有值 → 在线模式，调大模型 API
        output_dir: 输出目录，默认在 hotspot_sources_dir 旁边创建 llm_patches/

    Returns:
        成功返回 patches 目录路径，失败返回 None
    """
    if not os.path.isdir(hotspot_sources_dir):
        logger.error(f"热点源码目录不存在: {hotspot_sources_dir}")
        return None

    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(hotspot_sources_dir), "llm_patches")

    # 判断模式：配了 base_url 就是在线，否则离线
    if llm_config and llm_config.enabled:
        mode = "online"
        logger.info(f"=== 开始 LLM 性能优化分析（在线模式）===")
        logger.info(f"  API 地址: {llm_config.api_url}")
        if llm_config.model:
            logger.info(f"  模型: {llm_config.model}")
    else:
        mode = "offline"
        logger.info(f"=== 开始 LLM 性能优化分析（离线模式）===")

    logger.info(f"  输入: {hotspot_sources_dir}")
    logger.info(f"  输出: {output_dir}")

    # 第 1 步：读取热点数据，构建 prompt
    builder = PromptBuilder(hotspot_sources_dir)
    prompts = builder.build_all()
    if not prompts:
        logger.error("未能从热点数据中构建 prompt")
        return None
    logger.info(f"已为 {len(prompts)} 个函数构建 prompt")

    # 第 2 步：发送给大模型（或保存 prompt 文件）
    client = LLMClient(llm_config)
    responses = client.analyze(prompts, mode=mode, output_dir=output_dir)

    # 第 3 步：保存结果
    writer = PatchWriter(output_dir)
    writer.save_all(prompts, responses, mode=mode)

    logger.info(f"=== LLM 分析完成，结果保存在: {output_dir} ===")
    return output_dir