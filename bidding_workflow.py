# 统一使用 Quart 异步框架，删除 Flask 导入（避免混用导致异步错误）
import asyncio
from dataclasses import dataclass
from typing import List, Optional, Dict, Union
import json
import yaml
import os
import re
from llmkey import LLMClient
import pathlib
import logging
from config import Config
from prompts import Prompts
from prompt_manager import PromptManager
import time
from quart import Quart, jsonify, request, render_template, Blueprint  # 仅保留 Quart 导入

# ===================== 核心修复1：初始化 Quart 实例（替代 Flask） =====================
app = Quart(
    __name__,
    template_folder=Config.TEMPLATE_FOLDER,  # 从配置读取模板路径
    static_folder=Config.STATIC_FOLDER      # 从配置读取静态文件路径
)

# 将现有的路径常量替换为配置
BASE_DIR = Config.BASE_DIR
INPUT_DIR = Config.INPUT_DIR
OUTPUT_DIR = Config.OUTPUT_DIR
OUTLINE_DIR = Config.OUTLINE_DIR
LOG_DIR = Config.LOG_DIR

# 首先创建必要的目录（修复：明确所有目录，避免 KeyError）
for path in [INPUT_DIR, OUTPUT_DIR, OUTLINE_DIR, LOG_DIR]:
    path.mkdir(parents=True, exist_ok=True)

# 修改日志配置
logging.basicConfig(level=logging.INFO)  # 设置根日志器级别为 INFO

