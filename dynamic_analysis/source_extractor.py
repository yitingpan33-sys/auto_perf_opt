"""性能热点函数源码提取器

基于 AST 解析结果或文件搜索，提取热点函数的源代码片段。
"""

import os
import re
from common.logger import logger


class SourceExtractor:
    """从项目源码中提取热点函数的代码片段"""

    def __init__(self, project_config, ast_data=None):
        """
        Args:
            project_config: ProjectConfig 对象
            ast_data: AST 解析结果 {"functions": {name: {file, line}}, ...}，可选
        """
        self.config = project_config
        self.ast_data = ast_data
        self._func_locations = {}  # func_name -> (file_path, line_number)

        if ast_data and "functions" in ast_data:
            for func_name, info in ast_data["functions"].items():
                self._func_locations[func_name] = (info["file"], info["line"])

    def extract_for_functions(self, hotspot_funcs, context_lines=30):
        """为一组热点函数提取源码片段

        Args:
            hotspot_funcs: [{"function": "func_name", ...}, ...]
            context_lines: 提取函数定义周围多少行

        Returns:
            [{"function": name, "file": path, "start_line": N, "code": "..."}, ...]
        """
        results = []
        seen = set()

        for func in hotspot_funcs:
            func_name = func.get("function", "")
            if func_name in seen:
                continue
            seen.add(func_name)

            # 清理函数名（去除模板参数等）
            clean_name = self._clean_func_name(func_name)

            code_info = self._extract_single(clean_name, func_name, context_lines)
            if code_info:
                code_info["metrics"] = {
                    k: func.get(k)
                    for k in ["cpu_pct", "ipc", "cache_miss_rate",
                              "branch_miss_rate", "cycles", "total_samples"]
                    if k in func
                }
                results.append(code_info)

        return results

    def _extract_single(self, clean_name, original_name, context_lines):
        """提取单个函数的源码"""
        # 1) 优先从 AST 数据查找
        if clean_name in self._func_locations or original_name in self._func_locations:
            key = clean_name if clean_name in self._func_locations else original_name
            file_path, line_num = self._func_locations[key]
            return self._read_source_range(file_path, line_num, context_lines)

        # 2) 回退：在源文件目录中搜索函数定义
        for root_dir in self.config.code_scope.source_roots:
            search_dir = os.path.join(self.config.project_path, root_dir)
            if not os.path.isdir(search_dir):
                continue

            for dirpath, _, filenames in os.walk(search_dir):
                for fname in filenames:
                    if not fname.endswith((".cpp", ".hpp", ".h", ".cc")):
                        continue
                    file_path = os.path.join(dirpath, fname)
                    line_num = self._find_func_in_file(file_path, clean_name)
                    if line_num:
                        return self._read_source_range(
                            file_path, line_num, context_lines
                        )

        return None

    def _find_func_in_file(self, file_path, func_name):
        """在文件中搜索函数定义，返回行号"""
        # 取函数名的短名（去除命名空间和模板参数）
        short_name = func_name.split("::")[-1]
        # 匹配可能包含返回类型的函数定义
        pattern = re.compile(
            rf"^\s*(?:[\w:<>&,\s]+\s+)?{re.escape(short_name)}\s*\([^)]*\)\s*(?:const\s*)?{{?"
        )
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if pattern.search(line):
                        return i
        except Exception:
            pass
        return None

    def _read_source_range(self, file_path, start_line, context_lines):
        """从文件读取指定行范围的源码"""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            begin = max(0, start_line - context_lines // 3)
            end = min(len(all_lines), start_line + context_lines)

            code = "".join(all_lines[begin:end])
            return {
                "file": file_path,
                "start_line": begin + 1,
                "end_line": end,
                "func_line": start_line,
                "code": code,
            }
        except Exception as e:
            logger.debug(f"读取源码失败 {file_path}: {e}")
            return None

    @staticmethod
    def _clean_func_name(name):
        """清理函数名：去除模板参数、const 修饰等"""
        # 去除 (...) 内的参数类型（保留括号外的函数名）
        # 例如: "std::vector<Particle>::operator[]" -> 保持不变
        # 例如: "foo(int, double)" -> "foo"
        # 只取第一个 ( 之前的部分
        paren_idx = name.find("(")
        if paren_idx > 0:
            name = name[:paren_idx]
        # 去除尾部空格
        name = name.strip()
        # 简化模板参数（可选，但可能影响匹配）
        return name
