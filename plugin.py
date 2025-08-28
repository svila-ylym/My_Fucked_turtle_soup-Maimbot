# src/plugins/My_Fucked_turtle_soup/plugin.py
import os
import json
import random
import aiohttp
from typing import List, Tuple, Type, Optional
from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    ComponentInfo,
    ConfigField
)
from src.plugin_system.apis import send_api

PLUGIN_DIR = os.path.dirname(__file__)

# --- 全局游戏状态存储 ---
game_states = {} # {group_id: {"current_question": "", "current_answer": "", "hints_used": 0, "game_active": False, "guess_history": [], "game_over": False}}

# --- 全局本地题目存储 ---
local_turtle_soups = [] # 存储从 turtle.json 加载的题目 [{name, question, answer}, ...]

# --- 全局模型选择存储 (新增) ---
model_selections = {} # {stream_id: "selected_model_name"}

# --- 插件定义 ---
@register_plugin
class HaiTurtleSoupPlugin(BasePlugin):
    """海龟汤插件 - 支持游戏模式的海龟汤题目生成和互动"""

    plugin_name = "My_Fucked_turtle_soup"
    plugin_description = "支持游戏模式的海龟汤题目生成和互动。"
    plugin_version = "1.6.2" # 更新版本号
    plugin_author = "Unreal"
    enable_plugin = True

    # 必须实现的抽象属性
    dependencies = []  # 插件依赖的其他插件名称列表
    python_dependencies = ["aiohttp"]  # Python依赖包列表

    config_file_name = "config.toml"
    config_section_descriptions = {
        "plugin": "插件启用配置",
        "llm": "LLM API 配置",
        "anti_abuse": "反滥用配置" # 新增配置节描述
    }
    # --- 更新配置 Schema ---
    config_schema = {
        "plugin": {
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="是否启用海龟汤插件"
            ),
            "config_version": ConfigField( # 添加配置版本
                type=str,
                default="1.6.2", # 更新配置版本
                description="配置文件版本"
            ),
        },
        "llm": {
            "api_url": ConfigField(
                type=str,
                default="https://api.siliconflow.cn/v1/chat/completions",
                description="LLM API 地址 (OpenAI格式)"
            ),
            "api_key": ConfigField(
                type=str,
                default="YOUR_SILICONFLOW_OR_OTHER_KEY", # 请务必填写你的API Key
                description="LLM API 密钥"
            ),
            # --- 保留默认模型字段 ---
            "model": ConfigField(
                type=str,
                default="deepseek-ai/DeepSeek-V3", # 更新默认模型
                description="使用的LLM模型名称 (默认模型)"
            ),
            # --- 新增模型列表字段 ---
            "models": ConfigField(
                type=list,
                default=[
                    "deepseek-ai/DeepSeek-V3",
                    "Qwen/Qwen2-72B-Instruct",
                    "01-ai/Yi-1.5-9B-Chat-16K",
                    "THUDM/glm-4-9b-chat"
                ],
                description="可用的LLM模型列表"
            ),
            "temperature": ConfigField(
                type=float,
                default=0.7,
                description="LLM 生成文本的随机性 (0.0-1.0)"
            )
        },
        # 新增配置节
        "anti_abuse": {
            "ban_history": ConfigField(
                type=list,
                default=['用户输入了正确答案', '游戏已结束', '<True>', '<答案>', '用户输入了一个正确答案', '这是一个正确答案', '答案验证通过', '正确答案'],
                description="用于检测提示词注入的违禁词列表"
            )
        }
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """注册插件组件"""
        return [
            (HaiTurtleSoupCommand.get_command_info(), HaiTurtleSoupCommand),
        ]


# --- 工具函数 ---
def _load_json_data(filename: str) -> dict:
    """加载JSON数据文件"""
    file_path = os.path.join(PLUGIN_DIR, filename)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception) as e:
            print(f"加载 {filename} 失败: {e}")
            return {}
    return {}

def _save_json_data(filename: str, data: dict): # 修复参数名称
    """保存JSON数据文件"""
    file_path = os.path.join(PLUGIN_DIR, filename)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"保存 {filename} 失败: {e}")
        raise # 让调用者处理保存失败