# 创建文件处理器，用于详细日志
file_handler = logging.FileHandler(LOG_DIR / 'app.log', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

# 创建控制台处理器，只显示关键信息
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# 配置根日志器
root_logger = logging.getLogger()
root_logger.handlers = []  # 清除之前的处理器
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# 配置第三方库的日志级别
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('openai').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ===================== 核心修复2：创建并注册蓝图（路由必须注册才生效） =====================
prompt_bp = Blueprint(
    'prompt',
    __name__,
    template_folder=Config.TEMPLATE_FOLDER,
    static_folder=Config.STATIC_FOLDER
)


# ===================== 核心新增：全局PromptManager实例（供接口使用） =====================
global_prompt_manager = PromptManager(BASE_DIR / "config" / "prompt_config.json")

# ======================================================================================
# 数据模型部分（无修改）
# ======================================================================================
@dataclass
class OutlineNode:
    title: str
    level: int
    content_desc: Optional[str] = None
    children: List['OutlineNode'] = None

    def __post_init__(self):
        if self.children is None:
            self.children = []

    def to_dict(self):
        return {
            'title': self.title,
            'level': self.level,
            'content_desc': self.content_desc,
            'children': [child.to_dict() for child in self.children] if self.children else []
        }

@dataclass
class GenerationProgress:
    total_sections: int = 0
    completed_sections: int = 0
    current_section: str = ""

@dataclass
class SubSection:
    sub_section_title: str
    content_summary: str

    def to_dict(self):
        return {
            'sub_section_title': self.sub_section_title,
            'content_summary': self.content_summary
        }

@dataclass
class Section:
    section_title: str
    sub_sections: List[SubSection]

    def to_dict(self):
        return {
            'section_title': self.section_title,
            'sub_sections': [sub.to_dict() for sub in self.sub_sections]
        }

@dataclass
class Chapter:
    chapter_title: str
    sections: List[Section]

    def to_dict(self):
        return {
            'chapter_title': self.chapter_title,
            'sections': [section.to_dict() for section in self.sections]
        }

@dataclass
class Outline:
    body_paragraphs: List[Chapter]

    def to_dict(self):
        return {
            'body_paragraphs': [chapter.to_dict() for chapter in self.body_paragraphs]
        }

# ======================================================================================
# 核心修复3：BiddingWorkflow 类（异步构造函数改为同步 + 异步初始化分离）
# ======================================================================================
class BiddingWorkflow:
    # 修复：__init__ 必须是同步方法，删除 async 关键字
    def __init__(self):
        self.tech_content = ""
        self.score_content = ""
        self.outline = None
        self.generated_contents = {}
        self.llm_client = LLMClient()
        self.progress = GenerationProgress()
        self.full_document_content = ""
        self.document_save_path = OUTPUT_DIR / 'content.md'
        self.prompt_manager = PromptManager(BASE_DIR / "config" / "prompt_config.json")

    # 新增：独立的异步初始化方法（存放需要 await 的逻辑）
    async def init_async(self):
        # 若有异步初始化逻辑，写在这里（如 LLM 客户端预热）
        pass

    async def __aenter__(self):
        await self.init_async()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self, 'llm_client'):
            await self.llm_client.close()

    def load_input_files(self):
        """加载技术要求和评分标准文件"""
        try:
            tech_file = INPUT_DIR / 'tech.md'
            score_file = INPUT_DIR / 'score.md'

            if not tech_file.exists():
                logger.error(f"Tech file not found: {tech_file}")
                raise FileNotFoundError(f"Tech file not found: {tech_file}")
            if not score_file.exists():
                logger.error(f"Score file not found: {score_file}")
                raise FileNotFoundError(f"Score file not found: {score_file}")

            if tech_file.stat().st_size == 0:
                logger.error("Tech file is empty")
                raise ValueError("Tech file is empty")
            if score_file.stat().st_size == 0:
                logger.error("Score file is empty")
                raise ValueError("Score file is empty")

            with open(tech_file, 'r', encoding='utf-8') as f:
                self.tech_content = f.read()
                logger.info(f"Loaded tech file, size: {len(self.tech_content)} chars")

            with open(score_file, 'r', encoding='utf-8') as f:
                self.score_content = f.read()
                logger.info(f"Loaded score file, size: {len(self.score_content)} chars")

        except Exception as e:
            logger.error(f"Error loading input files: {e}", exc_info=True)
            raise

    # 核心修复4：JSON 清理函数去重 + 逻辑强化（删除重复代码 + 修复缩进）
    def clean_json_response(self, response: str) -> str:
        """强力清理 LLM 返回内容，确保为合法 JSON"""
        if not response:
            logger.warning("待清理的JSON响应为空")
            return ""

        cleaned = response.strip()
        # ========== 新增：修复转义错误（核心！解决 \"body_paragraphs 问题） ==========
        # 1. 移除多余的反斜杠（LLM 错误添加的转义）
        cleaned = cleaned.replace('\\"', '"')
        # 2. 移除换行符（避免多行JSON解析错误）
        cleaned = cleaned.replace('\n', '').replace('\r', '')
        # 3. 移除制表符/空格冗余
        cleaned = re.sub(r'\s+', ' ', cleaned)

        # 1. 去除代码块标记
        if cleaned.startswith('```json'):
            cleaned = cleaned[7:].strip()
        elif cleaned.startswith('```'):
            cleaned = cleaned[3:].strip()
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3].strip()

        # 2. 剔除前缀多余文字，只保留 JSON 主体
        first_brace = cleaned.find("{")
        first_bracket = cleaned.find("[")
        start_idx = -1
        if first_brace != -1 and first_bracket != -1:
            start_idx = min(first_brace, first_bracket)
        elif first_brace != -1:
            start_idx = first_brace
        elif first_bracket != -1:
            start_idx = first_bracket

        if start_idx != -1:
            cleaned = cleaned[start_idx:]
        else:
            logger.warning("未找到 JSON 起始符号 { 或 [")
            return ""

        # 3. 修复常见 JSON 错误（未转义引号、尾部逗号、多行字符串）
        cleaned = re.sub(r',\s*}', '}', cleaned)
        cleaned = re.sub(r',\s*]', ']', cleaned)

        # 4. 自动补全残缺的大括号/中括号（深度修复）
        def count_unclosed_chars(s: str, open_char: str, close_char: str) -> int:
            """统计未闭合的括号数量"""
            open_count = s.count(open_char)
            close_count = s.count(close_char)
            return max(0, open_count - close_count)

        # 补全大括号
        brace_diff = count_unclosed_chars(cleaned, '{', '}')
        if brace_diff > 0:
            cleaned += '}' * brace_diff
        # 补全中括号
        bracket_diff = count_unclosed_chars(cleaned, '[', ']')
        if bracket_diff > 0:
            cleaned += ']' * bracket_diff

        # 修复缩进：这部分之前缩进错误
        try:
            json.loads(cleaned)
            logger.info("JSON自动补全成功")
            return cleaned
        except json.JSONDecodeError as e2:
            logger.error(f"补全后仍无法解析: {e2}, 内容: {cleaned[:200]}...")
            return ""

    # 核心修复5：大纲生成函数添加 JSON 补全逻辑（修复缩进 + 逻辑整合）
    async def generate_outline(self) -> str:
        """生成大纲"""
        try:
            logger.info("=== Starting Outline Generation ===")

            messages = [{"role": "system", "content": self.prompt_manager.get_prompt("OUTLINE_SYSTEM_ROLE")}]
            messages.append({
                "role": "user",
                "content": self.prompt_manager.get_prompt("OUTLINE_TECH_USER").format(tech_content=self.tech_content)
            })
            messages.append({
                "role": "user",
                "content": self.prompt_manager.get_prompt("OUTLINE_SCORE_USER").format(score_content=self.score_content)
            })
            messages.append({
                "role": "user",
                "content": self.prompt_manager.get_prompt("OUTLINE_GENERATE_USER")
            })

            # 调用 LLM 生成
            outline_json = await self.llm_client.generate_text_async(
                messages=messages,
                require_json=True,
                require_outline=True
            )

            # 打印原始内容排查问题
            logger.info(f"LLM原始返回内容：[{outline_json[:200] if outline_json else '空'}]")
            logger.info(f"LLM返回内容长度：{len(outline_json) if outline_json else 0}")

            # 空值校验
            if not outline_json:
                logger.error("LLM返回空内容，大纲生成失败")
                return None

            # ========== 核心新增：补全残缺的JSON ==========
            def fix_broken_json(broken_json):
                """补全残缺的JSON"""
                try:
                    # 先尝试直接解析
                    return json.loads(broken_json)
                except json.JSONDecodeError:
                    # 补全缺失的闭合括号
                    open_braces = broken_json.count('{')
                    close_braces = broken_json.count('}')
                    open_brackets = broken_json.count('[')
                    close_brackets = broken_json.count(']')

                    # 补全括号
                    fixed_json = broken_json
                    fixed_json += '}' * (open_braces - close_braces)
                    fixed_json += ']' * (open_brackets - close_brackets)

                    try:
                        return json.loads(fixed_json)
                    except:
                        # 仍失败则返回空字典
                        return {}

            # 修复LLM返回的JSON
            fixed_outline_obj = fix_broken_json(outline_json)
            # 转回字符串（确保是合法JSON）
            fixed_outline_json = json.dumps(fixed_outline_obj, ensure_ascii=False, indent=2)

            # 保存修复后的JSON
            self.save_outline_json(fixed_outline_json)
            return fixed_outline_json

        except Exception as e:
            logger.error(f"Error generating outline: {e}", exc_info=True)
            raise

    # 以下函数无修改（split_long_text / parse_outline_json 等）
    def split_long_text(self, text: str, max_length: int = 3000) -> List[str]:
        if len(text) <= max_length:
            return [text]

        chunks = []
        current_chunk = []
        current_length = 0
        sentences = text.replace('\r', '').split('\n')
        for sentence in sentences:
            if len(sentence) > max_length:
                if current_chunk:
                    chunks.append('\n'.join(current_chunk))
                    current_chunk = []
                    current_length = 0
                for i in range(0, len(sentence), max_length):
                    chunks.append(sentence[i:i + max_length])
                continue

            if current_length + len(sentence) + 1 > max_length:
                chunks.append('\n'.join(current_chunk))
                current_chunk = [sentence]
                current_length = len(sentence)
            else:
                current_chunk.append(sentence)
                current_length += len(sentence) + 1

        if current_chunk:
            chunks.append('\n'.join(current_chunk))
        return chunks

    def parse_outline_json(self, outline_json: Union[str, dict]) -> Outline:
        try:
            if not outline_json:
                raise ValueError("Empty input received")

            if isinstance(outline_json, str):
                logger.debug("=== Input JSON String ===")
                logger.debug(outline_json[:500])
                data = json.loads(outline_json)
            else:
                data = outline_json

            logger.debug(f"Parsing outline data: {json.dumps(data, ensure_ascii=False)[:500]}")

            if not isinstance(data, dict) or 'body_paragraphs' not in data:
                raise ValueError("Invalid outline JSON: missing body_paragraphs")

            chapters = []
            for chapter_data in data['body_paragraphs']:
                if 'chapter_title' not in chapter_data or 'sections' not in chapter_data:
                    raise ValueError("Invalid chapter data")

                sections = []
                for section_data in chapter_data['sections']:
                    if 'section_title' not in section_data or 'sub_sections' not in section_data:
                        raise ValueError("Invalid section data")

                    sub_sections = []
                    for sub_section_data in section_data['sub_sections']:
                        if 'sub_section_title' not in sub_section_data or 'content_summary' not in sub_section_data:
                            raise ValueError("Invalid sub_section data")
                        sub_sections.append(SubSection(
                            sub_section_title=sub_section_data['sub_section_title'],
                            content_summary=sub_section_data['content_summary']
                        ))
                    sections.append(Section(section_data['section_title'], sub_sections))
                chapters.append(Chapter(chapter_data['chapter_title'], sections))

            return Outline(body_paragraphs=chapters)

        except Exception as e:
            logger.error(f"Error parsing outline JSON: {e}", exc_info=True)
            raise

    def generate_content_prompt(self, section: OutlineNode, context: str) -> str:
        return self.prompt_manager.get_prompt("CONTENT_PROMPT").format(
            tech_content=self.tech_content,
            score_content=self.score_content,
            outline=self.outline_to_markdown(),
            context=context,
            section_title=section.title,
            content_desc=section.content_desc
        )

    def outline_to_markdown(self) -> str:
        if not self.outline:
            return ""

        result = []
        for chapter in self.outline.body_paragraphs:
            result.append(f"# {chapter.chapter_title}")
            for section in chapter.sections:
                result.append(f"## {section.section_title}")
                for sub_section in section.sub_sections:
                    result.append(f"### {sub_section.sub_section_title}")
                    result.append(f"\n{sub_section.content_summary}\n")
        return "\n".join(result)

    def get_context_for_section(self, current_section: OutlineNode) -> str:
        context_parts = []
        parent_titles = []
        current_level = current_section.level

        def find_parents(node: OutlineNode, target: OutlineNode, path: List[str]):
            if node == target:
                return True
            for child in node.children:
                if find_parents(child, target, path):
                    path.append(node.title)
                    return True
            return False

        if self.outline:
            find_parents(self.outline, current_section, parent_titles)

        for title in parent_titles:
            if title in self.generated_contents:
                context_parts.append(f"## {title}\n{self.generated_contents[title]}\n")

        max_context_length = 2000
        context = "\n".join(context_parts)
        return context[-max_context_length:] if len(context) > max_context_length else context

    def save_outline(self):
        if not self.outline:
            logger.error("No outline to save")
            return

        try:
            outline_dict = self.outline.to_dict()
            json_path = OUTLINE_DIR / 'outline.json'
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(outline_dict, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved outline JSON to {json_path}")

            md_content = self.outline_to_markdown()
            md_path = OUTLINE_DIR / 'outline.md'
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(md_content)
            logger.info(f"Saved outline markdown to {md_path}")

        except Exception as e:
            logger.error(f"Error saving outline: {e}", exc_info=True)
            raise

    def save_content(self, section_title: str, content: str):
        self.generated_contents[section_title] = content
        content_file = OUTPUT_DIR / 'content.md'

        if len(self.generated_contents) == 1:
            with open(content_file, 'w', encoding='utf-8') as f:
                f.write("# 技术方案\n\n")
                f.write(self.outline_to_markdown())
                f.write("\n\n## 详细内容\n\n")

        with open(content_file, 'a', encoding='utf-8') as f:
            f.write(f"### {section_title}\n\n")
            f.write(content)
            f.write("\n\n")

    def count_sections(self, node: OutlineNode) -> int:
        count = 1 if node.level == 3 else 0
        for child in node.children:
            count += self.count_sections(child)
        return count

    async def generate_full_content_async(self) -> bool:
        start_time = time.time()
        try:
            if not self.outline:
                logger.error("No outline available")
                return False

            logger.info("=== Starting Content Generation ===")
            sections_to_generate = []
            for chapter in self.outline.body_paragraphs:
                for section in chapter.sections:
                    for sub_section in section.sub_sections:
                        sections_to_generate.append({
                            'title': sub_section.sub_section_title,
                            'content_summary': sub_section.content_summary,
                            'chapter': chapter.chapter_title,
                        })

            total_sections = len(sections_to_generate)
            logger.info(f"Found {total_sections} sections to generate")
            semaphore = asyncio.Semaphore(15)

            async def process_section_with_semaphore(section):
                async with semaphore:
                    result = await self.llm_client.generate_section_content_async(section)
                    await asyncio.sleep(0.05)
                    return result

            results = []
            batch_size = 15
            for i in range(0, len(sections_to_generate), batch_size):
                batch = sections_to_generate[i:i + batch_size]
                batch_tasks = [process_section_with_semaphore(section) for section in batch]
                batch_results = await asyncio.gather(*batch_tasks)
                results.extend(batch_results)

                if i + batch_size < len(sections_to_generate):
                    await asyncio.sleep(0.2)
                logger.info(f"Progress: {len(results)}/{total_sections} sections completed")

            organized_results = self._organize_results(results, sections_to_generate)
            success, full_content = await self._save_results_async(organized_results)

            if success and full_content:
                self.full_document_content = full_content
                logger.info(f"Full document content stored, length: {len(self.full_document_content)} chars")

            elapsed_time = time.time() - start_time
            logger.info(f"Content generation completed in {elapsed_time:.2f} seconds")
            return success

        except Exception as e:
            logger.error(f"Error generating content: {e}")
            return False

    def _organize_results(self, results: List[Dict], sections: List[Dict]) -> Dict:
        organized = {}
        for result, section in zip(results, sections):
            chapter = section['chapter']
            if chapter not in organized:
                organized[chapter] = []
            organized[chapter].append(result)
        return organized

    async def _save_results_async(self, organized_results: Dict) -> (bool, str):
        try:
            content_parts = []
            for chapter, sections in organized_results.items():
                content_parts.append(f"# {chapter}\n\n")
                section_groups = {}
                for section in sections:
                    section_number = '.'.join(section['title'].split()[:1][0].split('.')[:2])
                    section_title = section['title']
                    section_prefix = section_number + ' '
                    full_section_title = next(
                        (title for title in section['title'].split('\n') if title.startswith(section_prefix)),
                        section_prefix + '未知标题'
                    )
                    if section_number not in section_groups:
                        section_groups[section_number] = {
                            'title': full_section_title,
                            'subsections': []
                        }
                    section_groups[section_number]['subsections'].append(section)

                for section_number in sorted(section_groups.keys()):
                    group = section_groups[section_number]
                    content_parts.append(f"## {group['title']}\n\n")
                    for subsection in group['subsections']:
                        content_parts.append(f"### {subsection['title']}\n\n{subsection['content']}\n\n")

            full_content = "\n".join(content_parts)
            with open(self.document_save_path, 'w', encoding='utf-8') as f:
                f.write(full_content)

            logger.info(f"Full document saved to {self.document_save_path}, size: {len(full_content)} chars")
            return (True, full_content)
        except Exception as e:
            logger.error(f"Error saving results: {e}")
            return (False, "")

    def save_outline_json(self, outline_json: str):
        try:
            OUTLINE_DIR.mkdir(parents=True, exist_ok=True)
            json_file = OUTLINE_DIR / 'outline.json'
            with open(json_file, 'w', encoding='utf-8') as f:
                f.write(outline_json)
            logger.info(f"Saved outline JSON to {json_file}")

            md_content = self._convert_outline_to_markdown(outline_json)
            md_file = OUTLINE_DIR / 'outline.md'
            with open(md_file, 'w', encoding='utf-8') as f:
                f.write(md_content)
            logger.info(f"Saved outline Markdown to {md_file}")

        except Exception as e:
            logger.error(f"Error saving outline: {e}", exc_info=True)
            raise

    def _convert_outline_to_markdown(self, outline_json: str) -> str:
        try:
            outline = json.loads(outline_json)
            md_lines = []
            for chapter in outline["body_paragraphs"]:
                md_lines.append(f"# {chapter['chapter_title']}\n")
                for section in chapter["sections"]:
                    md_lines.append(f"## {section['section_title']}\n")
                    for sub_section in section["sub_sections"]:
                        md_lines.append(f"### {sub_section['sub_section_title']}\n")
                        md_lines.append(f"{sub_section['content_summary']}\n")
                md_lines.append("\n")
            return "\n".join(md_lines)
        except Exception as e:
            logger.error(f"Error converting outline to markdown: {e}")
            raise

def dict_to_outline(data: dict) -> OutlineNode:
    node = OutlineNode(
        title=data['title'],
        level=data['level'],
        content_desc=data.get('content_desc')
    )
    if data.get('children'):
        node.children = [dict_to_outline(child) for child in data['children']]
    return node

# ======================================================================================
# 核心修复6：提示词管理接口（删除 sync_to_async 残留，改为同步调用）
# ======================================================================================
@prompt_bp.route('/api/prompts', methods=['GET'])
async def get_all_prompts():
    try:
        # 修复：删除 sync_to_async，直接同步调用（轻量操作无需异步）
        prompts_data = global_prompt_manager.get_all_prompts()
        return jsonify({
            "code": 200,
            "msg": "success",
            "data": prompts_data
        })
    except Exception as e:
        logger.error(f"Error getting all prompts: {e}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"获取提示词失败：{str(e)}",
            "data": None
        }), 500

