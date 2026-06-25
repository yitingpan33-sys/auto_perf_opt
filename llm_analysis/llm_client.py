"""与大模型通信

支持两种模式:
- offline: 把 prompt 保存成文件，用户手动复制给大模型
- online:  直接调大模型 API，自动获取回复
"""

import os
import json
import requests
from common.logger import logger


class LLMClient:
    """大模型客户端"""

    def __init__(self, llm_config=None):
        """
        Args:
            llm_config: LLMConfig 对象（来自 project_config.yaml 的 llm 段）
                        为 None 时默认离线模式
        """
        if llm_config is None:
            # 离线模式，不需要 API 参数
            self.api_url = None
            self.api_key = None
            self.model = None
            self.timeout = 120
        else:
            self.api_url = llm_config.api_url
            self.api_key = llm_config.api_key
            self.model = llm_config.model
            self.timeout = llm_config.timeout

    # ── 公共方法：批量和单个 prompt ──

    def analyze(self, prompts, mode="offline", output_dir=None):
        """分析一组 prompt

        Args:
            prompts: PromptBuilder.build_all() 的返回值
            mode: "offline" 或 "online"
            output_dir: 输出目录（offline 模式需要）

        Returns:
            list of dict: [{"func_name": "...", "response": "大模型的回复"}, ...]
            离线模式下 response 为空字符串
        """
        if mode == "offline":
            return self._offline_mode(prompts, output_dir)
        elif mode == "online":
            return self._online_mode(prompts)
        else:
            raise ValueError(f"不支持的 mode: {mode}，请用 'offline' 或 'online'")

    # ── 离线模式：保存 prompt 文件 ──

    def _offline_mode(self, prompts, output_dir):
        """把 prompt 保存成文件，用户手动喂给大模型

        生成两个文件：
        - prompt_all.md:  所有函数合在一起，复制粘贴一次就行
        - prompt_single/:  每个函数单独一个文件，逐个分析
        """
        from llm_analysis.prompt_builder import PromptBuilder

        os.makedirs(output_dir, exist_ok=True)

        # 合并版本（一次搞定所有函数）
        builder = PromptBuilder.__new__(PromptBuilder)  # 借用一下方法
        combined_path = os.path.join(output_dir, "prompt_all.md")
        combined_text = builder.build_combined_prompt(prompts) if hasattr(builder, 'build_combined_prompt') else ""
        # 直接在这里写合并逻辑
        combined_text = self._make_combined_prompt(prompts)
        with open(combined_path, "w", encoding="utf-8") as f:
            f.write(combined_text)
        logger.info(f"合并 prompt 已保存: {combined_path}")

        # 单个版本（逐个分析更精准）
        single_dir = os.path.join(output_dir, "prompt_single")
        os.makedirs(single_dir, exist_ok=True)
        for i, p in enumerate(prompts, 1):
            safe_name = self._safe_name(p["func_name"])
            file_path = os.path.join(single_dir, f"{i:02d}_{safe_name}.md")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(p["prompt"])
        logger.info(f"单个 prompt 已保存: {single_dir}/ (共 {len(prompts)} 个)")

        # 返回空 response（离线模式没有大模型回复）
        return [{"func_name": p["func_name"], "response": ""} for p in prompts]

    # ── 在线模式：调 API ──

    def _online_mode(self, prompts):
        """依次调用大模型 API 分析每个函数"""
        if not self.api_url:
            raise ValueError("online 模式需要提供 api_url")

        results = []
        for i, p in enumerate(prompts, 1):
            logger.info(f"正在请求大模型分析 ({i}/{len(prompts)}): {p['func_name'][:50]}")
            response = self._call_api(p["prompt"])
            results.append({
                "func_name": p["func_name"],
                "response": response,
            })
        return results

    def _call_api(self, prompt_text):
        """发送一次 API 请求"""
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": prompt_text},
        ]

        payload = {"messages": messages, "stream": False}
        if self.model:
            payload["model"] = self.model

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            resp = requests.post(
                self.api_url,
                json=payload,
                headers=headers,
                timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            # 兼容 OpenAI 格式的响应
            return data["choices"][0]["message"]["content"]
        except requests.RequestException as e:
            logger.error(f"API 请求失败: {e}")
            return f"[API 请求失败: {e}]"
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"API 响应格式异常: {e}")
            return f"[API 响应解析失败: {e}]"

    # ── 辅助方法 ──

    def _system_prompt(self):
        return (
            "你是一个资深的 C++ 性能优化专家。"
            "用户会给你一个函数的源码和性能数据，"
            "请你分析这段代码的性能瓶颈，并给出优化后的完整代码。"
            "用 ```cpp 代码块输出优化后的代码。"
        )

    def _make_combined_prompt(self, prompts):
        """把所有函数的 prompt 合成一个文件"""
        lines = [
            "# C++ 性能优化分析请求\n",
            f"下面有 **{len(prompts)}** 个需要分析的函数。\n",
            "请逐个分析每个函数，给出：\n",
            "1. 性能瓶颈分析（简短）\n",
            "2. 优化后的完整代码（用 ```cpp 代码块）\n\n",
            "---\n",
        ]
        for i, p in enumerate(prompts, 1):
            lines.append(f"\n## 函数 {i}：{p['func_name']}\n")
            lines.append(p["prompt"])
            lines.append(f"\n---\n")
        return "\n".join(lines)

    @staticmethod
    def _safe_name(name):
        """把函数名转成安全的文件名"""
        keep = []
        for ch in name:
            if ch.isalnum() or ch in "_-.":
                keep.append(ch)
            else:
                keep.append("_")
        result = "".join(keep)
        if len(result) > 80:
            result = result[:80]
        return result