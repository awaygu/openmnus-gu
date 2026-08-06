import asyncio
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, model_validator
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import config
from app.logger import logger
from app.tool.base import BaseTool, ToolResult
from app.tool.search import (
    BaiduSearchEngine,
    BingSearchEngine,
    DuckDuckGoSearchEngine,
    GoogleSearchEngine,
    WebSearchEngine,
)
from app.tool.search.base import SearchItem


class SearchResult(BaseModel):
    """表示搜索引擎返回的单个搜索结果。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    position: int = Field(description="在搜索结果中的位置")
    url: str = Field(description="搜索结果的 URL")
    title: str = Field(default="", description="搜索结果的标题")
    description: str = Field(
        default="", description="搜索结果的描述或摘要"
    )
    source: str = Field(description="提供此结果的搜索引擎")
    raw_content: Optional[str] = Field(
        default=None, description="如果可用，来自搜索结果页面的原始内容"
    )

    def __str__(self) -> str:
        """搜索结果的字符串表示。"""
        return f"{self.title} ({self.url})"


class SearchMetadata(BaseModel):
    """关于搜索操作的元数据。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    total_results: int = Field(description="找到的结果总数")
    language: str = Field(description="用于搜索的语言代码")
    country: str = Field(description="用于搜索的国家代码")


class SearchResponse(ToolResult):
    """来自网页搜索工具的结构化响应，继承自 ToolResult。"""

    query: str = Field(description="执行的搜索查询")
    results: List[SearchResult] = Field(
        default_factory=list, description="搜索结果列表"
    )
    metadata: Optional[SearchMetadata] = Field(
        default=None, description="关于搜索的元数据"
    )

    @model_validator(mode="after")
    def populate_output(self) -> "SearchResponse":
        """根据搜索结果填充输出或错误字段。"""
        if self.error:
            return self

        result_text = [f"Search results for '{self.query}':"]

        for i, result in enumerate(self.results, 1):
            # Add title with position number
            title = result.title.strip() or "No title"
            result_text.append(f"\n{i}. {title}")

            # Add URL with proper indentation
            result_text.append(f"   URL: {result.url}")

            # Add description if available
            if result.description.strip():
                result_text.append(f"   Description: {result.description}")

            # Add content preview if available
            if result.raw_content:
                content_preview = result.raw_content[:1000].replace("\n", " ").strip()
                if len(result.raw_content) > 1000:
                    content_preview += "..."
                result_text.append(f"   Content: {content_preview}")

        # Add metadata at the bottom if available
        if self.metadata:
            result_text.extend(
                [
                    f"\nMetadata:",
                    f"- Total results: {self.metadata.total_results}",
                    f"- Language: {self.metadata.language}",
                    f"- Country: {self.metadata.country}",
                ]
            )

        self.output = "\n".join(result_text)
        return self


