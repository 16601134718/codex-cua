---
name: codex-cua
description: 通过原 Codex/ChatGPT 的 computer use 运行时（cua-repl + Chrome 扩展 + Sky 原生 AX）操作本机 Chrome 和任意 Mac 应用。当需要真实点击网页、填表、读辅助功能树、截图看屏幕、操作非浏览器 App 时使用。含常驻会话客户端与踩坑规范。
---

# codex-cua

把 Codex 桌面端内置的 computer use 运行时直接驱动，**不需要 ChatGPT.app 在运行**。

## 入口

```bash
python3 ~/.dsh/skills/codex-cua/cuad.py send --ms 90000 '<javascript>'        # 默认不截图
python3 ~/.dsh/skills/codex-cua/cuad.py send --ms 90000 --shot '<javascript>' # 附带截图
python3 ~/.dsh/skills/codex-cua/cuad.py run task.js --ms 180000               # 跑 js 文件
python3 ~/.dsh/skills/codex-cua/cuad.py stop / clean / status / policy / pending / log / jobs
```

首次运行会**自动拉起**常驻会话（约 4 秒），不需要先 `start`。
**会话变量跨调用存活**：一次 send 里 `const T = await cua.getTab(...)`，下次还能用。

## 内置助手

| 助手 | 作用 |
| --- | --- |
| `QA()` | Chrome app 绑定（缓存，别反复 `cua.getApp`） |
| `TREE()` | 静默读完整 AX 树（`emit:false`） |
| `Q(/正则/, limit)` | 按文字找节点 → `[{n, line}]`，只回命中项 |
| `QC(/正则/)` | 找到并点击 |
| `QT(/正则/, nth, opts)` | 选中文字，支持 `prefix`/`suffix`/`selectionType` |
| `OBS()` | 廉价差分观察 |
| `RUN(steps, {stopOnError})` | 批量执行，依赖链遇错即停 |

## 七条硬规范

1. **别用 `disableDiffing:true` 做常规观察** —— 官方要求优先用默认差分。
2. **别在 `getAXState`/`getScreenshot` 前加 `setTimeout`** —— 内部已有等待，双重等待会卡几十秒。
3. **批处理** —— 只合并已知目标的确定性动作；弹窗/跳转/布局变化先重新观察。
4. **`getApp`/`getTab`/`createBrowserTab` 的返回已带初始状态**，不用再补一次。
5. **结果未知（unknown）时重新观察，绝不重发原动作。**
6. **优先节点号动作，坐标是兜底。** 用坐标前先标定偏移。
7. **`emit:false` 省上下文** —— 整棵 AX 树 300+ 行 vs 命中项 127 字节。

## 两个必踩的坑

**hover 隐藏控件**（`display:none`，不进 AX 树）

```js
await tab.drag([卡片外的x, 行y], [卡片内的x, 行y]);
await new Promise(r => setTimeout(r, 900));   // 这处等待必要（等 CSS 过渡）
const h = await Q(/link 删除/);
await tab.click(h[0].n);
```

**`--shot` 会拍前台标签**，不是绑定标签 —— 后台标签会连续返回陈旧截图，且调用方察觉不到。

```js
globalThis.CUATAB = await cua.getTab(t.id, { browser: "1" });   // 建完就设
```

自检：**比对两次截图的 sha256**，相同即说明拍的是同一画面。

## 延迟基线（本机实测）

| 项目 | 耗时 |
| --- | --- |
| 热调用（纯 JS） | 0.09 – 0.14 s |
| `Q()` 定位 | 0.11 – 0.14 s |
| 截图 | ~0.5 s（**不是瓶颈**） |
| 首次绑定标签（冷） | 3 – 9 s |
| 首次 AX 读（冷） | ~9 s，之后每次 ~0.2 s |
| 崩溃恢复 | ~4 s |

## 协作纪律

同一 Chrome 窗口只允许一个操作者；请求串行 ≠ JS 变量隔离。同一文件不并行写。
