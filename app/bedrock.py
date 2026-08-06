import json
import sys
import time
import uuid
from datetime import datetime
from typing import Dict, List, Literal, Optional

import boto3


# 全局变量，用于在函数调用之间跟踪当前工具使用 ID
# 临时解决方案
CURRENT_TOOLUSE_ID = None


# 用于处理 OpenAI 风格响应格式的类
class OpenAIResponse:
    def __init__(self, data):
        # 递归地将嵌套的字典和列表转换为 OpenAIResponse 对象
        for key, value in data.items():
            if isinstance(value, dict):
                value = OpenAIResponse(value)
            elif isinstance(value, list):
                value = [
                    OpenAIResponse(item) if isinstance(item, dict) else item
                    for item in value
                ]
            setattr(self, key, value)

    def model_dump(self, *args, **kwargs):
        # 将对象转换为字典并添加时间戳
        data = self.__dict__
        data["created_at"] = datetime.now().isoformat()
        return data


# 用于与 Amazon Bedrock 交互的主客户端类
class BedrockClient:
    def __init__(self):
        # 初始化 Bedrock 客户端，你需要先配置 AWS 环境
        try:
            self.client = boto3.client("bedrock-runtime")
            self.chat = Chat(self.client)
        except Exception as e:
            print(f"Error initializing Bedrock client: {e}")
            sys.exit(1)


# 聊天接口类
class Chat:
    def __init__(self, client):
        self.completions = ChatCompletions(client)


