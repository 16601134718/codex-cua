# codex-cua

将 ChatGPT / Codex 桌面端内置的 computer use 运行时（`cua-repl`）从外部驱动，
用于在 macOS 上控制 Chrome 浏览器与原生应用。

运行时不依赖 ChatGPT 应用进程，也不向 OpenAI 发送请求（`cua-repl` 为本地进程）。

---

## 概述

该运行时提供三类能力：

| 能力 | 说明 |
| --- | --- |
| Chrome 扩展后端 | 基于 CDP 的页面控制，支持 Playwright 定位器 |
| 原生辅助功能树（AX） | 读取并操作任意 macOS 应用的界面元素 |
| 屏幕捕获与输入 | 截图、点击、拖拽、键盘输入 |

本项目提供一个常驻会话客户端 `src/cuad.py`，通过文件的请求/响应队列与 `cua-repl`
进程通信，可在多次调用之间保持 JavaScript 上下文与对象绑定。

---

## 环境要求

- macOS
- 已安装并登录 ChatGPT / Codex 桌面端，Chrome 已安装对应扩展
- Python 3.9+

---

## 快速开始

```bash
git clone <repo-url>
cd codex-cua

# 第一步：自检环境（推荐）
python3 src/cuad.py doctor

# 第二步：跑一次真实调用
python3 src/cuad.py send --ms 90000 '
  const browsers = await cua.listBrowsers();
  nodeRepl.write("extension: " + browsers.filter(b => b.type === "extension").length);
'
```

`doctor` 会逐项检查运行时目录、扩展服务、Sky 服务、状态目录和扩展连接，
并给出每一项的修复建议。全部 `OK` 即可直接使用。

首次调用会自动启动常驻会话（约 4 秒），无需手动 `start`。
输出 `extension: 1` 表示扩展浏览器已连接。

---

## 使用

### 命令行

```bash
python3 src/cuad.py doctor                                  # 环境自检
python3 src/cuad.py send --ms 90000 '<javascript>'          # 执行，默认不截图
python3 src/cuad.py send --ms 90000 --shot '<javascript>'   # 附带截图
python3 src/cuad.py run task.js --ms 180000                 # 执行 js 文件
python3 src/cuad.py status                                  # 会话状态
python3 src/cuad.py stop                                    # 停止会话
python3 src/cuad.py clean                                   # 清理截图与队列
python3 src/cuad.py log [n]                                 # 查看审计记录
python3 src/cuad.py policy manual|auto-safe                 # 审批策略
python3 src/cuad.py pending / answer accept|decline         # 手动应答审批
```

会话变量在多次调用之间保持。例如在一个 `send` 中执行
`const T = await cua.getTab(id)`，后续调用可直接引用 `T`。

### 浏览器操作

```js
const browser = await cua.getBrowser("1");
const tab = await cua.createBrowserTab("1", "https://example.com");
globalThis.CUATAB = await cua.getTab(tab.id, { browser: "1" });

await tab.getAXState();
await tab.click(元素索引);
await tab.setValue(元素索引, 值);
await tab.playwright.evaluate(() => document.title);
```

### 原生应用操作

```js
const app = await cua.getApp("com.apple.finder");
await app.getAXState();
await app.click(元素索引);
await app.selectText(元素索引, "原文");
await app.paste("替换文本");
await app.pressKey("Return");
```

### 预置助手函数

| 函数 | 说明 |
| --- | --- |
| `QA()` | 返回缓存的 Chrome 应用绑定 |
| `TREE()` | 读取完整 AX 树，`emit: false`，不写入上下文 |
| `Q(/regex/, limit)` | 按文本匹配节点，返回 `[{n, line}]` |
| `QC(/regex/)` | 匹配并点击 |
| `QT(/regex/, nth, opts)` | 选中节点文本，支持 `prefix` / `suffix` / `selectionType` |
| `OBS()` | 差分观察（会写入结果） |
| `RUN(steps, {stopOnError})` | 批量执行；`stopOnError` 遇错即停 |

