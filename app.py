from quart import Quart, jsonify, request, render_template
from quart_cors import cors
import logging
from config import Config
import json
from datetime import datetime
import os
import pathlib
from bidding_workflow import app as bidding_app, BiddingWorkflow
from quart import Quart
from bidding_workflow import BiddingWorkflow, prompt_bp  # 导入提示词蓝图

# 初始化配置
app = Quart(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB 最大请求限制
app = cors(app, allow_origin="*", allow_methods=["GET", "POST"])  # 跨域配置
logger = logging.getLogger(__name__)

# 确保输出目录存在（强化：自动创建所有需要的输出目录）
os.makedirs(Config.OUTLINE_DIR if hasattr(Config, 'OUTLINE_DIR') else 'outputs/outline', exist_ok=True)
os.makedirs('inputs', exist_ok=True)
os.makedirs('outputs/document', exist_ok=True)  # 新增：确保终稿目录存在
os.makedirs('outputs', exist_ok=True)  # 新增：确保根输出目录存在

app.register_blueprint(prompt_bp)


# 首页路由
@app.route('/')
async def index():
    return await render_template('index.html')


# 大纲生成页面
@app.route('/outline')
async def outline_page():
    return await render_template('outline.html')


# 终稿生成页面
@app.route('/document')
async def document_page():
    return await render_template('document.html')


# 保存输入内容（技术要求+评分标准）
@app.route('/save_input', methods=['POST'])
async def save_input():
    try:
        # 获取前端JSON数据
        data = await request.get_json()
        tech_content = data.get('tech_content', '').strip()
        score_content = data.get('score_content', '').strip()

        # 定义输入文件路径
        tech_file_path = os.path.join('inputs', 'tech.md')
        score_file_path = os.path.join('inputs', 'score.md')

        # 写入文件（强化：确保目录存在）
        os.makedirs(os.path.dirname(tech_file_path), exist_ok=True)
        with open(tech_file_path, 'w', encoding='utf-8') as f:
            f.write(tech_content)
        with open(score_file_path, 'w', encoding='utf-8') as f:
            f.write(score_content)

        return jsonify({
            'success': True,
            'msg': '技术要求和评分标准保存成功',
            'data': {}
        })
    except Exception as e:
        logger.error(f"保存输入失败：{str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'msg': f'保存失败：{str(e)}',
            'data': {}
        })