# 处理聊天完成功能的核心类
class ChatCompletions:
    def __init__(self, client):
        self.client = client

    def _convert_openai_tools_to_bedrock_format(self, tools):
        # 将 OpenAI 函数调用格式转换为 Bedrock 工具格式
        bedrock_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                function = tool.get("function", {})
                bedrock_tool = {
                    "toolSpec": {
                        "name": function.get("name", ""),
                        "description": function.get("description", ""),
                        "inputSchema": {
                            "json": {
                                "type": "object",
                                "properties": function.get("parameters", {}).get(
                                    "properties", {}
                                ),
                                "required": function.get("parameters", {}).get(
                                    "required", []
                                ),
                            }
                        },
                    }
                }
                bedrock_tools.append(bedrock_tool)
        return bedrock_tools

    def _convert_openai_messages_to_bedrock_format(self, messages):
        # 将 OpenAI 消息格式转换为 Bedrock 消息格式
        bedrock_messages = []
        system_prompt = []
        for message in messages:
            if message.get("role") == "system":
                system_prompt = [{"text": message.get("content")}]
            elif message.get("role") == "user":
                bedrock_message = {
                    "role": message.get("role", "user"),
                    "content": [{"text": message.get("content")}],
                }
                bedrock_messages.append(bedrock_message)
            elif message.get("role") == "assistant":
                bedrock_message = {
                    "role": "assistant",
                    "content": [{"text": message.get("content")}],
                }
                openai_tool_calls = message.get("tool_calls", [])
                if openai_tool_calls:
                    bedrock_tool_use = {
                        "toolUseId": openai_tool_calls[0]["id"],
                        "name": openai_tool_calls[0]["function"]["name"],
                        "input": json.loads(
                            openai_tool_calls[0]["function"]["arguments"]
                        ),
                    }
                    bedrock_message["content"].append({"toolUse": bedrock_tool_use})
                    global CURRENT_TOOLUSE_ID
                    CURRENT_TOOLUSE_ID = openai_tool_calls[0]["id"]
                bedrock_messages.append(bedrock_message)
            elif message.get("role") == "tool":
                bedrock_message = {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": CURRENT_TOOLUSE_ID,
                                "content": [{"text": message.get("content")}],
                            }
                        }
                    ],
                }
                bedrock_messages.append(bedrock_message)
            else:
                raise ValueError(f"Invalid role: {message.get('role')}")
        return system_prompt, bedrock_messages

    def _convert_bedrock_response_to_openai_format(self, bedrock_response):
        # 将 Bedrock 响应格式转换为 OpenAI 格式
        content = ""
        if bedrock_response.get("output", {}).get("message", {}).get("content"):
            content_array = bedrock_response["output"]["message"]["content"]
            content = "".join(item.get("text", "") for item in content_array)
        if content == "":
            content = "."

        # 处理响应中的工具调用
        openai_tool_calls = []
        if bedrock_response.get("output", {}).get("message", {}).get("content"):
            for content_item in bedrock_response["output"]["message"]["content"]:
                if content_item.get("toolUse"):
                    bedrock_tool_use = content_item["toolUse"]
                    global CURRENT_TOOLUSE_ID
                    CURRENT_TOOLUSE_ID = bedrock_tool_use["toolUseId"]
                    openai_tool_call = {
                        "id": CURRENT_TOOLUSE_ID,
                        "type": "function",
                        "function": {
                            "name": bedrock_tool_use["name"],
                            "arguments": json.dumps(bedrock_tool_use["input"]),
                        },
                    }
                    openai_tool_calls.append(openai_tool_call)

        # 构建最终的 OpenAI 格式响应
        openai_format = {
            "id": f"chatcmpl-{uuid.uuid4()}",
            "created": int(time.time()),
            "object": "chat.completion",
            "system_fingerprint": None,
            "choices": [
                {
                    "finish_reason": bedrock_response.get("stopReason", "end_turn"),
                    "index": 0,
                    "message": {
                        "content": content,
                        "role": bedrock_response.get("output", {})
                        .get("message", {})
                        .get("role", "assistant"),
                        "tool_calls": openai_tool_calls
                        if openai_tool_calls != []
                        else None,
                        "function_call": None,
                    },
                }
            ],
            "usage": {
                "completion_tokens": bedrock_response.get("usage", {}).get(
                    "outputTokens", 0
                ),
                "prompt_tokens": bedrock_response.get("usage", {}).get(
                    "inputTokens", 0
                ),
                "total_tokens": bedrock_response.get("usage", {}).get("totalTokens", 0),
            },
        }
        return OpenAIResponse(openai_format)

    async def _invoke_bedrock(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
        tools: Optional[List[dict]] = None,
        tool_choice: Literal["none", "auto", "required"] = "auto",
        **kwargs,
    ) -> OpenAIResponse:
        # Bedrock 模型的非流式调用
        (
            system_prompt,
            bedrock_messages,
        ) = self._convert_openai_messages_to_bedrock_format(messages)
        response = self.client.converse(
            modelId=model,
            system=system_prompt,
            messages=bedrock_messages,
            inferenceConfig={"temperature": temperature, "maxTokens": max_tokens},
            toolConfig={"tools": tools} if tools else None,
        )
        openai_response = self._convert_bedrock_response_to_openai_format(response)
        return openai_response

    async def _invoke_bedrock_stream(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
        tools: Optional[List[dict]] = None,
        tool_choice: Literal["none", "auto", "required"] = "auto",
        **kwargs,
    ) -> OpenAIResponse:
        # Bedrock 模型的流式调用
        (
            system_prompt,
            bedrock_messages,
        ) = self._convert_openai_messages_to_bedrock_format(messages)
        response = self.client.converse_stream(
            modelId=model,
            system=system_prompt,
            messages=bedrock_messages,
            inferenceConfig={"temperature": temperature, "maxTokens": max_tokens},
            toolConfig={"tools": tools} if tools else None,
        )

        # 初始化响应结构
        bedrock_response = {
            "output": {"message": {"role": "", "content": []}},
            "stopReason": "",
            "usage": {},
            "metrics": {},
        }
        bedrock_response_text = ""
        bedrock_response_tool_input = ""

        # 处理流式响应
        stream = response.get("stream")
        if stream:
            for event in stream:
                if event.get("messageStart", {}).get("role"):
                    bedrock_response["output"]["message"]["role"] = event[
                        "messageStart"
                    ]["role"]
                if event.get("contentBlockDelta", {}).get("delta", {}).get("text"):
                    bedrock_response_text += event["contentBlockDelta"]["delta"]["text"]
                    print(
                        event["contentBlockDelta"]["delta"]["text"], end="", flush=True
                    )
                if event.get("contentBlockStop", {}).get("contentBlockIndex") == 0:
                    bedrock_response["output"]["message"]["content"].append(
                        {"text": bedrock_response_text}
                    )
                if event.get("contentBlockStart", {}).get("start", {}).get("toolUse"):
                    bedrock_tool_use = event["contentBlockStart"]["start"]["toolUse"]
                    tool_use = {
                        "toolUseId": bedrock_tool_use["toolUseId"],
                        "name": bedrock_tool_use["name"],
                    }
                    bedrock_response["output"]["message"]["content"].append(
                        {"toolUse": tool_use}
                    )
                    global CURRENT_TOOLUSE_ID
                    CURRENT_TOOLUSE_ID = bedrock_tool_use["toolUseId"]
                if event.get("contentBlockDelta", {}).get("delta", {}).get("toolUse"):
                    bedrock_response_tool_input += event["contentBlockDelta"]["delta"][
                        "toolUse"
                    ]["input"]
                    print(
                        event["contentBlockDelta"]["delta"]["toolUse"]["input"],
                        end="",
                        flush=True,
                    )
                if event.get("contentBlockStop", {}).get("contentBlockIndex") == 1:
                    bedrock_response["output"]["message"]["content"][1]["toolUse"][
                        "input"
                    ] = json.loads(bedrock_response_tool_input)
        print()
        openai_response = self._convert_bedrock_response_to_openai_format(
            bedrock_response
        )
        return openai_response

    def create(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
        stream: Optional[bool] = True,
        tools: Optional[List[dict]] = None,
        tool_choice: Literal["none", "auto", "required"] = "auto",
        **kwargs,
    ) -> OpenAIResponse:
        # 聊天完成的主入口点
        bedrock_tools = []
        if tools is not None:
            bedrock_tools = self._convert_openai_tools_to_bedrock_format(tools)
        if stream:
            return self._invoke_bedrock_stream(
                model,
                messages,
                max_tokens,
                temperature,
                bedrock_tools,
                tool_choice,
                **kwargs,
            )
        else:
            return self._invoke_bedrock(
                model,
                messages,
                max_tokens,
                temperature,
                bedrock_tools,
                tool_choice,
                **kwargs,
            )