@prompt_bp.route('/api/prompts', methods=['POST'])
async def save_prompt():
    try:
        request_data = await request.get_json()
        key = request_data.get('key')
        content = request_data.get('content')
        is_custom = request_data.get('is_custom', False)

        if not key or content is None:
            return jsonify({
                "code": 400,
                "msg": "缺少必要参数：key 或 content",
                "data": None
            }), 400

        if is_custom:
            custom_prompts = global_prompt_manager.user_prompts.get("CUSTOM_PROMPTS", {})
            custom_prompts[key] = content
            global_prompt_manager.user_prompts["CUSTOM_PROMPTS"] = custom_prompts
            global_prompt_manager.save_prompt("CUSTOM_PROMPTS", custom_prompts)
        else:
            global_prompt_manager.save_prompt(key, content)

        return jsonify({
            "code": 200,
            "msg": "提示词保存成功",
            "data": None
        })
    except Exception as e:
        logger.error(f"Error saving prompt: {e}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"保存提示词失败：{str(e)}",
            "data": None
        }), 500

@prompt_bp.route('/api/prompts/<string:key>', methods=['DELETE'])
async def delete_prompt(key):
    try:
        delete_success = global_prompt_manager.delete_prompt(key)
        if not delete_success:
            return jsonify({
                "code": 400,
                "msg": "删除失败：不允许删除系统提示词，或该自定义提示词不存在",
                "data": None
            }), 400

        return jsonify({
            "code": 200,
            "msg": "提示词删除成功",
            "data": None
        })
    except Exception as e:
        logger.error(f"Error deleting prompt: {e}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"删除提示词失败：{str(e)}",
            "data": None
        }), 500