# 核心大纲生成接口（整合所有逻辑，解决重复路由问题）
@app.route('/generate_outline', methods=['GET', 'POST'])
async def generate_outline():
    logger.info(f"Received {request.method} request to /generate_outline")
    logger.info(f"Request headers: {request.headers}")

    async with BiddingWorkflow() as workflow:  # 异步上下文管理器
        try:
            logger.info("Starting outline generation")

            # 1. 加载输入文件并校验
            logger.info("Loading input files")
            workflow.load_input_files()

            # 校验输入内容非空
            if not hasattr(workflow, 'tech_content') or len(workflow.tech_content.strip()) == 0:
                return jsonify({
                    'success': False,
                    'msg': '技术要求文件（tech.md）内容为空，请先填写并保存',
                    'data': {}
                })
            if not hasattr(workflow, 'score_content') or len(workflow.score_content.strip()) == 0:
                return jsonify({
                    'success': False,
                    'msg': '评分标准文件（score.md）内容为空，请先填写并保存',
                    'data': {}
                })

            # 2. 生成大纲
            logger.info("Generating outline")
            outline_json = await workflow.generate_outline()
            if not outline_json:
                logger.error("Failed to generate outline (empty result)")
                return jsonify({
                    'success': False,
                    'msg': 'LLM生成大纲内容为空，请检查模型配置或输入内容详细度',
                    'data': {}
                })

            # 3. 解析并保存大纲
            logger.info("Parsing outline JSON")
            workflow.outline = workflow.parse_outline_json(outline_json)

            logger.info("Saving outline")
            workflow.save_outline()

            # 4. 读取本地生成的大纲文件（核心：返回文件内容给前端）
            outline_dir = Config.OUTLINE_DIR if hasattr(Config, 'OUTLINE_DIR') else pathlib.Path('outputs/outline')
            outline_json_path = os.path.join(outline_dir, 'outline.json')
            outline_md_path = os.path.join(outline_dir, 'outline.md')

            # 读取JSON文件内容（强化：处理文件不存在的异常）
            try:
                with open(outline_json_path, 'r', encoding='utf-8') as f:
                    local_json_content = json.load(f)  # 解析为字典，方便前端直接使用
            except FileNotFoundError:
                local_json_content = {}
                logger.warning(f"大纲JSON文件未找到：{outline_json_path}")

            # 读取Markdown文件内容（强化：处理文件不存在的异常）
            try:
                with open(outline_md_path, 'r', encoding='utf-8') as f:
                    local_md_content = f.read()  # 字符串格式，支持前端Markdown渲染
            except FileNotFoundError:
                local_md_content = ""
                logger.warning(f"大纲MD文件未找到：{outline_md_path}")

            logger.info("Outline generation completed successfully, file content loaded")

            # 5. 标准化返回（包含本地文件内容+路径，方便前端渲染）
            return jsonify({
                'success': True,
                'msg': '大纲生成成功',
                'data': {
                    'outline_dict': workflow.outline.to_dict() if hasattr(workflow.outline,
                                                                          'to_dict') else local_json_content,
                    'json_content': local_json_content,  # JSON格式大纲
                    'md_content': local_md_content,  # Markdown格式大纲
                    'json_path': outline_json_path,  # 本地JSON文件路径
                    'md_path': outline_md_path  # 本地MD文件路径
                }
            })
        except Exception as e:
            logger.error(f"Error in generate_outline: {str(e)}", exc_info=True)
            return jsonify({
                'success': False,
                'msg': f'大纲生成失败：{str(e)}',
                'data': {}
            })


# 大纲生成API（兼容原有/api/v1/outline路由）
@app.route('/api/v1/outline', methods=['POST'])
async def create_outline():
    try:
        request_data = await request.get_json()
        async with BiddingWorkflow() as workflow:
            logger.info("Starting API outline generation")
            workflow.load_input_files()

            # 生成大纲
            outline_json = await workflow.generate_outline()
            if not outline_json:
                return jsonify({
                    "code": 1,
                    "message": "Failed to generate outline",
                    "data": None
                }), 500

            # 读取本地文件内容（强化：处理文件不存在）
            outline_dir = Config.OUTLINE_DIR if hasattr(Config, 'OUTLINE_DIR') else 'outputs/outline'
            outline_json_path = os.path.join(outline_dir, 'outline.json')
            try:
                with open(outline_json_path, 'r', encoding='utf-8') as f:
                    local_json_content = json.load(f)
            except FileNotFoundError:
                local_json_content = {}
                logger.warning(f"大纲JSON文件未找到：{outline_json_path}")

            current_time = datetime.now().isoformat()
            response_data = {
                "code": 0,
                "message": "success",
                "data": {
                    "outline": json.dumps(local_json_content),  # 真实大纲JSON字符串
                    "task_status": "completed",
                    "created_at": current_time,
                    "updated_at": current_time
                }
            }
            return jsonify(response_data)
    except Exception as e:
        logger.error(f"Error in create_outline: {str(e)}", exc_info=True)
        return jsonify({
            "code": 1,
            "message": str(e),
            "data": None
        }), 500


