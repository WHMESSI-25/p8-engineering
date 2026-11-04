# -*- coding: utf-8 -*-
"""P7 命令行入口：跑一次完整的求职匹配

    python main.py --jd data/jd/1.md --resume data/resume.md            # 交互式（两个暂停点等你确认）
    python main.py --jd data/jd/1.md --resume data/resume.md --auto     # 自动接受两个暂停点（冒烟用）
    python main.py ... --auto --add-must "必须会 Go"                     # 顺带验证「人审改动是否传导」

每次运行把完整 state 写到 runs/<run_id>.json，用于复盘定位信息在哪一步变形。
"""
import argparse
import json
import time
import uuid
from pathlib import Path

from graph import build
from nodes import matcher
from state import fold_job, new_state

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"

# DeepSeek 单价（美元/百万 token），与 docs/指标口径.md 保持一致
PRICE_IN, PRICE_OUT, FX = 0.14 / 1_000_000, 0.28 / 1_000_000, 7.1


def _cost(state: dict) -> tuple[float, int]:
    tin = sum(t.get("tokens_in", 0) for t in state.get("trace", []))
    tout = sum(t.get("tokens_out", 0) for t in state.get("trace", []))
    return (tin * PRICE_IN + tout * PRICE_OUT) * FX, tin + tout


def _replay(path: str) -> None:
    """回放一次历史运行：不发任何请求，只看当时的完整状态"""
    st = json.loads(Path(path).read_text(encoding="utf-8"))
    print(f"回放：{path}")
    print(f"状态={st.get('status')}  run_id={st.get('run_id')}")
    print(f"解析：{len(st.get('job', {}).get('must', []))} 条硬性 / "
          f"{len(st.get('job', {}).get('plus', []))} 条加分")
    m = st.get("match", {})
    print(f"匹配：{m.get('score')} 分（job_rev={m.get('job_rev')}）")
    for b in m.get("breakdown", []):
        print(f"  [{b['level']}] {b['item'][:30]} ← {b.get('evidence', {}).get('sid', '')}")
    print("缺口：")
    for g in m.get("gaps", []):
        print(f"  ({g['severity']}/{g['status']}) {g['desc'][:40]}")
    a = st.get("advice", {})
    print(f"改写 {len(a.get('rewrites', []))} 条 / 面试题 {len(a.get('questions', []))} 道")
    print("各节点耗时：")
    for t in st.get("trace", []):
        print(f"  {t['step']:<8} {t.get('ms', 0):>6}ms tokens={t.get('tokens', 0)}")
    if st.get("errors"):
        print("错误记录：")
        for e in st["errors"]:
            print(f"  {e['type']}：{e['detail'][:50]}")


def _show_pause(values: dict, node: str) -> None:
    if node == "gate1":
        job = values.get("job", {})
        print("\n" + "=" * 60)
        print("【人工确认点 1】解析结果")
        if job.get("semantic_invalid"):
            print("  ⚠ 没抽到任何硬性条件——多半是 JD 写成宣传文案了。")
            print("    请在这里手动补充条件；若直接放行，后面的匹配结果没有意义。")
        print(f"  岗位：{job.get('title', '')}")
        print(f"  硬性（{len(job.get('must', []))} 条）：")
        for m in job.get("must", []):
            print(f"    - {m['text']}")
        print(f"  加分（{len(job.get('plus', []))} 条）：")
        for p in job.get("plus", []):
            print(f"    - {p['text']}")
        print(f"  简历句子：{len(values.get('resume', {}).get('sentences', []))} 句")
        print("=" * 60)
    else:
        match = values.get("match", {})
        print("\n" + "=" * 60)
        print(f"【人工确认点 2】匹配结果：{match.get('score')} 分")
        for b in match.get("breakdown", []):
            print(f"    [{b['level']}] {b['item']}")
        print(f"  缺口 {len(match.get('gaps', []))} 条：")
        for g in match.get("gaps", []):
            print(f"    ({g['severity']}) {g['desc']}")
        print("=" * 60)


