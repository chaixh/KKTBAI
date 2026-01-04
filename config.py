from pathlib import Path
import logging

class Config:
    BASE_DIR = Path(__file__).parent  # 修改为 bidding 目录
    
    # 输入输出路径配置
    INPUT_DIR = BASE_DIR / "inputs"  # bidding/inputs
    OUTPUT_DIR = BASE_DIR / "outputs"  # bidding/outputs
    OUTLINE_DIR = OUTPUT_DIR / "outline"  # bidding/outputs/outline
    LOG_DIR = BASE_DIR / "logs"  # bidding/logs
    
    # LLM 配置
    LLM_API_KEY = "d004e9b7-19e0-49e8-99ef-b89dfdc211ad"
    LLM_API_BASE = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    LLM_MODEL = "doubao-seed-1-6-251015"  # 火山引擎有效模型 ID（固定写法，无需修改）
    LLM_TIMEOUT = 300  # 超时时间足够
    
    MAX_RETRIES = 3
    MAX_TOKENS = 8192
    TEMPERATURE = 0.7
    TOP_P = 0.1
    TIMEOUT = 30
    
    # 重试配置
    RETRY_DELAY = 2
    RETRY_BACKOFF = 1.5
    
    # API 配置
    REQUEST_TIMEOUT = 30
    
    # 代理配置
    USE_PROXY = False  # 是否使用代理
    PROXY_URLS = {
        'http': "http://127.0.0.1:10808",
        'https': "http://127.0.0.1:10808"  # HTTPS 也使用 HTTP 代理
    }

# 修改日志级别为 DEBUG
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(Config.LOG_DIR / 'app.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
) 