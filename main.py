"""
AstrBot Gotify消息同步插件

企业级的Gotify消息到QQ的同步推送插件。
支持实时消息同步、消息过滤、格式化、重试机制等功能。
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api import logger

# 导入插件模块
from .config import GotifyConfig, get_config
from .core import GotifyClient, MessageHandler, QQPusher
from .utils import setup_logging, get_logger, DataStorage, MessageHistory
from .utils.security import sanitize_message


@register(
    "gotify_sync",
    "AstrBot-Gotify-Plugin",
    "企业级Gotify消息同步推送插件，支持实时消息同步、消息过滤、格式化等功能",
    "1.0.5"
)
class GotifySyncPlugin(Star):
    """Gotify消息同步插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.astrbot_config = config  # AstrBot配置对象
        self.logger = get_logger(__name__)
        self.config: Optional[GotifyConfig] = None
        self.storage: Optional[DataStorage] = None
        self.message_history: Optional[MessageHistory] = None
        self.gotify_client: Optional[GotifyClient] = None
        self.message_handler: Optional[MessageHandler] = None
        self.qq_pusher: Optional[QQPusher] = None
        self._client_task: Optional[asyncio.Task] = None
        self._retry_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """插件初始化"""
        self.logger.info("初始化Gotify同步插件")

        try:
            # 加载配置
            await self._load_config()

            # 设置日志
            setup_logging(
                level=self.config.logging.level,
                log_format=self.config.logging.format,
                log_file=str(Path(self.config.storage.data_dir) / "gotify_plugin.log")
            )

            # 初始化存储
            self._init_storage()

            # 初始化组件
            await self._init_components()

            # 注册事件回调
            self._setup_callbacks()

            # 启动服务
            await self._start_services()

            self.logger.info("Gotify同步插件初始化完成")

        except Exception as e:
            self.logger.error(f"插件初始化失败: {e}")
            raise

    async def _load_config(self):
        """加载配置"""
        try:
            # 从AstrBot配置系统获取配置
            if self.astrbot_config:
                # 将AstrBot配置转换为GotifyConfig格式
                config_dict = {
                    "gotify": {
                        "server_url": self.astrbot_config.get("gotify", {}).get("server_url", "https://gotify.example.com"),
                        "app_token": self.astrbot_config.get("gotify", {}).get("app_token", ""),
                        "timeout": self.astrbot_config.get("gotify", {}).get("timeout", 30),
                        "heartbeat_interval": self.astrbot_config.get("gotify", {}).get("heartbeat_interval", 30),
                        "reconnect": self.astrbot_config.get("gotify", {}).get("reconnect", {
                            "enabled": True,
                            "max_attempts": 10,
                            "backoff_factor": 2,
                            "max_delay": 60
                        })
                    },
                    "qq": {
                        "target_users": self.astrbot_config.get("qq", {}).get("target_users", []),
                        "message_format": self.astrbot_config.get("qq", {}).get("message_format", {
                            "include_title": True,
                            "include_priority": True,
                            "include_timestamp": True,
                            "max_message_length": 2000
                        })
                    },
                    "message": self.astrbot_config.get("message", {
                        "deduplication": {"enabled": True, "window_seconds": 60},
                        "buffer": {"enabled": True, "batch_size": 10, "flush_interval": 5},
                        "filters": {"min_priority": 1, "blocked_app_ids": [], "blocked_titles": []}
                    }),
                    "storage": self.astrbot_config.get("storage", {
                        "data_dir": "./astrbot_plugin_gotify/data",
                        "max_log_size": "10MB",
                        "backup_count": 5
                    }),
                    "logging": self.astrbot_config.get("logging", {
                        "level": "INFO",
                        "format": "json"
                    })
                }
                self.config = GotifyConfig(**config_dict)
            else:
                # 回退到文件配置
                config_path = Path(__file__).parent / "config" / "default.json"
                self.config = get_config(str(config_path))

            self.logger.info(f"配置加载成功: Gotify服务器={self.config.gotify.server_url}")

        except Exception as e:
            self.logger.error(f"配置加载失败: {e}")
            # 创建默认配置
            self.config = GotifyConfig(
                gotify={
                    "server_url": "https://gotify.example.com",
                    "app_token": "your_app_token_here"
                },
                qq={
                    "target_users": ["24A91XXXXXXXXXXXXX"]  # 示例会话ID
                }
            )
            raise

    def _init_storage(self):
        """初始化存储"""
        self.storage = DataStorage(self.config.storage.data_dir)
        self.message_history = MessageHistory(self.storage)
        self.logger.info("存储系统初始化完成")

    async def _init_components(self):
        """初始化组件"""
        # 初始化消息处理器
        self.message_handler = MessageHandler(self.config, self.message_history)
        await self.message_handler.start()

        # 初始化QQ推送服务
        self.qq_pusher = QQPusher(self.config, self.storage, self.context)

        # 初始化Gotify客户端
        self.gotify_client = GotifyClient(self.config, self.storage)

        self.logger.info("核心组件初始化完成")

    def _setup_callbacks(self):
        """设置事件回调"""
        # Gotify连接事件回调
        self.gotify_client.set_callbacks(
            on_message=self._on_gotify_message,
            on_connect=self._on_gotify_connect,
            on_disconnect=self._on_gotify_disconnect
        )

        # 消息处理回调
        self.message_handler.set_processed_callback(self._on_message_processed)

    async def _start_services(self):
        """启动服务"""
        # 启动Gotify客户端
        self._client_task = asyncio.create_task(self.gotify_client.start())
        self.logger.info("Gotify客户端已启动")

        # 启动重试处理器
        await self.qq_pusher.start_retry_processor()
        self.logger.info("重试处理器已启动")

    async def _on_gotify_message(self, message_data):
        """Gotify消息回调"""
        self.logger.info(f"收到Gotify消息: ID={message_data.get('id')}")

        # 处理消息
        await self.message_handler.process_message(message_data)

    async def _on_gotify_connect(self):
        """Gotify连接回调"""
        self.logger.info("Gotify连接已建立")
        # 可以在这里发送连接成功通知

    async def _on_gotify_disconnect(self, error=None):
        """Gotify断开连接回调"""
        if error:
            self.logger.error(f"Gotify连接断开: {error}")
        else:
            self.logger.info("Gotify连接已断开")

    async def _on_message_processed(self, message_data, formatted_message):
        """消息处理完成回调"""
        self.logger.debug("消息处理完成，开始QQ推送")

        # 推送到QQ
        results = await self.qq_pusher.send_message(message_data, formatted_message)

        # 记录推送结果
        success_count = sum(1 for r in results if r.success)
        self.logger.info(f"QQ推送完成: 成功={success_count}/{len(results)}")

    # 插件指令处理
    @filter.command("gotify_status")
    async def gotify_status(self, event: AstrMessageEvent):
        """查看Gotify同步状态"""
        try:
            status_parts = ["📊 Gotify同步状态", "=" * 30]

            # Gotify客户端状态
            if self.gotify_client:
                client_status = self.gotify_client.get_status()
                status_parts.append(f"🔗 Gotify客户端:")
                status_parts.append(f"   运行状态: {'✅ 运行中' if client_status['is_running'] else '❌ 已停止'}")
                status_parts.append(f"   连接状态: {'✅ 已连接' if client_status['is_connected'] else '❌ 未连接'}")
                status_parts.append(f"   收到消息: {client_status['stats']['messages_received']}")
                if client_status['stats']['last_message_time']:
                    status_parts.append(f"   最后消息: {client_status['stats']['last_message_time']}")

            # 消息处理状态
            if self.message_handler:
                handler_stats = self.message_handler.get_stats()
                status_parts.append(f"\n📝 消息处理器:")
                status_parts.append(f"   收到消息: {handler_stats['messages_received']}")
                status_parts.append(f"   过滤消息: {handler_stats['messages_filtered']}")
                status_parts.append(f"   处理消息: {handler_stats['messages_processed']}")

                buffer_status = self.message_handler.get_buffer_status()
                if buffer_status['enabled']:
                    status_parts.append(f"   缓冲区: {buffer_status['size']}/{buffer_status['batch_size']}")

            # QQ推送状态
            if self.qq_pusher:
                pusher_status = self.qq_pusher.get_status()
                status_parts.append(f"\n📤 QQ推送服务:")
                status_parts.append(f"   目标用户: {len(pusher_status['target_users'])}")
                status_parts.append(f"   发送成功: {pusher_status['stats']['messages_sent']}")
                status_parts.append(f"   发送失败: {pusher_status['stats']['messages_failed']}")
                status_parts.append(f"   成功率: {pusher_status['stats']['success_rate']:.1f}%")
                status_parts.append(f"   重试队列: {pusher_status['retry_queue_size']}")

            status_text = '\n'.join(status_parts)
            yield event.plain_result(status_text)

        except Exception as e:
            self.logger.error(f"获取状态失败: {e}")
            yield event.plain_result(f"❌ 获取状态失败: {str(e)}")

    @filter.command("gotify_recent")
    async def gotify_recent(self, event: AstrMessageEvent):
        """查看最近的Gotify消息"""
        try:
            if not self.message_history:
                yield event.plain_result("❌ 消息历史未初始化")
                return

            limit = self._parse_command_limit(event, default=3)
            messages = self.message_history.get_recent_messages(limit=limit)

            if not messages:
                yield event.plain_result("📭 最近没有新的Gotify消息")
                return

            lines: List[str] = [
                f"🗒️ 最近Gotify消息（展示 {len(messages)} 条）",
                "=" * 30
            ]

            for idx, msg in enumerate(messages, 1):
                title = sanitize_message(msg.get('title') or "无标题")
                content = sanitize_message(msg.get('message') or "").replace('\r', ' ').replace('\n', ' ')
                if len(content) > 120:
                    content = content[:117] + "..."

                created_at = self._format_timestamp(msg.get('created_at') or msg.get('received_at'))
                priority = msg.get('priority', 5)
                status_icon = "✅" if msg.get('qq_sent') else "⏳"
                gotify_id = msg.get('gotify_id', msg.get('id', 'N/A'))

                lines.append(f"{idx}. {status_icon} [{created_at}] P{priority} {title}")
                lines.append(f"   ID: {gotify_id}")
                if content:
                    lines.append(f"   💬 {content}")

            yield event.plain_result('\n'.join(lines))

        except Exception as e:
            self.logger.error(f"获取最近消息失败: {e}")
            yield event.plain_result(f"❌ 获取最近消息失败: {str(e)}")

    @filter.command("gotify_flush")
    async def gotify_flush(self, event: AstrMessageEvent):
        """手动刷新消息缓冲区"""
        try:
            if not self.message_handler:
                yield event.plain_result("❌ 消息处理器未初始化")
                return

            await self.message_handler.flush_buffer()
            yield event.plain_result("✅ 消息缓冲区已刷新")

        except Exception as e:
            self.logger.error(f"刷新缓冲区失败: {e}")
            yield event.plain_result(f"❌ 刷新缓冲区失败: {str(e)}")

    @filter.command("gotify_retry")
    async def gotify_retry(self, event: AstrMessageEvent):
        """手动处理重试队列"""
        try:
            if not self.qq_pusher:
                yield event.plain_result("❌ QQ推送服务未初始化")
                return

            await self.qq_pusher.process_retry_queue()

            retry_status = self.qq_pusher.get_retry_queue_status()
            yield event.plain_result(f"✅ 重试队列已处理，剩余任务: {retry_status['size']}")

        except Exception as e:
            self.logger.error(f"处理重试队列失败: {e}")
            yield event.plain_result(f"❌ 处理重试队列失败: {str(e)}")

    async def terminate(self):
        """插件销毁"""
        self.logger.info("正在停止Gotify同步插件")

        try:
            # 停止重试处理器
            if self.qq_pusher:
                await self.qq_pusher.stop_retry_processor()

            # 停止Gotify客户端
            if self.gotify_client:
                await self.gotify_client.stop()

            if self.message_handler:
                await self.message_handler.stop()

            # 取消任务
            if self._client_task and not self._client_task.done():
                self._client_task.cancel()
                try:
                    await self._client_task
                except asyncio.CancelledError:
                    pass

            self.logger.info("Gotify同步插件已停止")

        except Exception as e:
            self.logger.error(f"插件停止时出错: {e}")

    def _extract_command_arguments(self, event: AstrMessageEvent) -> List[str]:
        """提取指令参数"""
        possible_attrs = ["command_args", "args"]
        for attr in possible_attrs:
            value = getattr(event, attr, None)
            if value:
                if isinstance(value, (list, tuple)):
                    args = [str(v).strip() for v in value if str(v).strip()]
                else:
                    args = [str(value).strip()]
                if args:
                    return args

        text = ""
        get_plain = getattr(event, "get_plain_text", None)
        if callable(get_plain):
            try:
                text = get_plain() or ""
            except Exception:
                text = ""
        if not text:
            text = str(getattr(event, "text_content", "") or "")

        text = text.strip()
        if not text:
            return []

        parts = text.split()
        if len(parts) <= 1:
            return []

        return [p for p in parts[1:] if p]

    def _parse_command_limit(self, event: AstrMessageEvent, default: int = 3) -> int:
        """解析命令中的条数参数"""
        limit = default
        args = self._extract_command_arguments(event)
        if args:
            try:
                limit = int(args[0])
            except ValueError:
                self.logger.warning(f"指令参数无法解析为整数: {args[0]}")
        return max(1, min(20, limit))

    def _format_timestamp(self, ts_value) -> str:
        """格式化时间戳"""
        if not ts_value:
            return "--"
        try:
            if isinstance(ts_value, str):
                dt = datetime.fromisoformat(ts_value.replace('Z', '+00:00'))
            else:
                dt = ts_value
            return dt.strftime('%m-%d %H:%M')
        except Exception:
            return str(ts_value)
