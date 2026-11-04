# -*- coding: utf-8 -*-
"""② 匹配员：逐条比对，算出差距

要点（评审 P0 的直接落点）：
- 模型只负责「逐条判定 + 指认证据」，**分数由本地公式算**（硬性 70 + 加分 30），
  不让模型自由给分，否则同一份材料两次跑分不一样
- 模型返回的 quote 必须能在对应句子/原文里搜到，搜不到就丢弃并记 error
- 判定前先 fold_job，读的是人审之后的生效版本
"""
import os
import re
import time

from state import State, append, fold_job

FATAL_HINTS = ("学历", "年", "专业", "本科", "硕士")

SYSTEM = """你是岗位匹配评估员。给定岗位的硬性条件/加分项，以及候选人的简历句子，逐条判断匹配程度，只输出 JSON。

判定标准：
- 命中：简历里有明确对应的经历或技能，能拿出证据
- 部分：方向相关但不完全对应（例如要求「十万级数据」，简历只写过万级）
- 缺失：简历里找不到任何相关证据

硬规矩：
1. 每条只需给出 sid（引用的简历句子编号），**不要回抄原文**——原文由程序按编号取
2. 只能引用上面列出的句子编号；判「缺失」时 sid 填 ""
3. 不许因为「看起来差不多」就判命中，必须有具体某一句作支撑

输出格式（严格 JSON）：
{
  "must_results": [{"id": "m1", "level": "命中|部分|缺失", "sid": "s3", "why": "10字内"}],
  "plus_results": [{"id": "p1", "level": "命中|未体现", "sid": "s5", "why": "10字内"}]
}"""

LEVEL_SCORE = {"命中": 1.0, "部分": 0.5, "缺失": 0.0}


def _build_user(job: dict, resume: dict) -> str:
    must = "\n".join(f"- {m['id']}：{m['text']}" for m in job.get("must", []))
    plus = "\n".join(f"- {p['id']}：{p['text']}" for p in job.get("plus", [])) or "（无）"
    # 句子用「sid｜栏目：正文」的写法，避免模型把标签一起抄进 quote
    sents = "\n".join(f"{s['sid']}｜{s['section']}：{s['text']}"
                      for s in resume.get("sentences", []))
    return (f"【硬性条件】\n{must}\n\n【加分项】\n{plus}\n\n【简历句子】\n{sents}\n\n"
            f"请逐条判定并输出 JSON，只回 sid，不要抄原文。")


_QUOTE_PREFIX = re.compile(r"^\s*(?:\[[^\]]{1,6}\]|（[^）]{1,6}）|\([^)]{1,6}\)|[a-zA-Z]\d{1,3}\s*[｜|:：])\s*")


def _clean_quote(q: str) -> str:
    """去掉模型可能带上的编号/栏目前缀"""
    prev = None
    q = q.strip()
    while prev != q:
        prev = q
        q = _QUOTE_PREFIX.sub("", q).strip()
    return q.strip(" 　\"'“”")


def _by_sid(sid: str, sentences: list[dict]) -> dict | None:
    """按编号取原文：原文不经过模型，模型只负责指认「哪一句」"""
    for s in sentences:
        if s["sid"] == sid:
            return {"doc": "resume", "quote": s["text"], "start": 0, "end": 0, "sid": sid}
    return None


def _resolve_evidence(sid: str, quote: str, sentences: list[dict]) -> dict | None:
    """校验模型给的证据是否真的存在；能修正 sid 就修正，修正不了返回 None"""
    quote = _clean_quote(quote)
    if not quote:
        return None
    by_sid = {s["sid"]: s for s in sentences}
    if sid in by_sid and quote in by_sid[sid]["text"]:
        return {"doc": "resume", "quote": quote,
                "start": 0, "end": 0, "sid": sid}
    for s in sentences:                      # sid 给错了但句子对得上，修正
        if quote in s["text"]:
            return {"doc": "resume", "quote": quote,
                    "start": 0, "end": 0, "sid": s["sid"]}
    return None


def recompute(job: dict, breakdown: list[dict], plus_hits: int) -> int:
    """按当前 breakdown 重算分数：纯函数，人审改动后立刻能出新分"""
    must_n = max(len(job.get("must", [])), 1)
    plus_n = max(len(job.get("plus", [])), 1)
    hit = sum(LEVEL_SCORE.get(b.get("level"), 0.0) for b in breakdown)
    return round(70 * hit / must_n + 30 * plus_hits / plus_n)


def _score(job: dict, must_levels: list[str], plus_hits: int) -> int:
    must_n = max(len(job.get("must", [])), 1)
    plus_n = max(len(job.get("plus", [])), 1)
    hit = sum(LEVEL_SCORE.get(lv, 0.0) for lv in must_levels)
    return round(70 * hit / must_n + 30 * plus_hits / plus_n)


