from app.config import config
from app.tool.python_execute import PythonExecute


class NormalPythonExecute(PythonExecute):
    """用于执行 Python 代码的工具，具有超时和安全限制。"""

    name: str = "python_execute"
    description: str = """执行 Python 代码用于深入数据分析 / 数据报告（任务结论）/ 其他不直接可视化的普通任务。"""
    parameters: dict = {
        "type": "object",
        "properties": {
            "code_type": {
                "description": "代码类型，数据处理 / 数据报告 / 其他",
                "type": "string",
                "default": "process",
                "enum": ["process", "report", "others"],
            },
            "code": {
                "type": "string",
                "description": """要执行的 Python 代码。
# 注意
1. 代码应生成包含数据集概述、列详细信息、基本统计、派生指标、时间序列比较、异常值和关键洞察的综合文本报告。
2. 对所有输出使用 print()，以便分析（包括"数据集概述"或"预处理结果"等部分）清晰可见并保存
3. 将任何报告 / 处理后的文件 / 每个分析结果保存在工作区目录：{directory}
4. 数据报告需要内容丰富，包括您的整体分析过程和相应的数据可视化。
5. 您可以逐步调用此工具，从摘要到深入进行数据分析，同时保存数据报告""".format(
                    directory=config.workspace_root
                ),
            },
        },
        "required": ["code"],
    }

    async def execute(self, code: str, code_type: str | None = None, timeout=5):
        return await super().execute(code, timeout)