class WebContentFetcher:
    """用于获取网页内容的工具类。"""

    @staticmethod
    async def fetch_content(url: str, timeout: int = 10) -> Optional[str]:
        """
        从网页获取并提取主要内容。

        Args:
            url: 要获取内容的 URL
            timeout: 请求超时时间（秒）

        Returns:
            提取的文本内容，如果获取失败则返回 None
        """
        headers = {
            "WebSearch": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        try:
            # 使用 asyncio 在线程池中运行 requests
            response = await asyncio.get_event_loop().run_in_executor(
                None, lambda: requests.get(url, headers=headers, timeout=timeout)
            )

            if response.status_code != 200:
                logger.warning(
                    f"Failed to fetch content from {url}: HTTP {response.status_code}"
                )
                return None

            # 使用 BeautifulSoup 解析 HTML
            soup = BeautifulSoup(response.text, "html.parser")

            # 删除 script 和 style 元素
            for script in soup(["script", "style", "header", "footer", "nav"]):
                script.extract()

            # 获取文本内容
            text = soup.get_text(separator="\n", strip=True)

            # 清理空白并限制大小（最大 100KB）
            text = " ".join(text.split())
            return text[:10000] if text else None

        except Exception as e:
            logger.warning(f"Error fetching content from {url}: {e}")
            return None


class WebSearch(BaseTool):
    """使用各种搜索引擎搜索网页信息。"""

    name: str = "web_search"
    description: str = """搜索网页以获取关于任何主题的实时信息。
    此工具返回包含相关信息、URL、标题和描述的全面搜索结果。
    如果主要搜索引擎失败，它会自动回退到备用引擎。"""
    parameters: dict = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "（必需）要提交给搜索引擎的搜索查询。",
            },
            "num_results": {
                "type": "integer",
                "description": "（可选）要返回的搜索结果数量。默认为 5。",
                "default": 5,
            },
            "lang": {
                "type": "string",
                "description": "（可选）搜索结果的语言代码（默认: en）。",
                "default": "en",
            },
            "country": {
                "type": "string",
                "description": "（可选）搜索结果的国家代码（默认: us）。",
                "default": "us",
            },
            "fetch_content": {
                "type": "boolean",
                "description": "（可选）是否从结果页面获取完整内容。默认为 false。",
                "default": False,
            },
        },
        "required": ["query"],
    }
    _search_engine: dict[str, WebSearchEngine] = {
        "google": GoogleSearchEngine(),
        "baidu": BaiduSearchEngine(),
        "duckduckgo": DuckDuckGoSearchEngine(),
        "bing": BingSearchEngine(),
    }
    content_fetcher: WebContentFetcher = WebContentFetcher()

    async def execute(
        self,
        query: str,
        num_results: int = 5,
        lang: Optional[str] = None,
        country: Optional[str] = None,
        fetch_content: bool = False,
    ) -> SearchResponse:
        """
        执行网页搜索并返回详细的搜索结果。

        Args:
            query: 要提交给搜索引擎的搜索查询
            num_results: 要返回的搜索结果数量（默认: 5）
            lang: 搜索结果的语言代码（默认来自配置）
            country: 搜索结果的国家代码（默认来自配置）
            fetch_content: 是否从结果页面获取内容（默认: False）

        Returns:
            包含搜索结果和元数据的结构化响应
        """
        # 从配置获取设置
        retry_delay = (
            getattr(config.search_config, "retry_delay", 60)
            if config.search_config
            else 60
        )
        max_retries = (
            getattr(config.search_config, "max_retries", 3)
            if config.search_config
            else 3
        )

        # 如果未指定，使用配置中的 lang 和 country 值
        if lang is None:
            lang = (
                getattr(config.search_config, "lang", "en")
                if config.search_config
                else "en"
            )

        if country is None:
            country = (
                getattr(config.search_config, "country", "us")
                if config.search_config
                else "us"
            )

        search_params = {"lang": lang, "country": country}

        # 当所有引擎都失败时，尝试重试搜索
        for retry_count in range(max_retries + 1):
            results = await self._try_all_engines(query, num_results, search_params)

            if results:
                # 如果请求，则获取内容
                if fetch_content:
                    results = await self._fetch_content_for_results(results)

                # 返回成功的结构化响应
                return SearchResponse(
                    status="success",
                    query=query,
                    results=results,
                    metadata=SearchMetadata(
                        total_results=len(results),
                        language=lang,
                        country=country,
                    ),
                )

            if retry_count < max_retries:
                # 所有引擎都失败，等待并重试
                logger.warning(
                    f"All search engines failed. Waiting {retry_delay} seconds before retry {retry_count + 1}/{max_retries}..."
                )
                await asyncio.sleep(retry_delay)
            else:
                logger.error(
                    f"All search engines failed after {max_retries} retries. Giving up."
                )

        # 返回错误响应
        return SearchResponse(
            query=query,
            error="All search engines failed to return results after multiple retries.",
            results=[],
        )

    async def _try_all_engines(
        self, query: str, num_results: int, search_params: Dict[str, Any]
    ) -> List[SearchResult]:
        """按配置的顺序尝试所有搜索引擎。"""
        engine_order = self._get_engine_order()
        failed_engines = []

        for engine_name in engine_order:
            engine = self._search_engine[engine_name]
            logger.info(f"🔎 Attempting search with {engine_name.capitalize()}...")
            search_items = await self._perform_search_with_engine(
                engine, query, num_results, search_params
            )

            if not search_items:
                continue

            if failed_engines:
                logger.info(
                    f"Search successful with {engine_name.capitalize()} after trying: {', '.join(failed_engines)}"
                )

            # 将搜索项转换为结构化结果
            return [
                SearchResult(
                    position=i + 1,
                    url=item.url,
                    title=item.title
                    or f"Result {i+1}",  # 确保我们始终有一个标题
                    description=item.description or "",
                    source=engine_name,
                )
                for i, item in enumerate(search_items)
            ]

        if failed_engines:
            logger.error(f"All search engines failed: {', '.join(failed_engines)}")
        return []

    async def _fetch_content_for_results(
        self, results: List[SearchResult]
    ) -> List[SearchResult]:
        """获取网页内容并将其添加到搜索结果中。"""
        if not results:
            return []

        # 为每个结果创建任务
        tasks = [self._fetch_single_result_content(result) for result in results]

        # 类型注释以帮助类型检查器
        fetched_results = await asyncio.gather(*tasks)

        # 显式验证返回类型
        return [
            (
                result
                if isinstance(result, SearchResult)
                else SearchResult(**result.dict())
            )
            for result in fetched_results
        ]

    async def _fetch_single_result_content(self, result: SearchResult) -> SearchResult:
        """获取单个搜索结果的内容。"""
        if result.url:
            content = await self.content_fetcher.fetch_content(result.url)
            if content:
                result.raw_content = content
        return result

    def _get_engine_order(self) -> List[str]:
        """确定尝试搜索引擎的顺序。"""
        preferred = (
            getattr(config.search_config, "engine", "google").lower()
            if config.search_config
            else "google"
        )
        fallbacks = (
            [engine.lower() for engine in config.search_config.fallback_engines]
            if config.search_config
            and hasattr(config.search_config, "fallback_engines")
            else []
        )

        # 从首选引擎开始，然后是备用引擎，最后是剩余的引擎
        engine_order = [preferred] if preferred in self._search_engine else []
        engine_order.extend(
            [
                fb
                for fb in fallbacks
                if fb in self._search_engine and fb not in engine_order
            ]
        )
        engine_order.extend([e for e in self._search_engine if e not in engine_order])

        return engine_order

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10)
    )
    async def _perform_search_with_engine(
        self,
        engine: WebSearchEngine,
        query: str,
        num_results: int,
        search_params: Dict[str, Any],
    ) -> List[SearchItem]:
        """使用给定的引擎和参数执行搜索。"""
        return await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: list(
                engine.perform_search(
                    query,
                    num_results=num_results,
                    lang=search_params.get("lang"),
                    country=search_params.get("country"),
                )
            ),
        )


if __name__ == "__main__":
    web_search = WebSearch()
    search_response = asyncio.run(
        web_search.execute(
            query="Python programming", fetch_content=True, num_results=1
        )
    )
    print(search_response.to_tool_result())
