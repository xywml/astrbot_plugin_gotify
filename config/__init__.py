"""
配置管理模块

提供插件配置的加载、验证和管理功能。
支持环境变量和配置文件的双重配置源。
"""

from .settings import GotifyConfig, get_config

__all__ = ["GotifyConfig", "get_config"]