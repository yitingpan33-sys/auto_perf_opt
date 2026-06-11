一、安装必备环境
# 系统依赖（apt离线源）
sudo apt install libclang-14-dev graphviz linux-tools-common linux-tools-$(uname -r) libgtest-dev libbenchmark-dev cmake g++

# Python依赖（pip离线源）
pip install pyyaml clang graphviz

二、代码结构
auto_perf_opt/
├── common/
│   ├── __init__.py
│   ├── config.py      # 项目配置管理
│   └── logger.py      # 统一日志
├── external_tools/
│   ├── __init__.py
│   ├── clang_adapter.py  # Clang工具封装
│   └── perf_adapter.py   # Perf工具封装
├── static_analysis/
│   ├── __init__.py
│   ├── ast_parser.py     # AST解析器
│   └── graph_generator.py # 可视化图生成
├── dynamic_analysis/
│   ├── __init__.py
│   ├── benchmark_runner.py # 基准测试运行器
│   └── hotspot_analyzer.py # 热点分析器
└── main.py  # 主程序入口

三、所需库安装
pip install pyyaml
pip install libclang==14.0.6
sudo apt-get install -y graphviz
pip install graphviz 
pip install requests
