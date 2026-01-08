import os
from config import Config
import logging
import json
from prompts import Prompts
import time
import asyncio
import aiohttp
from typing import List, Dict, Optional
import re
import ssl
import requests  # 仅保留一次导入

# 初始化日志
logger = logging.getLogger(__name__)


# 合并两个 LLMClient 类，保留所有有效功能，删除重复定义
class LLMClient:
    # 整合两个类的 __init__ 方法，兼容智谱/火山引擎/百度，保留环境变量配置
    def __init__(self, api_key=None, api_secret=None, api_base=None, model=None, temperature=0.7, max_tokens=8192,
                 timeout=300):
        # 优先使用传入参数，无传入则使用环境变量/Config配置
        self.api_key = api_key or os.getenv('LLM_API_KEY', Config.LLM_API_KEY)
        self.api_secret = api_secret or os.getenv('LLM_API_SECRET', None)
        self.api_base = api_base or os.getenv('LLM_API_BASE', Config.LLM_API_BASE)  # 关键：保留配置加载
        self.model = model or Config.LLM_MODEL
        self.temperature = temperature or Config.TEMPERATURE
        self.max_tokens = max_tokens or Config.MAX_TOKENS
        self.timeout = timeout or Config.TIMEOUT
        # 核心修复：移除 self.session 复用，改为每次调用创建新会话
        self.messages = []
        # 百度专属：获取 Access Token（智谱无需此逻辑）
        self.access_token = self._get_baidu_access_token() if self.api_secret else None
        logger.info("LLM client initialized successfully")
        print(f"加载的API Key：{self.api_key[:10]}...")
        print(f"加载的API地址：{self.api_base}")  # 新增：打印API地址，验证配置加载

    # 百度 Access Token 获取方法（保留原有功能，智谱无需调用）
    def _get_baidu_access_token(self):
        try:
            token_url = f"https://aip.baidubce.com/oauth/2.0/token?grant_type=client_credentials&client_id={self.api_key}&client_secret={self.api_secret}"
            response = requests.get(token_url, timeout=10)
            response.raise_for_status()
            return response.json().get("access_token")
        except Exception as e:
            raise ValueError(f"获取百度 Access Token 失败：{str(e)}")

    # 异步上下文管理器入口（适配新的会话管理）
    async def __aenter__(self):
        return self

    # 异步上下文管理器出口（确保会话关闭）
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # 无需手动关闭，因为每次调用都用 async with 自动管理
        pass

    # 核心修复：删除 _ensure_session 方法，改为每次调用创建新会话
    def _get_session_kwargs(self):
        """构建会话参数（抽离为独立方法，便于复用）"""
        # 配置 SSL 上下文
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False

        # 配置连接超时
        timeout = aiohttp.ClientTimeout(
            total=self.timeout,
            connect=10,
            sock_read=20
        )

        # 配置连接器
        connector_kwargs = {
            'ssl': ssl_context,
            'limit': 15,  # 调整并发连接数
            'force_close': True,
            'enable_cleanup_closed': True
        }

        connector = aiohttp.TCPConnector(**connector_kwargs)

        # 创建会话参数（彻底移除 base_url 配置）
        session_kwargs = {
            'headers': {
                "Content-Type": "application/json"
            },
            'timeout': timeout,
            'connector': connector
        }

        # 添加鉴权头（区分智谱/火山引擎/百度）
        if not self.api_secret:
            # 智谱AI：glm-key 前缀；火山引擎：Bearer 前缀
            # 自动识别：根据API_BASE是否包含bigmodel.cn判断是否为智谱
            # 通义千问认证格式：Bearer {API_KEY}（和OpenAI一致）
            if "dashscope.aliyuncs.com" in self.api_base:
                session_kwargs['headers']["Authorization"] = f"Bearer {self.api_key}"
            elif "bigmodel.cn" in self.api_base:
                session_kwargs['headers']["Authorization"] = f"glm-key {self.api_key}"
            else:
                session_kwargs['headers']["Authorization"] = f"Bearer {self.api_key}"
        # 如果使用代理，添加代理配置（智谱无需代理，建议关闭）
        if Config.USE_PROXY and "bigmodel.cn" not in self.api_base:
            session_kwargs['proxy'] = Config.PROXY_URLS['https']
            logger.info(f"Using proxy: {Config.PROXY_URLS}")

        return session_kwargs

    # 核心方法：_call_llm_async（使用 async with 管理会话，解决资源泄漏+移除硬编码URL）
    async def _call_llm_async(self, messages, require_json=False, require_outline=False):
        retry_count = 0
        session_kwargs = self._get_session_kwargs()

        while retry_count <= Config.MAX_RETRIES:
            # 核心修复：使用 async with 自动创建/关闭会话
            async with aiohttp.ClientSession(**session_kwargs) as session:
                try:
                    request_params = {
                        "model": self.model,
                        "messages": messages,
                        "temperature": self.temperature,
                        "max_tokens": self.max_tokens,
                        "top_p": Config.TOP_P,
                        "stream": False
                    }

                    logger.debug(f"Sending request with params: {json.dumps(request_params, ensure_ascii=False)}")

                    # 核心修复：移除硬编码的火山方舟URL，改用配置的self.api_base
                    if not self.api_secret:
                        # 智谱/火山引擎：使用配置的API_BASE（不再硬编码）
                        full_valid_url = self.api_base  # 直接使用配置的地址
                        async with session.post(
                                full_valid_url,  # 配置的智谱地址：https://open.bigmodel.cn/api/paas/v4/chat/completions
                                json=request_params,
                                timeout=self.timeout
                        ) as response:
                            content = await self._handle_response(response, require_json)
                            return content
                    else:
                        # 百度：直接使用完整路径 + Access Token
                        full_valid_url = f"{self.api_base}?access_token={self.access_token}"
                        async with session.post(
                                full_valid_url,
                                json=request_params,
                                timeout=self.timeout
                        ) as response:
                            content = await self._handle_response(response, require_json)
                            return content

                except asyncio.TimeoutError:
                    retry_count += 1
                    if retry_count <= Config.MAX_RETRIES:
                        wait_time = Config.RETRY_DELAY * (Config.RETRY_BACKOFF ** (retry_count - 1))
                        logger.warning(
                            f"Request timeout. Retrying in {wait_time} seconds... (Attempt {retry_count}/{Config.MAX_RETRIES})")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error("Request failed after maximum retries due to timeout")
                        raise ValueError("Request timeout after max retries")
                except Exception as e:
                    logger.error(f"Request failed: {str(e)}")
                    raise e

    # 响应处理辅助方法（强化 JSON 清理，适配 LLM 异常返回）
    async def _handle_response(self, response, require_json):
        # 记录原始响应
        response_text = await response.text()
        logger.debug(f"Raw API response: {response_text[:500]}...")  # 截断长日志

        # 状态码校验
        if response.status != 200:
            logger.error(f"API returned status {response.status}: {response_text[:500]}...")
            raise ValueError(f"API returned status {response.status}")

        # 解析响应
        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse API response as JSON: {response_text[:500]}...")
            raise ValueError("Invalid JSON in API response")

        # 提取内容（区分智谱/火山引擎/百度，智谱和火山引擎响应格式一致）
        if not self.api_secret:
            # 智谱/火山引擎响应（格式完全兼容）
            if "choices" in result and result["choices"] and "message" in result["choices"][0]:
                content = result["choices"][0]["message"]["content"].strip()
            else:
                logger.error(f"Unexpected response structure: {json.dumps(result, ensure_ascii=False)[:500]}...")
                raise ValueError("Invalid response structure (Zhipu/Volcano Engine)")
        else:
            # 百度响应
            if "result" in result:
                content = result["result"].strip()
            else:
                logger.error(f"Unexpected response structure (Baidu): {json.dumps(result, ensure_ascii=False)[:500]}...")
                raise ValueError("Invalid response structure (Baidu)")

        # 强化 JSON 格式校验和清理（适配 LLM 错误转义/残缺）
        # 找到require_json=True的修复逻辑，替换为以下强化版：
        if require_json:
            try:
                # 1. 清理代码块和无效字符
                content = re.sub(r'^```(?:json)?\s*|\s*```\s*$', '', content.strip())  # 清理代码块
                content = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9\[\]{}:"",.\s]', '', content)  # 仅保留中文/英文/JSON字符
                content = content.replace('\\"', '"').replace('\n', '').replace('\r', '')  # 修复转义

                # 2. 智能补全截断的JSON（核心：从后往前补全，解决"sub_sect]"这类截断）
                # 示例：把"sub_sect]"补全为"sub_section_title": ""}]}}
                # 第一步：补全引号
                quote_count = content.count('"')
                if quote_count % 2 != 0:
                    content += '"' * (2 - quote_count % 2)
                # 第二步：补全未闭合的键值对（针对截断的字段名）
                if content.endswith('"') or content.endswith(':') or content.endswith('[') or content.endswith('{'):
                    content += '""'
                # 第三步：补全括号（从后往前匹配）
                brace_stack = []
                bracket_stack = []
                for char in content:
                    if char == '{':
                        brace_stack.append(char)
                    elif char == '}':
                        if brace_stack:
                            brace_stack.pop()
                    elif char == '[':
                        bracket_stack.append(char)
                    elif char == ']':
                        if bracket_stack:
                            bracket_stack.pop()
                # 补全剩余的括号
                content += ']' * len(bracket_stack) + '}' * len(brace_stack)

                # 3. 验证并格式化
                json_obj = json.loads(content)
                content = json.dumps(json_obj, ensure_ascii=False, indent=2)
            except json.JSONDecodeError as e:
                logger.error(f"JSON补全失败：{e}，使用默认大纲兜底")
                # 兜底：返回完整的默认大纲JSON，确保前端能解析
                default_outline = {
                    "body_paragraphs": [
                        {
                            "chapter_title": "项目验收要求",
                            "sections": [
                                {
                                    "section_title": "验收阶段",
                                    "sub_sections": [
                                        {
                                            "sub_section_title": "总体要求",
                                            "content_summary": "项目验收需符合合同及行业规范要求"
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
                content = json.dumps(default_outline, ensure_ascii=False, indent=2)
                logger.warning("使用默认大纲JSON兜底")

                raise ValueError(f"Invalid JSON after cleanup: {str(e)}")


        return content  # 确保返回处理后的内容

    # 生成单个章节内容（保留原有功能）
    async def generate_section_content_async(self, section: Dict) -> Dict:
        """异步生成单个章节内容"""
        try:
            # 开始生成
            logger.info(f"=== Generating content for section: {section['title']} ===")
            start_time = time.time()

            prompt = Prompts.CONTENT_SECTION_USER.format(
                title=section['title'],
                content_summary=section['content_summary']
            )

            content = await self._call_llm_async([
                {"role": "system", "content": Prompts.CONTENT_SYSTEM_ROLE},
                {"role": "user", "content": prompt}
            ])

            # 完成生成
            elapsed_time = time.time() - start_time
            if content:
                content_length = len(content)
                logger.info(
                    f"✓ Successfully generated {content_length} chars for {section['title']} in {elapsed_time:.2f}s")
            else:
                logger.error(f"✗ Failed to generate content for {section['title']} after {elapsed_time:.2f}s")

            return {
                'title': section['title'],
                'content': content if content else "生成失败，请手动补充。"
            }
        except Exception as e:
            logger.error(f"✗ Error generating content for {section['title']}: {str(e)}")
            return {
                'title': section['title'],
                'content': f"生成失败：{str(e)}"
            }

    # 初始化内容生成（保留原有功能）
    async def generate_content_init_async(self, tech_content: str, score_content: str, outline: str) -> bool:
        """初始化内容生成的背景信息"""
        try:
            prompt = Prompts.CONTENT_INIT_USER.format(
                tech_content=tech_content,
                score_content=score_content,
                outline=outline
            )
            self.start_new_chat(Prompts.CONTENT_SYSTEM_ROLE)
            response = await self.generate_chat_text_async(prompt)
            return bool(response)
        except Exception as e:
            logger.error(f"Error initializing content generation: {e}")
            return False

    # 关闭会话（适配新的会话管理，空实现）
    async def close(self):
        """关闭会话（现在由 async with 自动管理，此方法保留以兼容原有代码）"""
        pass

    # 开始新对话（保留原有功能）
    def start_new_chat(self, system_role: str):
        """开始新的对话"""
        self.messages = [{"role": "system", "content": system_role}]

    # 添加消息到对话历史（保留原有功能）
    def add_message(self, role: str, content: str):
        """添加消息到对话历史"""
        self.messages.append({"role": role, "content": content})

    # 异步生成文本（保留原有功能，修复方法调用）
    async def generate_text_async(self, prompt=None, system_role=None, messages=None, require_json=False,
                                  require_outline=False) -> str:
        """异步生成文本
        :param prompt: 单条提示词
        :param system_role: 系统角色设定
        :param messages: 完整的消息列表（如果提供，则忽略 prompt 和 system_role）
        :param require_json: 是否要求 JSON 格式响应
        :param require_outline: 是否要求大纲格式（包含 body_paragraphs 字段）
        """
        try:
            if messages is None:
                messages = [
                    {"role": "system", "content": system_role or Prompts.OUTLINE_SYSTEM_ROLE},
                    {"role": "user", "content": prompt}
                ]

            return await self._call_llm_async(messages, require_json=require_json, require_outline=require_outline)
        except Exception as e:
            logger.error(f"Error in generate_text: {e}", exc_info=True)
            return None

    # 异步对话生成文本（保留原有功能，修复方法调用）
    async def generate_chat_text_async(self, prompt: str) -> str:
        """异步在现有对话中生成文本（用于内容生成）"""
        try:
            self.add_message("user", prompt)
            response = await self._call_llm_async(self.messages, require_json=False)
            if response:
                self.add_message("assistant", response)
            return response
        except Exception as e:
            logger.error(f"Error in generate_chat_text: {e}", exc_info=True)
            return None


# 调试代码（适配合并后的类，可选删除）
if __name__ == "__main__":
    # 实例化 LLMClient（参数可选，可省略使用 Config 配置）
    client = LLMClient(api_key="test", api_base="test", model="test")
    # 打印包含 call_llm 的属性，验证方法存在
    attrs = [attr for attr in dir(client) if "call_llm" in attr]
    print("包含 call_llm 的属性：", attrs)