---

## 工作原理

### 整体结构

```
  你的 agent / 脚本
        │  python3 src/cuad.py send '<js>'
        ▼
  ┌─────────────────────────────────────────┐
  │  cuad.py（客户端，无状态）                │
  │  · 写入 .cuad/req.jsonl                  │
  │  · 轮询 .cuad/res.jsonl 取结果            │
  └───────────────┬─────────────────────────┘
                  │ 文件队列（同一台机器）
                  ▼
  ┌─────────────────────────────────────────┐
  │  daemon：node cua-repl.mjs（常驻进程）    │
  │  · 持有 MCP 会话                          │
  │  · 持有 node_repl 的 JS 上下文            │
  └───┬─────────────────────┬───────────────┘
      │ MCP (stdio)         │ MCP (stdio)
      ▼                     ▼
  Chrome 扩展宿主        Sky 服务
  （浏览器操作）        （原生 AX + 截图）
  ← 由 Google Chrome 托管   ← 由 launchd 托管
```

**三个进程互相独立**：客户端每次调用都是新进程，跑完就退出；
`cua-repl` 常驻；Chrome 扩展宿主和 Sky 服务由系统托管，
即便 ChatGPT 桌面端退出，它们仍在运行。

### 为什么用文件队列而不是 HTTP

daemon 与客户端之间用两个 append-only 的 JSONL 文件通信，原因有三：

1. **无端口占用** —— 不需要监听端口，不会和别的服务冲突
2. **跨进程无需握手** —— 客户端写一行就退出，不必维持连接
3. **崩溃可诊断** —— 队列文件留在磁盘上，事后能看看到底发了什么

### 为什么快：省掉的不是执行，是初始化

每次新建 `cua-repl` 进程需要做 MCP `initialize` 握手、加载 `node_repl`、
重新连扩展和 Sky —— 实测 **3 到 11 秒**。常驻之后这一步只在第一次发生。

| | 每次新建进程 | 常驻会话 |
| --- | --- | --- |
| 热调用 | 3 – 11 s | **0.09 – 0.14 s** |
| 首次调用 | 同左 | 3 – 9 s（仅一次） |

### 为什么变量能跨调用存活

`cua-repl` 内部跑的是 `node_repl` —— 一个**持久的 JavaScript 上下文**。
`send` 执行的代码不是新脚本，而是往同一个上下文里追加执行。

所以这成立：

```bash
# 第一次：建立绑定
python3 src/cuad.py send 'globalThis.CUATAB = await cua.getTab(tabId, {browser:"1"})'

# 第二次：直接用，不用重新绑定
python3 src/cuad.py send 'nodeRepl.write(await CUATAB.url())'
```

这也是为什么 `--shot` 需要 `CUATAB`：截图时要知道拍哪个标签。

### 一次调用的完整生命周期

1. 客户端把 `{id, code, deadline, shot}` 追加到 `req.jsonl`
2. 若 daemon 未运行，先拉起（约 4 秒），再重放**这一次**请求
3. daemon 读出请求，通过 MCP `tools/call` 发给 `cua-repl`
   （`_meta` 里带上 `x-codex-turn-metadata`）
4. `cua-repl` 在持久上下文里执行，把结果写回 `res.jsonl`
5. 客户端轮询拿到结果，打印输出；原始响应留在 `.cuad/raw-<job>.json`
6. 超过 `--ms` 仍未返回 → 返回 `unknown`，**不重放**

### 崩溃恢复

守护进程异常退出时，客户端返回 `unknown` 状态，**不会自动重放**。
调用方应重新观察界面状态后自行决定后续动作。写操作重放可能造成重复提交。

下一次 `send` 会自动重新拉起 daemon（不必手动 `start`）。

### 审批

服务端可能发起 `elicitation/create` 请求。客户端将其写入 `.cuad/pending.json`，
等待显式应答；超时按 fail-closed 处理（取消操作）。

