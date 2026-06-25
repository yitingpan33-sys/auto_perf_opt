"""性能热点函数源码提取器

从项目中提取热点函数的完整源代码，输出到独立目录供 LLM 分析。
"""

import os
import re
import json
from common.logger import logger


class SourceExtractor:
    """从项目源码中提取热点函数的代码片段"""

    def __init__(self, project_config, ast_data=None):
        """
        Args:
            project_config: ProjectConfig 对象
            ast_data: AST 解析结果 {"functions": {name: {file, line}}, ...}
        """
        self.config = project_config
        self.ast_data = ast_data
        # func_name -> (file_path, line_number)
        # 同时存储完全限定名和短名，方便匹配
        self._func_locations = {}
        # class_name -> {name, file, line, bases}
        self._class_locations = {}

        if ast_data and "functions" in ast_data:
            for func_name, info in ast_data["functions"].items():
                short_name = func_name.split("::")[-1]
                self._func_locations[func_name] = (info["file"], info["line"])
                # 也存短名，方便 perf 数据中带类前缀的名字匹配
                if short_name != func_name:
                    if short_name not in self._func_locations:
                        self._func_locations[short_name] = (info["file"], info["line"])

        if ast_data and "classes" in ast_data:
            for cls_name, info in ast_data["classes"].items():
                self._class_locations[cls_name] = info

    # ── 公共接口 ──────────────────────────────────────────

    def save_to_directory(self, hotspot_funcs, output_dir, context_lines=5):
        """将热点函数源码提取并保存到独立目录

        输出结构:
            hotspot_sources/
            ├── 01_checkCellPairs.cpp      # 每个热点函数一个文件
            ├── 02_updateAverage.cpp
            ├── ...
            ├── types.h                    # 关联的类型/结构体定义
            └── metrics.json               # 性能指标摘要
        """
        os.makedirs(output_dir, exist_ok=True)
        results = []
        seen = set()

        # 标准库/系统函数前缀，跳过不提取（不在项目中）
        SKIP_PREFIXES = ("std::", "__", "operator new", "operator delete",
                         "GOMP_", "libc", "libpthread", "libstdc")

        for i, func in enumerate(hotspot_funcs, 1):
            func_name = func.get("function", "")
            if func_name in seen:
                continue
            seen.add(func_name)

            # 跳过标准库函数
            if func_name.startswith(SKIP_PREFIXES) or func_name.startswith("["):
                continue

            code_info = self._extract_function_body(func_name)
            if not code_info:
                logger.warning(f"未找到函数源码: {func_name}")
                continue

            # 写入独立文件
            file_index = f"{i:02d}"
            safe_name = self._safe_filename(func_name)
            output_path = os.path.join(output_dir, f"{file_index}_{safe_name}.cpp")

            metrics = {
                k: func.get(k)
                for k in ["cpu_pct", "ipc", "cache_miss_rate",
                           "branch_miss_rate", "cycles", "total_samples"]
                if k in func
            }

            with open(output_path, "w", encoding="utf-8") as f:
                f.write(f"// ============================================================\n")
                f.write(f"// 热点函数 #{i}\n")
                f.write(f"// 函数名: {func_name}\n")
                f.write(f"// 源文件: {code_info['file']}\n")
                f.write(f"// 行号: {code_info['start_line']}-{code_info['end_line']}\n")
                if metrics:
                    f.write(f"//\n")
                    f.write(f"// 性能指标:\n")
                    for k, v in metrics.items():
                        f.write(f"//   {k}: {v}\n")
                f.write(f"// ============================================================\n\n")
                f.write(code_info["code"])

            code_info["function"] = func_name
            code_info["metrics"] = metrics
            code_info["output_file"] = output_path
            results.append(code_info)

        # 提取相关的类型定义
        if results:
            types_file = self._extract_related_types(results, output_dir)
            if types_file:
                logger.info(f"类型定义已保存: {types_file}")

        # 写 metrics.json 摘要
        metrics_path = os.path.join(output_dir, "metrics.json")
        summary = []
        for r in results:
            summary.append({
                "function": r.get("function", ""),
                "file": r.get("file", ""),
                "lines": f"{r.get('start_line', '?')}-{r.get('end_line', '?')}",
                "metrics": r.get("metrics", {}),
                "output_file": r.get("output_file", ""),
            })
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        logger.info(f"源码已提取到: {output_dir} (共 {len(results)} 个函数)")

        return results

    def extract_for_functions(self, hotspot_funcs, context_lines=30):
        """（兼容旧接口）为一组热点函数提取源码片段

        Returns:
            [{"function": name, "file": path, "start_line": N, "code": "...", "metrics": {...}}, ...]
        """
        results = []
        seen = set()

        for func in hotspot_funcs:
            func_name = func.get("function", "")
            if func_name in seen:
                continue
            seen.add(func_name)

            code_info = self._extract_function_body(func_name)
            if code_info:
                code_info["metrics"] = {
                    k: func.get(k)
                    for k in ["cpu_pct", "ipc", "cache_miss_rate",
                               "branch_miss_rate", "cycles", "total_samples"]
                    if k in func
                }
                code_info["function"] = func_name
                results.append(code_info)

        return results

    # ── 函数体提取 ─────────────────────────────────────────

    def _extract_function_body(self, func_name):
        """提取函数的完整源码（从签名到闭合大括号）

        通过名字匹配 + 花括号计数，获取完整的函数体。
        """
        # 1) 尝试从 AST 数据定位
        file_path, start_line = self._locate_function(func_name)

        # 2) 如果 AST 定位失败，回退到文件搜索
        if not file_path:
            file_path, start_line = self._search_function(func_name)

        if not file_path:
            return None

        # 3) 读取完整函数体（花括号匹配）
        return self._read_function_body(file_path, start_line)

    def _locate_function(self, func_name):
        """通过 AST 数据定位函数"""
        clean = self._clean_func_name(func_name)

        # 尝试多种匹配方式
        for candidate in [clean, func_name,
                          clean.split("::")[-1],
                          func_name.split("::")[-1]]:
            if candidate in self._func_locations:
                return self._func_locations[candidate]

        # 部分匹配：perf 名 "Diagnostics::updateAverage(double)"
        # AST 名 "updateAverage"
        for ast_name, loc in self._func_locations.items():
            if ast_name in clean or clean.endswith("::" + ast_name):
                return loc

        return None, None

    def _search_function(self, func_name):
        """在源文件目录中搜索函数定义"""
        clean = self._clean_func_name(func_name)
        short_name = clean.split("::")[-1]

        for root_dir in self.config.code_scope.source_roots:
            search_dir = os.path.join(self.config.project_path, root_dir)
            if not os.path.isdir(search_dir):
                continue

            for dirpath, _, filenames in os.walk(search_dir):
                for fname in filenames:
                    if not fname.endswith((".cpp", ".hpp", ".h", ".cc", ".cxx")):
                        continue
                    file_path = os.path.join(dirpath, fname)
                    line_num = self._find_func_definition(file_path, short_name, clean)
                    if line_num:
                        return file_path, line_num

        return None, None

    def _find_func_definition(self, file_path, short_name, full_name):
        """在文件中搜索函数定义行（支持跨行签名）

        处理风格:
          void Foo::bar(           ← 匹配这一行
              int x)               ← 参数可跨行
          {                        ← { 可在后续行
            ...
          }
        """
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            for i, line in enumerate(lines):
                # 快速过滤：行中必须包含函数名或 operator
                if short_name not in line and "operator" not in line:
                    continue
                # 必须是函数签名行：包含 (，且不是函数调用（前面有返回类型/类名模式）
                if "(" not in line:
                    continue

                # 匹配函数签名开头：可选返回值 + 可选 ClassName:: + 函数名 + (
                sig_pattern = re.compile(
                    rf"(?:^|\s)(?:(?:[\w:<>&,\s\*]+?::)?{re.escape(short_name)}|operator\s*\w*)\s*\(.*"
                )
                if not sig_pattern.search(line):
                    continue

                # 排除明显的非定义行：
                stripped = line.strip()
                # 前向声明（以 ; 结尾）
                if stripped.endswith(";"):
                    continue
                # 纯调用（行首就是函数名 + (，前面没有返回类型）
                # 启发式：如果行以空格开头且包含 :: 则是类外定义，否则可能只是调用
                # 不过这里简单处理：在后面搜索 { 来确认

                # 从当前行向后查找 {（最多看 10 行）
                for lookahead in range(10):
                    idx = i + lookahead
                    if idx >= len(lines):
                        break
                    look_line = lines[idx]
                    if "{" in look_line:
                        # 找到了 {，确认这是函数定义
                        # 还要排除一些误匹配（如 if/for/while/try）
                        if re.search(r'\b(if|for|while|switch|catch|try)\s*\(', look_line):
                            break
                        return i + 1
                    # 如果在找到 { 之前遇到了 ;，说明是声明不是定义
                    if ";" in look_line and lookahead < 3:
                        break

        except Exception:
            pass
        return None

    def _read_function_body(self, file_path, start_line):
        """从函数定义行开始，用花括号计数读取完整函数体

        算法:
        1. 从 start_line 向后扫描，找到第一个 {
        2. 从 { 开始计数，遇到 { 加 1，遇到 } 减 1
        3. 计数回到 0 时，函数体结束
        4. 返回从函数签名行到闭合 } 的完整代码
        """
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            idx = start_line - 1  # 转为 0-based
            if idx >= len(all_lines):
                return None

            # 从 start_line 向前搜索函数签名开始（找到前置的返回类型等）
            sig_start = idx
            # 向前最多找 5 行，找到函数签名的开始
            for back in range(1, 6):
                if idx - back >= 0:
                    prev = all_lines[idx - back].strip()
                    # 如果前一行有 ) 或 { 结尾且不包含函数名，可能是上一个函数/语句
                    if prev.endswith(")") or prev.endswith("{") or prev.endswith(";"):
                        break
                    sig_start = idx - back

            # 从 start_line 找到第一个 {
            brace_idx = None
            for j in range(idx, len(all_lines)):
                if "{" in all_lines[j]:
                    brace_idx = j
                    break

            if brace_idx is None:
                # 没有找到 {，退回旧逻辑：取上下文行数
                begin = max(0, sig_start)
                end = min(len(all_lines), idx + 80)
                return {
                    "file": file_path,
                    "start_line": begin + 1,
                    "end_line": end,
                    "func_line": start_line,
                    "code": "".join(all_lines[begin:end]),
                }

            # 从第一个 { 开始花括号计数
            depth = 0
            end_idx = brace_idx
            # 简化处理：跳过字符串和注释中的括号（不完美但够用）
            in_line_comment = False
            in_block_comment = False

            for j in range(brace_idx, len(all_lines)):
                line = all_lines[j]

                # 简化：跳过整行注释
                stripped = line.strip()
                if stripped.startswith("//"):
                    continue

                for ch in line:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end_idx = j
                            break

                if depth == 0 and end_idx != brace_idx:
                    break

            # 如果没有找到匹配的 }（文件不完整？），取 200 行
            if depth != 0:
                end_idx = min(len(all_lines) - 1, brace_idx + 200)

            code = "".join(all_lines[sig_start:end_idx + 1])

            # 如果结果太短（可能匹配错了），扩大范围
            if len(code.strip().split("\n")) < 3:
                begin = max(0, sig_start)
                end = min(len(all_lines), idx + 80)
                code = "".join(all_lines[begin:end])
                end_idx = end - 1

            return {
                "file": file_path,
                "start_line": sig_start + 1,
                "end_line": end_idx + 1,
                "func_line": start_line,
                "code": code,
            }
        except Exception as e:
            logger.debug(f"读取函数体失败 {file_path}:{start_line}: {e}")
            return None

    # ── 类型定义提取 ───────────────────────────────────────

    def _extract_related_types(self, func_results, output_dir):
        """从热点函数中分析引用的类型，提取对应的类/结构体定义"""
        used_types = set()

        for r in func_results:
            code = r.get("code", "")
            file_path = r.get("file", "")
            # 从源码中提取可能的类型名
            found = self._scan_types_in_code(code)
            used_types.update(found)

            # 如果函数在 .cpp 文件中，找对应的头文件
            if file_path.endswith(".cpp") or file_path.endswith(".cc"):
                header = self._find_corresponding_header(file_path)
                if header:
                    used_types.add(("header", header))

        if not used_types:
            return None

        types_path = os.path.join(output_dir, "types.h")
        with open(types_path, "w", encoding="utf-8") as f:
            f.write("// ============================================================\n")
            f.write("// 热点函数相关的类型定义\n")
            f.write("// 自动提取，供 LLM 分析上下文使用\n")
            f.write("// ============================================================\n\n")

            for item in used_types:
                if isinstance(item, tuple) and item[0] == "header":
                    # 是整个头文件
                    header_path = item[1]
                    f.write(f"\n// === 头文件: {os.path.basename(header_path)} ===\n\n")
                    try:
                        with open(header_path, "r", encoding="utf-8", errors="replace") as hf:
                            f.write(hf.read())
                    except Exception:
                        pass
                else:
                    # 是类型名，从 AST 数据中查找
                    type_name = item
                    if type_name in self._class_locations:
                        info = self._class_locations[type_name]
                        body = self._read_function_body(info["file"], info["line"])
                        if body:
                            f.write(f"\n// === 类型: {type_name} ===\n")
                            f.write(f"// 源文件: {info['file']}:{info['line']}\n\n")
                            f.write(body["code"])
                            f.write("\n")

        return types_path

    def _scan_types_in_code(self, code):
        """扫描源码中引用的自定义类型名"""
        types = set()
        # 简单的启发式：匹配大写开头的标识符，排除关键字和标准库类型
        std_types = {"std", "vector", "string", "map", "set", "pair", "unique_ptr",
                     "shared_ptr", "size_t", "double", "int", "float", "bool", "void",
                     "char", "long", "short", "unsigned", "const", "auto", "static",
                     "if", "else", "for", "while", "return", "true", "false"}
        # 匹配类似类名的标识符（大写开头，后跟字母数字）
        pattern = re.compile(r'\b([A-Z][a-zA-Z0-9_]+)\b')
        for match in pattern.finditer(code):
            word = match.group(1)
            if word not in std_types and len(word) > 1:
                if word in self._class_locations:
                    types.add(word)
        return types

    def _find_corresponding_header(self, cpp_file):
        """找到 .cpp 文件对应的头文件"""
        base = os.path.splitext(cpp_file)[0]
        for ext in [".h", ".hpp"]:
            candidate = base + ext
            if os.path.isfile(candidate):
                return candidate
        return None

    # ── 工具方法 ───────────────────────────────────────────

    @staticmethod
    def _clean_func_name(name):
        """清理 perf 里的函数名：去掉参数类型和模板噪声"""
        # 取第一个 ( 之前的部分
        paren_idx = name.find("(")
        if paren_idx > 0:
            name = name[:paren_idx]
        # 去掉 <...> 模板参数（只对末尾的，保留类名中的）
        # 简化处理：找到第一个非嵌套的 < 如果它在名字末尾部分
        name = name.strip()
        return name

    @staticmethod
    def _safe_filename(func_name):
        """生成安全的文件名"""
        # 替换特殊字符
        name = func_name.replace("::", "_")
        name = re.sub(r'[<>:"|?*]', '_', name)
        name = re.sub(r'\s+', '_', name)
        # 截断过长名字
        if len(name) > 60:
            name = name[:60]
        return name