def _apply_edit(app, config, values, add_must: str | None) -> None:
    """把人审改动写进 edits（不改 job 本体）"""
    if not add_must:
        return
    job = values["job"]
    new_must = list(job["must"]) + [{
        "id": f"m{len(job['must']) + 1}",
        "text": add_must,
        "why": "用户在人审点 1 追加",
        "span": [0, 0],
    }]
    edit = {"point": 1, "target": "job.must",
            "before": job["must"], "after": new_must,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    app.update_state(config, {
        "edits": list(values.get("edits", [])) + [edit],
        "decisions": list(values.get("decisions", [])) + [{
            "point": 1, "action": "edit", "diff": {"+must": add_must},
            "ts": edit["ts"], "duration_ms": 0, "blind_accept": False}],
    })
    print(f"  ↳ 已追加硬性条件：{add_must}（job.rev 将 +1）")


def _apply_reject(app, config, values, ids: list[str]) -> None:
    """人审点 2：划掉不认同的缺口 → 对应条件改判命中 → 本地重算分数（不调模型）"""
    if not ids:
        return
    gaps = [dict(g) for g in values["match"]["gaps"]]
    breakdown = [dict(b) for b in values["match"]["breakdown"]]
    hit_items, rejected = set(), []
    for g in gaps:
        if g["gap_id"] in ids and g.get("status") == "accepted":
            g["status"] = "rejected"
            g["reason"] = "人审不认同该判定"
            hit_items.add(g.get("item", ""))
            rejected.append(g["gap_id"])
    for b in breakdown:
        if b["item"] in hit_items:
            b["level"] = "命中"

    job = fold_job(values["job"], values.get("edits", []))
    old = values["match"].get("score", 0)
    score = matcher.recompute(job, breakdown, values["match"].get("plus_hits", 0))
    match = dict(values["match"])
    match.update({"gaps": gaps, "breakdown": breakdown, "score": score,
                  "rev": match.get("rev", 1) + 1})
    app.update_state(config, {
        "match": match,
        "decisions": list(values.get("decisions", [])) + [{
            "point": 2, "action": "edit", "diff": {"rejected": rejected, "score": [old, score]},
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "duration_ms": 0, "blind_accept": False}],
    })
    print(f"  ↳ 已划掉 {len(rejected)} 条缺口：{rejected}，分数 {old} → {score}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jd")
    ap.add_argument("--resume")
    ap.add_argument("--auto", action="store_true", help="自动接受两个暂停点")
    ap.add_argument("--add-must", default=None, help="在人审点 1 追加一条硬性条件（测试用）")
    ap.add_argument("--reject", default="", help="在人审点 2 划掉的缺口编号，逗号分隔（测试用）")
    ap.add_argument("--replay", default=None, help="回放一次历史运行（runs/*.json），不发请求")
    args = ap.parse_args()

    if args.replay:
        _replay(args.replay)
        return
    if not args.jd or not args.resume:
        ap.error("跑一次任务需要同时给 --jd 和 --resume；只回放历史请用 --replay")

    jd = Path(args.jd).read_text(encoding="utf-8")
    resume = Path(args.resume).read_text(encoding="utf-8")
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    config = {"configurable": {"thread_id": run_id}}

    app = build()
    result = app.invoke(new_state(jd, resume, run_id), config)

    edited = False
    while True:
        snap = app.get_state(config)
        if not snap.next:
            break
        node = snap.next[0]
        _show_pause(snap.values, node)
        if node == "gate1":
            extra = args.add_must
            if not args.auto and not extra:
                # 提问即确认：直接输入要补的条件，回车表示不改
                raw = input("  → 要补充硬性条件吗？直接输入一句，回车跳过：").strip()
                extra = raw or None
            if not edited:
                _apply_edit(app, config, snap.values, extra)
                edited = True
        elif node == "gate2":
            ids = [x.strip() for x in args.reject.split(",") if x.strip()]
            if not ids and not args.auto:
                raw = input("  → 要划掉哪些缺口？填编号逗号分隔，回车跳过：").strip()
                ids = [x.strip() for x in raw.split(",") if x.strip()]
            _apply_reject(app, config, snap.values, ids)
        result = app.invoke(None, config)

    final = app.get_state(config).values

    print("\n" + "=" * 60)
    print(f"运行完成：{run_id}  状态={final.get('status')}")
    m = final.get("match", {})
    print(f"匹配度：{m.get('score')} 分（job_rev={m.get('job_rev')}）")
    a = final.get("advice", {})
    if a.get("rewrites"):
        print("改写建议：")
        for r in a["rewrites"]:
            print(f"  - [{r['src_sid']}→{r['gap_id']}] {r['改句'][:60]}…")
    flagged = [r for r in a.get("rewrites", []) if r.get("flag")]
    if flagged:
        print("⚠ 被数字校验拦下的改写（疑似编造，原句里没有这些数字）：")
        for r in flagged:
            print(f"  - [{r['src_sid']}] {r['改句'][:70]}")
    if a.get("questions"):
        print("面试题：")
        for q in a["questions"][:5]:
            print(f"  - {q['q']}")
    cost, tokens = _cost(final)
    print(f"本次消耗：{tokens} tokens ≈ ¥{cost:.4f}")
    if final.get("errors"):
        print(f"错误记录：{len(final['errors'])} 条")
    print("=" * 60)

    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"{run_id}.json"
    out.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完整 state 已保存：{out}")


if __name__ == "__main__":
    main()
