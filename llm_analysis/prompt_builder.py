"""构建发给大模型的 prompt

从 hotspot_sources 目录读取每个热点函数的源码和指标，
拼成一个大模型能理解的 prompt。
"""

import os
import json
import re
from common.logger import logger


# ── prompt 模板 ────────────────────────────────────────

SYSTEM_PROMPT = """你是一个资深的 C++ 性能优化专家。用户会给你一个函数的源码和性能数据，
请你分析这段代码的性能瓶颈，并给出优化后的完整代码。

回复格式要求：
1. 先简要分析瓶颈（1-3 句话）
2. 然后用 ```cpp 代码块给出优化后的完整函数
3. 必要时给出优化说明（比如为什么这样改）

注意：
- 只输出一个函数的优化版本，不要一次输出多个函数
- 保持代码风格一致（缩进、命名习惯）
- 不要改函数的对外接口（参数和返回值类型不能变）
- 如果代码已经合理，直接说"无需优化"即可"""

FUNCTION_PROMPT_TEMPLATE = """## 函数名
{func_name}

## 源文件位置
{source_file}（第 {start_line} 到第 {end_line} 行）

## 性能指标
| 指标 | 数值 | 含义 |
|------|------|------|
| CPU 占比 | {cpu_pct}% | 该函数占总 CPU 时间的百分比 |
| IPC | {ipc} | 每个时钟周期执行的指令数，低于 1.5 说明 CPU 在等内存 |
| Cache Miss 率 | {cache_miss}% | 内存访问有多少没命中 CPU 缓存 |
| 分支预测失败率 | {branch_miss}% | 分支指令有多少预测错了 |

## 完整源码
```cpp
{source_code}
```

请分析这个函数的性能瓶颈并给出优化后的代码。"""


class PromptBuilder:
    """从 hotspot_sources 目录读取数据，构建 prompt"""

    def __init__(self, sources_dir):
        self.sources_dir = sources_dir

        # 加载 metrics.json（热点函数的摘要信息）
        metrics_path = os.path.join(sources_dir, "metrics.json")
        if not os.path.isfile(metrics_path):
            raise FileNotFoundError(f"未找到 metrics.json: {metrics_path}")
        with open(metrics_path, "r", encoding="utf-8") as f:
            self.metrics_list = json.load(f)

        # 加载 types.h（类型定义，给大模型提供上下文）
        types_path = os.path.join(sources_dir, "types.h")
        self.types_code = ""
        if os.path.isfile(types_path):
            with open(types_path, "r", encoding="utf-8") as f:
                self.types_code = f.read()

        logger.info(f"加载了 {len(self.metrics_list)} 个热点函数的元数据")

    def build_all(self):
        """为所有热点函数构建 prompt

        Returns:
            list of dict: [{
                "func_name": "...",
                "source_file": "...",
                "code": "...",
                "metrics": {...},
                "prompt": "完整的 prompt 文本"
            }, ...]
        """
        prompts = []

        for item in self.metrics_list:
            func_name = item.get("function", "")
            source_file = item.get("file", "")

            # 读取对应的源码文件
            output_file = item.get("output_file", "")
            source_code = ""
            if output_file and os.path.isfile(output_file):
                source_code = self._read_source_code(output_file)

            if not source_code:
                logger.warning(f"找不到 {func_name} 的源码，跳过")
                continue

            # 提取行号
            lines_range = item.get("lines", "?-?")
            start_line, end_line = self._parse_lines(lines_range)

            # 取性能指标
            metrics = item.get("metrics", {})

            # 填充模板，构建完整 prompt
            prompt_text = FUNCTION_PROMPT_TEMPLATE.format(
                func_name=func_name,
                source_file=source_file,
                start_line=start_line,
                end_line=end_line,
                cpu_pct=metrics.get("cpu_pct", "?"),
                ipc=metrics.get("ipc", "?"),
                cache_miss=metrics.get("cache_miss_rate", "?"),
                branch_miss=metrics.get("branch_miss_rate", "?"),
                source_code=source_code,
            )

            # 如果有类型定义，拼在 prompt 末尾
            if self.types_code:
                prompt_text += f"\n\n## 相关类型定义（供参考）\n```cpp\n{self.types_code}\n```\n"

            prompts.append({
                "func_name": func_name,
                "source_file": source_file,
                "code": source_code,
                "metrics": metrics,
                "prompt": prompt_text,
            })

        return prompts

    def build_combined_prompt(self, prompts):
        """把所有函数的 prompt 合并成一个大 prompt

        用于离线模式：一次性把所有内容放进一个文件，
        用户复制粘贴一次就能让大模型分析所有函数。
        """
        parts = [
            SYSTEM_PROMPT,
            "\n---\n",
            "下面有 {0} 个需要分析的函数，请逐个给出优化方案。".format(len(prompts)),
            "对每个函数，请先写函数名（如 ## checkCellPairs），再给出分析和优化代码。\n",
        ]

        for i, p in enumerate(prompts, 1):
            parts.append(f"\n{'='*60}")
            parts.append(f"第 {i} 个函数\n")
            parts.append(p["prompt"])

        return "\n".join(parts)

    # ── 辅助方法 ──

    def _read_source_code(self, file_path):
        """读取源码文件，去掉最前面的注释头（// 开头的元数据）"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            # 去掉文件开头的元数据注释（以 // ==== 开头到 // ==== 结尾的块）
            # 匹配从第一个 // === 到第二个 // === 之间的内容
            content = re.sub(r'^// =+\n(?://.*\n)*// =+\n\n', '', content, count=1)
            return content.strip()
        except Exception as e:
            logger.warning(f"读取源码失败 {file_path}: {e}")
            return ""

    @staticmethod
    def _parse_lines(lines_str):
        """解析 "129-142" 这种行号字符串"""
        parts = lines_str.split("-")
        if len(parts) == 2:
            return parts[0], parts[1]
        return lines_str, "?"