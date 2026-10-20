#!/usr/bin/env python3
"""
cuad.py — Codex computer use 常驻会话客户端（含审批流）

传输语义：每条请求只发送一次；超时/进程中断为 unknown，禁止自动重放。
恢复进程仅用于下一条新请求。先重新观察，再决定是否再次操作。
--ms 是包含启动、排队、执行与回传的总期限。默认不截图；--shot 显式截图。
原始 MCP 响应保留在 .cuad/raw-<job>.json，图片仍需调用方 read_image。

命令
  doctor                         环境自检（推荐第一步）
  start                          启动常驻会话
  send '<js>'                    在会话里执行 JS（会等审批）
  send --bg '<js>'               后台执行，立即返回 job id
  jobs                           查看后台任务
  pending                        查看待审批请求
  answer accept|decline [json]    应答审批
  policy                         查看/设置策略
  log                            审批审计日志
  status / stop
  clean                          清理截图与队列
  help                           显示本说明

审批流说明
  MCP 服务端在需要确认时会发 elicitation/create 请求。本客户端：
   1. 把请求写入 .cuad/pending.json 并打印出来
   2. 等 answer（默认 180s）
   3. 超时即按 fail-closed 取消，与 Codex 行为一致
  policy=auto-safe 时，只对"只读类"动作自动批准；删除/输入/提交类一律要人工应答。
"""
import subprocess, json, sys, os, time, pathlib, signal, re, queue, threading, uuid, fcntl

ROOT = os.environ.get("CUA_RUNTIME_DIR",
    "/Applications/ChatGPT.app/Contents/Resources/cua_node")
HOME = os.path.expanduser("~")
CP = os.path.expanduser(os.environ.get("CUA_BROWSER_SERVICE_DIR",
    "~/.codex/plugins/cache/openai-bundled/chrome/latest"))
CP_VERSION = os.path.basename(os.path.realpath(CP))
BASE = pathlib.Path(os.environ.get("CUAD_STATE_DIR",
    os.path.expanduser("~/.cuad")))
REQ, RES = BASE / "req.jsonl", BASE / "res.jsonl"
PIDF, LOGF = BASE / "pid", BASE / "daemon.log"
READY, START_LOCK = BASE / "ready.json", BASE / "startup.lock"
PEND, ANS = BASE / "pending.json", BASE / "answer.json"
AUDIT, JOBS, POL = BASE / "audit.jsonl", BASE / "jobs.jsonl", BASE / "policy.txt"

ELICIT_TIMEOUT = 180
KEEP_SHOTS = int(os.environ.get("CUAD_KEEP_SHOTS", "4"))   # 只保留最近几张截图

