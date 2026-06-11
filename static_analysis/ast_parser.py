import os
import clang.cindex
from external_tools.clang_adapter import ClangAdapter
from common.logger import logger


class ASTParser:
    def __init__(self, project_config):
        self.config = project_config
        self.clang = ClangAdapter(project_config)
        self.functions = {}  # 函数名 -> 函数信息
        self.classes = {}  # 类名 -> 类信息
        self.call_graph = {}  # 调用者 -> [被调用者列表]

    def parse_project(self):
        logger.info("开始解析项目...")

        # 遍历所有源文件
        for root, dirs, files in os.walk(self.config.project_path):
            # 过滤排除目录
            dirs[:] = [d for d in dirs if d not in self.config.code_scope.exclude_dirs]

            for file in files:
                if file.endswith(('.cpp', '.hpp', '.h', '.cc')):
                    file_path = os.path.join(root, file)
                    self._parse_file(file_path)

        logger.info(f"项目解析完成，共发现 {len(self.functions)} 个函数，{len(self.classes)} 个类")
        return {
            "functions": self.functions,
            "classes": self.classes,
            "call_graph": self.call_graph
        }

    def _parse_file(self, file_path):
        """解析单个文件"""
        tu = self.clang.parse_file(file_path)
        if not tu:
            return

        self._traverse_ast(tu.cursor, file_path)

    def _traverse_ast(self, node, file_path, current_func=None):
        """递归遍历AST节点"""
        # 只处理当前文件的节点
        if node.location.file and node.location.file.name != file_path:
            return

        # 处理函数声明
        if node.kind == clang.cindex.CursorKind.FUNCTION_DECL and node.is_definition():
            func_name = node.spelling
            self.functions[func_name] = {
                "name": func_name,
                "file": file_path,
                "line": node.location.line
            }
            self.call_graph[func_name] = []
            current_func = func_name

        # 处理类声明
        elif node.kind == clang.cindex.CursorKind.CLASS_DECL:
            class_name = node.spelling
            if class_name:
                self.classes[class_name] = {
                    "name": class_name,
                    "file": file_path,
                    "line": node.location.line,
                    "bases": []
                }
                # 提取基类
                for child in node.get_children():
                    if child.kind == clang.cindex.CursorKind.CXX_BASE_SPECIFIER:
                        base_name = child.spelling
                        self.classes[class_name]["bases"].append(base_name)

        # 处理函数调用
        elif node.kind == clang.cindex.CursorKind.CALL_EXPR and current_func:
            called_func = node.spelling
            if called_func and called_func not in self.call_graph[current_func]:
                self.call_graph[current_func].append(called_func)

        # 递归处理子节点
        for child in node.get_children():
            self._traverse_ast(child, file_path, current_func)