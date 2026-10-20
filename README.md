# dsh-codex-cua

在 DeepSeek Harness 里直接使用 Codex 的 computer use 能力。

装一个插件，DSH 的 agent 就能操作 Chrome 和任意 macOS 原生应用 ——
走的是 ChatGPT / Codex 桌面端自带的运行时（`cua-repl` + Chrome 扩展 + Sky 原生 AX），
不需要启动 ChatGPT 应用，也不向 OpenAI 发送请求（`cua-repl` 为本地进程）。

---

## 为什么是「无缝」

装好之后**没有额外步骤**：

| 你做的 | 实际发生的 |
| --- | --- |
| 插件市场点安装，或 `dsh plugin add dsh-codex-cua` | 插件把 `codex-cua` 技能写入 `~/.dsh/skills/codex-cua/` |
| 开新会话，让 agent 操作网页或应用 | agent 加载该技能，直接调 `cuad.py` |

`SKILL.md` 里已经写好调用规范、踩坑清单和延迟基线，
**agent 不需要额外指导怎么用**。

---

## 安装

### 通过插件市场

在 DSH 的**插件市场**里搜索 `dsh-codex-cua`，点安装。

### 通过命令行

```bash
dsh plugin --profile <你的 profile> add dsh-codex-cua
```

### 手动安装

```bash
git clone https://github.com/16601134718/dsh-codex-cua.git
cd dsh-codex-cua
node scripts/verify.mjs          # 可选：先自检
mkdir -p ~/.dsh/skills/codex-cua/docs
cp assets/SKILL.md assets/cuad.py ~/.dsh/skills/codex-cua/
cp assets/docs/*.md ~/.dsh/skills/codex-cua/docs/
```

装完确认：

```bash
python3 ~/.dsh/skills/codex-cua/cuad.py doctor
```

---

## 环境要求

- macOS
- 已安装 **ChatGPT / Codex 桌面端**，并在 Chrome 里装好对应扩展
  （插件只负责启动这个运行时，不包含它）
- Python 3.9+（运行 `cuad.py`）

不满足时 `cuad.py doctor` 会逐项指出缺什么、怎么补。

---

## 它能做什么

```bash
CUAD=~/.dsh/skills/codex-cua/cuad.py

# 浏览器：读页面、点元素、填表
python3 $CUAD send --ms 90000 '
  const b = await cua.getBrowser("1");
  const t = await cua.createBrowserTab("1", "https://example.com");
  globalThis.CUATAB = await cua.getTab(t.id, { browser: "1" });
  nodeRepl.write(await CUATAB.playwright.evaluate(() => document.title));
'

# 原生应用：读辅助功能树、点击、输入
python3 $CUAD send --ms 90000 '
  const app = await cua.getApp("com.apple.finder");
  nodeRepl.write((await app.getAXState()).slice(0, 400));
'
```

会话变量跨调用存活：一次 `send` 里建的绑定，下次还能用。

---

## 工作原理

### 整体结构

```
  DSH agent
      │  加载 codex-cua 技能 → 调 cuad.py
      ▼
  cuad.py（客户端，无状态）
      │  写 .cuad/req.jsonl / 轮询 .cuad/res.jsonl
      ▼
  daemon：node cua-repl.mjs（常驻）
      │  持有 MCP 会话 + node_repl 的 JS 上下文
      ├──────────────┬──────────────┐
      ▼              ▼              ▼
  Chrome 扩展宿主   Sky 服务      （由系统托管）
  浏览器操作      原生 AX + 截图
```

三个进程互相独立：客户端每次调用都是新进程；`cua-repl` 常驻；
Chrome 扩展宿主和 Sky 服务由 Google Chrome 与 launchd 托管，
**ChatGPT 桌面端退出后它们仍在运行**。

### 为什么快

每次新建 `cua-repl` 要做 MCP 握手、加载 `node_repl`、重连扩展和 Sky ——
实测 3 到 11 秒。常驻之后这一步只在第一次发生。

| | 每次新建进程 | 常驻会话 |
| --- | --- | --- |
| 热调用 | 3 – 11 s | 0.09 – 0.14 s |
| 首次调用 | 同左 | 3 – 9 s（仅一次） |

