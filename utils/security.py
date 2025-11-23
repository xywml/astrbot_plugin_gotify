"""
安全工具模块

提供加密解密、输入验证等安全功能。
"""

import hashlib
import secrets
from typing import Optional, Dict, Any
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64
import re


class SecurityManager:
    """安全管理器"""

    def __init__(self, password: Optional[str] = None):
        """初始化安全管理器

        Args:
            password: 用于加密的密码，如果为None则使用随机密码
        """
        if password is None:
            password = secrets.token_urlsafe(32)

        self.password = password.encode()
        self.salt = b'gotify_plugin_salt'  # 在实际应用中应该使用随机salt并存储

        # 生成加密密钥
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self.salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(self.password))
        self.cipher = Fernet(key)

    def encrypt(self, data: str) -> str:
        """加密数据"""
        encrypted_data = self.cipher.encrypt(data.encode())
        return base64.urlsafe_b64encode(encrypted_data).decode()

    def decrypt(self, encrypted_data: str) -> str:
        """解密数据"""
        try:
            encrypted_bytes = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted_data = self.cipher.decrypt(encrypted_bytes)
            return decrypted_data.decode()
        except Exception:
            raise ValueError("解密失败：无效的加密数据或错误的密钥")

    def hash_data(self, data: str) -> str:
        """计算数据哈希值"""
        return hashlib.sha256(data.encode()).hexdigest()


# 全局安全管理器实例
_security_manager = SecurityManager()


def encrypt_token(token: str) -> str:
    """加密Token"""
    return _security_manager.encrypt(token)


def decrypt_token(encrypted_token: str) -> str:
    """解密Token"""
    return _security_manager.decrypt(encrypted_token)


def validate_input(input_data: str, max_length: int = 10000) -> bool:
    """验证输入数据

    Args:
        input_data: 输入数据
        max_length: 最大长度限制

    Returns:
        bool: 验证是否通过
    """
    if not isinstance(input_data, str):
        return False

    if len(input_data) > max_length:
        return False

    # 检查潜在的XSS攻击
    xss_patterns = [
        r'<script[^>]*>.*?</script>',
        r'javascript:',
        r'vbscript:',
        r'onload\s*=',
        r'onerror\s*=',
        r'onclick\s*=',
    ]

    for pattern in xss_patterns:
        if re.search(pattern, input_data, re.IGNORECASE):
            return False

    # 检查SQL注入模式
    sql_patterns = [
        r'(union|select|insert|update|delete|drop|create|alter)\s',
        r'--',
        r'/\*.*?\*/',
        r'\'\s*or\s*\'\d+\'\s*=\s*\'\d+',
        r'"\s*or\s*"\d+"\s*=\s*"\d+',
    ]

    for pattern in sql_patterns:
        if re.search(pattern, input_data, re.IGNORECASE):
            return False

    return True


def sanitize_message(message: str) -> str:
    """清理消息内容，移除潜在的有害内容"""
    if not isinstance(message, str):
        return ""

    # 移除控制字符（保留换行符和制表符）
    message = ''.join(char for char in message if char.isprintable() or char in '\n\t')

    # 限制长度
    if len(message) > 10000:
        message = message[:10000] + "..."

    return message.strip()


def validate_session_id(session_id: str) -> bool:
    """验证AstrBot会话ID格式"""
    if not isinstance(session_id, str):
        return False

    session_id = session_id.strip()
    if not session_id:
        return False

    # 支持Session ID（如：24A91XXXXXXXXXXXXX）
    # 或UMO格式（如：小兮:FriendMessage:24A91XXXXXXXXXXXXX）
    if ':' in session_id:
        # UMO格式验证
        parts = session_id.split(':')
        return len(parts) >= 3 and all(part.strip() for part in parts)
    else:
        # Session ID格式验证（至少8位字符，支持字母数字）
        return len(session_id) >= 8 and bool(re.match(r'^[a-zA-Z0-9]+$', session_id))


def validate_qq_number(qq: str) -> bool:
    """验证QQ号格式（已弃用，保留兼容性）"""
    if not isinstance(qq, str):
        return False

    return bool(re.match(r'^\d{5,12}$', qq))


def validate_gotify_url(url: str) -> bool:
    """验证Gotify服务器URL格式"""
    if not isinstance(url, str):
        return False

    url_pattern = r'^https?://[a-zA-Z0-9.-]+(?:\:[0-9]+)?(?:/.*)?$'
    return bool(re.match(url_pattern, url))


def generate_message_id(message_data: Dict[str, Any]) -> str:
    """生成消息唯一ID"""
    # 使用消息的关键信息生成哈希值作为ID
    content = f"{message_data.get('id', '')}{message_data.get('title', '')}{message_data.get('message', '')}{message_data.get('created_at', '')}"
    return _security_manager.hash_data(content)[:16]


class InputValidator:
    """输入验证器"""

    @staticmethod
    def validate_gotify_message(message_data: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """验证Gotify消息格式"""
        required_fields = ['id', 'message']

        for field in required_fields:
            if field not in message_data:
                return False, f"缺少必需字段: {field}"

        # 验证消息ID
        if not isinstance(message_data['id'], int) or message_data['id'] <= 0:
            return False, "无效的消息ID"

        # 验证消息内容
        if not isinstance(message_data['message'], str) or not message_data['message'].strip():
            return False, "消息内容不能为空"

        # 验证优先级
        if 'priority' in message_data:
            priority = message_data['priority']
            if not isinstance(priority, int) or priority < 1 or priority > 10:
                return False, "优先级必须是1-10之间的整数"

        # 验证标题
        if 'title' in message_data and message_data['title'] is not None:
            title = message_data['title']
            if not isinstance(title, str):
                return False, "标题必须是字符串"
            if len(title) > 200:
                return False, "标题长度不能超过200字符"

        # 验证应用ID
        if 'appid' in message_data:
            appid = message_data['appid']
            if not isinstance(appid, int) or appid <= 0:
                return False, "无效的应用ID"

        return True, None