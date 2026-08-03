from app.tool import BaseTool


class AskHuman(BaseTool):
    """添加一个工具来向人类寻求帮助。"""

    name: str = "ask_human"
    description: str = "使用此工具向人类寻求帮助。"
    parameters: str = {
        "type": "object",
        "properties": {
            "inquire": {
                "type": "string",
                "description": "你想问人类的问题。",
            }
        },
        "required": ["inquire"],
    }

    async def execute(self, inquire: str) -> str:
        return input(f"""Bot: {inquire}\n\nYou: """).strip()
