# MCP 交互与 _meta 注入

## 通信方式

`cua-repl` 是一个标准 MCP 服务器，通过 stdio 通信，协议版本 `2025-06-18`。

客户端 `cuad.py` 的交互流程：

1. 启动子进程 `node cua-repl.mjs`
2. 发送 `initialize`（声明 `capabilities.elicitation`）
3. 发送 `notifications/initialized`
4. 循环：`tools/call` (name: `js`) → 读取响应

## 工具接口

`cua-repl` 只暴露一个工具：

| 字段 | 内容 |
| --- | --- |
| name | `js` |
| arguments.code | 要执行的 JavaScript |
| arguments.timeout_ms | 执行超时（毫秒） |

返回值中的 `content` 为数组，元素类型为 `text` 或 `image`。

## `_meta` 注入（关键）

### 现象

未注入 `_meta` 时，浏览器相关调用会返回：

```
Browsers: Error: Missing required Codex turn metadata: session_id, turn_id
```

### 原因

`cua-repl` 要求每次 `tools/call` 携带 turn 元数据，用于关联调用来源。
该信息通过 MCP 的 `_meta` 字段传递，而非环境变量。

### 解法

在 `tools/call` 的 `params` 中注入：

```json
{
  "name": "js",
  "arguments": { "code": "...", "timeout_ms": 120000 },
  "_meta": {
    "x-codex-turn-metadata": {
      "session_id": "<uuid>",
      "turn_id": "<uuid>"
    },
    "form_elicitation_supported": true
  }
}
```

`session_id` 与 `turn_id` 可以是随机生成的 UUID，不要求对应真实会话。

环境变量方式无效，已验证。

### `form_elicitation_supported`

该字段决定 `nodeRepl` 是否提供表单审批能力。
缺少时，需要审批的操作会直接失败。

## 审批流程

服务端可能主动发起请求（带 `method` 与 `id`）：

```json
{ "jsonrpc": "2.0", "id": 7, "method": "elicitation/create", "params": { ... } }
```

客户端必须应答，否则操作无法继续：

```json
{ "jsonrpc": "2.0", "id": 7, "result": { "action": "accept", "content": {} } }
```

`action` 取值：`accept` / `decline`。

`cuad.py` 的处理方式：

1. 写入 `.cuad/pending.json`
2. 等待 `.cuad/answer.json`（默认 180 秒）
3. 超时则按 fail-closed 取消操作

## 运行时环境变量

启动 `cua-repl` 时需设置：

| 变量 | 说明 |
| --- | --- |
| `NODE_PATH` | `<runtime>/lib/node_modules` |
| `CUA_REPL_NODE_REPL_PATH` | `<runtime>/bin/node_repl` |
| `CUA_REPL_ENABLED_SURFACES` | `browser,computer` |
| `NODE_REPL_TRUSTED_SERVICES` | JSON，声明信任的服务路径 |
| `SKY_CUA_SERVICE_PATH` | Sky 服务应用路径 |
| `CODEX_HOME` | Codex 配置目录 |
| `BROWSER_USE_CODEX_APP_VERSION` | 应用版本号 |

`NODE_REPL_TRUSTED_SERVICES` 需要同时包含 `browser` 与 `sky`，
缺少任一项会导致对应通道不可用。

## 进程结构

| 组件 | 托管方 | 说明 |
| --- | --- | --- |
| `cua-repl` / `node_repl` | 由客户端启动 | 常驻会话进程 |
| Chrome 扩展宿主 | Google Chrome | 原生消息宿主 |
| Sky（原生 AX + 截图） | launchd | SMAppService 注册，自动重启 |

三者均不依赖 ChatGPT 应用进程，应用退出后仍可正常工作。