# 内容生成接口
@app.route('/generate_content', methods=['POST'])
async def generate_content():
    async with BiddingWorkflow() as workflow:
        try:
            workflow.load_input_files()

            # 加载本地大纲（强化：处理文件不存在）
            outline_dir = Config.OUTLINE_DIR if hasattr(Config, 'OUTLINE_DIR') else 'outputs/outline'
            outline_json_path = os.path.join(outline_dir, 'outline.json')
            try:
                with open(outline_json_path, 'r', encoding='utf-8') as f:
                    outline_dict = json.load(f)
                    workflow.outline = workflow.parse_outline_json(outline_dict)
            except FileNotFoundError:
                logger.error(f"大纲文件未找到，无法生成内容：{outline_json_path}")
                return jsonify({
                    'success': False,
                    'msg': '大纲文件不存在，请先生成大纲',
                    'data': {}
                }), 500

            # 生成完整内容
            success = await workflow.generate_full_content_async()
            if success:
                # 新增：读取生成的content.md内容返回给前端
                content_md_path = os.path.join('outputs', 'content.md')
                try:
                    with open(content_md_path, 'r', encoding='utf-8') as f:
                        content_md = f.read()
                except FileNotFoundError:
                    content_md = ""
                    logger.warning(f"内容文件未找到：{content_md_path}")

                return jsonify({
                    'success': True,
                    'msg': '内容生成成功',
                    'data': {
                        'content_md': content_md,  # 返回真实内容
                        'content_path': content_md_path
                    }
                })
            else:
                return jsonify({
                    'success': False,
                    'msg': 'Content generation failed',
                    'data': {}
                }), 500
        except Exception as e:
            logger.error(f"Error in generate_content: {str(e)}", exc_info=True)
            return jsonify({
                'success': False,
                'msg': f'内容生成失败：{str(e)}',
                'data': {}
            }), 500


# 终稿生成接口（核心修复：读取本地已生成的content.md，返回真实内容）
@app.route('/generate_document', methods=['POST'])
async def generate_document():
    async with BiddingWorkflow() as workflow:
        try:
            workflow.load_input_files()

            # 1. 先生成并保存大纲（依赖大纲）
            outline_json = await workflow.generate_outline()
            if not outline_json:
                return jsonify({
                    'success': False,
                    'msg': '先生成大纲失败，无法生成终稿',
                    'data': {}
                }), 500
            workflow.outline = workflow.parse_outline_json(outline_json)
            workflow.save_outline()

            # 2. 生成终稿内容
            generate_success = await workflow.generate_full_content_async()
            if not generate_success:
                return jsonify({
                    'success': False,
                    'msg': '终稿内容生成失败',
                    'data': {}
                }), 500

            # 3. 核心修复：读取日志中实际保存的content.md文件（而非依赖workflow属性）
            # 日志显示文件保存在 outputs/content.md，直接读取这个文件
            content_md_path = os.path.join('outputs', 'content.md')
            if not os.path.exists(content_md_path):
                logger.error(f"终稿文件未找到：{content_md_path}")
                return jsonify({
                    'success': False,
                    'msg': '终稿文件生成但未找到，请检查路径',
                    'data': {}
                }), 500

            # 读取真实终稿内容（UTF-8编码，确保中文正常）
            with open(content_md_path, 'r', encoding='utf-8') as f:
                full_document_content = f.read()
            simple_content = full_document_content[
                                 :1000] + f"\n\n...（内容过长，完整内容请查看本地文件：{content_md_path}）"

            # 4. 额外保存一份到document目录（兜底）
            document_dir = pathlib.Path('outputs/document')
            document_dir.mkdir(parents=True, exist_ok=True)
            backup_md_path = document_dir / 'document.md'
            with open(backup_md_path, 'w', encoding='utf-8') as f:
                f.write(full_document_content)

            # 5. 返回真实内容给前端（核心：解决“无返回内容”问题）
            return jsonify({
                'success': True,
                'msg': '终稿生成成功（文件已保存到outputs/content.md）',
                'data': {
                    'document_content': full_document_content,  # 真实的终稿内容字符串
                    'simple_content': simple_content,           # 简化内容（供前端渲染）
                    'backup_content_path': str(backup_md_path),  # 备份文件路径
                    'content_length': len(full_document_content)  # 内容长度，验证完整性
                }
            })

        except Exception as e:
            logger.error(f"Error generating document: {e}", exc_info=True)
            return jsonify({
                'success': False,
                'msg': f'终稿生成失败：{str(e)}',
                'data': {}
            }), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5001)