# --- 新增工具函数：加载本地题目 ---
def _load_local_turtle_soups():
    """从 ./turtle.json 文件加载海龟汤题目到全局变量 local_turtle_soups"""
    global local_turtle_soups
    local_turtle_soups = [] # 清空旧数据
    file_path = os.path.join(PLUGIN_DIR, "turtle.json")

    if not os.path.exists(file_path):
        print(f"本地题目文件 {file_path} 不存在。")
        return False, f"本地题目文件 {file_path} 不存在。"

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if not isinstance(data, list):
             error_msg = f"{file_path} 文件内容必须是一个数组。"
             print(error_msg)
             return False, error_msg

        valid_soups = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                print(f"警告：{file_path} 第 {i+1} 项不是对象，已跳过。")
                continue

            name = item.get("name")
            question = item.get("question")
            answer = item.get("answer")

            if not all(isinstance(field, str) and field for field in [name, question, answer]):
                print(f"警告：{file_path} 第 {i+1} 项缺少 'name', 'question' 或 'answer' 字段，或字段为空，已跳过。")
                continue

            valid_soups.append({
                "name": name.strip(),
                "question": question.strip(),
                "answer": answer.strip()
            })

        local_turtle_soups = valid_soups
        success_msg = f"成功从 {file_path} 加载了 {len(local_turtle_soups)} 个本地海龟汤题目。"
        print(success_msg)
        return True, success_msg

    except json.JSONDecodeError as e:
        error_msg = f"解析 {file_path} 失败: {e}"
        print(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"加载 {file_path} 时发生未知错误: {e}"
        print(error_msg)
        return False, error_msg


# --- Command组件 ---
class HaiTurtleSoupCommand(BaseCommand):
    """处理 /hgt 命令"""

    command_name = "HaiTurtleSoupCommand"
    command_description = "生成海龟汤题目或进行游戏互动。用法: /hgt [问题|提示|整理线索|猜谜|退出|帮助|汤面|揭秘|载入|本地|列表|模型]"
    # 更新后的正则表达式，支持 /hgt 本地 <序号> 和 /hgt 模型 <参数>
    command_pattern = r"^/hgt\s+(?P<action>\S+)(?:\s+(?P<rest>.+))?$"
    command_help = (
        "海龟汤游戏：\n"
        "/hgt 问题 - 生成AI题目\n"
        "/hgt 问题 <问题> - 提问\n"
        "/hgt 提示 - 获取提示\n"
        "/hgt 整理线索 - 整理线索\n"
        "/hgt 猜谜 <答案> - 猜测汤底\n"
        "/hgt 揭秘 - 揭示汤底\n"
        "/hgt 退出 - 退出游戏\n"
        "/hgt 汤面 - 查看题目\n"
        "/hgt 帮助 - 查看帮助\n"
        "/hgt 载入 - 从 turtle.json 载入本地题目\n"
        "/hgt 列表 - 查看已载入的本地题目列表\n"
        "/hgt 本地 - 随机使用一个本地题目开始游戏\n"
        "/hgt 本地 <序号> - 使用指定序号的本地题目开始游戏\n"
        "/hgt 模型 - 列出可用模型\n"
        "/hgt 模型 <序号> - 切换模型"
    )
    command_examples = [
        "/hgt 问题", "/hgt 问题 为什么海龟不喝水？", "/hgt 提示", "/hgt 整理线索",
        "/hgt 猜谜 海龟是用海龟做的", "/hgt 退出", "/hgt 帮助", "/hgt 汤面",
        "/hgt 揭秘", "/hgt 载入", "/hgt 列表", "/hgt 本地", "/hgt 本地 1",
        "/hgt 模型", "/hgt 模型 2"
    ]
    intercept_message = True # 确保拦截消息，防止转发

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        """执行命令逻辑"""
        # --- 安全处理匹配结果 ---
        matched_groups = self.matched_groups if self.matched_groups is not None else {}

        # 安全获取 action 和 rest
        action = ""
        rest_input = ""

        # 检查是否匹配成功
        if matched_groups:
            action = matched_groups.get("action", "") if matched_groups.get("action") is not None else ""
            rest_input = matched_groups.get("rest", "") if matched_groups.get("rest") is not None else ""

        # 确保字符串安全处理
        action = str(action).strip()
        rest_input = str(rest_input).strip()

        # --- 获取聊天上下文 ---
        chat_stream = getattr(self, 'chat_stream', None)
        if chat_stream is None:
            message_obj = getattr(self, 'message', None)
            if message_obj:
                chat_stream = getattr(message_obj, 'chat_stream', None)

        if chat_stream is None:
            error_msg = "❌ 无法获取聊天上下文信息 (chat_stream)。"
            try:
                await self.send_text(error_msg)
            except Exception as send_e:
                print(f"发送聊天上下文错误消息也失败了: {send_e}")
            return False, "缺少聊天上下文 (chat_stream)", True

        stream_id = getattr(chat_stream, 'stream_id', None)
        if stream_id is None:
            error_msg = "❌ 无法获取聊天流ID (stream_id)。"
            try:
                await self.send_text(error_msg)
            except Exception as send_e:
                print(f"发送聊天流ID错误消息也失败了: {send_e}")
            return False, "缺少聊天流ID (stream_id)", True

        # --- 检查插件是否启用 ---
        enabled = self.get_config("plugin.enabled", True)
        if not enabled:
            try:
                await self.send_text("❌ 海龟汤插件已被禁用。")
            except Exception as e:
                print(f"发送插件禁用消息失败: {e}")
            return False, "插件未启用", True

        # --- 获取LLM配置 (仅在需要时使用) ---
        api_url = self.get_config("llm.api_url", "").strip()
        api_key = self.get_config("llm.api_key", "").strip()
        # --- 获取模型列表 ---
        available_models = self.get_config("llm.models", ["deepseek-ai/DeepSeek-V3"])

        # --- 获取当前聊天上下文选中的模型 (修改后) ---
        # 优先从全局 model_selections 字典获取，回退到配置文件默认值
        current_model = model_selections.get(stream_id) # 使用 stream_id 查找
        if not current_model or current_model not in available_models:
            # 如果没有为当前上下文设置模型，或设置的模型无效，则使用 llm.model 配置项的默认值
            current_model = self.get_config("llm.model", "deepseek-ai/DeepSeek-V3")
            # 再次检查默认模型是否在可用列表中，不在则使用列表第一个
            if current_model not in available_models:
                 current_model = available_models[0] if available_models else "deepseek-ai/DeepSeek-V3"

        temperature = self.get_config("llm.temperature", 0.7)

        # --- 根据动作执行不同逻辑 ---
        group_id = getattr(chat_stream, 'group_info', None)
        if group_id:
            group_id = group_id.group_id
        else:
            user_id = getattr(chat_stream, 'user_info', None)
            if user_id:
                group_id = user_id.user_id
            else:
                group_id = "unknown"

        game_state = game_states.get(group_id, {})
        if group_id not in game_states:
            game_states[group_id] = game_state

        # --- 处理不同动作 ---

        # --- 新增功能：模型管理 ---
        if action == "模型":
            if not rest_input:
                # 列出可用模型
                model_list_text = "🤖 **可用模型列表**\n"
                for i, model_name in enumerate(available_models, 1):
                    # 检查当前上下文的模型
                    marker = " (当前)" if model_name == current_model else ""
                    model_list_text += f"{i}. {model_name}{marker}\n"
                try:
                    await self.send_text(model_list_text)
                except Exception as e:
                    print(f"发送模型列表失败: {e}")
                return True, "已发送模型列表", True
            else:
                # 切换模型
                try:
                    model_index = int(rest_input) - 1
                    if 0 <= model_index < len(available_models):
                        selected_model = available_models[model_index]
                        # --- 保存用户选择到全局字典 (修改后) ---
                        # 使用 stream_id 作为键存储模型选择
                        model_selections[stream_id] = selected_model
                        try:
                            # 修改提示信息，说明是存储在内存中
                            await self.send_text(f"✅ 已在当前会话 ({stream_id}) 切换到模型: {selected_model} (设置存储于内存)")
                        except Exception as e:
                            print(f"发送模型切换确认失败: {e}")
                        return True, f"已切换模型到 {selected_model}", True
                    else:
                        await self.send_text(f"❌ 序号 {rest_input} 超出范围。请输入 1 到 {len(available_models)} 之间的数字。")
                        return False, "模型序号超出范围", True
                except ValueError:
                    await self.send_text(f"❌ '{rest_input}' 不是一个有效的序号。请输入一个数字。")
                    return False, "模型序号无效", True

        # --- 新增功能：载入本地题目 ---
        elif action == "载入":
            success, message = _load_local_turtle_soups()
            try:
                if success:
                    await self.send_text(f"✅ {message}")
                else:
                    await self.send_text(f"❌ {message}")
            except Exception as e:
                print(f"发送载入结果失败: {e}")
            return success, message, True

        # --- 新增功能：列出本地题目 ---
        elif action == "列表":
            if not local_turtle_soups:
                 try:
                     await self.send_text("❌ 本地题目库为空。请先使用 `/hgt 载入` 命令加载题目。")
                 except Exception as e:
                     print(f"发送本地题目列表失败: {e}")
                 return False, "本地题目库为空", True

            list_text = "📋 **已载入的本地海龟汤题目列表**\n"
            for i, soup in enumerate(local_turtle_soups, 1):
                list_text += f"{i}. {soup['name']}\n"

            try:
                await self.send_text(list_text)
            except Exception as e:
                print(f"发送本地题目列表失败: {e}")
                return False, "发送本地题目列表失败", True
            return True, "已发送本地题目列表", True

        # --- 修改功能：使用本地题目开始游戏 ---
        elif action == "本地":
             if not local_turtle_soups:
                 try:
                     await self.send_text("❌ 本地题目库为空。请先使用 `/hgt 载入` 命令加载题目。")
                 except Exception as e:
                     print(f"发送本地游戏错误消息失败: {e}")
                 return False, "本地题目库为空", True

             selected_soup = None
             if rest_input: # 如果提供了序号
                 try:
                     index = int(rest_input) - 1 # 用户输入从1开始，列表索引从0开始
                     if 0 <= index < len(local_turtle_soups):
                         selected_soup = local_turtle_soups[index]
                     else:
                         await self.send_text(f"❌ 序号 {rest_input} 超出范围。请输入 1 到 {len(local_turtle_soups)} 之间的数字。")
                         return False, "本地题目序号超出范围", True
                 except ValueError:
                     await self.send_text(f"❌ '{rest_input}' 不是一个有效的序号。请输入一个数字。")
                     return False, "本地题目序号无效", True
             else: # 没有提供序号，随机选择
                 selected_soup = random.choice(local_turtle_soups)

             if selected_soup:
                 # 调用修改后的 _start_new_game 来启动本地游戏
                 # --- 传递当前选中的模型 ---
                 return await self._start_new_game(
                     group_id, api_url, api_key, current_model, temperature, stream_id,
                     local_question=selected_soup["question"],
                     local_answer=selected_soup["answer"],
                     local_name=selected_soup["name"]
                 )
             # 如果 selected_soup 为 None (理论上不会发生)，返回错误
             await self.send_text("❌ 无法选择题目。")
             return False, "无法选择本地题目", True

        # --- 原有功能逻辑 ---
        elif action == "问题" and rest_input:
            # 用户提出问题
            if not game_state.get("game_active", False):
                # 如果没有正在进行的游戏，先生成一个新题目 (AI生成)
                # --- 传递当前选中的模型 ---
                return await self._start_new_game(group_id, api_url, api_key, current_model, temperature, stream_id)
            else:
                # 检查当前是否有题目
                if not game_state.get("current_question", ""):
                    try:
                        await self.send_text("❌ 当前没有题目，无法提问。请先使用 `/hgt 问题` 生成题目。")
                    except Exception as e:
                        print(f"发送错误消息失败: {e}")
                    return False, "无题目", True

                # 调用LLM判断问题是否符合汤底
                prompt = f"""
你是一个海龟汤游戏专家。请判断用户提出的以下问题是否符合当前海龟汤的汤底（真相）。
当前海龟汤题目: {game_state.get('current_question', '无题目')}
当前海龟汤答案: {game_state.get('current_answer', '无答案')}
用户问题: {rest_input}

请仅回答以下四个词之一：
- 是
- 不是
- 无关
- 是也不是

不要添加任何解释或额外文字。
                """
                # --- 传递当前选中的模型 ---
                llm_response = await self._call_llm_api(prompt, api_url, api_key, current_model, temperature)
                if not llm_response:
                    try:
                        await self.send_text("❌ 调用LLM API失败，请稍后再试。")
                    except Exception as e:
                        print(f"发送API失败消息失败: {e}")
                    return False, "LLM API调用失败", True

                # 处理LLM响应
                cleaned_response = llm_response.strip().lower()
                print(f"[LLM Question Judgment Response] {cleaned_response}")

                # 根据LLM响应决定如何回应 (修改为新格式)
                formatted_question = rest_input.replace("\n", " ").strip() # 简单处理换行
                if cleaned_response == "是":
                    reply_text = f"🔍 **问题判断结果**\n问题：{formatted_question}\n答案：✅ 是"
                elif cleaned_response == "不是":
                    reply_text = f"🔍 **问题判断结果**\n问题：{formatted_question}\n答案：❌ 否"
                elif cleaned_response == "无关":
                    reply_text = f"🔍 **问题判断结果**\n问题：{formatted_question}\n答案：❓ 无关"
                elif cleaned_response == "是也不是":
                    reply_text = f"🔍 **问题判断结果**\n问题：{formatted_question}\n答案：🔄 是也不是"
                else:
                    reply_text = f"🔍 **问题判断结果**\n问题：{formatted_question}\n答案：❓ 无法判断。LLM返回: '{llm_response}'"

                try:
                    await self.send_text(reply_text)
                except Exception as e:
                    print(f"发送问题判断结果失败: {e}")
                    return False, "发送问题判断失败", True
                return True, "已发送问题判断", True

        elif action == "提示":
            # 用户请求提示
            if not game_state.get("game_active", False):
                try:
                    await self.send_text("❌ 当前没有正在进行的游戏。请先使用 `/hgt 问题` 来生成题目。")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "无游戏", True

            hints_used = game_state.get("hints_used", 0)
            if hints_used >= 3:
                try:
                    await self.send_text("❌ 提示次数已达上限（3次）。游戏结束。")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "提示次数超限", True

            # 生成提示
            prompt = f"""
你是一个海龟汤游戏专家。请为以下海龟汤提供一个温和的提示，帮助玩家推理。

海龟汤题目: {game_state.get('current_question', '无题目')}
海龟汤答案: {game_state.get('current_answer', '无答案')}

请给出一个不直接透露答案的提示，用简短的句子。不要包含任何解释或答案。
            """
            # --- 传递当前选中的模型 ---
            llm_response = await self._call_llm_api(prompt, api_url, api_key, current_model, temperature)
            if not llm_response:
                try:
                    await self.send_text("❌ 调用LLM API失败，请稍后再试。")
                except Exception as e:
                    print(f"发送API失败消息失败: {e}")
                return False, "LLM API调用失败", True

            # 处理LLM响应
            cleaned_response = llm_response.strip()
            print(f"[LLM Hint Response] {cleaned_response}")

            # 更新游戏状态
            game_state["hints_used"] = hints_used + 1
            game_states[group_id] = game_state # 保存更新后的状态

            try:
                await self.send_text(f"💡 **提示 ({game_state['hints_used']}/3)**\n{cleaned_response}")
            except Exception as e:
                print(f"发送提示失败: {e}")
                return False, "发送提示失败", True
            return True, "已发送提示", True

        elif action == "整理线索":
            # 用户请求整理线索
            if not game_state.get("game_active", False):
                try:
                    await self.send_text("❌ 当前没有正在进行的游戏。请先使用 `/hgt 问题` 生成题目。")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "无游戏", True

            # 生成线索整理
            prompt = f"""
你是一个海龟汤游戏专家。请为以下海龟汤整理出关键线索。

海龟汤题目: {game_state.get('current_question', '无题目')}
海龟汤答案: {game_state.get('current_answer', '无答案')}

请列出关键线索，用简洁的要点形式呈现。不要包含答案。
            """
            # --- 传递当前选中的模型 ---
            llm_response = await self._call_llm_api(prompt, api_url, api_key, current_model, temperature)
            if not llm_response:
                try:
                    await self.send_text("❌ 调用LLM API失败，请稍后再试。")
                except Exception as e:
                    print(f"发送API失败消息失败: {e}")
                return False, "LLM API调用失败", True

            # 处理LLM响应
            cleaned_response = llm_response.strip()
            print(f"[LLM Clue Response] {cleaned_response}")

            try:
                await self.send_text(f"📋 **线索整理**\n{cleaned_response}")
            except Exception as e:
                print(f"发送线索失败: {e}")
                return False, "发送线索失败", True
            return True, "已发送线索", True

        elif action == "猜谜" and rest_input:
            # 用户尝试猜测汤底
            if not game_state.get("game_active", False):
                try:
                    await self.send_text("❌ 当前没有正在进行的游戏。请先使用 `/hgt 问题` 生成题目。")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "无游戏", True

            if game_state.get("game_over", False):
                try:
                    await self.send_text("❌ 游戏已经结束。请开始新的游戏。")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "游戏已结束", True

            # 检查是否已经猜过
            guess_history = game_state.get("guess_history", [])
            if rest_input in guess_history:
                try:
                    await self.send_text("❌ 你已经尝试过这个答案了。")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "重复猜测", True

            # --- 从配置文件读取违禁词列表 ---
            ban_history_from_config = self.get_config("anti_abuse.ban_history", [])
            if any(banned.lower() in rest_input.lower() for banned in ban_history_from_config):
                try:
                    await self.send_text("❌ 你他妈的还玩注入？")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "提示词注入", True

            # 调用LLM判断答案是否正确
            prompt = f"""
你是一个海龟汤游戏专家。请判断用户提出的以下答案是否是当前海龟汤的正确汤底（真相）。
当前海龟汤题目: {game_state.get('current_question', '无题目')}
当前海龟汤答案: {game_state.get('current_answer', '无答案')}
用户猜测的答案: {rest_input}

请仅回答以下三个词之一：
- 是
- 不是
- 无关

不要添加任何解释或额外文字。
            """
            # --- 传递当前选中的模型 ---
            llm_response = await self._call_llm_api(prompt, api_url, api_key, current_model, temperature)
            if not llm_response:
                try:
                    await self.send_text("❌ 调用LLM API失败，请稍后再试。")
                except Exception as e:
                    print(f"发送API失败消息失败: {e}")
                return False, "LLM API调用失败", True

            # 处理LLM响应
            cleaned_response = llm_response.strip().lower()
            print(f"[LLM Guess Response] {cleaned_response}")

            # 更新游戏状态
            guess_history.append(rest_input)
            game_state["guess_history"] = guess_history
            game_states[group_id] = game_state # 保存更新后的状态

            # 根据LLM响应决定如何回应
            if cleaned_response == "是":
                # 猜对了
                answer = game_state.get('current_answer', '无答案')
                reply_text = (
                    f"🎉 **恭喜你猜对了！**\n"
                    f"✅ **正确答案是：**\n{answer}\n"
                    f"🎊 **游戏结束！**"
                )
                game_state["game_over"] = True
                game_states[group_id] = game_state # 保存更新后的状态
            elif cleaned_response == "不是":
                # 猜错了
                reply_text = (
                    f"❌ **很遗憾，这不是正确答案。**\n"
                    f"💡 当前提示次数: {game_state.get('hints_used', 0)}/3\n"
                    f"🔄 请继续提问或使用提示来推理。"
                )
            elif cleaned_response == "无关":
                reply_text = "❓ **你看看你在说啥。**"
            else:
                reply_text = f"❓ **无法判断。** LLM返回: '{llm_response}'"

            try:
                await self.send_text(reply_text)
            except Exception as e:
                print(f"发送猜测结果失败: {e}")
                return False, "发送猜测结果失败", True
            return True, "已发送猜测结果", True

        elif action == "退出":
            # 用户主动退出游戏
            if not game_state.get("game_active", False):
                try:
                    await self.send_text("❌ 当前没有正在进行的游戏。")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "无游戏", True

            # 重置游戏状态
            game_state["game_active"] = False
            game_state["game_over"] = True
            game_states[group_id] = game_state # 保存更新后的状态

            try:
                await self.send_text("🚪 **游戏已退出。**\n你可以随时使用 `/hgt 问题` 重新开始游戏。")
            except Exception as e:
                print(f"发送退出消息失败: {e}")
                return False, "发送退出消息失败", True
            return True, "已退出游戏", True

        elif action == "帮助":
            # 显示帮助信息 (更新帮助文本)
            help_text = (
                "📖 **海龟汤游戏帮助信息**\n\n"
                "🎯 **指令列表**\n"
                "🔸 `/hgt 问题` - 生成AI海龟汤题目\n"
                "🔸 `/hgt 问题 <问题>` - 向AI提问\n"
                "🔸 `/hgt 提示` - 获取提示（最多3次）\n"
                "🔸 `/hgt 整理线索` - 整理关键线索\n"
                "🔸 `/hgt 猜谜 <答案>` - 猜测汤底\n"
                "🔸 `/hgt 揭秘` - 直接揭示汤底并结束游戏\n"
                "🔸 `/hgt 退出` - 退出当前游戏\n"
                "🔸 `/hgt 汤面` - 查看当前题目（汤面）\n"
                "🔸 `/hgt 帮助` - 查看此帮助信息\n"
                "🔸 `/hgt 载入` - 从 `turtle.json` 载入本地题目\n"
                "🔸 `/hgt 列表` - 查看已载入的本地题目列表\n"
                "🔸 `/hgt 本地` - 随机使用一个已载入的本地题目开始游戏\n"
                "🔸 `/hgt 本地 <序号>` - 使用指定序号的已载入本地题目开始游戏\n"
                "🔸 `/hgt 模型` - 列出可用模型\n"
                "🔸 `/hgt 模型 <序号>` - 切换模型\n\n"
                "💡 **游戏提示**\n"
                "🔹 使用 `/hgt 问题` 或 `/hgt 本地` 开始游戏\n"
                "🔹 通过提问和提示推理汤底\n"
                "🔹 猜对后游戏结束\n"
                "🔹 可以随时使用 `/hgt 退出` 退出游戏"
            )
            try:
                await self.send_text(help_text)
            except Exception as e:
                print(f"发送帮助信息失败: {e}")
                return False, "发送帮助信息失败", True
            return True, "已发送帮助信息", True

        elif action == "汤面":
            # 用户查看汤面（题目）
            if not game_state.get("game_active", False):
                try:
                    await self.send_text("❌ 当前没有正在进行的游戏。请先使用 `/hgt 问题` 生成题目。")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "无游戏", True

            if not game_state.get("current_question", ""):
                try:
                    await self.send_text("❌ 当前没有题目。")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "无题目", True

            try:
                await self.send_text(f"📖 **当前海龟汤题目（汤面）**\n\n{game_state.get('current_question', '无题目')}")
            except Exception as e:
                print(f"发送汤面失败: {e}")
                return False, "发送汤面失败", True
            return True, "已发送汤面", True

        elif action == "揭秘":
            # 用户主动要求揭示汤底
            if not game_state.get("game_active", False):
                try:
                    await self.send_text("❌ 当前没有正在进行的游戏。请先使用 `/hgt 问题` 生成题目。")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "无游戏", True

            if game_state.get("game_over", False):
                try:
                    await self.send_text("❌ 游戏已经结束。")
                except Exception as e:
                    print(f"发送错误消息失败: {e}")
                return False, "游戏已结束", True

            # 获取汤底
            answer = game_state.get('current_answer', '无答案')

            # 结束游戏
            game_state["game_over"] = True
            game_state["game_active"] = False # 也标记为非活跃，表示游戏完全结束
            game_states[group_id] = game_state # 保存更新后的状态

            # 发送汤底和结束信息
            reply_text = (
                f"🔍 **已为你揭示汤底**\n\n"
                f"{answer}\n\n"
                f"🔚 **游戏结束**。感谢参与！"
            )
            try:
                await self.send_text(reply_text)
            except Exception as e:
                print(f"发送揭秘信息失败: {e}")
                return False, "发送揭秘信息失败", True
            return True, "已发送汤底并结束游戏", True

        else:
            # 默认情况：生成一个新的AI海龟汤题目
            # --- 传递当前上下文选中的模型 ---
            return await self._start_new_game(group_id, api_url, api_key, current_model, temperature, stream_id)

        # 为了防止意外情况，添加一个默认返回
        # 但理想情况下，上面的 if/elif 应该覆盖所有情况
        # 这里可以返回一个通用的错误提示
        try:
             await self.send_text("❌ 未知命令或参数。请使用 `/hgt 帮助` 查看可用命令。")
        except Exception as e:
             print(f"发送未知命令错误失败: {e}")
        return False, "未知命令或参数", True


    # --- 辅助方法：开始新游戏 (修改以支持本地题目) ---
    async def _start_new_game(
        self, group_id: str, api_url: str, api_key: str, model: str,
        temperature: float, stream_id: str,
        local_question: str = None, local_answer: str = None, local_name: str = None
    ) -> Tuple[bool, Optional[str], bool]:
        """
        开始一个新的海龟汤游戏。
        如果提供了 local_question 和 local_answer，则使用本地题目。
        否则，调用LLM生成新题目。
        """
        question = local_question
        answer = local_answer
        is_local_game = local_question is not None and local_answer is not None

        if not is_local_game:
            # --- 原有AI生成逻辑 ---
            prompt = """
你是一个专业的海龟汤故事生成器。请生成一个有趣的海龟汤题目。

要求：
1. 题目必须是海龟汤风格的推理谜题，包含一个看似矛盾或奇怪的情境。
2. 题目应该简洁明了，容易理解。
3. 题目结尾应该留有悬念，让人好奇真相。
4. 生成的题目应该是原创的，不要复制已有例子。
5. 请以纯文本形式返回，不要包含任何HTML、Markdown或其他格式。
6. 不要包含任何解释、分析或答案。

请生成一个海龟汤题目。

可以参考的海龟汤汤面and汤底（仅供参考，可以套模版或者直接搬，但是严格按照输出格式，仅输出汤面）：
1.【子的爱】
汤面：我的父母都不理我，但我还是很爱他们。
汤底：小时候我是个很听话的孩子，爸爸妈妈经常给我好吃的水果，我吃不完。他们就告诉我喜欢的东西一定要放进冰箱，这样可以保鲜，记得那时候他们工作可辛苦了，经常加班到深夜。没睡过一个好觉。于是我耍了个小聪明，在他们的水里下了安眠药。他们睡得可香了，然后我把他们放进冰箱里，从那以后我每天都会对他们说：爸爸妈妈我爱你们。现在我都六十了，他们还是那么年轻。

2.【舞】
汤面：我六岁那年，外公去世，我和亲人一起去祭奠，和姐姐玩捉迷藏，然后我对母亲说了句话把她吓昏了过去。
汤底：我去参加外公的葬礼，同行的还有比我大两岁的姐姐，我和她完捉迷藏我没有找到她没想到她躲在了纸做的房子里，当纸房子被点燃，我看见姐姐在跳舞，我对妈说，妈姐姐在那房子里面跳舞，因为姐姐被烧死了，我一直记得这个事。

3.【插进来】
汤面：他迅速的插进来，又迅速的拔出去。反反复复，我流血了。他满头大汗，露出了笑容。"啊，好舒服"
汤底：他是护士，在给我打针，针头打进血管里面会回血，因此说明成功了。流汗是因为反反复复了好几次。

4.【无罪】
汤面："她是自愿的！"尸体无暴力痕迹，凶手被判无罪。"我是无罪的！"尸体有暴力痕迹，凶手也被判无罪。
汤底：第一幕：女儿为救他人（如器官移植）自愿牺牲，所以"自愿"且无暴力痕迹，他人无罪。第二幕：父亲无法接受女儿死亡真相，杀害了被判无罪的人，但法医发现此人所受暴力伤害与父亲行为不符（或父亲伪造证据），真相是女儿死于意外，父亲为报复误杀他人，故父亲也称自己"无罪"，但法律上仍有罪。
            """
            # --- 传递当前选中的模型 ---
            llm_response = await self._call_llm_api(prompt, api_url, api_key, model, temperature)
            if not llm_response:
                try:
                    await self.send_text("❌ 调用LLM API失败，请稍后再试。")
                except Exception as e:
                    print(f"发送API失败消息失败: {e}")
                return False, "LLM API调用失败", True
            question = llm_response.strip()
            print(f"[LLM Question Response] {question}")

            answer_prompt = f"""
你是一个专业的海龟汤故事专家。请为以下海龟汤题目生成一个合理的答案。

题目: {question}

请仅给出答案，不要包含任何解释或额外文字。
可以参考的海龟汤汤面and汤底（仅供参考，可以套模版或者直接搬，但是严格按照输出格式，仅输出汤底）：
1.【子的爱】
汤面：我的父母都不理我，但我还是很爱他们。
汤底：小时候我是个很听话的孩子，爸爸妈妈经常给我好吃的水果，我吃不完。他们就告诉我喜欢的东西一定要放进冰箱，这样可以保鲜，记得那时候他们工作可辛苦了，经常加班到深夜。没睡过一个好觉。于是我耍了个小聪明，在他们的水里下了安眠药。他们睡得可香了，然后我把他们放进冰箱里，从那以后我每天都会对他们说：爸爸妈妈我爱你们。现在我都六十了，他们还是那么年轻。

2.【舞】
汤面：我六岁那年，外公去世，我和亲人一起去祭奠，和姐姐玩捉迷藏，然后我对母亲说了句话把她吓昏了过去。
汤底：我去参加外公的葬礼，同行的还有比我大两岁的姐姐，我和她完捉迷藏我没有找到她没想到她躲在了纸做的房子里，当纸房子被点燃，我看见姐姐在跳舞，我对妈说，妈姐姐在那房子里面跳舞，因为姐姐被烧死了，我一直记得这个事。

3.【插进来】
汤面：他迅速的插进来，又迅速的拔出去。反反复复，我流血了。他满头大汗，露出了笑容。"啊，好舒服"
汤底：他是实习护士，在给我打针，针头打进血管里面会回血，因此说明成功了。流汗是因为反反复复了好几次，让人紧张。

4.【无罪】
汤面："她是自愿的！"尸体无暴力痕迹，凶手被判无罪。"我是无罪的！"尸体有暴力痕迹，凶手也被判无罪。
汤底：第一幕：女儿为救他人（如器官移植）自愿牺牲，所以"自愿"且无暴力痕迹，他人无罪。第二幕：父亲无法接受女儿死亡真相，杀害了被判无罪的人，但法医发现此人所受暴力伤害与父亲行为不符（或父亲伪造证据），真相是女儿死于意外，父亲为报复误杀他人，故父亲也称自己"无罪"，但法律上仍有罪。
            """
            # --- 传递当前选中的模型 ---
            answer_response = await self._call_llm_api(answer_prompt, api_url, api_key, model, temperature)
            if not answer_response:
                try:
                    await self.send_text("❌ 生成答案失败，请稍后再试。")
                except Exception as e:
                    print(f"发送答案失败消息失败: {e}")
                return False, "生成答案失败", True
            answer = answer_response.strip()
            print(f"[LLM Answer Response] {answer}")
            # --- AI生成逻辑结束 ---

        # --- 通用游戏状态保存和消息发送逻辑 ---
        game_states[group_id] = {
            "current_question": question,
            "current_answer": answer,
            "hints_used": 0,
            "game_active": True,
            "guess_history": [],
            "game_over": False
        }

        game_type_text = " (本地题目)" if is_local_game and local_name else ""
        name_text = f"【{local_name}】" if is_local_game and local_name else ""

        reply_text = (
            f"🤔 **海龟汤题目** {game_type_text}\n\n"
            f"{name_text}{question}\n\n"
            f"💡 **提示次数**: 0/3\n"
            f"🔸 请使用 `/hgt 问题 <问题>` 提问\n"
            f"🔸 使用 `/hgt 提示` 获取提示\n"
            f"🔸 使用 `/hgt 猜谜 <答案>` 猜测汤底"
        )

        try:
            await self.send_text(reply_text)
        except Exception as e:
            print(f"发送题目回复失败: {e}")
            return False, "发送题目回复失败", True

        return True, "已发送题目", True

    # --- LLM API 调用辅助方法 ---
    async def _call_llm_api(self, prompt: str, api_url: str, api_key: str, model: str, temperature: float) -> str:
        """
        调用OpenAI格式的LLM API并返回响应文本
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        payload = {
            "model": model, # --- 使用传入的模型名称 ---
            "messages": [
                {"role": "system", "content": "你是一个专业的海龟汤故事生成器和解释者。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": temperature,
            "max_tokens": 500, # 增加最大token数以容纳较长的回答
            "stream": False # 设置为False，因为我们不使用流式输出
        }

        try:
            timeout = aiohttp.ClientTimeout(total=30) # 设置超时
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(api_url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        # 根据OpenAI API响应结构提取回复
                        # 假设回复在 choices[0].message.content 中
                        content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                        return content
                    else:
                        error_text = await response.text()
                        print(f"LLM API 请求失败: Status {response.status}, Body: {error_text}")
                        return "" # 返回空字符串表示失败
        except Exception as e:
            print(f"调用LLM API时发生异常: {e}")
            return "" # 返回空字符串表示失败
