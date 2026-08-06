from pydantic import Field

from app.agent.toolcall import ToolCallAgent
from app.config import config
from app.prompt.visualization import NEXT_STEP_PROMPT, SYSTEM_PROMPT
from app.tool import Terminate, ToolCollection
from app.tool.chart_visualization.chart_prepare import VisualizationPrepare
from app.tool.chart_visualization.data_visualization import DataVisualization
from app.tool.chart_visualization.python_execute import NormalPythonExecute


class DataAnalysis(ToolCallAgent):
    """
    一个使用规划来解决各种数据分析任务的数据分析 agent。

    此 agent 扩展了 ToolCallAgent，提供了一套全面的工具和能力，
    包括数据分析、图表可视化、数据报告。
    """

    name: str = "Data_Analysis"
    description: str = "一个利用 Python 和数据可视化工具来解决各种数据分析任务的分析 agent"

    system_prompt: str = SYSTEM_PROMPT.format(directory=config.workspace_root)
    next_step_prompt: str = NEXT_STEP_PROMPT

    max_observe: int = 15000
    max_steps: int = 20

    # 添加通用工具到工具集合
    available_tools: ToolCollection = Field(
        default_factory=lambda: ToolCollection(
            NormalPythonExecute(),
            VisualizationPrepare(),
            DataVisualization(),
            Terminate(),
        )
    )
