# Tabbit2API

将 [Tabbit 浏览器](https://tabbitbrowser.com/) 封装为本地 OpenAI 兼容 API 代理，供 Codex、Claude Code、OpenCode 等 AI Agent 工具调用。

通过 Chrome DevTools Protocol (CDP) 连接已登录的 Tabbit 浏览器，利用 webpack 模块注入直接调用 Tabbit 内部聊天 API，**不依赖 UI 自动化**（不模拟打字、不点击按钮），稳定可靠。

## 工作原理

```
┌─────────────┐     HTTP      ┌──────────────┐     CDP/WebSocket     ┌─────────────┐
│  AI Agent   │ ──────────── │  Proxy (v3)  │ ───────────────────── │   Tabbit    │
│ (OpenCode)  │  OpenAI API  │  :9090       │  webpack injection    │  Browser    │
└─────────────┘              └──────────────┘                       └─────────────┘
```

1. Agent 发送 OpenAI 格式的 `/v1/chat/completions` 请求
2. 代理通过 CDP 连接 Tabbit，注入 JavaScript 代码
3. JS 代码通过 `webpackChunk_N_E` 获取 Tabbit 内部的 `sendMessage` 函数
4. 直接调用内部 API 发送消息，监听 `setMessages` 回调获取响应
5. 返回 OpenAI 兼容格式的 JSON 响应

**核心优势**：不依赖页面 DOM、不模拟用户操作、不拦截网络请求，直接调用内部模块，响应捕获率接近 100%。

## 快速开始

### 前置条件

- **macOS**（已测试）/ Windows / Linux
- **Tabbit 浏览器** 已安装并登录
- **Python 3.10+**
- **websocket-client** Python 包

### 安装

```bash
# 克隆仓库
git clone https://github.com/ken861222/tabbit2api.git
cd tabbit2api

# 安装依赖
pip3 install websocket-client
```

### 启动

```bash
# 第一步：启动 Tabbit 浏览器（带 CDP）
/Applications/Tabbit.app/Contents/MacOS/Tabbit \
  --remote-debugging-port=9222 \
  "--remote-allow-origins=*" \
  --no-first-run \
  --disable-gpu-compositing \
  --disable-extensions &

# 第二步：启动代理
python3 tabbit_proxy_v3.py
```

或使用一键启动脚本：

```bash
./start_tabbit.sh && python3 tabbit_proxy_v3.py
```

启动成功后会自动：
1. 连接 Tabbit CDP 端口
2. 导航到聊天页面
3. 动态发现 webpack 模块（`sendMessage`、`modes`）
4. 发送测试请求验证连通性
5. 在 `http://127.0.0.1:9090` 启动 HTTP 服务

### 验证

```bash
# 健康检查
curl http://127.0.0.1:9090/health

# 列出模型
curl -H "Authorization: Bearer sk-tabbit-local" http://127.0.0.1:9090/v1/models

# 发送消息
curl -X POST http://127.0.0.1:9090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"hello"}]}'
```

## API 接口

### `GET /health`

健康检查。

```json
{"status": "ok", "modules": true}
```

- `modules: true` 表示 webpack 模块已发现，代理可用

### `GET /v1/models`

列出可用模型（OpenAI 格式）。

### `POST /v1/chat/completions`

发送聊天请求。

**请求体**（OpenAI Chat Completions 格式）：

```json
{
  "model": "gpt-5.5",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "stream": false
}
```

**参数说明**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `model` | string | 模型名称，见下方模型列表 |
| `messages` | array | 消息数组，支持 `system`、`user`、`assistant` 角色 |
| `stream` | boolean | 是否流式输出，默认 `false` |

**响应**（OpenAI 格式）：

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1782108029,
  "model": "GPT-5.5",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "Hello! How can I help?"},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
