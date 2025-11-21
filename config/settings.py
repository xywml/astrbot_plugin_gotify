"""
配置设置模型定义

使用Pydantic进行配置验证和管理。
支持从文件和环境变量加载配置。
"""

import os
import json
from pathlib import Path
from typing import List, Optional, Dict, Any, Union
from pydantic import BaseModel, Field, validator, root_validator
from pydantic_settings import BaseSettings


class ReconnectConfig(BaseModel):
    """重连配置"""
    enabled: bool = True
    max_attempts: int = Field(default=10, ge=1, le=100)
    backoff_factor: int = Field(default=2, ge=1, le=10)
    max_delay: int = Field(default=60, ge=5, le=300)


class GotifyServerConfig(BaseModel):
    """Gotify服务器配置"""
    server_url: str = Field(..., description="Gotify服务器URL")
    app_token: str = Field(..., description="应用Token")
    timeout: int = Field(default=30, ge=5, le=300, description="连接超时时间(秒)")
    heartbeat_interval: int = Field(default=30, ge=10, le=300, description="心跳间隔(秒)")
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)

    @validator('server_url')
    def validate_server_url(cls, v):
        if not v.startswith(('http://', 'https://')):
            raise ValueError('server_url必须以http://或https://开头')
        return v.rstrip('/')

    @validator('app_token')
    def validate_app_token(cls, v):
        if not v or len(v) < 10:
            raise ValueError('app_token不能为空且长度应至少为10个字符')
        return v


class MessageFormatConfig(BaseModel):
    """消息格式配置"""
    include_title: bool = True
    include_priority: bool = True
    include_timestamp: bool = True
    max_message_length: int = Field(default=2000, ge=100, le=10000)


class QQConfig(BaseModel):
    """QQ推送配置"""
    target_users: List[str] = Field(default_factory=list, description="目标QQ号列表")
    message_format: MessageFormatConfig = Field(default_factory=MessageFormatConfig)

    @validator('target_users')
    def validate_target_users(cls, v):
        if not v:
            raise ValueError('target_users不能为空，至少需要指定一个QQ号')
        # 简单的QQ号格式验证
        for qq in v:
            if not qq.isdigit() or len(qq) < 5 or len(qq) > 12:
                raise ValueError(f'无效的QQ号格式: {qq}')
        return v


class DeduplicationConfig(BaseModel):
    """消息去重配置"""
    enabled: bool = True
    window_seconds: int = Field(default=60, ge=10, le=3600)


class MessageBufferConfig(BaseModel):
    """消息缓冲配置"""
    enabled: bool = True
    batch_size: int = Field(default=10, ge=1, le=100)
    flush_interval: int = Field(default=5, ge=1, le=60)


class MessageFiltersConfig(BaseModel):
    """消息过滤配置"""
    min_priority: int = Field(default=1, ge=1, le=10)
    blocked_app_ids: List[int] = Field(default_factory=list)
    blocked_titles: List[str] = Field(default_factory=list)


class MessageConfig(BaseModel):
    """消息处理配置"""
    deduplication: DeduplicationConfig = Field(default_factory=DeduplicationConfig)
    buffer: MessageBufferConfig = Field(default_factory=MessageBufferConfig)
    filters: MessageFiltersConfig = Field(default_factory=MessageFiltersConfig)


class StorageConfig(BaseModel):
    """存储配置"""
    data_dir: str = Field(default="./data", description="数据存储目录")
    max_log_size: str = Field(default="10MB", description="最大日志文件大小")
    backup_count: int = Field(default=5, ge=1, le=50, description="日志备份数量")

    @validator('data_dir')
    def validate_data_dir(cls, v):
        # 确保数据目录路径存在
        Path(v).mkdir(parents=True, exist_ok=True)
        return v


class LoggingConfig(BaseModel):
    """日志配置"""
    level: str = Field(default="INFO", description="日志级别")
    format: str = Field(default="json", description="日志格式")

    @validator('level')
    def validate_level(cls, v):
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if v.upper() not in valid_levels:
            raise ValueError(f'日志级别必须是以下之一: {valid_levels}')
        return v.upper()

    @validator('format')
    def validate_format(cls, v):
        if v not in ['json', 'text']:
            raise ValueError('日志格式必须是json或text')
        return v


class GotifyConfig(BaseSettings):
    """Gotify插件主配置"""

    # 配置文件路径
    config_file: Optional[str] = None

    # 各模块配置
    gotify: GotifyServerConfig
    qq: QQConfig
    message: MessageConfig = Field(default_factory=MessageConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    class Config:
        env_file = ".env"
        env_nested_delimiter = "__"
        case_sensitive = False

    @classmethod
    def load_from_file(cls, config_file: Optional[str] = None) -> "GotifyConfig":
        """从配置文件加载配置"""
        if config_file is None:
            config_file = os.environ.get('GOTIFY_CONFIG_FILE', 'config/default.json')

        config_path = Path(config_file)
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)

        # 从环境变量覆盖配置
        env_config = {}
        if os.getenv('GOTIFY_SERVER_URL'):
            env_config['gotify'] = env_config.get('gotify', {})
            env_config['gotify']['server_url'] = os.getenv('GOTIFY_SERVER_URL')

        if os.getenv('GOTIFY_APP_TOKEN'):
            env_config['gotify'] = env_config.get('gotify', {})
            env_config['gotify']['app_token'] = os.getenv('GOTIFY_APP_TOKEN')

        if os.getenv('QQ_TARGET_USERS'):
            env_config['qq'] = env_config.get('qq', {})
            qq_users = [qq.strip() for qq in os.getenv('QQ_TARGET_USERS').split(',')]
            env_config['qq']['target_users'] = qq_users

        # 合并配置
        if env_config:
            config_data.update(env_config)

        return cls(**config_data)

    def save_to_file(self, config_file: Optional[str] = None):
        """保存配置到文件"""
        if config_file is None:
            config_file = self.config_file or 'config/default.json'

        config_path = Path(config_file)
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # 转换为字典并隐藏敏感信息
        config_dict = self.dict()
        if 'gotify' in config_dict and 'app_token' in config_dict['gotify']:
            token = config_dict['gotify']['app_token']
            if len(token) > 8:
                config_dict['gotify']['app_token'] = token[:4] + '*' * (len(token) - 8) + token[-4:]

        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config_dict, f, ensure_ascii=False, indent=2)


def get_config(config_file: Optional[str] = None) -> GotifyConfig:
    """获取配置实例"""
    try:
        return GotifyConfig.load_from_file(config_file)
    except FileNotFoundError:
        # 如果配置文件不存在，创建默认配置
        logger.warning("配置文件不存在，创建默认配置文件")
        default_config = GotifyConfig(
            gotify=GotifyServerConfig(
                server_url="https://gotify.example.com",
                app_token="your_app_token_here"
            ),
            qq=QQConfig(target_users=["123456789"])
        )
        default_config.save_to_file(config_file)
        return default_config