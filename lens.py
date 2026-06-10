#!/usr/bin/env python3
"""lens 统一入口 — 把日常动作收成一条命令。Python 标准库 only。

  python3 lens.py            # 晨间流程：采集 → AI 总结 → 看板（自动开浏览器）
  python3 lens.py morning    # 同上
  python3 lens.py collect    # 只采集（参数透传 collect.py，如 --source hf-models / --list）
  python3 lens.py digest     # 只总结（参数透传 summarize.py，如 --all / --engine claude）
  python3 lens.py serve      # 只起看板并自动开浏览器（参数透传 server.py，如 --port 9000）
  python3 lens.py demo       # 示例数据看板 = server.py --demo --open（不采集、不跑 LLM）
  python3 lens.py test       # 全套零网络测试

晨间流程是容错的：采集失败（断网）不挡总结，总结失败（机器上没有 codex/claude CLI）
不挡看板——最坏情况你也能打开看板处理已有条目。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run_step(label: str, argv: list[str]) -> int:
    print(f"\n━━ {label} ━━")
    return subprocess.run([sys.executable, *argv], cwd=ROOT).returncode


def morning() -> int:
    failed = []
    if run_step("1/3 采集（collect.py）", ["collect.py"]) != 0:
        failed.append("采集")
    if run_step("2/3 AI 总结（summarize.py）", ["summarize.py"]) != 0:
        failed.append("总结")
    if failed:
        print(f"\n⚠ {' / '.join(failed)}失败，看板照常打开（处理已有条目不受影响）。")
    return run_step("3/3 看板（server.py）", ["server.py", "--open"])


def main() -> int:
    cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else "morning"), sys.argv[2:]
    table = {
        "collect": lambda: run_step("采集", ["collect.py", *rest]),
        "digest": lambda: run_step("AI 总结", ["summarize.py", *rest]),
        "serve": lambda: run_step("看板", ["server.py", "--open", *rest]),
        "demo": lambda: run_step("示例看板", ["server.py", "--demo", "--open", *rest]),
        "test": lambda: subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", *rest],
            cwd=ROOT).returncode,
        "morning": morning,
    }
    if cmd in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if cmd not in table:
        print(f"未知子命令：{cmd}\n")
        print(__doc__.strip())
        return 2
    try:
        return table[cmd]()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