@prompt_bp.route('/api/prompts/reset/<string:key>', methods=['POST'])
async def reset_prompt(key):
    try:
        global_prompt_manager.reset_prompt(key)
        return jsonify({
            "code": 200,
            "msg": "提示词重置为默认值成功",
            "data": None
        })
    except Exception as e:
        logger.error(f"Error resetting prompt: {e}", exc_info=True)
        return jsonify({
            "code": 500,
            "msg": f"重置提示词失败：{str(e)}",
            "data": None
        }), 500

@prompt_bp.route('/prompt-manage')
async def prompt_manage_page():
    return await render_template('prompt_manage.html')

# ======================================================================================
# 业务接口（修复：实例化后调用异步初始化）
# ======================================================================================
@prompt_bp.route('/generate_outline', methods=['POST'])
async def generate_outline():
    workflow = BiddingWorkflow()
    await workflow.init_async()  # 新增：调用异步初始化
    try:
        logger.info("Starting outline generation")
        workflow.load_input_files()
        outline_json = await workflow.generate_outline()
        if not outline_json:
            logger.error("Failed to generate outline")
            return jsonify({"status": "error", "message": "Failed to generate outline"}), 500

        workflow.outline = workflow.parse_outline_json(outline_json)
        workflow.save_outline()
        return jsonify({
            "status": "success",
            "outline": workflow.outline.to_dict()
        })
    except Exception as e:
        logger.error(f"Error in generate_outline: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@prompt_bp.route('/generate_content', methods=['POST'])
async def generate_content():
    workflow = BiddingWorkflow()
    await workflow.init_async()
    try:
        success = await workflow.generate_full_content_async()
        return jsonify({"status": "success" if success else "error"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        await workflow.llm_client.close()

@prompt_bp.route('/generate_document', methods=['POST'])
async def generate_document():
    workflow = BiddingWorkflow()
    await workflow.init_async()
    try:
        workflow.load_input_files()
        with open(OUTLINE_DIR / 'outline.json', 'r', encoding='utf-8') as f:
            outline_dict = json.load(f)
            workflow.outline = workflow.parse_outline_json(outline_dict)

        success = await workflow.generate_full_content_async()
        if not success:
            return jsonify({"status": "error", "message": "Failed to generate content"}), 500

        return jsonify({
            "status": "success",
            "message": "Document generated successfully",
            "document_content": workflow.full_document_content,
            "save_path": str(workflow.document_save_path)
        })
    except Exception as e:
        logger.error(f"Error generating document: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# 注册蓝图（所有路由定义完成后）
app.register_blueprint(prompt_bp)

# ======================================================================================
# 启动服务
# ======================================================================================
if __name__ == '__main__':
    app.run(debug=True, port=5001, host='0.0.0.0')