ENV = dict(os.environ)
ENV.update({
    "NODE_PATH": f"{ROOT}/lib/node_modules",
    "CUA_REPL_NODE_REPL_PATH": f"{ROOT}/bin/node_repl",
    "CUA_REPL_ENABLED_SURFACES": "browser,computer",
    "NODE_REPL_NATIVE_PIPE_CONNECT_TIMEOUT_MS": "1000",
    "NODE_REPL_NODE_MODULE_DIRS": f"{ROOT}/lib/node_modules",
    "NODE_REPL_NODE_PATH": f"{ROOT}/bin/node",
    "NODE_REPL_TRUSTED_CODE_PATHS": f"{HOME}/.codex:{ROOT}/lib/node_modules",
    "CODEX_HOME": f"{HOME}/.codex",
    "BROWSER_USE_AVAILABLE_BACKENDS": "chrome,iab,mcpapps",
    "BROWSER_USE_TINYSKY_ENABLED": "1",
    "BROWSER_USE_CODEX_APP_BUILD_FLAVOR": "prod",
    "BROWSER_USE_CODEX_APP_VERSION": CP_VERSION,
    "NODE_REPL_TRUSTED_SERVICES": json.dumps({
        "browser": f"{CP}/scripts/browser-service.mjs", "sky": "@oai/sky/service"}),
    "SKY_CUA_SERVICE_PATH": f"{HOME}/.codex/computer-use/Codex Computer Use.app",
    "CODEX_CLI_PATH": "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
})
PRELUDE = r"""
/* ---- 定位 / 批处理助手 ----------------------------------------------------
   规范依据：原厂 CUA 文档 + Codex 传输交接（讨论室 [35]）
   1. Q 内部需要完整树才能匹配；必须 emit:false，否则整棵树灌进模型上下文。
      不要改成默认差分——原本存在但未变化的节点会找不到。
   2. 不要在 getAXState/getScreenshot 前加 setTimeout：原接口内部已有等待。
   3. 批处理只合并「已知目标的确定性动作」；出现弹窗/跳转/布局变化必须先重新观察。
   4. unknown = 结果未知 → 重新观察，禁止重发原动作。
   ------------------------------------------------------------------------- */
if (!globalThis.Q) {
  globalThis.QA = async () => (globalThis.CA = globalThis.CA || await cua.getApp("com.google.Chrome"));

  /* 静默读完整 AX 树：内部匹配用，不 emit */
  globalThis.TREE = async () => {
    const ca = await globalThis.QA();
    return String(await ca.getAXState({ disableDiffing: true, emit: false })).split("\n");
  };

  /* 廉价观察：默认差分并 emit，用于批次结束后确认界面变化 */
  globalThis.OBS = async () => await (await globalThis.QA()).getAXState();

  /* 按文字/正则找节点。只返回命中项，不返回整树 */
  globalThis.Q = async (pat, limit = 40) => {
    const L = await globalThis.TREE();
    const re = pat instanceof RegExp ? pat : new RegExp(pat);
    const hits = [];
    for (let i = 0; i < L.length && hits.length < limit; i++) {
      const m = L[i].match(/^\s*(\d+) /);
      if (m && re.test(L[i])) hits.push({ n: +m[1], line: L[i].trim().slice(0, 110) });
    }
    return hits;
  };

  /* 找节点并点击 */
  globalThis.QC = async (pat, nth = 0) => {
    const h = await globalThis.Q(pat);
    if (!h.length) throw new Error("找不到匹配节点: " + pat);
    await (await globalThis.QA()).click(h[nth].n);
    return h[nth];
  };

  /* 选中节点文字；opts 支持 {prefix, suffix, selectionType} */
  globalThis.QT = async (pat, nth = 0, opts) => {
    const h = await globalThis.Q(pat);
    if (!h.length) throw new Error("找不到匹配节点: " + pat);
    await (await globalThis.QA()).selectText(h[nth].n, h[nth].line, opts);
    return h[nth];
  };

  /* 批处理。默认遇错继续；依赖型步骤用 { stopOnError: true } 遇错即停 */
  globalThis.RUN = async (steps, opts = {}) => {
    const stop = opts.stopOnError === true;
    const log = [];
    let stopped = false;
    for (const s of steps) {
      const name = s[0], fn = s[1];
      try {
        const r = await fn();
        log.push("OK   " + name + (r !== undefined ? " -> " + String(r).slice(0, 90) : ""));
      } catch (e) {
        log.push("FAIL " + name + " -> " + String((e && e.message) || e).slice(0, 110));
        if (stop) { stopped = true; break; }
      }
    }
    if (stopped) log.push("STOP 遇错停止，后续步骤未执行（依赖链已断）");
    return log.join("\n");
  };
}
"""


SHOT = r"""
/* ---- 自动截图 ----------------------------------------------------------
   重要：必须拍【CUA 绑定的标签】，不能拍整窗/当前活动标签。
   整窗截图拍到的是前台那个标签；若绑定的标签在后台，会拿到陈旧画面，
   且调用方无法从返回值察觉——会误判成"看到了目标页面"。
   绑定约定：建/认领标签后设置 globalThis.CUATAB = 该标签。
   ---------------------------------------------------------------------- */
try {
  let __tgt = globalThis.CUATAB || globalThis.T || globalThis.TAB;
  if (!__tgt || typeof __tgt.getScreenshot !== "function") __tgt = await globalThis.QA();
  await __tgt.getScreenshot();
  nodeRepl.write("[shot target=" + (__tgt === globalThis.CUATAB ? "CUATAB" : (__tgt === globalThis.CA ? "整窗(注意可能非目标标签)" : "other")) + "]");
} catch (e) { try { nodeRepl.write("[shot failed: " + String(e && e.message || e).slice(0,80) + "]"); } catch (_) {} }
"""

