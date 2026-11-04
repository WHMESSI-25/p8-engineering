# -*- coding: utf-8 -*-
"""P8 评估：跑完 10 个 case，按指标口径打分并报三个数

    python evaluate.py                # 全量跑
    python evaluate.py c01 c05        # 只跑指定 case
    P7_STUB=1 python evaluate.py      # 规则版（不烧 token，用于 CI 结构回归）

判据见 docs/指标口径.md：四项全中记 1 分。
真正花钱的只有三次 LLM 调用，所以成本按 trace 里的 tokens_in/out 累加。
"""
import json
import os
import statistics
import sys
import time
from pathlib import Path

from graph import build
from state import new_state

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "eval" / "cases.json"
RESULTS = ROOT / "eval" / "results"

# DeepSeek deepseek-chat（V4-Flash 非思考模式）单价，美元/百万 token
PRICE_IN = 0.14 / 1_000_000
PRICE_OUT = 0.28 / 1_000_000
FX = 7.1                      # 美元→人民币
BAD_ERRORS = ("possible_fabrication", "ghost_gap_ref", "bad_src_sid")


def run_case(case: dict) -> dict:
    """跑一个 case：自动跳过两个人审点，返回最终 state"""
    jd = (ROOT / case["jd"]).read_text(encoding="utf-8")
    resume = (ROOT / case["resume"]).read_text(encoding="utf-8")
    rid = f"eval-{case['id']}-{int(time.time())}"
    config = {"configurable": {"thread_id": rid}}
    app = build()
    app.invoke(new_state(jd, resume, rid), config)
    guard = 0
    while app.get_state(config).next and guard < 10:
        app.invoke(None, config)
        guard += 1
    return app.get_state(config).values


def _hit_any(text: str, words: list[str]) -> bool:
    return any(w.lower() in text.lower() for w in words)


def judge(case: dict, st: dict) -> dict:
    """四项判据 + 一个附加（是否按预期劝退）"""
    job, match, advice = st.get("job", {}), st.get("match", {}), st.get("advice", {})
    must_texts = [m["text"] for m in job.get("must", [])]
    gap_texts = [g["desc"] for g in match.get("gaps", [])]
    errs = {e.get("type") for e in st.get("errors", [])}

    # ① 硬性条件抽全（散文 JD 则要求判出语义空转）
    if case.get("expect_semantic_invalid"):
        must_ok = bool(job.get("semantic_invalid"))
    else:
        must_ok = all(any(_hit_any(t, grp) for t in must_texts) for grp in case["expect_must"])

    # ② 缺口不遗漏
    gap_ok = all(any(_hit_any(t, grp) for t in gap_texts) for grp in case["expect_gaps"])

    # ③ 零编造
    clean_ok = not (errs & set(BAD_ERRORS))
    if case.get("expect_semantic_invalid"):
        clean_ok = clean_ok and "semantic_invalid" in errs

    # ④ 分数落区间
    lo, hi = case["score_range"]
    score = match.get("score", 0)
    score_ok = lo <= score <= hi

    # 附加：是否按预期劝退（不计入四项，单独展示）
    refuse_ok = (len(advice.get("rewrites", [])) == 0) if case.get("refuse") else bool(advice.get("rewrites"))

    return {"must_ok": must_ok, "gap_ok": gap_ok, "clean_ok": clean_ok,
            "score_ok": score_ok, "refuse_ok": refuse_ok,
            "passed": all([must_ok, gap_ok, clean_ok, score_ok])}


def cost_of(st: dict) -> tuple[float, int]:
    """返回（人民币成本, 总 token）"""
    tin = sum(t.get("tokens_in", 0) for t in st.get("trace", []))
    tout = sum(t.get("tokens_out", 0) for t in st.get("trace", []))
    return (tin * PRICE_IN + tout * PRICE_OUT) * FX, tin + tout


def main() -> None:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data["cases"]
    only = set(sys.argv[1:])
    if only:
        cases = [c for c in cases if c["id"] in only]

    rows, details = [], []
    t_all = time.time()
    for c in cases:
        t0 = time.time()
        st = run_case(c)
        elapsed = time.time() - t0
        verdict = judge(c, st)
        cost, tokens = cost_of(st)
        rows.append({
            "id": c["id"], "desc": c["desc"], **verdict,
            "score": st.get("match", {}).get("score", 0),
            "cost": cost, "tokens": tokens, "ms": int(elapsed * 1000),
        })
        details.append({"case": c, "verdict": verdict, "state": st})
        flag = "✔" if verdict["passed"] else "✘"
        print(f"{flag} {c['id']} {c['desc'][:22]:<24} "
              f"分={rows[-1]['score']:>3} 成本=¥{cost:.4f} 延迟={elapsed:5.1f}s "
              f"[must{'✓' if verdict['must_ok'] else '✗'} "
              f"gap{'✓' if verdict['gap_ok'] else '✗'} "
              f"净{'✓' if verdict['clean_ok'] else '✗'} "
              f"分{'✓' if verdict['score_ok'] else '✗'}]")

    n = len(rows)
    passed = sum(r["passed"] for r in rows)
    total_cost = sum(r["cost"] for r in rows)
    lat = sorted(r["ms"] for r in rows)
    p50 = lat[len(lat) // 2] if lat else 0

    print("\n" + "=" * 62)
    print(f"通过率：{passed}/{n} = {passed / n * 100:.0f}%")
    print(f"总成本：¥{total_cost:.4f}   单次：¥{total_cost / n:.4f}   千次：¥{total_cost / n * 1000:.2f}")
    print(f"延迟 P50：{p50 / 1000:.1f}s   最长：{lat[-1] / 1000 if lat else 0:.1f}s")
    print("=" * 62)

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    mode = "stub" if os.environ.get("P7_STUB") == "1" else "llm"
    out = RESULTS / f"{stamp}-{mode}.json"
    out.write_text(json.dumps({
        "mode": mode, "stamp": stamp, "total_sec": round(time.time() - t_all, 1),
        "pass_rate": passed / n, "avg_cost": total_cost / n, "p50_ms": p50,
        "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已保存：{out}")


if __name__ == "__main__":
    main()
