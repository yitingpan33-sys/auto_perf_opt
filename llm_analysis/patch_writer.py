"""保存大模型的优化结果

把大模型的回复解析出来，提取优化后的代码，
保存成可以直接查看和复制粘贴的文件。
"""

import os
import re
from common.logger import logger


class PatchWriter:
    """把大模型回复保存成补丁文件"""

    def __init__(self, output_dir):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def save_all(self, prompts, responses, mode="offline"):
        """保存所有函数的分析结果

        Args:
            prompts: PromptBuilder 返回的 prompt 列表
            responses: LLMClient 返回的 response 列表
            mode: "offline" 或 "online"
        """
        summary_items = []

        for i, (p, r) in enumerate(zip(prompts, responses), 1):
            func_name = p["func_name"]
            response_text = r.get("response", "")
            safe_name = self._safe_name(func_name)
            prefix = f"{i:02d}_{safe_name}"

            if mode == "online" and response_text:
                # 在线模式：保存大模型的原始回复 + 提取的代码
                self._save_analysis_file(p, response_text, prefix)
                extracted = self._extract_cpp_code(response_text)

                if extracted:
                    # 每个提取到的代码块保存为独立的 .cpp 文件
                    for j, code_block in enumerate(extracted):
                        code_file = f"{prefix}_optimized.cpp"
                        if len(extracted) > 1:
                            code_file = f"{prefix}_optimized_v{j+1}.cpp"
                        self._save_code_file(func_name, p, code_block, code_file)
                        summary_items.append({
                            "func_name": func_name,
                            "patch_file": code_file,
                            "has_code": True,
                        })
                else:
                    # 大模型没给出代码（可能说"无需优化"）
                    summary_items.append({
                        "func_name": func_name,
                        "analysis_file": f"{prefix}_analysis.md",
                        "has_code": False,
                    })
            else:
                # 离线模式：只保存 prompt，大模型还没回复
                summary_items.append({
                    "func_name": func_name,
                    "status": "等待人工分析",
                    "prompt_file": f"prompt_single/{prefix}.md",
                })

        # 写一个总结文件
        self._save_summary(summary_items, mode)

    # ── 内部方法 ──

    def _save_analysis_file(self, prompt_data, response_text, prefix):
        """保存大模型的完整回复（包含分析 + 代码）"""
        file_path = os.path.join(self.output_dir, f"{prefix}_analysis.md")
        metrics = prompt_data.get("metrics", {})

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"# {prompt_data['func_name']}\n\n")
            f.write(f"**源文件**: `{prompt_data['source_file']}`\n\n")
            f.write("## 性能指标\n\n")
            f.write(f"| 指标 | 数值 |\n")
            f.write(f"|------|------|\n")
            f.write(f"| CPU 占比 | {metrics.get('cpu_pct', '?')}% |\n")
            f.write(f"| IPC | {metrics.get('ipc', '?')} |\n")
            f.write(f"| Cache Miss 率 | {metrics.get('cache_miss_rate', '?')}% |\n")
            f.write(f"| 分支预测失败率 | {metrics.get('branch_miss_rate', '?')}% |\n\n")
            f.write("## 大模型分析\n\n")
            f.write(response_text)
        logger.info(f"分析报告已保存: {file_path}")

    def _save_code_file(self, func_name, prompt_data, optimized_code, filename):
        """保存优化后的代码为 .cpp 文件"""
        file_path = os.path.join(self.output_dir, filename)
        metrics = prompt_data.get("metrics", {})

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"// ============================================================\n")
            f.write(f"// LLM 优化建议\n")
            f.write(f"// 原始函数: {func_name}\n")
            f.write(f"// 源文件:   {prompt_data['source_file']}\n")
            f.write(f"//\n")
            f.write(f"// 优化前指标: CPU {metrics.get('cpu_pct', '?')}% | "
                    f"IPC {metrics.get('ipc', '?')} | "
                    f"CacheMiss {metrics.get('cache_miss_rate', '?')}%\n")
            f.write(f"//\n")
            f.write(f"// ⚠️  这是大模型生成的代码，请人工审核后再使用\n")
            f.write(f"// ============================================================\n\n")
            f.write(optimized_code)
        logger.info(f"优化代码已保存: {file_path}")

    def _save_summary(self, items, mode):
        """保存总结文件"""
        file_path = os.path.join(self.output_dir, "README.md")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write("# LLM 性能优化结果\n\n")

            if mode == "offline":
                f.write("> ⚠️ 这是离线模式。请将 prompt 文件的内容复制给大模型，"
                        "获取回复后再运行 `--mode online` 或在代码中整合。\n\n")
                f.write("## 操作步骤\n\n")
                f.write("1. 打开 `prompt_all.md`，复制全部内容\n")
                f.write("2. 粘贴到 ChatGPT / Claude / 内网大模型 的对话框\n")
                f.write("3. 等待回复\n")
                f.write("4. （可选）将回复保存到对应的 `_response.md` 文件中\n\n")

            f.write("## 函数列表\n\n")
            f.write("| # | 函数 | 状态 |\n")
            f.write("|---|------|------|\n")
            for i, item in enumerate(items, 1):
                status = item.get("status", "✅ 已有优化代码" if item.get("has_code") else "⚠️ 待审核")
                f.write(f"| {i} | `{item['func_name'][:60]}` | {status} |\n")

    def _extract_cpp_code(self, text):
        """从大模型回复中提取 ```cpp 代码块

        返回所有 cpp 代码块的内容列表。
        如果一个都没有，返回空列表。
        """
        # 匹配 ```cpp 或 ```c++ 到 ``` 之间的内容
        pattern = re.compile(r'```(?:cpp|c\+\+)\s*\n(.*?)```', re.DOTALL)
        matches = pattern.findall(text)
        return [m.strip() for m in matches]

    @staticmethod
    def _safe_name(name):
        """函数名转安全文件名"""
        keep = []
        for ch in name:
            if ch.isalnum() or ch in "_-.":
                keep.append(ch)
            else:
                keep.append("_")
        result = "".join(keep)
        return result[:80] if len(result) > 80 else result