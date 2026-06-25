import os
import subprocess
import clang.cindex
from common.logger import logger


def _find_libclang():
    """自动探测系统中可用的 libclang.so 路径"""
    # 候选路径列表，按优先级排列
    candidates = []

    # 1. 通过 find 命令搜索
    try:
        result = subprocess.run(
            ["find", "/usr/lib", "-maxdepth", "4", "-name", "libclang.so*"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line and os.path.isfile(line):
                    # 优先选择非符号链接的 .so 文件（数字结尾的如 .so.1）
                    if os.path.islink(line):
                        candidates.append(line)
                    else:
                        candidates.insert(0, line)
    except Exception:
        pass

    # 2. 常见路径
    common_paths = [
        "/usr/lib/llvm-14/lib/libclang.so.1",
        "/usr/lib/llvm-14/lib/libclang.so",
        "/usr/lib/llvm-15/lib/libclang.so.1",
        "/usr/lib/llvm-15/lib/libclang.so",
        "/usr/lib/llvm-16/lib/libclang.so.1",
        "/usr/lib/llvm-16/lib/libclang.so",
        "/usr/lib/llvm/libclang.so.1",
        "/usr/lib/llvm/libclang.so",
        "/usr/lib/x86_64-linux-gnu/libclang-14.so.1",
        "/usr/lib/x86_64-linux-gnu/libclang-15.so.1",
        "/usr/lib/x86_64-linux-gnu/libclang-16.so.1",
        "/usr/lib/libclang.so",
        "/usr/local/lib/libclang.so",
    ]
    for p in common_paths:
        if os.path.isfile(p) and p not in candidates:
            candidates.append(p)

    return candidates


class ClangAdapter:
    def __init__(self, project_config):
        clang_initialized = False
        last_error = None

        candidates = _find_libclang()

        if not candidates:
            raise RuntimeError(
                "未找到 libclang.so。请安装 clang 开发库:\n"
                "  sudo apt install libclang-14-dev   # Ubuntu/Debian\n"
                "  或设置环境变量 CLANG_LIB_PATH=/path/to/libclang.so"
            )

        for lib_path in candidates:
            try:
                clang.cindex.Config.set_library_file(lib_path)
                self.index = clang.cindex.Index.create()

                # 完整编译参数：添加 OpenMP 和 nlohmann/json 支持
                self.default_compile_args = [
                    '-x', 'c++',
                    '-std=c++17',
                    '-fopenmp',
                    '-isystem', '/usr/include/c++/11',
                    '-isystem', '/usr/include/x86_64-linux-gnu/c++/11',
                    '-isystem', '/usr/include/c++/11/backward',
                    '-isystem', '/usr/lib/gcc/x86_64-linux-gnu/11/include',
                    '-isystem', '/usr/local/include',
                    '-isystem', '/usr/include/x86_64-linux-gnu',
                    '-isystem', '/usr/include',
                    '-I', f'{project_config.project_path}/include',
                    '-I', '/usr/include/nlohmann'
                ]

                logger.info(f"✅ Clang初始化成功，使用库文件: {lib_path}")
                clang_initialized = True
                break

            except Exception as e:
                last_error = str(e)
                logger.debug(f"尝试 {lib_path} 失败: {e}")
                continue

        if not clang_initialized:
            logger.error(
                f"❌ Clang初始化失败，尝试了 {len(candidates)} 个路径。"
                f"最后一个错误: {last_error}\n"
                f"请确保 Python clang 绑定与系统的 libclang.so 版本匹配:\n"
                f"  pip install clang==14.0.0   # 安装与系统 libclang 匹配的版本"
            )
            raise RuntimeError(f"Clang初始化失败: {last_error}")

    def parse_file(self, file_path, compile_args=None):
        if compile_args is None:
            compile_args = self.default_compile_args

        try:
            tu = self.index.parse(file_path, args=compile_args)

            # 收集 clang 诊断信息用于调试
            diagnostics = list(tu.diagnostics)
            if diagnostics:
                diag_msgs = [f"  [{d.severity}] {d.format()}" for d in diagnostics[:5]]
                logger.debug(f"解析 {os.path.basename(file_path)} 的诊断信息:\n" + "\n".join(diag_msgs))

            logger.debug(f"成功解析文件: {file_path}")
            return tu
        except Exception as e:
            logger.error(f"解析文件失败 {file_path}: {e}")
            return None