`policy manual`（默认）要求每个请求人工应答；
`policy auto-safe` 仅对只读类请求自动批准，写入类仍需人工应答。

---

## 已知问题与注意事项

以下内容来自实际调试，非理论推测。

### 1. 延迟主要来自观测往返，而非单次调用

实测数据：

| 项目 | 耗时 |
| --- | --- |
| 热调用（纯 JavaScript） | 0.09 – 0.14 s |
| 单次截图 | ~0.5 s |
| 首次绑定标签（冷启动） | 3 – 9 s |
| 首次 AX 读取（冷启动） | ~9 s，后续约 0.2 s |
| 一次「截图 → 分析 → 规划」往返 | 20 – 40 s |

单个操作耗时很低，完整任务的耗时主要由往返次数决定。
优化方向是减少往返，而非优化单次调用。

### 2. 三条应遵循的规范

以下三条见于官方 CUA 文档，违反会显著增加延迟：

**使用默认差分输出**

> "Prefer this default diff output; pass `{ disableDiffing: true }` only when you need a fresh full accessibility tree."

仅在需要完整树时（例如按文本搜索节点）使用 `disableDiffing: true`。

**不要在观测调用前手动等待**

> "`getAXState()`, `getScreenshot()` and `getAXStateAndScreenshot()` automatically wait an appropriate amount of time before capturing new state. … don't pause or delay (ex: `setTimeout(...)`) before getting UI state."

额外的 `setTimeout` 会与内部等待叠加，是长时间卡顿的常见原因。

**合并确定性动作**

> "Batch deterministic actions and the resulting `getAXState()` into one call."

仅合并目标已知且确定的动作；出现弹窗、跳转或布局变化时应重新观测后再定位。

### 3. `emit: false` 与上下文占用

按文本匹配节点需要完整 AX 树。若不加 `emit: false`，整棵树会写入模型上下文
（实测约 300 行），而实际需要的匹配结果通常不足 200 字节。

`Q()` 内部使用完整树匹配并设置 `emit: false`，只返回命中项。

### 4. 仅在悬停时出现的控件

部分界面的操作按钮使用 `display: none`，悬停后才显示，因此不存在于 AX 树中。
可通过拖拽使指针停留在目标元素上：

```js
await tab.drag([元素外坐标, 行 y], [元素内坐标, 行 y]);
await new Promise(r => setTimeout(r, 900));   // 等待 CSS 过渡，此处必要
const hits = await Q(/link 删除/);
await tab.click(hits[0].n);
```

拖拽的终点即指针停留位置；由于 mousedown 与 mouseup 不在同一元素上，
不会触发点击事件。

### 5. 坐标偏移

`Vec2` 的坐标空间存在未文档化的物理像素偏移（本机实测约 `+46, +40`）。
使用坐标前应先标定：点击一个无副作用的位置，截图确认指针实际落点，取差值。

### 6. 弹窗按钮被合并为单一文本节点

部分弹窗的两个按钮在 AX 树中合并为一个文本节点（如 `text 取消 确认`），
无法通过元素索引单独点击，需使用标定后的坐标。

### 7. `--shot` 的截图目标

`--shot` 优先截取 `globalThis.CUATAB` 指向的标签。
若未设置，则截取窗口当前活动标签——当被操作的标签位于后台时，
会重复返回同一张未更新的截图，且调用方无法从返回值中察觉。

设置方式：

```js
globalThis.CUATAB = await cua.getTab(tabId, { browser: "1" });
```

自检方法：比较连续两次截图的 SHA-256，相同则说明画面未更新。

### 8. 自绘控件应使用元素索引点击

对 canvas 或自绘控件使用文本匹配定位时，可能出现点击被记录但状态未改变的情况
（例如步数增加而画面未变化）。应改用元素索引（如 `locator("button").nth(i)`）
精确定位。

### 9. 部分站点的反自动化行为

