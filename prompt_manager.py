# prompt_manager.py
import json
from pathlib import Path
from prompts import Prompts  # 导入原始默认提示词（用于重置）

class PromptManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.default_prompts = self._get_default_prompts()  # 原始默认值
        self.user_prompts = self._load_user_prompts()      # 用户修改后的值

    def _get_default_prompts(self) -> dict:
        """从prompts.py提取所有默认提示词"""
        return {
            key: value for key, value in Prompts.__dict__.items()
            if not key.startswith("__") and isinstance(value, str)
        }

    def _load_user_prompts(self) -> dict:
        """加载用户配置（首次运行/文件损坏时自动初始化配置文件）"""
        try:
            if not self.config_path.exists():
                # 初始化配置文件（写入默认值）
                self._init_default_config()
                return self.default_prompts
            # 加载已有配置
            with open(self.config_path, "r", encoding="utf-8") as f:
                # 尝试解析JSON
                user_prompts = json.load(f)
                # 校验配置完整性（补充缺失的默认字段）
                for key, value in self.default_prompts.items():
                    if key not in user_prompts:
                        user_prompts[key] = value
                # 确保CUSTOM_PROMPTS字段存在
                if "CUSTOM_PROMPTS" not in user_prompts:
                    user_prompts["CUSTOM_PROMPTS"] = {}
                return user_prompts
        except (json.JSONDecodeError, Exception) as e:
            # 解析失败/其他异常：打印错误信息，重新初始化配置文件
            print(f"提示词配置文件损坏或格式错误：{e}，将自动重建默认配置")
            self._init_default_config()
            return self.default_prompts

    def _init_default_config(self):
        """初始化默认配置文件"""
        # 确保config目录存在
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        # 写入默认配置
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.default_prompts, f, ensure_ascii=False, indent=2)
        # 补充CUSTOM_PROMPTS字段（若不存在）
        if "CUSTOM_PROMPTS" not in self.default_prompts:
            user_prompts = self.default_prompts.copy()
            user_prompts["CUSTOM_PROMPTS"] = {}
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(user_prompts, f, ensure_ascii=False, indent=2)

    def save_prompt(self, key: str, content: str) -> None:
        """保存单个提示词（支持新增/修改）"""
        self.user_prompts[key] = content
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.user_prompts, f, ensure_ascii=False, indent=2)

    def delete_prompt(self, key: str) -> bool:
        """删除自定义提示词（不允许删除系统默认提示词）"""
        if key in self.default_prompts:
            return False  # 系统提示词不允许删除
        if key in self.user_prompts.get("CUSTOM_PROMPTS", {}):
            del self.user_prompts["CUSTOM_PROMPTS"][key]
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.user_prompts, f, ensure_ascii=False, indent=2)
            return True
        return False

    def reset_prompt(self, key: str) -> None:
        """将提示词重置为默认值"""
        if key in self.default_prompts:
            self.user_prompts[key] = self.default_prompts[key]
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.user_prompts, f, ensure_ascii=False, indent=2)

    def get_all_prompts(self) -> dict:
        """获取所有提示词（系统默认+用户自定义）"""
        return {
            "system": {k: self.user_prompts[k] for k in self.default_prompts},
            "custom": self.user_prompts.get("CUSTOM_PROMPTS", {})
        }

    def get_prompt(self, key: str) -> str:
        """根据key获取提示词内容"""
        if key in self.default_prompts:
            return self.user_prompts.get(key, self.default_prompts[key])
        return self.user_prompts.get("CUSTOM_PROMPTS", {}).get(key, "")