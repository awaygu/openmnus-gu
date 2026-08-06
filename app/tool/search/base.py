from typing import List, Optional

from pydantic import BaseModel, Field


class SearchItem(BaseModel):
    """表示单个搜索结果项"""

    title: str = Field(description="搜索结果的标题")
    url: str = Field(description="搜索结果的 URL")
    description: Optional[str] = Field(
        default=None, description="搜索结果的描述或摘要"
    )

    def __str__(self) -> str:
        """搜索结果项的字符串表示。"""
        return f"{self.title} - {self.url}"


class WebSearchEngine(BaseModel):
    """网页搜索引擎的基类。"""

    model_config = {"arbitrary_types_allowed": True}

    def perform_search(
        self, query: str, num_results: int = 10, *args, **kwargs
    ) -> List[SearchItem]:
        """
        执行网页搜索并返回搜索结果项列表。

        Args:
            query (str): 要提交给搜索引擎的搜索查询。
            num_results (int, optional): 要返回的搜索结果数量。默认为 10。
            args: 其他参数。
            kwargs: 其他关键字参数。

        Returns:
            List[SearchItem]: 匹配搜索查询的 SearchItem 对象列表。
        """
        raise NotImplementedError
