import yaml
import os
from dataclasses import dataclass
from common.logger import logger


@dataclass
class ExecutableConfig:
    path: str
    args: list = None
    run_duration: int = None  # None=等待程序自然结束

    def __post_init__(self):
        if self.args is None:
            self.args = []
        self.path = os.path.abspath(self.path)


@dataclass
class CodeScopeConfig:
    source_roots: list = None
    exclude_dirs: list = None

    def __post_init__(self):
        if self.source_roots is None:
            self.source_roots = ["src", "include"]
        if self.exclude_dirs is None:
            self.exclude_dirs = ["build", "third_party", "test", "vendor", "deps"]


@dataclass
class ProjectConfig:
    project_name: str
    project_path: str
    executable: ExecutableConfig
    code_scope: CodeScopeConfig
    build_dir: str = "build"

    def __post_init__(self):
        self.project_path = os.path.abspath(self.project_path)
        self.build_dir = os.path.join(self.project_path, self.build_dir)

        # 转换嵌套对象
        if isinstance(self.executable, dict):
            self.executable = ExecutableConfig(**self.executable)
        if isinstance(self.code_scope, dict):
            self.code_scope = CodeScopeConfig(**self.code_scope)


def load_config(config_path):
    if not os.path.exists(config_path):
        logger.error(f"配置文件不存在: {config_path}")
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, 'r') as f:
        config_data = yaml.safe_load(f)

    return ProjectConfig(**config_data)