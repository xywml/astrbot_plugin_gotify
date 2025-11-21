"""
Gotify WebSocket客户端模块

提供异步的Gotify WebSocket连接和消息接收功能。
包含自动重连、心跳保活等企业级特性。
"""

import asyncio
import json
import logging
import time
from typing import Optional, Callable, Dict, Any, List
from datetime import datetime

import aiohttp

from ..config import GotifyConfig
from ..utils.logger import get_logger, LogContext
from ..utils.storage import DataStorage
from ..utils.security import InputValidator


class ReconnectStrategy:
    """重连策略"""

    def __init__(self, max_attempts: int = 10, backoff_factor: int = 2, max_delay: int = 60):
        self.max_attempts = max_attempts
        self.backoff_factor = backoff_factor
        self.max_delay = max_delay

    def get_delay(self, attempt: int) -> float:
        """获取重连延迟时间"""
        delay = self.backoff_factor ** attempt
        return min(delay, self.max_delay)

    def should_reconnect(self, attempt: int) -> bool:
        """判断是否应该重连"""
        return attempt < self.max_attempts


class GotifyClient:
    """Gotify WebSocket客户端"""

    def __init__(self, config: GotifyConfig, storage: DataStorage):
        """初始化Gotify客户端

        Args:
            config: Gotify配置
            storage: 数据存储管理器
        """
        self.config = config
        self.storage = storage
        self.logger = get_logger(__name__)

        # 连接相关
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.is_running = False
        self.is_connected = False

        # 重连策略
        self.reconnect_strategy = ReconnectStrategy(
            max_attempts=config.gotify.reconnect.max_attempts,
            backoff_factor=config.gotify.reconnect.backoff_factor,
            max_delay=config.gotify.reconnect.max_delay
        )

        # 事件回调
        self.on_message_callback: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_connect_callback: Optional[Callable[[], None]] = None
        self.on_disconnect_callback: Optional[Callable[[Optional[str]], None]] = None

        # 统计信息
        self.stats = {
            'messages_received': 0,
            'connection_attempts': 0,
            'last_message_time': None,
            'connection_time': None
        }

        # 消息ID跟踪（用于去重）
        self.last_message_id = 0

    def set_callbacks(
        self,
        on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[Optional[str]], None]] = None
    ):
        """设置事件回调函数"""
        self.on_message_callback = on_message
        self.on_connect_callback = on_connect
        self.on_disconnect_callback = on_disconnect

    async def start(self):
        """启动WebSocket连接"""
        if self.is_running:
            self.logger.warning("Gotify客户端已经在运行")
            return

        self.is_running = True
        self.logger.info("启动Gotify WebSocket客户端")

        try:
            await self._connect_loop()
        except Exception as e:
            self.logger.error(f"Gotify客户端异常退出: {e}")
        finally:
            await self.stop()

    async def stop(self):
        """停止WebSocket连接"""
        self.is_running = False
        self.logger.info("停止Gotify WebSocket客户端")

        await self._close_connection()

        if self.session:
            await self.session.close()
            self.session = None

    async def _connect_loop(self):
        """连接循环"""
        reconnect_attempts = 0

        while self.is_running:
            try:
                self.stats['connection_attempts'] += 1
                await self._establish_connection()
                reconnect_attempts = 0  # 重置重连计数

                # 连接成功，开始消息循环
                await self._message_loop()

            except Exception as e:
                self.logger.error(f"连接异常: {e}")
                self.is_connected = False

                # 保存连接状态
                self.storage.save_connection_status(
                    "error",
                    str(e),
                    {"attempt": reconnect_attempts}
                )

                if not self.reconnect_strategy.should_reconnect(reconnect_attempts):
                    self.logger.error("达到最大重连次数，停止尝试")
                    break

                reconnect_attempts += 1
                delay = self.reconnect_strategy.get_delay(reconnect_attempts)

                self.logger.info(f"{delay}秒后尝试第{reconnect_attempts}次重连")
                await asyncio.sleep(delay)

    async def _establish_connection(self):
        """建立WebSocket连接"""
        await self._close_connection()

        # 创建HTTP会话
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=self.config.gotify.timeout)
            self.session = aiohttp.ClientSession(timeout=timeout)

        # 构建WebSocket URL
        ws_url = self.config.gotify.server_url.replace('http://', 'ws://').replace('https://', 'wss://')
        ws_url += f"/stream?token={self.config.gotify.app_token}"

        self.logger.info(f"连接到Gotify WebSocket: {ws_url}")

        try:
            # 建立WebSocket连接
            self.ws = await self.session.ws_connect(
                ws_url,
                heartbeat=self.config.gotify.heartbeat_interval,
                headers={
                    'User-Agent': 'AstrBot-Gotify-Plugin/1.0'
                }
            )

            self.is_connected = True
            self.stats['connection_time'] = datetime.now().isoformat()

            # 保存连接状态
            self.storage.save_connection_status("connected")

            # 调用连接回调
            if self.on_connect_callback:
                await self._safe_call(self.on_connect_callback)

            self.logger.info("Gotify WebSocket连接已建立")

            # 获取现有消息以确定起始点
            await self._get_existing_messages()

        except aiohttp.ClientError as e:
            self.logger.error(f"WebSocket连接失败: {e}")
            raise

    async def _get_existing_messages(self):
        """获取现有消息以确定消息ID起始点"""
        try:
            url = f"{self.config.gotify.server_url}/message"
            headers = {'Authorization': f'Bearer {self.config.gotify.app_token}'}

            async with self.session.get(url, headers=headers) as response:
                if response.status == 200:
                    messages = await response.json()
                    if messages:
                        self.last_message_id = max(msg['id'] for msg in messages)
                        self.logger.info(f"找到历史消息，最新消息ID: {self.last_message_id}")
                else:
                    self.logger.warning(f"获取历史消息失败: HTTP {response.status}")

        except Exception as e:
            self.logger.warning(f"获取历史消息异常: {e}")

    async def _message_loop(self):
        """消息接收循环"""
        if not self.ws:
            raise RuntimeError("WebSocket连接未建立")

        self.logger.info("开始消息接收循环")

        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    error = msg.data or "WebSocket错误"
                    self.logger.error(f"WebSocket错误: {error}")
                    raise RuntimeError(f"WebSocket错误: {error}")
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                    self.logger.info("WebSocket连接已关闭")
                    break

        except asyncio.CancelledError:
            self.logger.info("消息循环被取消")
        except Exception as e:
            self.logger.error(f"消息循环异常: {e}")
            raise

    async def _handle_message(self, message_data: str):
        """处理接收到的消息"""
        try:
            # 解析JSON消息
            data = json.loads(message_data)

            with LogContext(self.logger, message_id=data.get('id')):
                self.logger.debug(f"收到原始消息: {message_data}")

                # 验证消息格式
                is_valid, error_msg = InputValidator.validate_gotify_message(data)
                if not is_valid:
                    self.logger.warning(f"消息格式验证失败: {error_msg}")
                    return

                # 检查消息ID是否重复
                message_id = data.get('id', 0)
                if message_id <= self.last_message_id:
                    self.logger.debug(f"跳过旧消息: {message_id}")
                    return

                # 更新最后消息ID
                self.last_message_id = message_id

                # 更新统计信息
                self.stats['messages_received'] += 1
                self.stats['last_message_time'] = datetime.now().isoformat()

                # 添加时间戳
                data['received_at'] = datetime.now().isoformat()

                self.logger.info(f"收到新消息: ID={message_id}, 标题={data.get('title', 'N/A')}")

                # 保存消息到存储
                self.storage.save_message(data)

                # 调用消息回调
                if self.on_message_callback:
                    await self._safe_call(self.on_message_callback, data)

        except json.JSONDecodeError as e:
            self.logger.error(f"消息JSON解析失败: {e}")
        except Exception as e:
            self.logger.error(f"处理消息异常: {e}")

    async def _safe_call(self, callback: Callable, *args, **kwargs):
        """安全调用回调函数"""
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(*args, **kwargs)
            else:
                callback(*args, **kwargs)
        except Exception as e:
            self.logger.error(f"回调函数执行异常: {e}")

    async def _close_connection(self):
        """关闭当前连接"""
        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                self.logger.debug(f"关闭WebSocket连接异常: {e}")
            finally:
                self.ws = None

        self.is_connected = False

        # 保存连接状态
        self.storage.save_connection_status("disconnected")

        # 调用断开连接回调
        if self.on_disconnect_callback:
            await self._safe_call(self.on_disconnect_callback)

    def get_status(self) -> Dict[str, Any]:
        """获取客户端状态"""
        return {
            'is_running': self.is_running,
            'is_connected': self.is_connected,
            'stats': self.stats.copy(),
            'last_message_id': self.last_message_id,
            'reconnect_attempts': self.reconnect_strategy.max_attempts
        }

    async def send_message(self, title: str, message: str, priority: int = 5, extras: Optional[Dict[str, Any]] = None):
        """发送消息到Gotify（用于测试等用途）"""
        if not self.session:
            raise RuntimeError("客户端未启动")

        url = f"{self.config.gotify.server_url}/message?token={self.config.gotify.app_token}"
        data = {
            'title': title,
            'message': message,
            'priority': priority
        }

        if extras:
            data['extras'] = extras

        try:
            async with self.session.post(url, json=data) as response:
                if response.status == 200:
                    result = await response.json()
                    self.logger.info(f"消息发送成功: ID={result.get('id')}")
                    return result
                else:
                    error_text = await response.text()
                    self.logger.error(f"消息发送失败: HTTP {response.status}, {error_text}")
                    raise RuntimeError(f"消息发送失败: HTTP {response.status}")

        except aiohttp.ClientError as e:
            self.logger.error(f"消息发送异常: {e}")
            raise