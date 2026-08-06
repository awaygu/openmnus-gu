from app.tool.chart_visualization.python_execute import NormalPythonExecute


class VisualizationPrepare(NormalPythonExecute):
    """用于图表生成准备的工具"""

    name: str = "visualization_preparation"
    description: str = "使用 Python 代码生成 data_visualization 工具的元数据。输出：1) JSON 信息。2) 清理后的 CSV 数据文件（可选）。"
    parameters: dict = {
        "type": "object",
        "properties": {
            "code_type": {
                "description": "代码类型，visualization: csv -> chart；insight: 选择要添加到图表的洞察",
                "type": "string",
                "default": "visualization",
                "enum": ["visualization", "insight"],
            },
            "code": {
                "type": "string",
                "description": """用于 data_visualization 准备的 Python 代码。
## Visualization Type（可视化类型）
1. 数据加载逻辑
2. CSV 数据和图表描述生成
2.1 CSV 数据（您想要可视化的数据，从原始数据清理/转换，保存为 .csv）
2.2 CSV 数据的图表描述（图表标题或描述应简洁明了。示例：'产品销售分布'、'月度收入趋势'。）
3. 将信息保存到 json 文件中。（格式：{"csvFilePath": string, "chartTitle": string}[]）
## Insight Type（洞察类型）
1. 从 data_visualization 结果中选择要添加到图表的洞察。
2. 将信息保存到 json 文件中。（格式：{"chartPath": string, "insights_id": number[]}[]）
# 注意
1. 您可以根据不同的可视化需求生成一个或多个 CSV 数据。
2. 使每个图表数据简单、干净且不同。
3. Json 文件以 utf-8 编码保存，并打印路径：print(json_path)
""",
            },
        },
        "required": ["code", "code_type"],
    }
