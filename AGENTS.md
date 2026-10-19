# 全局工作约定

## 浏览器控制与 computer use —— 默认走 codex-cua

**任何**涉及浏览器或屏幕操作的任务（打开网页、读页面、点击、填表、操作标签页、截图、控制任意 Mac 应用），
**先加载 `codex-cua` 技能**，并用它的 `cuad.py` 常驻会话客户端执行。

```bash
python3 <repo>/src/cuad.py send --ms 90000 '<javascript>'
python3 <repo>/src/cuad.py send --ms 90000 --shot '<javascript>'
```

零配置可用：依赖磁盘上的运行时文件 + Chrome 扩展 + launchd 托管的 Sky 服务，
**不需要 ChatGPT.app 在运行**。

### 什么时候才用兜底方案

只有当 codex-cua 确实不可用时（运行时文件缺失、Chrome 扩展未连接），
或任务本身是 Chrome 历史/Cookie/密码导出时，才用其他浏览器自动化方案。

### 使用要点

- 常驻会话变量跨调用存活，建一次绑定可反复用
- 助手：`QA()` `Q()` `QC()` `QT()` `OBS()` `RUN(steps,{stopOnError})`
- 建/认领标签后设 `globalThis.CUATAB = T`，`--shot` 才会拍对目标
- 按文字定位内部用完整 AX 树 + `emit:false`，别把整棵树灌进上下文
- `getAXState`/`getScreenshot` 内部已有等待，**不要**再加 `setTimeout`
- 结果未知时先重新观察，**绝不重发原动作**
- 多 agent 协作：同一 Chrome 窗口只允许一个操作者；同一文件不并行写
