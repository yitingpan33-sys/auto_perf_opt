import clang.cindex
from common.logger import logger


class ClangAdapter:
    def __init__(self, project_config):
        CLANG_LIB_PATH = '/usr/lib/llvm-14/lib/libclang.so.1'

        try:
            clang.cindex.Config.set_library_file(CLANG_LIB_PATH)
            self.index = clang.cindex.Index.create()

            # 完整的编译参数：添加OpenMP和nlohmann/json支持
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

            logger.info(f"✅ Clang初始化成功，使用库文件: {CLANG_LIB_PATH}")
        except Exception as e:
            logger.error(f"❌ Clang初始化失败: {e}")
            raise RuntimeError(f"Clang初始化失败: {e}")

    def parse_file(self, file_path, compile_args=None):
        if compile_args is None:
            compile_args = self.default_compile_args

        try:
            tu = self.index.parse(file_path, args=compile_args)
            logger.debug(f"成功解析文件: {file_path}")
            return tu
        except Exception as e:
            logger.error(f"解析文件失败 {file_path}: {e}")
            return None