```

## 模型列表

| 模型 ID | Tabbit 显示名 |
|---------|--------------|
| `gpt-5.5` | GPT-5.5 |
| `gpt-5.4` | GPT-5.4 |
| `gpt-5.2-chat` | GPT-5.2-Chat |
| `claude-opus-4.8` | Claude-Opus-4.8 |
| `claude-opus-4.7` | Claude-Opus-4.7 |
| `claude-sonnet-4.6` | Claude-Sonnet-4.6 |
| `claude-haiku-4.5` | Claude-Haiku-4.5 |
| `deepseek-v4-pro` | DeepSeek-V4-Pro |
| `deepseek-v4-flash` | DeepSeek-V4-Flash |
| `deepseek-v3.2` | DeepSeek-V3.2 |
| `gemini-3.5-flash` | Gemini-3.5-Flash |
| `gemini-3.1-pro` | Gemini-3.1-Pro |
| `minimax-m3` | MiniMax-M3 |
| `minimax-m2.7` | MiniMax-M2.7 |
| `kimi-k2.6` | Kimi-K2.6 |
| `kimi-k2.5` | Kimi-K2.5 |
| `glm-5.1` | GLM-5.1 |
| `qwen3.5-plus` | Qwen3.5-Plus |
| `doubao-seed-1.8` | Doubao-Seed-1.8 |

**优先级链**（使用 `tabbit/priority` 或 `default` 时按顺序尝试）：

Claude-Opus-4.7 → GPT-5.5 → Claude-Sonnet-4.6 → GPT-5.4 → DeepSeek-V4-Pro → GLM-5.1 → Gemini-3.1-Pro

## Agent 集成

### OpenCode / Codex

在配置文件中添加：

```toml
[providers.tabbit]
api_base = "http://127.0.0.1:9090/v1"
api_key = "sk-tabbit-local"
model = "gpt-5.5"
```

### Claude Code

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:9090
export ANTHROPIC_API_KEY=sk-tabbit-local
```

### 通用 OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:9090/v1",
    api_key="sk-tabbit-local"
)

response = client.chat.completions.create(
    model="gpt-5.5",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

## macOS 开机自启

使用 launchctl 实现开机自动启动代理：

```bash
# 创建 plist 文件
cat > ~/Library/LaunchAgents/com.tabbit.proxy.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tabbit.proxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/tabbit_proxy_v3.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/tabbit2api</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/tabbit_proxy.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/tabbit_proxy.log</string>
</dict>
</plist>
EOF

# 加载服务
launchctl load ~/Library/LaunchAgents/com.tabbit.proxy.plist
```

> **注意**：Tabbit 浏览器需要手动启动（带 CDP 参数），代理会自动连接。

## 技术细节

### 为什么用 webpack 注入而不是 UI 自动化？

| 方案 | 原理 | 问题 |
|------|------|------|
| UI 自动化 | 模拟打字 + 点击发送按钮 + 拦截网络请求 | 页面导航后拦截器丢失，~70% 失败率 |
| **Webpack 注入** | 直接调用内部 `sendMessage` API | 不依赖 DOM，~99% 成功率 |

### Webpack 模块发现

代理启动时会动态扫描 Tabbit 的 webpack 模块，找到：
- `sendMessage`：发送聊天消息的函数（通过特征签名 `setMessages` + `onChatFinish` 识别）
- `modes`：聊天模式枚举（通过 `ASK` 属性识别）

这种方式兼容不同 Tabbit 版本，无需硬编码模块 ID。

### 参考项目

- [hwttop5/tabbit2api](https://github.com/hwttop5/tabbit2api) — Node.js 实现，使用 Playwright + webpack 注入，本项目的核心思路来源于此

## 故障排除

### Tabbit 启动后立即崩溃

```bash
# 添加 --disable-gpu-compositing 参数
/Applications/Tabbit.app/Contents/MacOS/Tabbit \
  --remote-debugging-port=9222 \
  "--remote-allow-origins=*" \
  --no-first-run \
  --disable-gpu-compositing \
  --disable-extensions &
```

### CDP 连接失败 (503)

Tabbit 刚启动时 CDP 端口可能还未就绪，等待几秒后重试。代理内置了重试逻辑。

### 模块发现失败

确保 Tabbit 已登录且有可用的会话页面。代理会自动导航到 `/chat/new`，但如果登录态失效需要重新登录。

### 响应超时

- 默认超时 180 秒，可通过修改 `TIMEOUT_S` 调整
- 如果某个模型超时，代理会自动尝试优先级链中的下一个模型

## 项目结构

```
tabbit2api/
├── tabbit_proxy_v3.py    # 主代理脚本（当前版本）
├── tabbit_proxy_v2.py    # 旧版（UI 自动化，已弃用）
├── start_tabbit.sh       # Tabbit 启动脚本
├── README.md             # 本文档
└── .gitignore
```

## 许可

MIT License

## 致谢

- [Tabbit](https://tabbitbrowser.com/) — 免费的多模型 AI 浏览器
- [hwttop5/tabbit2api](https://github.com/hwttop5/tabbit2api) — webpack 注入方案的原始实现
