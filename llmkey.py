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
    # 整合两个类的 __init__ 方法，兼容火山引擎/百度，保留环境变量配置
    def __init__(self, api_key=None, api_secret=None, api_base=None, model=None, temperature=0.7, max_tokens=8192,
                 timeout=300):
        # 优先使用传入参数，无传入则使用环境变量/Config配置
        self.api_key = api_key or os.getenv('LLM_API_KEY', Config.LLM_API_KEY)
        self.api_secret = api_secret or os.getenv('LLM_API_SECRET', None)
        self.api_base = api_base or os.getenv('LLM_API_BASE', Config.LLM_API_BASE)
        self.model = model or Config.LLM_MODEL
        self.temperature = temperature or Config.TEMPERATURE
        self.max_tokens = max_tokens or Config.MAX_TOKENS
        self.timeout = timeout or Config.TIMEOUT
        # 核心修复：移除 self.session 复用，改为每次调用创建新会话
        self.messages = []
        # 百度专属：获取 Access Token
        self.access_token = self._get_baidu_access_token() if self.api_secret else None
        logger.info("LLM client initialized successfully")
        print(f"加载的API Key：{self.api_key[:10]}...")

    # 百度 Access Token 获取方法（保留原有功能）
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

        # 添加鉴权头（区分火山引擎/百度）
        if not self.api_secret:
            session_kwargs['headers']["Authorization"] = f"glm-key {self.api_key}"
        # 如果使用代理，添加代理配置
        if Config.USE_PROXY:
            session_kwargs['proxy'] = Config.PROXY_URLS['https']
            logger.info(f"Using proxy: {Config.PROXY_URLS}")

        return session_kwargs

    # 核心方法：_call_llm_async（使用 async with 管理会话，解决资源泄漏）
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

                    # 区分火山方舟/百度（直接使用完整绝对路径，无任何拼接）
                    if not self.api_secret:
                        # 火山方舟豆包大模型 正确完整有效接口路径
                        full_valid_url = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
                        async with session.post(
                                full_valid_url,  # 直接传完整路径，无 base_url 冲突
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

        # 提取内容（区分火山引擎/百度）
        if not self.api_secret:
            # 火山引擎（方舟）响应
            if "choices" in result and result["choices"] and "message" in result["choices"][0]:
                content = result["choices"][0]["message"]["content"].strip()
            else:
                logger.error(f"Unexpected response structure (Volcano): {json.dumps(result, ensure_ascii=False)[:500]}...")
                raise ValueError("Invalid response structure (Volcano Engine Ark)")
        else:
            # 百度响应
            if "result" in result:
                content = result["result"].strip()
            else:
                logger.error(f"Unexpected response structure (Baidu): {json.dumps(result, ensure_ascii=False)[:500]}...")
                raise ValueError("Invalid response structure (Baidu)")

        # 强化 JSON 格式校验和清理（适配 LLM 错误转义/残缺）
        if require_json:
            try:
                # 第一步：清理代码块标记
                if content.startswith('```'):
                    content = re.sub(r'^```(?:json)?\s*|\s*```\s*$', '', content).strip()
                # 第二步：修复转义错误（核心！解决 \"body_paragraphs 问题）
                content = content.replace('\\"', '"').replace('\n', '').replace('\r', '')
                # 第三步：补全残缺括号
                brace_diff = content.count('{') - content.count('}')
                bracket_diff = content.count('[') - content.count(']')
                if brace_diff > 0:
                    content += '}' * brace_diff
                if bracket_diff > 0:
                    content += ']' * bracket_diff
                # 第四步：验证并格式化
                json_obj = json.loads(content)
                content = json.dumps(json_obj, ensure_ascii=False, indent=2)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in response after cleanup: {content[:500]}... Error: {e}")
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