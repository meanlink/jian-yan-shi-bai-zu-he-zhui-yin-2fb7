#!/usr/bin/env bash
# 一键启动三室演示：1 个协调端 + 3 个独立参与端（各自端口与 SQLite）
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p data
echo "生成演示数据（私有标签仅写入各室数据文件）…"
python3 -m cofail.demo.make_data

pids=()
cleanup() {
  echo ""
  echo "停止全部服务…"
  kill "${pids[@]}" 2>/dev/null || true
}
trap cleanup EXIT

python3 -m cofail.coordinator.serve & pids+=($!)
python3 -m cofail.lab.serve lab-a & pids+=($!)
python3 -m cofail.lab.serve lab-b & pids+=($!)
python3 -m cofail.lab.serve lab-c & pids+=($!)

sleep 1.5
echo ""
echo "服务已启动："
echo "  协调端（发起/进度/简报）: http://127.0.0.1:8100"
echo "  参与端 lab-a:            http://127.0.0.1:8101"
echo "  参与端 lab-b:            http://127.0.0.1:8102"
echo "  参与端 lab-c:            http://127.0.0.1:8103"
echo "按 Ctrl+C 停止。"
wait
