from enum import Enum
from typing import Dict, List, Union

from app.agent.base import BaseAgent
from app.flow.base import BaseFlow
from app.flow.planning import PlanningFlow


class FlowType(str, Enum):
    """流程类型枚举"""
    PLANNING = "planning"


class FlowFactory:
    """用于创建不同类型流程的工厂类，支持多个 agent"""

    @staticmethod
    def create_flow(
        flow_type: FlowType,
        agents: Union[BaseAgent, List[BaseAgent], Dict[str, BaseAgent]],
        **kwargs,
    ) -> BaseFlow:
        """创建指定类型的流程

        Args:
            flow_type: 流程类型
            agents: agent 实例（可以是单个、列表或字典）
            **kwargs: 其他参数

        Returns:
            创建的流程实例

        Raises:
            ValueError: 如果流程类型未知
        """
        flows = {
            FlowType.PLANNING: PlanningFlow,
        }

        flow_class = flows.get(flow_type)
        if not flow_class:
            raise ValueError(f"Unknown flow type: {flow_type}")

        return flow_class(agents, **kwargs)
