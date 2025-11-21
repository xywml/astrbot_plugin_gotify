# AstrBot Gotify消息同步插件

企业级的Gotify消息到QQ的同步推送插件，支持实时消息同步、消息过滤、格式化、重试机制等功能。

## 🚀 功能特性

### 核心功能
- **实时消息同步**: 通过WebSocket实时接收Gotify消息
- **QQ推送**: 将Gotify消息推送到指定QQ用户
- **消息去重**: 防止重复消息推送
- **消息过滤**: 支持优先级、应用ID、标题过滤
- **消息格式化**: 自定义消息格式和长度限制
- **批量处理**: 支持消息缓冲和批量推送

### 企业级特性
- **异步架构**: 基于asyncio的高性能异步处理
- **自动重连**: WebSocket断线自动重连，支持指数退避
- **重试机制**: QQ推送失败自动重试
- **数据持久化**: SQLite数据库存储消息历史和统计信息
- **结构化日志**: 支持JSON和文本格式日志
- **配置管理**: 符合AstrBot标准的配置文件支持
- **监控统计**: 详细的运行状态和统计信息
- **安全防护**: 输入验证、XSS防护、SQL注入防护

## 📦 安装

### 1. 安装依赖
```bash
pip install -r requirements.txt
```

### 2. 配置插件
插件使用AstrBot标准配置系统，主要配置项包括：

**必需配置：**
- `gotify.server_url`: Gotify服务器地址 (必需)
- `gotify.app_token`: Gotify应用Token (必需)
- `qq.target_users`: 目标QQ号列表 (必需)

**可选配置：**
- `gotify.timeout`: 连接超时时间 (默认30秒)
- `gotify.heartbeat_interval`: 心跳间隔 (默认30秒)
- `gotify.reconnect`: 自动重连配置
- `qq.message_format`: 消息格式配置
- `message.filters`: 消息过滤规则
- `message.deduplication`: 消息去重设置
- `message.buffer`: 消息缓冲配置

### 3. 配置示例

通过AstrBot管理面板或配置文件设置：

```json
{
  "gotify": {
    "server_url": "https://your-gotify-server.com",
    "app_token": "your_gotify_app_token_here"
  },
  "qq": {
    "target_users": [
      "123456789",
      "987654321"
    ]
  }
}
```

## ⚙️ 配置说明

### Gotify服务器配置
- `server_url`: Gotify服务器完整URL
- `app_token`: 在Gotify管理面板创建应用时获得的Token
- `timeout`: WebSocket连接超时时间(秒)
- `heartbeat_interval`: 心跳检测间隔(秒)
- `reconnect`: 自动重连设置
  - `enabled`: 是否启用自动重连
  - `max_attempts`: 最大重试次数
  - `backoff_factor`: 重试间隔指数增长因子
  - `max_delay`: 最大重试延迟(秒)

### QQ推送配置
- `target_users`: 接收消息的QQ号列表，支持多个
- `message_format`: 消息显示格式
  - `include_title`: 是否包含消息标题
  - `include_priority`: 是否显示优先级
  - `include_timestamp`: 是否包含时间戳
  - `max_message_length`: 最大消息长度

### 消息处理配置
- `filters`: 消息过滤规则
  - `min_priority`: 最低优先级阈值(1-10)
  - `blocked_app_ids`: 屏蔽的应用ID列表
  - `blocked_titles`: 屏蔽的关键词标题列表
- `deduplication`: 去重设置
  - `enabled`: 是否启用去重
  - `window_seconds`: 去重时间窗口(秒)
- `buffer`: 批量处理设置
  - `enabled`: 是否启用消息缓冲
  - `batch_size`: 批量处理大小
  - `flush_interval`: 刷新间隔(秒)

## 🔧 使用方法

### 插件命令

#### `/gotify_status`
查看插件运行状态：
```
📊 Gotify同步状态
==============================
🔗 Gotify客户端:
   运行状态: ✅ 运行中
   连接状态: ✅ 已连接
   收到消息: 15
   最后消息: 2024-01-01T12:00:00

📝 消息处理器:
   收到消息: 15
   过滤消息: 2
   处理消息: 13

📤 QQ推送服务:
   目标用户: 2
   发送成功: 26
   发送失败: 0
   成功率: 100.0%
   重试队列: 0
```

#### `/gotify_flush`
手动刷新消息缓冲区

#### `/gotify_retry`
手动处理重试队列

## 🏗️ 架构设计

### 模块结构
```
astrbot_plugin_gotify/
├── main.py                 # 插件入口点
├── metadata.yaml          # 插件元数据
├── _conf_schema.json      # AstrBot配置Schema
├── requirements.txt       # 依赖管理
├── config/
│   ├── __init__.py
│   ├── settings.py        # 配置模型和验证
│   └── default.json       # 默认配置
├── core/
│   ├── __init__.py
│   ├── gotify_client.py   # Gotify WebSocket客户端
│   ├── message_handler.py # 消息处理器
│   └── qq_pusher.py      # QQ推送服务
├── utils/
│   ├── __init__.py
│   ├── logger.py         # 日志工具
│   ├── storage.py        # 数据持久化
│   └── security.py       # 安全工具
└── data/                  # 数据存储目录
```

### 核心组件

1. **GotifyClient**: WebSocket客户端，负责与Gotify服务器建立连接和接收消息
2. **MessageHandler**: 消息处理器，负责过滤、格式化和去重
3. **QQPusher**: QQ推送服务，负责将消息推送到指定QQ用户
4. **DataStorage**: 数据存储管理器，使用SQLite存储历史数据
5. **ConfigManager**: 配置管理器，支持AstrBot标准配置

### 数据流程
```
Gotify服务器 → WebSocket → GotifyClient → MessageHandler → QQPusher → QQ用户
                         ↓
                    DataStorage ← 数据持久化
```

## 📊 监控和日志

### 日志文件
- `data/gotify_plugin.log`: 主日志文件
- 支持日志轮转，默认最大10MB，保留5个备份

### 数据库表
- `message_history`: 消息历史记录
- `connection_status`: 连接状态记录
- `statistics`: 统计信息

### 监控指标
- 消息接收数量
- 消息处理成功率
- QQ推送成功率
- 连接状态
- 重试队列长度

## 🔒 安全特性

- **输入验证**: 严格的输入数据验证和清理
- **XSS防护**: 移除潜在的XSS攻击代码
- **SQL注入防护**: 使用参数化查询
- **Token加密**: 敏感信息加密存储
- **访问控制**: 支持QQ号白名单

## 🚨 故障处理

### 常见问题

1. **连接失败**
   - 检查Gotify服务器地址和Token
   - 确认网络连接正常
   - 查看日志文件获取详细错误信息

2. **消息推送失败**
   - 检查QQ号格式是否正确
   - 确认AstrBot机器人运行正常
   - 查看重试队列状态

3. **配置错误**
   - 验证必需配置项是否完整
   - 检查Gotify服务器URL格式
   - 确认App Token有效性

### 调试模式
设置日志级别为DEBUG以获取详细信息：
```json
{
  "logging": {
    "level": "DEBUG",
    "format": "json"
  }
}
```

## 🤝 贡献

欢迎提交Issue和Pull Request！

## 📄 许可证

MIT License

## 🔗 相关链接

- [AstrBot文档](https://astrbot.app)
- [Gotify官方文档](https://gotify.net/)
- [WebSocket API文档](https://gotify.net/docs/stream)
