#!/bin/bash
# 延迟测量脚本
# 用法: bash bench/latency.sh [路径到 cuad.py]

CUAD="${1:-assets/cuad.py}"
cd "$(dirname "$0")/.." || exit 1

echo "════════════════════════════════════════"
echo " codex-cua 延迟测量   $(date '+%F %T')"
echo "════════════════════════════════════════"

t() {
  local name="$1"; shift
  local s=$(python3 -c 'import time;print(time.time())')
  eval "$@" > /dev/null 2>&1
  local e=$(python3 -c 'import time;print(time.time())')
  printf "  %-34s %8.2f s\n" "$name" "$(python3 -c "print($e-$s)")"
}

echo
echo "【冷启动】"
python3 "$CUAD" stop >/dev/null 2>&1
t "首次调用（含自动启动）" "python3 $CUAD send --ms 60000 'nodeRepl.write(1)'"

echo
echo "【热调用，各 3 次】"
for i in 1 2 3; do
  t "纯 JavaScript #$i" "python3 $CUAD send --ms 60000 'nodeRepl.write(2)'"
done

echo
echo "【观测】"
for i in 1 2 3; do
  t "AX 读取 #$i" "python3 $CUAD send --ms 60000 'await (await QA()).getAXState()'"
done
for i in 1 2; do
  t "截图 #$i" "python3 $CUAD send --ms 60000 --shot 'nodeRepl.write(3)'"
done

echo
echo "【定位】"
for i in 1 2 3; do
  t "Q() 文本匹配 #$i" "python3 $CUAD send --ms 60000 'const h=await Q(/Chrome/);nodeRepl.write(h.length)'"
done

echo
echo "════════════════════════════════════════"
du -sh ~/.cuad 2>/dev/null | awk '{print " 状态目录: "$1}'
echo "════════════════════════════════════════"
