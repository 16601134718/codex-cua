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

python3 src/cuad.py send --ms 90000 '
  const browsers = await cua.listBrowsers();
  nodeRepl.write(browsers.filter(b => b.type === "extension").length);
'
```

首次调用会自动启动常驻会话（约 4 秒），无需手动 `start`。
输出为 `1` 表示扩展浏览器已连接。

---

## 使用

### 命令行

```bash
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

## 设计说明

### 常驻会话

`cua-repl` 在独立进程中长期运行，客户端通过 `.cuad/req.jsonl` 与 `.cuad/res.jsonl`
交换消息。相比每次调用新建进程，该设计带来两项收益：

- 避免重复的 MCP 初始化与首次连接开销
- 保留 JavaScript 上下文，绑定对象可复用

### 崩溃恢复

守护进程异常退出时，客户端返回 `unknown` 状态，**不会自动重放**。
调用方应重新观察界面状态后自行决定后续动作。写操作重放可能造成重复提交。

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
├── src/cuad.py          常驻会话客户端
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
