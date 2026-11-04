# -*- coding: utf-8 -*-
"""③ 建议员：只针对 accepted 的缺口给改写与面试题

硬约束（代码强制，不靠模型自觉）：
- 改写必须绑 gap_id + 简历原句 sid；sid 不存在直接丢弃
- 「原句」由本地按 sid 回填，不采信模型抄的版本（防抄错、防改写原文）
- 数字防伪：改句里出现原句没有的数字 → 标记 possible_fabrication
"""
import os
import re
import time

from state import State, append

NUM = re.compile(r"\d+(?:\.\d+)?")

SYSTEM = """你是求职简历顾问。给定若干「能力缺口」和候选人的简历句子，请给改写建议和面试准备题，只输出 JSON。

硬规矩：
1. 每条改写必须指明 gap_id（针对哪个缺口）和 src_sid（改写哪一句），src_sid 必须是给定句子里的编号
2. **不得编造简历里没有的经历、公司、项目、数字**；只能用已有事实换一种更有说服力的说法
3. 改句要具体、可读，别写「建议加强 XX」这种空话
4. 面试题 5 道，要针对缺口和岗位要求，写清考点
5. 行动建议 3 条，是候选人本周就能做的事
6. **简历里没有的信息（可到岗天数、实习期、薪资期望、语言成绩等）不得替候选人承诺**。
   遇到这类缺口，改写里不要写「可保证每周到岗 5 天」，而是写「此处建议补充你的可到岗安排」，
   把决定权交回候选人

输出格式（严格 JSON）：
{
  "rewrites": [{"gap_id": "g1", "src_sid": "s6", "改句": "...", "理由": "..."}],
  "questions": [{"q": "...", "考点": "..."}],
  "actions": ["...", "...", "..."]
}"""


def _build_user(gaps: list[dict], sentences: list[dict], job: dict) -> str:
    g = "\n".join(f"- {x['gap_id']}（{x['severity']}）：{x['desc']}" for x in gaps) or "（无）"
    s = "\n".join(f"{x['sid']}｜{x['section']}：{x['text']}" for x in sentences)
    return (f"【目标岗位】{job.get('title', '')}\n\n【能力缺口】\n{g}\n\n"
            f"【简历句子】\n{s}\n\n请输出改写建议、5 道面试题、3 条行动建议。"
            f"改写的 src_sid 必须来自上面的编号。")


def run_llm(state: State) -> dict:
    from llm import chat_json

    t0 = time.time()
    resume = state["resume"]
    match = state["match"]
    job = state["job"]
    sentences = resume.get("sentences", [])
    by_sid = {s["sid"]: s for s in sentences}
    accepted = [g for g in match.get("gaps", []) if g.get("status") == "accepted"]
    accepted_ids = {g["gap_id"] for g in accepted}

    data, usage = chat_json(SYSTEM, _build_user(accepted, sentences, job))
    errors, rewrites = [], []

    for r in (data.get("rewrites") or [])[:10]:
        gid, sid = str(r.get("gap_id", "")), str(r.get("src_sid", ""))
        if gid not in accepted_ids:
            errors.append({"step": "advisor", "type": "ghost_gap_ref",
                           "detail": f"引用了不在 accepted 里的缺口 {gid}", "recovered": True})
            continue
        if sid not in by_sid:
            errors.append({"step": "advisor", "type": "bad_src_sid",
                           "detail": f"src_sid={sid} 不存在，已丢弃该条改写", "recovered": True})
            continue

        origin = by_sid[sid]["text"]                       # 原句由本地回填
        new_text = str(r.get("改句", "")).strip()
        if not new_text:
            continue

        # 数字防伪，分三档：
        #   原句里有 → 正常
        #   原句没有但简历别处有 → 合法引用（跨句取材），放行
        #   全篇简历都没有 → 判为编造，打 flag
        # 注意按「数字整体」比较，不能用子串：'3' 是 '2023.09' 的子串，会漏判
        origin_nums = set(NUM.findall(origin))
        resume_nums = set(NUM.findall(state["input"]["resume"]))
        invented = [n for n in NUM.findall(new_text)
                    if n not in origin_nums and n not in resume_nums]
        flag = ""
        if invented:
            flag = "possible_fabrication"
            errors.append({"step": "advisor", "type": "number_not_in_source",
                           "detail": f"{gid} 的改句出现简历里没有的数字 {invented}", "recovered": False})

        rewrites.append({"src_sid": sid, "原句": origin, "改句": new_text,
                         "gap_id": gid, "理由": str(r.get("理由", ""))[:60],
                         "flag": flag})

    questions = [{"q": str(q.get("q", ""))[:120], "考点": str(q.get("考点", ""))[:30]}
                 for q in (data.get("questions") or []) if q.get("q")][:5]
    actions = [str(a)[:60] for a in (data.get("actions") or [])][:3]

    return {
        "advice": {"rev": 1, "match_rev": match.get("rev", 1),
                   "rewrites": rewrites, "questions": questions, "actions": actions},
        "errors": list(state.get("errors", [])) + errors,
        "trace": append(state, "trace", {
            "step": "advisor", "ms": int((time.time() - t0) * 1000),
            "tokens": usage["prompt"] + usage["completion"],
            "tokens_in": usage["prompt"], "tokens_out": usage["completion"],
            "note": f"改写={len(rewrites)} 题={len(questions)} 动作={len(actions)}",
        }),
    }


# ============ 规则版（P7_STUB=1） ============
def _pick_sentence(resume: dict, gap: dict):
    sents = resume.get("sentences", [])
    if not sents:
        return None
    for key in ("项目", "实习", "技能"):
        for s in sents:
            if s["section"] == key:
                return s
    return max(sents, key=lambda s: len(s["text"]))


def run_stub(state: State) -> dict:
    t0 = time.time()
    resume, match = state["resume"], state["match"]
    accepted = [g for g in match.get("gaps", []) if g.get("status") == "accepted"]

    rewrites = []
    for gap in accepted:
        sent = _pick_sentence(resume, gap)
        if not sent:
            continue
        rewrites.append({"src_sid": sent["sid"], "原句": sent["text"],
                         "改句": f"{sent['text']}（补充可量化结果：{gap['desc']}）",
                         "gap_id": gap["gap_id"], "理由": f"弥补缺口：{gap['desc']}", "flag": ""})

    questions = [{"q": "你为什么选择这个岗位？", "考点": "动机"}]
    actions = [f"补充：{g['desc']}" for g in accepted[:3]]

    return {
        "advice": {"rev": 1, "match_rev": match.get("rev", 1),
                   "rewrites": rewrites, "questions": questions, "actions": actions},
        "trace": append(state, "trace", {
            "step": "advisor", "ms": int((time.time() - t0) * 1000), "tokens": 0,
            "note": f"[规则版] 改写={len(rewrites)}",
        }),
    }


def run(state: State) -> dict:
    """分级开关：P7_STUB_ADVISOR=1 单独让本节点走规则版，P7_STUB=1 则全部走规则"""
    if os.environ.get("P7_STUB") == "1" or os.environ.get("P7_STUB_ADVISOR") == "1":
        return run_stub(state)
    return run_llm(state)
