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
class LLMConfig:
    """大模型配置"""
    provider: str = "openai"         # 接口类型（openai 兼容格式）
    api_key: str = ""                # API 密钥
    base_url: str = ""               # API 地址，例如 http://10.0.0.5:8000/v1
    model: str = ""                  # 模型名称
    temperature: float = 0.1         # 温度（越低越稳定）
    max_tokens: int = 4096           # 最大输出 token 数
    timeout: int = 120               # 请求超时秒数

    @property
    def api_url(self):
        """拼接完整的 chat completions 地址"""
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    @property
    def enabled(self):
        """是否启用了 LLM"""
        return bool(self.base_url)


@dataclass
class ProjectConfig:
    project_name: str
    project_path: str
    executable: ExecutableConfig
    code_scope: CodeScopeConfig
    llm: LLMConfig = None            # LLM 配置，可选的
    build_dir: str = "build"

    def __post_init__(self):
        self.project_path = os.path.abspath(self.project_path)
        self.build_dir = os.path.join(self.project_path, self.build_dir)

        # 转换嵌套对象
        if isinstance(self.executable, dict):
            self.executable = ExecutableConfig(**self.executable)
        if isinstance(self.code_scope, dict):
            self.code_scope = CodeScopeConfig(**self.code_scope)
        if isinstance(self.llm, dict):
            self.llm = LLMConfig(**self.llm)
        elif self.llm is None:
            self.llm = LLMConfig()


def load_config(config_path):
    if not os.path.exists(config_path):
        abs_path = os.path.abspath(config_path)
        logger.error(f"配置文件不存在: {abs_path}")
        raise FileNotFoundError(
            f"配置文件不存在: {abs_path}\n"
            f"请确认文件路径正确，或参考示例配置创建新的配置文件。\n"
            f"用法: python main.py -c <config.yaml>"
        )

    with open(config_path, 'r') as f:
        config_data = yaml.safe_load(f)

    return ProjectConfig(**config_data)