# -*- coding: utf-8 -*-
"""结构回归测试：不调用模型（P7_STUB=1），只验证编排本身没坏

放进 CI 每次 push 跑，零成本。真模型的质量评测走 evaluate.py（手动触发）。

覆盖四件事：
1. 两个人工确认点确实会暂停
2. 人审改动能传导（job.rev 自增、匹配重算）
3. 低分真的会走劝退分支（不产改写）
4. 散文型 JD 会被判语义空转
"""
import os
import sys

os.environ["P7_STUB"] = "1"        # 必须在导入节点之前设置

from pathlib import Path

from graph import build
from state import new_state

ROOT = Path(__file__).resolve().parent
FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✔ " if cond else "  ✘ ") + name + (f"（{detail}）" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def fresh():
    jd = (ROOT / "data/jd/1.md").read_text(encoding="utf-8")
    resume = (ROOT / "data/resume.md").read_text(encoding="utf-8")
    import uuid
    app = build()
    cfg = {"configurable": {"thread_id": "ci-" + uuid.uuid4().hex[:8]}}
    app.invoke(new_state(jd, resume, cfg["configurable"]["thread_id"]), cfg)
    return app, cfg


def main() -> int:
    print("1. 人工确认点会不会暂停")
    app, cfg = fresh()
    check("第一个暂停点存在", bool(app.get_state(cfg).next),
          f"next={app.get_state(cfg).next}")
    app_x, cfg_x = fresh()
    app_x.invoke(None, cfg_x)
    check("第二个暂停点存在", bool(app_x.get_state(cfg_x).next))

    print("2. 人审改动能不能传导")
    # 正确时序：在「人审点 1」暂停时改条件，再放行 → 匹配员必须用新版本重算
    values = app.get_state(cfg).values          # 此刻停在人审点 1 之前
    job = values["job"]
    edit = {"point": 1, "target": "job.must",
            "before": job["must"],
            "after": job["must"] + [{"id": "m99", "text": "必须会 Rust", "why": "测试", "span": [0, 0]}],
            "ts": "ci"}
    app.update_state(cfg, {"edits": [edit]})
    app.invoke(None, cfg)                        # 放行 → 匹配员重算 → 停在人审点 2
    st = app.get_state(cfg).values
    check("job 本体未被覆盖", st["job"]["rev"] == 1)
    check("edits 已记录", len(st["edits"]) == 1)
    check("匹配结果用新版本重算（job_rev=2）", st["match"].get("job_rev") == 2,
          f"实际 {st['match'].get('job_rev')}")

    print("2b. 守卫：匹配之后再改条件，必须留痕（stale_match）")
    app_b = build()
    cfg_b = {"configurable": {"thread_id": "ci-stale"}}
    jd1 = (ROOT / "data/jd/1.md").read_text(encoding="utf-8")
    resume_b = (ROOT / "data/resume.md").read_text(encoding="utf-8")
    app_b.invoke(new_state(jd1, resume_b, "ci-stale"), cfg_b)
    app_b.invoke(None, cfg_b)                    # 穿过人审点 1，停在人审点 2（match 已算完）
    v_b = app_b.get_state(cfg_b).values
    app_b.update_state(cfg_b, {"edits": [{
        "point": 2, "target": "job.must", "before": v_b["job"]["must"],
        "after": v_b["job"]["must"] + [{"id": "m98", "text": "必须会 Rust", "why": "测试", "span": [0, 0]}],
        "ts": "ci"}]})
    app_b.invoke(None, cfg_b)
    errs = {e.get("type") for e in app_b.get_state(cfg_b).values.get("errors", [])}
    check("守卫记下 stale_match", "stale_match" in errs, f"实际 errors={errs}")

    print("3. 低分会不会劝退")
    jd3 = (ROOT / "data/jd/3.md").read_text(encoding="utf-8")
    resume = (ROOT / "data/resume.md").read_text(encoding="utf-8")
    app2 = build()
    cfg2 = {"configurable": {"thread_id": "ci-refuse"}}
    app2.invoke(new_state(jd3, resume, "ci-refuse"), cfg2)
    guard = 0
    while app2.get_state(cfg2).next and guard < 5:
        app2.invoke(None, cfg2)
        guard += 1
    st2 = app2.get_state(cfg2).values
    check("低分不产改写", len(st2["advice"].get("rewrites", [])) == 0)
    check("有差距清单", len(st2["advice"].get("actions", [])) > 0)

    print("4. 散文型 JD 判语义空转")
    jd4 = (ROOT / "data/jd/4-prose.md").read_text(encoding="utf-8")
    app3 = build()
    cfg3 = {"configurable": {"thread_id": "ci-prose"}}
    app3.invoke(new_state(jd4, resume, "ci-prose"), cfg3)
    st3 = app3.get_state(cfg3).values
    check("must 为空且标记 semantic_invalid", st3["job"].get("semantic_invalid") is True)

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print("结构回归全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
