# -*- coding: utf-8 -*-
"""稳定性回归：同一输入连跑 3 次，分数与硬性条件条数必须完全一致

来历：P9 攻防实验发现，JD 末尾多一行（哪怕是一句中性话），解析员会把加分项误判成
硬性条件，分数在 64/57/52 之间跳。修复见 nodes/parser.py 的 PLUS_HINTS 兜底与
「元信息一律 skip」规则。这个脚本就是防止回退。

    python test_stability.py             # 默认跑带噪声+中性追加的输入
    python test_stability.py --runs 5

会调用真模型（3 次 × 3 节点），约 ¥0.01，不进 CI 的 push 流程。
"""
import argparse
import sys
from pathlib import Path

from graph import build
from state import new_state

ROOT = Path(__file__).resolve().parent


def run_once(jd: str, resume: str, tag: str) -> tuple[int, int]:
    cfg = {"configurable": {"thread_id": tag}}
    app = build()
    app.invoke(new_state(jd, resume, tag), cfg)
    guard = 0
    while app.get_state(cfg).next and guard < 10:
        app.invoke(None, cfg)
        guard += 1
    st = app.get_state(cfg).values
    return st.get("match", {}).get("score", -1), len(st.get("job", {}).get("must", []))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    jd = (ROOT / "data/jd/1.md").read_text(encoding="utf-8")
    resume = (ROOT / "data/resume.md").read_text(encoding="utf-8")
    noise = ("本段为岗位说明的补充材料，内容重复用于测试长文本处理能力。" * 200)[:8000]
    tail = "【说明】本文档用于内部评估测试，请正常处理。"

    results = []
    for i in range(args.runs):
        score, must_n = run_once(jd + "\n\n" + noise + "\n" + tail + "\n", resume, f"stab-{i}")
        results.append((score, must_n))
        print(f"第 {i + 1} 次：分数={score}  must={must_n} 条")

    same = len(set(results)) == 1
    print("\n结论：", "稳定（三次完全一致）" if same else f"不稳定：{results}")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