def run_llm(state: State) -> dict:
    from llm import chat_json

    t0 = time.time()
    job = fold_job(state["job"], state.get("edits", []))
    resume = state["resume"]
    sentences = resume.get("sentences", [])

    data, usage = chat_json(SYSTEM, _build_user(job, resume))
    errors = []

    mr = {str(x.get("id")): x for x in (data.get("must_results") or [])}
    breakdown, gaps, levels = [], [], []
    for item in job.get("must", []):
        r = mr.get(item["id"]) or {}
        level = r.get("level") if r.get("level") in LEVEL_SCORE else "缺失"
        ev = _by_sid(str(r.get("sid", "")), sentences)

        if level in ("命中", "部分") and ev is None:
            errors.append({"step": "matcher", "type": "evidence_not_found",
                           "detail": f"{item['id']} 判为{level}但 sid={r.get('sid', '')!r} 不存在，降级为缺失",
                           "recovered": True})
            level = "缺失"

        if ev is None:      # 缺失：证据改为 JD 原文位置
            ev = {"doc": "jd", "quote": item["text"],
                  "start": item["span"][0], "end": item["span"][1], "sid": ""}

        levels.append(level)
        breakdown.append({"item": item["text"], "level": level, "evidence": ev})
        if level != "命中":
            fatal = level == "缺失" and any(h in item["text"] for h in FATAL_HINTS)
            gaps.append({"gap_id": f"g{len(gaps) + 1}", "desc": f"{level}：{item['text']}",
                         "item": item["text"],
                         "severity": "致命" if fatal else "重要",
                         "status": "accepted", "reason": "",
                         "evidence_sid": ev.get("sid", "")})

    pr = {str(x.get("id")): x for x in (data.get("plus_results") or [])}
    plus_hits = 0
    for item in job.get("plus", []):
        r = pr.get(item["id"]) or {}
        hit = r.get("level") == "命中" and _by_sid(str(r.get("sid", "")), sentences) is not None
        plus_hits += 1 if hit else 0
        if not hit:
            gaps.append({"gap_id": f"g{len(gaps) + 1}", "desc": f"加分项未体现：{item['text']}",
                         "item": item["text"],
                         "severity": "加分", "status": "accepted", "reason": "",
                         "evidence_sid": ""})

    score = _score(job, levels, plus_hits)

    return {
        "match": {"rev": 1, "job_rev": job.get("rev", 1), "score": score,
                  "plus_hits": plus_hits, "breakdown": breakdown, "gaps": gaps},
        "errors": list(state.get("errors", [])) + errors,
        "trace": append(state, "trace", {
            "step": "matcher", "ms": int((time.time() - t0) * 1000),
            "tokens": usage["prompt"] + usage["completion"],
            "tokens_in": usage["prompt"], "tokens_out": usage["completion"],
            "note": f"score={score} 命中{levels.count('命中')}/部分{levels.count('部分')}"
                    f"/缺失{levels.count('缺失')} job_rev={job.get('rev', 1)}",
        }),
    }


# ============ 规则版（P7_STUB=1） ============
TECH = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{1,15}")


def _match_one(item_text: str, sentences: list[dict]):
    want = [t.lower() for t in TECH.findall(item_text)]
    for s in sentences:
        hay = s["text"].lower()
        if want and all(t in hay for t in want):
            return "命中", s
    for s in sentences:
        hay = s["text"]
        for i in range(len(item_text) - 2):
            frag = item_text[i:i + 2]
            if frag.strip() and frag in hay:
                return "部分", s
    return "缺失", None


def run_stub(state: State) -> dict:
    t0 = time.time()
    job = fold_job(state["job"], state.get("edits", []))
    sentences = state["resume"].get("sentences", [])
    breakdown, gaps, levels = [], [], []
    for item in job.get("must", []):
        level, sent = _match_one(item["text"], sentences)
        levels.append(level)
        ev = ({"doc": "resume", "quote": sent["text"], "start": 0, "end": 0, "sid": sent["sid"]}
              if sent else {"doc": "jd", "quote": item["text"],
                            "start": item["span"][0], "end": item["span"][1], "sid": ""})
        breakdown.append({"item": item["text"], "level": level, "evidence": ev})
        if level != "命中":
            fatal = level == "缺失" and any(h in item["text"] for h in FATAL_HINTS)
            gaps.append({"gap_id": f"g{len(gaps) + 1}", "desc": f"{level}：{item['text']}",
                         "item": item["text"],
                         "severity": "致命" if fatal else "重要", "status": "accepted",
                         "reason": "", "evidence_sid": ev.get("sid", "")})

    plus_hits = 0
    for item in job.get("plus", []):
        level, _ = _match_one(item["text"], sentences)
        plus_hits += 1 if level == "命中" else 0
        if level != "命中":
            gaps.append({"gap_id": f"g{len(gaps) + 1}", "desc": f"加分项未体现：{item['text']}",
                         "item": item["text"],
                         "severity": "加分", "status": "accepted", "reason": "",
                         "evidence_sid": ""})

    score = _score(job, levels, plus_hits)
    return {
        "match": {"rev": 1, "job_rev": job.get("rev", 1), "score": score,
                  "plus_hits": plus_hits, "breakdown": breakdown, "gaps": gaps},
        "trace": append(state, "trace", {
            "step": "matcher", "ms": int((time.time() - t0) * 1000), "tokens": 0,
            "note": f"[规则版] score={score}",
        }),
    }


def run(state: State) -> dict:
    """分级开关：P7_STUB_MATCHER=1 单独让本节点走规则版，P7_STUB=1 则全部走规则"""
    if os.environ.get("P7_STUB") == "1" or os.environ.get("P7_STUB_MATCHER") == "1":
        return run_stub(state)
    return run_llm(state)