### 为什么变量能跨调用存活

`cua-repl` 内部跑的是 `node_repl` —— 一个持久的 JavaScript 上下文。
`send` 执行的是追加到这个上下文里的代码，不是新脚本。

### 一次调用的生命周期

1. 客户端把 `{id, code, deadline, shot}` 追加到 `req.jsonl`
2. daemon 未运行则先拉起（约 4 秒），再重放**这一次**请求
3. daemon 通过 MCP `tools/call` 发给 `cua-repl`（`_meta` 带 turn 元数据）
4. 结果写回 `res.jsonl`，客户端轮询取走
5. 超过 `--ms` 未返回 → 返回 `unknown`，**不重放**

崩溃后下一次 `send` 会自动重新拉起 daemon。

---

## 插件做了什么

`lib/index.js` 的 `apply(ctx)` 在挂载时：

1. 读取包内 `assets/`
2. 写入 `~/.dsh/skills/codex-cua/`（内容相同则跳过，幂等）
3. 记录日志；失败只警告，不中断 profile 组装

**卸载插件不会删除已安装的技能文件** —— 它们可以独立使用，
删用户数据是不该有的副作用。想彻底清理：

```bash
rm -rf ~/.dsh/skills/codex-cua
```

---

## 使用规范（写进技能里的那些）

1. 观察一律用默认差分输出，`disableDiffing: true` 只在需要完整树时用
2. **不要在 `getAXState` / `getScreenshot` 前加 `setTimeout`** —— 内部已经等了
3. 只把目标已知的确定性动作合并进一次调用；弹窗/跳转先重新观察
4. **结果未知时重新观察，绝不重发写操作**
5. 优先元素索引操作，坐标是兜底；用坐标前先标定偏移
6. `emit: false` 可避免整棵 AX 树灌进上下文

详细展开见 [`assets/docs/pitfalls.md`](assets/docs/pitfalls.md)。

---

## 故障排查

先跑：

```bash
python3 ~/.dsh/skills/codex-cua/cuad.py doctor
```

它会检查运行时目录、扩展服务、Sky 服务、状态目录、扩展连接，每项给修复建议。

常见问题：

| 现象 | 原因 |
| --- | --- |
| `extension: 0` | Chrome 未运行，或桌面端从未启动过（扩展宿主由它注册） |
| `Missing required Codex turn metadata` | `tools/call` 的 `_meta` 没注入，见 `docs/protocol.md` |
| 一直返回 `unknown` | daemon 没拿到结果，看 `.cuad/daemon.log`；常见是 Chrome 崩了 |
| `Debugger unattached` | 站点主动断 CDP，只能用原生 AX 通道 |
| 截图总是同一张 | 没设 `CUATAB`，`--shot` 拍的是前台标签 |

---

## 安全说明

**这个插件的权限等于用户自己的权限。**

- `send` 执行任意 JavaScript，跑在本机用户身份下
- DSH 加载技能后，agent 会**自行**调用它 —— 请确认你接受这个行为
- 默认 `policy manual`：服务端发起的审批请求都要显式应答，超时按取消处理
- `policy auto-safe` 不是安全模式，只是对只读类请求免确认
- 结果未知不重放是**安全语义**，不是性能取舍

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
dsh-codex-cua/
├── package.json          dsh.bundle 清单
├── cordis.patch.yml      挂载声明
├── lib/index.js          apply(ctx)：把技能装进 DSH home
├── scripts/verify.mjs    不装 DSH 也能跑的自检
├── assets/
│   ├── SKILL.md          技能定义（agent 读这个）
│   ├── cuad.py           常驻会话客户端
│   └── docs/             pitfalls.md · protocol.md
├── bench/latency.sh      延迟测量
└── LICENSE
```

---

## 环境变量

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `DSH_HOME` | `~/.dsh` | 技能安装位置 |
| `CUA_RUNTIME_DIR` | `/Applications/ChatGPT.app/Contents/Resources/cua_node` | 运行时目录 |
| `CUA_BROWSER_SERVICE_DIR` | `~/.codex/plugins/cache/openai-bundled/chrome/latest` | 扩展服务 |
| `CUAD_STATE_DIR` | `~/.cuad` | 状态与截图 |

---

## License

MIT
