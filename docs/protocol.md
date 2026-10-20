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

## 客户端与 daemon 的队列协议

客户端和 daemon 之间不通过网络通信，而是同一台机器上的两个 JSONL 文件。

### 请求 `.cuad/req.jsonl`

每行一条，追加写入：

```json
{"id": "a1b2c3", "code": "...js...", "deadline": 1234567.89, "shot": false}
```

`deadline` 是 `time.monotonic()` 的绝对时刻，不是相对时长 ——
这样 daemon 排队期间也能正确计算剩余时间。

### 响应 `.cuad/res.jsonl`

```json
{"id": "a1b2c3", "ok": true, "out": "...", "state": "completed"}
```

`state` 取值：`completed` / `failed` / `unknown`。

`unknown` 表示期限内没拿到结果（daemon 崩溃、页面卡死、执行超时）。
**调用方必须把它当成"可能已经生效"来处理**，重新观察后再决定，
而不是当作失败重发。

### 为什么用绝对 deadline

早期版本用相对时长，daemon 重启后重放请求时会把总时长翻倍；
改为绝对时刻后，重放不会延长总期限。

### 原始响应

完整的 MCP 响应（含图片 base64）留在 `.cuad/raw-<job>.json`，
便于事后核对。图片本身不会塞进 `res.jsonl`。

### 并发

多个客户端可以同时写 `req.jsonl`（append 是原子的），
但**同一份 JS 里的操作不会并发** —— daemon 串行执行。
需要真正的并发要开多个会话，代价是会抢同一个 Chrome 窗口。