# Each process owns its session; never reuse another Codex conversation identity.
TURN_META = {"session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4())}

NOISE = ("## Computer Use", "Confirmation Modes", "<app_specific",
         "### Types of Instruction", "## API", "# Tab Cleanup",
         "# Browser Control", "# API Use")

# ---- 审批策略：这些关键词命中就自动批准（只读类）----
SAFE_HINTS = ("read", "list", "get", "state", "screenshot", "snapshot",
              "title", "url", "count", "info", "documentation")
# ---- 这些一律要人工应答（危险类）----
RISKY_HINTS = ("delete", "remove", "submit", "purchase", "pay", "send",
               "publish", "post", "upload", "captcha", "close", "reset",
               "write", "fill", "type", "click")


def prune_shots(keep=None):
    """只保留最近 keep 张截图，其余删掉"""
    keep = KEEP_SHOTS if keep is None else keep
    shots = sorted(BASE.glob("img-*"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in shots[keep:]:
        try: f.unlink()
        except Exception: pass
    return len(shots[keep:])


def policy():
    try:
        return POL.read_text().strip() or "manual"
    except Exception:
        return "manual"


# ------------------------------------------------------------------ daemon
class JsonLineInbox:
    """Drain a buffered pipe in one reader thread; never select on TextIOWrapper."""
    def __init__(self, stream):
        self.messages = queue.Queue()
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _read(self, stream):
        try:
            for line in stream:
                try:
                    self.messages.put((json.loads(line), time.monotonic()))
                except (ValueError, TypeError):
                    continue
        finally:
            self.messages.put((None, time.monotonic()))

    def get(self, timeout=0):
        return self.messages.get(timeout=max(0, timeout))


def daemon():
    import base64
    BASE.mkdir(parents=True, exist_ok=True)
    errf = open(LOGF, "a")
    p = subprocess.Popen(
        [f"{ROOT}/bin/node", f"{ROOT}/lib/node_modules/@oai/cua-repl/bin/cua-repl.mjs"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf, env=ENV, text=True, bufsize=1)
    inbox = JsonLineInbox(p.stdout)
    reqs = resf = None
    active = None
    seq = 1000

    def snd(o):
        p.stdin.write(json.dumps(o) + "\n")
        p.stdin.flush()

    def audit(rec):
        with open(AUDIT, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%FT%T"), **rec}, ensure_ascii=False) + "\n")

    def audit_tail(n=50):
        if not AUDIT.exists(): return []
        return [json.loads(l) for l in AUDIT.read_text(errors="replace").splitlines()[-n:]
                if l.strip()]

    def describe(params):
        """从 elicitation 请求里抽出可读描述"""
        try:
            msg = params.get("message") or ""
            schema = (params.get("requestedSchema") or {}).get("properties") or {}
            keys = ",".join(schema.keys())
            return f"{msg[:300]} | 字段: {keys[:200]}"
        except Exception:
            return json.dumps(params, ensure_ascii=False)[:300]

    def decide(params):
        """policy=auto-safe 时，只读类自动批准"""
        blob = json.dumps(params, ensure_ascii=False).lower()
        if policy() != "auto-safe":
            return None
        if any(k in blob for k in RISKY_HINTS):
            return None                      # 危险：交人工
        if any(k in blob for k in SAFE_HINTS):
            return "accept"
        return None

    def handle_elicit(req):
        rid = req.get("id")
        params = req.get("params") or {}
        desc = describe(params)
        audit({"kind": "elicitation", "id": rid, "desc": desc, "policy": policy()})

        auto = decide(params)
        if auto:
            audit({"kind": "elicitation-auto", "id": rid, "answer": auto})
            return {"jsonrpc": "2.0", "id": rid, "result": {"action": auto, "content": {}}}

        PEND.write_text(json.dumps({"id": rid, "params": params, "desc": desc,
                                    "ts": time.strftime("%FT%T")}, ensure_ascii=False, indent=1))
        elicit_deadline = min(time.monotonic() + ELICIT_TIMEOUT, active[1]["deadline_monotonic"] if active else init_deadline)
        while time.monotonic() < elicit_deadline:
            if ANS.exists():
                try:
                    a = json.loads(ANS.read_text())
                except Exception:
                    time.sleep(0.5); continue
                if a.get("for") == rid:
                    ANS.unlink(missing_ok=True); PEND.unlink(missing_ok=True)
                    audit({"kind": "elicitation-answer", "id": rid,
                           "action": a.get("action"), "content": a.get("content")})
                    return {"jsonrpc": "2.0", "id": rid,
                            "result": {"action": a.get("action", "decline"),
                                       "content": a.get("content") or {}}}
            time.sleep(0.4)
        PEND.unlink(missing_ok=True)
        audit({"kind": "elicitation-timeout", "id": rid, "desc": desc})
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32000,
                          "message": "Elicitation not answered within timeout; cancelled (fail-closed)."}}

    def finish(job, ok, out, state, received=None, rpc_result=None):
        now = time.monotonic()
        sent = job.get("dispatched_monotonic", now)
        received = received if received is not None else now
        timing = {
            "queue_ms": round(1000 * (sent - job.get("submitted_monotonic", sent)), 2),
            "rpc_ms": round(1000 * (received - sent), 2),
            "delivery_ms": round(1000 * (now - received), 2),
            "total_ms": round(1000 * (now - job.get("submitted_monotonic", sent)), 2),
        }
        record = {"id": job["id"], "ok": ok, "state": state, "out": out,
                  "tool_s": round((received - sent), 3), "timing": timing}
        # Preserve the full source response separately, including image/meta/error fields.
        if rpc_result is not None:
            raw = BASE / ("raw-" + job["id"] + ".json")
            fd = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(rpc_result, f, ensure_ascii=False)
            record["raw_result"] = str(raw)
        record["out"] += f"\n[tool {record['tool_s']:.3f}s; state={state}]"
        resf.write(json.dumps(record, ensure_ascii=False) + "\n")
        resf.flush()
        with open(JOBS, "a") as f:
            f.write(json.dumps({"id": job["id"], "ok": ok, "done": True, "state": state}) + "\n")

    def result_text(message, job):
        if "error" in message:
            return False, json.dumps(message["error"], ensure_ascii=False)
        result = message.get("result", {})
        texts = []
        image_index = 0
        for block in result.get("content", []):
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "image":
                image_index += 1
                ext = "jpg" if block.get("mimeType") == "image/jpeg" else "png"
                fp = BASE / f"img-{job['id']}-{image_index}.{ext}"
                try:
                    fp.write_bytes(base64.b64decode(block["data"], validate=True))
                    texts.append(f"[image: {fp}]")
                except (ValueError, OSError) as exc:
                    texts.append(f"[image decode failed; see raw_result: {exc}]")
        return not result.get("isError", False), "\n".join(texts)

    def stopped(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    try:
        snd({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {"elicitation": {"form": {}}},
            "clientInfo": {"name": "dsh-cuad", "version": "2.1"}}})
        init_deadline = time.monotonic() + 15
        while True:
            left = init_deadline - time.monotonic()
            if left <= 0:
                raise RuntimeError("MCP initialize timed out")
            try:
                message, _ = inbox.get(min(left, 0.1))
            except queue.Empty:
                continue
            if message is None:
                raise RuntimeError("MCP exited during initialize")
            if message.get("id") == 1 and "method" not in message:
                if "error" in message:
                    raise RuntimeError("MCP initialize failed: " + str(message["error"]))
                break
            if "method" in message and "id" in message:
                snd(handle_elicit(message))
        snd({"jsonrpc": "2.0", "method": "notifications/initialized"})
        reqs, resf = open(REQ, "a+"), open(RES, "a+")
        # Requests from an earlier daemon generation must never be replayed.
        reqs.seek(0, os.SEEK_END)
        ready_tmp = BASE / ("ready-" + str(os.getpid()) + ".tmp")
        ready_tmp.write_text(json.dumps({"pid": os.getpid(), "version": "2.1"}))
        ready_tmp.replace(READY)
        while True:
            try:
                message, received = inbox.get(0.02)
            except queue.Empty:
                message, received = {}, None
            if message is None:
                raise RuntimeError("MCP pipe closed; execution result unknown")
            if message:
                if "method" in message and "id" in message:
                    snd(handle_elicit(message))
                elif active and message.get("id") == active[0]:
                    job = active[1]
                    ok, out = result_text(message, job)
                    finish(job, ok, out, "completed" if ok else "failed", received, message)
                    active = None
            now = time.monotonic()
            if active and now >= active[1]["deadline_monotonic"]:
                raise RuntimeError("MCP deadline exceeded; execution result unknown")
            if p.poll() is not None:
                raise RuntimeError("MCP process exited; execution result unknown")
            if active:
                continue
            offset = reqs.tell()
            line = reqs.readline()
            if not line:
                continue
            if not line.endswith("\n"):
                reqs.seek(offset)  # A concurrent append has not finished yet.
                continue
            try:
                job = json.loads(line)
            except ValueError:
                continue
            if job.get("daemon_pid") != os.getpid():
                continue
            now = time.monotonic()
            if now >= job.get("deadline_monotonic", 0):
                finish(job, False, "请求在执行前已过期，未发送给 MCP。", "not_sent")
                continue
            seq += 1
            job["dispatched_monotonic"] = now
            active = (seq, job)
            with open(JOBS, "a") as f:
                f.write(json.dumps({"id": job["id"], "done": False, "code": job["code"][:120]}) + "\n")
            full = PRELUDE + "\n" + job["code"] + (SHOT if job.get("shot", False) else "")
            snd({"jsonrpc": "2.0", "id": seq, "method": "tools/call", "params": {
                "name": "js", "arguments": {"code": full,
                    "timeout_ms": max(1, int((job["deadline_monotonic"] - now) * 1000))},
                "_meta": {"x-codex-turn-metadata": TURN_META, "form_elicitation_supported": True}}})
    except Exception as exc:
        audit({"kind": "fatal", "desc": str(exc)})
        return 1
    finally:
        if active and resf:
            finish(active[1], False, "调用中断，结果未知；未自动重放。请重新观察页面后决定下一步。", "unknown")
        for f in (READY, PIDF):
            try:
                value = json.loads(f.read_text()) if f == READY else int(f.read_text())
                if (value.get("pid") if isinstance(value, dict) else value) == os.getpid():
                    f.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for f in (reqs, resf, p.stdin, p.stdout, errf):
            if f:
                f.close()


# ------------------------------------------------------------------ client
def daemon_alive():
    try:
        os.kill(int(PIDF.read_text().strip()), 0)
        return True
    except Exception:
        return False


def ready_pid():
    try:
        pid = int(PIDF.read_text().strip())
        ready = json.loads(READY.read_text())
        return pid if ready.get("pid") == pid and daemon_alive() else None
    except (OSError, ValueError):
        return None


def ensure_daemon(timeout=15):
    """Start only before dispatch; never restart/replay an in-flight request."""
    BASE.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(START_LOCK, "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("等待 daemon 启动锁超时；请求尚未发送。")
                time.sleep(0.02)
        if ready_pid():
            return False
        if daemon_alive():
            raise RuntimeError("现有 daemon 未就绪或仍是旧版本；请在空闲时 stop/start，请求尚未发送。")
        READY.unlink(missing_ok=True)
        PEND.unlink(missing_ok=True)
        ANS.unlink(missing_ok=True)
        pr = subprocess.Popen([sys.executable, __file__, "daemon"],
                              start_new_session=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=pr.wait, daemon=True).start()
        PIDF.write_text(str(pr.pid))
        while time.monotonic() < deadline:
            if ready_pid() == pr.pid:
                return True
            if pr.poll() is not None:
                raise RuntimeError("daemon 启动失败；请求尚未发送。查看 daemon.log / audit.jsonl。")
            time.sleep(0.02)
        pr.terminate()
        raise RuntimeError("daemon 就绪超时；请求尚未发送。")


def enqueue(code, deadline, shot=False):
    pid = ready_pid()
    if pid is None:
        raise RuntimeError("daemon 不可用；请求尚未发送。")
    job = {"id": uuid.uuid4().hex, "code": code, "shot": shot, "daemon_pid": pid,
           "submitted_monotonic": time.monotonic(), "deadline_monotonic": deadline}
    # A lock prevents simultaneous clients from interleaving a JSONL append.
    with open(REQ, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(job, ensure_ascii=False) + "\n")
        f.flush()
    return job


def _send_raw(code, timeout, shot=False):
    """One dispatch, one deadline. Any unknown outcome requires a fresh observation."""
    deadline = time.monotonic() + timeout
    try:
        ensure_daemon(timeout=min(15, timeout))
        job = enqueue(code, deadline, shot)
    except (OSError, RuntimeError) as exc:
        return {"ok": False, "state": "not_sent", "out": str(exc)}
    pos = 0
    approval_id = None
    while time.monotonic() < deadline:
        if RES.exists():
            with open(RES) as f:
                f.seek(pos)
                while True:
                    offset = f.tell()
                    line = f.readline()
                    if not line or not line.endswith("\n"):
                        pos = offset
                        break
                    try:
                        result = json.loads(line)
                    except ValueError:
                        continue
                    if result.get("id") == job["id"]:
                        return result
        if ready_pid() != job["daemon_pid"]:
            return {"id": job["id"], "ok": False, "state": "unknown",
                    "out": "daemon 中断，结果未知；未自动重放。请重新观察页面后决定下一步。"}
        if PEND.exists():
            try:
                pending = json.loads(PEND.read_text())
                if pending.get("id") != approval_id:
                    approval_id = pending.get("id")
                    print("待用户审批：" + pending.get("desc", ""), file=sys.stderr)
            except (OSError, ValueError):
                pass
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    return {"id": job["id"], "ok": False, "state": "unknown",
            "out": "调用达到期限，结果未知；未自动重放。请重新观察页面后决定下一步。"}


def doctor():
    """自检：确认运行环境、扩展连接与状态目录是否就绪。"""
    ok = True

    def chk(label, cond, detail="", fix=""):
        nonlocal ok
        mark = "OK  " if cond else "FAIL"
        if not cond:
            ok = False
        print(f"[{mark}] {label}" + (f"  — {detail}" if detail else ""))
        if not cond and fix:
            print(f"       修复: {fix}")

    print("codex-cua 自检\n" + "-" * 46)

    # 1. 运行时目录
    chk("运行时目录", os.path.isdir(ROOT), ROOT,
        "安装 ChatGPT/Codex 桌面端，或设置 CUA_RUNTIME_DIR 指向其 cua_node 目录")
    node_repl = os.path.join(ROOT, "bin", "node_repl")
    if os.path.isdir(ROOT):
        chk("node_repl 可执行", os.path.exists(node_repl), node_repl,
            "运行时目录不完整，重装桌面端")

    # 2. 扩展服务
    chk("扩展服务目录", os.path.isdir(CP), CP,
        "首次在 ChatGPT 里启用一次浏览器功能，或设置 CUA_BROWSER_SERVICE_DIR")

    # 3. Sky 服务（原生 AX + 截图）
    sky = os.path.join(HOME, ".codex", "computer-use", "Codex Computer Use.app")
    chk("Sky 服务", os.path.exists(sky), sky,
        "打开一次桌面端的 computer use 功能以完成注册")

    # 4. 状态目录可写
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        probe = BASE / ".doctor-probe"
        probe.write_text("1"); probe.unlink()
        chk("状态目录可写", True, str(BASE))
    except OSError as exc:
        chk("状态目录可写", False, str(exc), "检查磁盘权限，或设置 CUAD_STATE_DIR")

    # 5. 会话与扩展连接（需要真正跑一次）
    try:
        r = _send_raw('nodeRepl.write("EXT=" + (await cua.listBrowsers()).filter(b=>b.type==="extension").length)',
                      30, shot=False)
        out = r.get("out") or ""
        m = re.search(r'EXT=(\d+)', out)
        n = int(m.group(1)) if m else -1
        chk("Chrome 扩展已连接", r.get("ok") and n > 0,
            f"extension 浏览器 {n} 个" if n >= 0 else out[:80],
            "打开 Chrome 并确认 ChatGPT/Codex 扩展已安装且启用")
    except (OSError, RuntimeError) as exc:
        chk("Chrome 扩展已连接", False, str(exc)[:90], "先执行 start 看具体报错")

    # 6. 会话状态
    print("-" * 46)
    print(f"会话: {'运行中 pid=' + PIDF.read_text().strip() if PIDF.exists() else '未运行'}"
          f"   策略: {policy()}   状态目录: {BASE}")
    print("\n结论: " + ("全部就绪，可以直接用。" if ok else "存在未通过项，按上面的修复建议处理后重试。"))
    return 0 if ok else 1


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    a = sys.argv[1:]
    cmd = a[0] if a else "help"

    if cmd in ("help", "-h", "--help"):
        print(__doc__); return 0

    if cmd == "doctor":
        return doctor()

    if cmd == "daemon":
        return daemon()

    if cmd == "start":
        try:
            restarted = ensure_daemon()
            print(f"{'started' if restarted else 'already ready'} pid={PIDF.read_text().strip()}  policy={policy()}")
            return 0
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if cmd == "stop":
        if PIDF.exists():
            try:
                os.kill(int(PIDF.read_text().strip()), signal.SIGTERM)
            except ProcessLookupError:
                PIDF.unlink(missing_ok=True)
                READY.unlink(missing_ok=True)
        print("已请求停止 daemon；已有截图与调用记录保留。")
        return 0

    if cmd == "status":
        if not PIDF.exists(): print("not running"); return 1
        try:
            os.kill(int(PIDF.read_text().strip()), 0)
            print(f"running pid={PIDF.read_text().strip()}  policy={policy()}"); return 0
        except Exception:
            print("dead"); return 1

    if cmd == "policy":
        if len(a) > 1:
            POL.write_text(a[1]); print("policy=" + a[1])
        else:
            print("policy=" + policy())
        return 0

    if cmd == "pending":
        if PEND.exists():
            print(PEND.read_text()); return 0
        print("(无待审批)"); return 0

    if cmd == "answer":
        if len(a) < 2: print("用法: answer accept|decline [json内容]"); return 2
        action = a[1]
        content = {}
        if len(a) > 2:
            try: content = json.loads(a[2])
            except Exception: content = {}
        if not PEND.exists(): print("(没有待审批请求)"); return 1
        rid = json.loads(PEND.read_text()).get("id")
        ANS.write_text(json.dumps({"for": rid, "action": action, "content": content},
                                  ensure_ascii=False))
        print(f"已应答 {rid} → {action}"); return 0

    if cmd == "log":
        n = int(a[1]) if len(a) > 1 else 30
        if not AUDIT.exists(): print("(无记录)"); return 0
        for line in AUDIT.read_text(errors="replace").splitlines()[-n:]:
            try:
                d = json.loads(line)
                print(f"{d.get('ts','')}  {d.get('kind',''):<22} {str(d.get('desc') or d.get('action') or '')[:110]}")
            except Exception:
                print(line[:140])
        return 0

    if cmd == "run":
        if len(a) < 2: print("用法: run <file.js> [--ms N]"); return 2
        ms = 180000
        if "--ms" in a:
            i = a.index("--ms"); ms = int(a[i+1]); a = a[:i] + a[i+2:]
        shot = "--shot" in a
        a = [x for x in a if x not in ("--shot", "--noshot")]
        code = pathlib.Path(a[1]).read_text(encoding="utf-8")
        t0 = time.time(); r = _send_raw(code, ms / 1000, shot=shot); dt = time.time() - t0
        print(f"--- {dt:.1f}s {'OK' if r.get('ok') else 'ERR'} ---")
        print(r.get("out", ""))
        return 0 if r.get("ok") else 1

    if cmd == "clean":
        n = 0
        for f in list(BASE.glob("img-*")):
            try: f.unlink(); n += 1
            except Exception: pass
        for f in (PEND, ANS):
            f.unlink(missing_ok=True)
        print(f"已清理 {n} 张截图")
        return 0

    if cmd == "jobs":
        if not JOBS.exists(): print("(无)"); return 0
        for line in JOBS.read_text(errors="replace").splitlines()[-20:]:
            try:
                d = json.loads(line)
                print(f"{'done' if d.get('done') else 'RUN '}  {d['id']}  {str(d.get('code',''))[:80]}")
            except Exception: pass
        return 0

    if cmd == "send":
        ms = 120000
        shot = "--shot" in a          # 默认不截图；需要看图才加 --shot
        a = [x for x in a if x not in ("--shot", "--noshot")]
        if "--ms" in a:
            i = a.index("--ms"); ms = int(a[i+1]); a = a[:i] + a[i+2:]
        bg = "--bg" in a
        args = [x for x in a[1:] if x != "--bg"]
        code = args[0] if args else sys.stdin.read()
        if bg:
            try:
                ensure_daemon()
                job = enqueue(code, time.monotonic() + ms / 1000, shot)
                print(job["id"])
                return 0
            except (OSError, RuntimeError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
        t0 = time.time(); r = _send_raw(code, ms / 1000, shot=shot); dt = time.time() - t0
        print(f"--- {dt:.1f}s {'OK' if r.get('ok') else 'ERR'} ---")
        print(r.get("out", ""))
        return 0 if r.get("ok") else 1

    print(__doc__); return 0


if __name__ == "__main__":
    sys.exit(main())
