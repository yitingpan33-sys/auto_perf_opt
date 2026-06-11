from graphviz import Digraph, Graph
import os
from common.logger import logger


class GraphGenerator:
    def __init__(self, output_dir):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def generate_call_graph(self, call_graph, functions):
        """生成函数调用图"""
        logger.info("生成函数调用图...")
        dot = Digraph('function_call_graph', format='png', node_attr={'fontsize': '10'})

        # 添加函数节点
        for func_name in functions:
            dot.node(func_name, shape='box')

        # 添加调用边
        for caller, callees in call_graph.items():
            for callee in callees:
                if callee in functions:  # 只显示项目内的函数
                    dot.edge(caller, callee)

        output_path = os.path.join(self.output_dir, 'function_call_graph')
        dot.render(output_path, view=False)
        logger.info(f"函数调用图已生成: {output_path}.png")
        return f"{output_path}.png"

    def generate_class_inheritance_graph(self, classes):
        """生成类继承图"""
        logger.info("生成类继承图...")
        dot = Digraph('class_inheritance', format='png', node_attr={'fontsize': '10'})

        # 添加类节点
        for class_name in classes:
            dot.node(class_name, shape='ellipse')

        # 添加继承边
        for class_name, class_info in classes.items():
            for base_name in class_info["bases"]:
                if base_name in classes:
                    dot.edge(class_name, base_name)

        output_path = os.path.join(self.output_dir, 'class_inheritance_graph')
        dot.render(output_path, view=False)
        logger.info(f"类继承图已生成: {output_path}.png")
        return f"{output_path}.png"

    def render_dynamic_call_graph(self, dot_path):
        """将 DOT 文件渲染为 PNG 图片

        Args:
            dot_path: DOT 文件路径（由 HotspotAnalyzer 生成）

        Returns:
            PNG 文件路径，或 None（失败时）
        """
        if not os.path.exists(dot_path):
            logger.warning(f"DOT 文件不存在: {dot_path}")
            return None

        try:
            from graphviz import Source
            with open(dot_path, "r") as f:
                dot_source = f.read()
            src = Source(dot_source)
            output_path = dot_path.replace(".dot", "")
            src.render(output_path, view=False, format="png")
            logger.info(f"动态调用图已生成: {output_path}.png")
            return f"{output_path}.png"
        except Exception as e:
            logger.warning(f"渲染动态调用图失败: {e}")
            return None