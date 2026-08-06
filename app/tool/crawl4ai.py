"""
OpenManus 的 Crawl4AI 网页爬虫工具

此工具集成了 Crawl4AI，一个专为 LLM 和 AI agent 设计的高性能网页爬虫，
提供快速、精确且适合 AI 的数据提取，并生成干净的 Markdown。
"""

import asyncio
from typing import List, Union
from urllib.parse import urlparse

from app.logger import logger
from app.tool.base import BaseTool, ToolResult


class Crawl4aiTool(BaseTool):
    """
    由 Crawl4AI 驱动的网页爬虫工具。

    提供针对 AI 处理优化的干净 Markdown 提取。
    """

    name: str = "crawl4ai"
    description: str = """从网页提取干净、适合 AI 的内容的网页爬虫。

    功能：
    - 提取针对 LLM 优化的干净 Markdown 内容
    - 处理 JavaScript 密集型网站和动态内容
    - 支持在单个请求中处理多个 URL
    - 快速可靠，内置错误处理

    非常适合内容分析、研究和将网页内容提供给 AI 模型。"""

    parameters: dict = {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "（必需）要爬取的 URL 列表。可以是单个 URL 或多个 URL。",
                "minItems": 1,
            },
            "timeout": {
                "type": "integer",
                "description": "（可选）每个 URL 的超时时间（秒）。默认为 30。",
                "default": 30,
                "minimum": 5,
                "maximum": 120,
            },
            "bypass_cache": {
                "type": "boolean",
                "description": "（可选）是否绕过缓存并获取新内容。默认为 false。",
                "default": False,
            },
            "word_count_threshold": {
                "type": "integer",
                "description": "（可选）内容块的最小字数。默认为 10。",
                "default": 10,
                "minimum": 1,
            },
        },
        "required": ["urls"],
    }

    async def execute(
        self,
        urls: Union[str, List[str]],
        timeout: int = 30,
        bypass_cache: bool = False,
        word_count_threshold: int = 10,
    ) -> ToolResult:
        """
        执行指定 URL 的网页爬取。

        Args:
            urls: 要爬取的单个 URL 字符串或 URL 列表
            timeout: 每个 URL 的超时时间（秒）
            bypass_cache: 是否绕过缓存
            word_count_threshold: 内容块的最小字数

        Returns:
            包含爬取结果的 ToolResult
        """
        # 将 URL 规范化为列表
        if isinstance(urls, str):
            url_list = [urls]
        else:
            url_list = urls

        # 验证 URL
        valid_urls = []
        for url in url_list:
            if self._is_valid_url(url):
                valid_urls.append(url)
            else:
                logger.warning(f"Invalid URL skipped: {url}")

        if not valid_urls:
            return ToolResult(error="No valid URLs provided")

        try:
            # 导入 crawl4ai 组件
            from crawl4ai import (
                AsyncWebCrawler,
                BrowserConfig,
                CacheMode,
                CrawlerRunConfig,
            )

            # 配置浏览器设置
            browser_config = BrowserConfig(
                headless=True,
                verbose=False,
                browser_type="chromium",
                ignore_https_errors=True,
                java_script_enabled=True,
            )

            # 配置爬虫设置
            run_config = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS if bypass_cache else CacheMode.ENABLED,
                word_count_threshold=word_count_threshold,
                process_iframes=True,
                remove_overlay_elements=True,
                excluded_tags=["script", "style"],
                page_timeout=timeout * 1000,  # 转换为毫秒
                verbose=False,
                wait_until="domcontentloaded",
            )

            results = []
            successful_count = 0
            failed_count = 0

            # 处理每个 URL
            async with AsyncWebCrawler(config=browser_config) as crawler:
                for url in valid_urls:
                    try:
                        logger.info(f"🕷️ Crawling URL: {url}")
                        start_time = asyncio.get_event_loop().time()

                        result = await crawler.arun(url=url, config=run_config)

                        end_time = asyncio.get_event_loop().time()
                        execution_time = end_time - start_time

                        if result.success:
                            # 统计 Markdown 中的字数
                            word_count = 0
                            if hasattr(result, "markdown") and result.markdown:
                                word_count = len(result.markdown.split())

                            # 统计链接数
                            links_count = 0
                            if hasattr(result, "links") and result.links:
                                internal_links = result.links.get("internal", [])
                                external_links = result.links.get("external", [])
                                links_count = len(internal_links) + len(external_links)

                            # 统计图片数
                            images_count = 0
                            if hasattr(result, "media") and result.media:
                                images = result.media.get("images", [])
                                images_count = len(images)

                            results.append(
                                {
                                    "url": url,
                                    "success": True,
                                    "status_code": getattr(result, "status_code", 200),
                                    "title": result.metadata.get("title")
                                    if result.metadata
                                    else None,
                                    "markdown": result.markdown
                                    if hasattr(result, "markdown")
                                    else None,
                                    "word_count": word_count,
                                    "links_count": links_count,
                                    "images_count": images_count,
                                    "execution_time": execution_time,
                                }
                            )
                            successful_count += 1
                            logger.info(
                                f"✅ Successfully crawled {url} in {execution_time:.2f}s"
                            )

                        else:
                            results.append(
                                {
                                    "url": url,
                                    "success": False,
                                    "error_message": getattr(
                                        result, "error_message", "Unknown error"
                                    ),
                                    "execution_time": execution_time,
                                }
                            )
                            failed_count += 1
                            logger.warning(f"❌ Failed to crawl {url}")

                    except Exception as e:
                        error_msg = f"Error crawling {url}: {str(e)}"
                        logger.error(error_msg)
                        results.append(
                            {"url": url, "success": False, "error_message": error_msg}
                        )
                        failed_count += 1

            # 格式化输出
            output_lines = [f"🕷️ Crawl4AI Results Summary:"]
            output_lines.append(f"📊 Total URLs: {len(valid_urls)}")
            output_lines.append(f"✅ Successful: {successful_count}")
            output_lines.append(f"❌ Failed: {failed_count}")
            output_lines.append("")

            for i, result in enumerate(results, 1):
                output_lines.append(f"{i}. {result['url']}")

                if result["success"]:
                    output_lines.append(
                        f"   ✅ Status: Success (HTTP {result.get('status_code', 'N/A')})"
                    )
                    if result.get("title"):
                        output_lines.append(f"   📄 Title: {result['title']}")

                    if result.get("markdown"):
                        # 显示 Markdown 内容的前 300 个字符
                        content_preview = result["markdown"]
                        if len(result["markdown"]) > 300:
                            content_preview += "..."
                        output_lines.append(f"   📝 Content: {content_preview}")

                    output_lines.append(
                        f"   📊 Stats: {result.get('word_count', 0)} words, {result.get('links_count', 0)} links, {result.get('images_count', 0)} images"
                    )

                    if result.get("execution_time"):
                        output_lines.append(
                            f"   ⏱️ Time: {result['execution_time']:.2f}s"
                        )
                else:
                    output_lines.append(f"   ❌ Status: Failed")
                    if result.get("error_message"):
                        output_lines.append(f"   🚫 Error: {result['error_message']}")

                output_lines.append("")

            return ToolResult(output="\n".join(output_lines))

        except ImportError:
            error_msg = "Crawl4AI is not installed. Please install it with: pip install crawl4ai"
            logger.error(error_msg)
            return ToolResult(error=error_msg)
        except Exception as e:
            error_msg = f"Crawl4AI execution failed: {str(e)}"
            logger.error(error_msg)
            return ToolResult(error=error_msg)

    def _is_valid_url(self, url: str) -> bool:
        """验证 URL 格式是否正确。"""
        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc]) and result.scheme in [
                "http",
                "https",
            ]
        except Exception:
            return False
