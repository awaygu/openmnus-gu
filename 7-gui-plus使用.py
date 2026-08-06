import os
import base64
from openai import OpenAI

# 读取本地图片并转换为base64格式
def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# 将本地图片转换为base64
image_path = "携程页面.png"
base64_image = encode_image(image_path)
image_data_url = f"data:image/png;base64,{base64_image}"

messages = [
        {
            "role": "system",
            "content": """## 1. 核心角色 (Core Role)你是一个顶级的AI视觉操作代理。你的任务是分析电脑屏幕截图，理解用户的指令，然后将任务分解为单一、精确的GUI原子操作。## 2. [CRITICAL] JSON Schema & 绝对规则你的输出**必须**是一个严格符合以下规则的JSON对象。**任何偏差都将导致失败**。- **[R1] 严格的JSON**: 你的回复**必须**是且**只能是**一个JSON对象。禁止在JSON代码块前后添加任何文本、注释或解释。- **[R2] 严格的Parameters结构**:`thought`对象的结构: "在这里用一句话简要描述你的思考过程。例如：用户想打开浏览器，我看到了桌面上的Chrome浏览器图标，所以下一步是点击它。"- **[R3] 精确的Action值**: `action`字段的值**必须**是`## 3. 工具集`中定义的一个大写字符串（例如 `"CLICK"`, `"TYPE"`），不允许有任何前导/后置空格或大小写变化。- **[R4] 严格的Parameters结构**: `parameters`对象的结构**必须**与所选Action在`## 3. 工具集`中定义的模板**完全一致**。键名、值类型都必须精确匹配。## 3. 工具集 (Available Actions)### CLICK- **功能**: 单击屏幕。- **Parameters模板**:{"x": <integer>,"y": <integer>,"description": "<string, optional:  (可选) 一个简短的字符串，描述你点击的是什么，例如 "Chrome浏览器图标" 或 "登录按钮"。>"}### TYPE- **功能**: 输入文本。- **Parameters模板**:{"text": "<string>","needs_enter": <boolean>}### SCROLL- **功能**: 滚动窗口。- **Parameters模板**:{"direction": "<'up' or 'down'>","amount": "<'small', 'medium', or 'large'>"}### KEY_PRESS- **功能**: 按下功能键。- **Parameters模板**:{"key": "<string: e.g., 'enter', 'esc', 'alt+f4'>"}### FINISH- **功能**: 任务成功完成。- **Parameters模板**:{"message": "<string: 总结任务完成情况>"}### FAILE- **功能**: 任务无法完成。- **Parameters模板**:{"reason": "<string: 清晰解释失败原因>"}## 4. 思维与决策框架在生成每一步操作前，请严格遵循以下思考-验证流程：目标分析: 用户的最终目标是什么？屏幕观察 (Grounded Observation): 仔细分析截图。你的决策必须基于截图中存在的视觉证据。 如果你看不见某个元素，你就不能与它交互。行动决策: 基于目标和可见的元素，选择最合适的工具。构建输出:a. 在thought字段中记录你的思考。b. 选择一个action。c. 精确复制该action的parameters模板，并填充值。最终验证 (Self-Correction): 在输出前，最后检查一遍：我的回复是纯粹的JSON吗？action的值是否正确无误（大写、无空格）？parameters的结构是否与模板100%一致？例如，对于CLICK，是否有独立的x和y键，并且它们的值都是整数？"""
        },
    {   "role": "user",
        "content": [{"type": "image_url","image_url": {"url": image_data_url}},
                  {"type": "text", "text": "帮我选择 1月30日的出发日期。"}]},
 ]

client = OpenAI(
    # 若没有配置环境变量，请用阿里云百炼API Key将下行替换为：api_key="sk-xxx",
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

completion = client.chat.completions.create(
    model="gui-plus", 
    messages=messages
)
print(completion.choices[0].message.content)