某些站点会主动断开 CDP 调试器连接，表现为绑定成功但
`playwright.evaluate` 持续返回 `Debugger unattached`。此时只能退回原生 AX 通道。
此类站点在自动化创建的标签页中也可能长时间停留在加载状态。

另有实测规律：指针在浏览器窗口内移动会触发"用户接管"判定并断开调试器；
指针在窗口外活动则不影响。

---

## 故障排查

先跑 `python3 src/cuad.py doctor`，它会指出具体哪一项没过。

### `extension: 0` 或提示扩展未连接

Chrome 没有连接到扩展宿主。依次检查：

1. Chrome 是否在运行
2. ChatGPT / Codex 桌面端是否**至少启动过一次**（扩展宿主由它注册）
3. `~/.codex/plugins/cache/openai-bundled/chrome/latest` 是否存在

### `Missing required Codex turn metadata`

`tools/call` 的 `_meta` 没有注入。见 [docs/protocol.md](docs/protocol.md) 的 `_meta` 注入一节。

### 调用一直返回 `unknown`

daemon 没能在期限内拿到结果。看 `.cuad/daemon.log` 尾部，
常见原因：Chrome 崩了（所有 AppleScript / 点击调用会报"应用程序没有运行"）、
网络中断、或前端页面卡住。

### `Debugger unattached`

站点主动断开了 CDP。只能退回原生 AX 通道（`cua.getApp`），
详见 [docs/pitfalls.md](docs/pitfalls.md) 第 9 节。

### 截图总是同一张

没有设置 `CUATAB`，`--shot` 拍的是前台标签。见 pitfalls 第 7 节。

### 状态目录越来越大

`.cuad/` 下的截图不会自动清理，手动执行：

```bash
python3 src/cuad.py clean
```

---

## 安全说明

**这个工具的权限等于你自己的权限，请当做 shell 来对待。**

- `send` 执行的是**任意 JavaScript**，跑在本机用户身份下，
  能读写文件、发网络请求、操作任何可访问的界面
- 默认 `policy manual`：服务端发起的每个审批请求都要你显式应答
  （`answer accept`），超时按取消处理
- `policy auto-safe` 不是"安全模式"，它只是对只读类请求免确认，
  **写入类仍要人工应答**
- 结果未知时**绝不重放**是安全语义，不是性能取舍 ——
  重复提交、重复删除的代价远高于重试一次
- 常驻会话的变量在进程存活期间一直保留，敏感值不要长期挂在全局

## 免责声明

```
本项目与 OpenAI 无任何关联，未经其认可或授权。
使用前需自行合法安装并授权 ChatGPT / Codex 桌面端。
在官方客户端之外驱动其私有运行时可能违反相关服务条款，风险由使用者自行承担。
本项目强依赖内部实现细节，官方更新后可能失效，不提供版本兼容性保证。
仅支持 macOS。
本项目不包含、不分发任何 OpenAI 的二进制文件，仅调用用户本机已有的运行时。
本项目不绕过任何计费机制。
```

---

## 目录结构

```
codex-cua/
├── src/cuad.py          常驻会话客户端（含 doctor 自检）
├── docs/
│   ├── pitfalls.md      已知问题详解
│   └── protocol.md      MCP 交互与 _meta 注入
├── bench/
│   └── latency.sh       延迟测量脚本
├── SKILL.md             agent 技能描述
├── AGENTS.md            全局约定片段
└── LICENSE
```

---

## 环境变量

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `CUA_RUNTIME_DIR` | `/Applications/ChatGPT.app/Contents/Resources/cua_node` | 运行时目录 |
| `CUA_BROWSER_SERVICE_DIR` | `~/.codex/plugins/cache/openai-bundled/chrome/latest` | 扩展服务目录 |
| `CUAD_STATE_DIR` | `~/.cuad` | 状态、队列与截图目录 |
| `CUAD_KEEP_SHOTS` | `4` | 保留截图数量 |

---

## License

MIT
