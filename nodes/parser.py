# -*- coding: utf-8 -*-
"""① 解析员：JD + 简历 → 结构化事实

2026-11-04 重构（联调发现的问题）：
同一个 JD 连跑三次，模型抽出的硬性条件条数分别是 4、4、6——
它有时把「本科及以上学历」和「专业要求」合并，有时把「到岗天数」和「实习期」拆开。
合并那次，专业不符会被学历的「命中」掩盖，错误要到第三环才暴露。

修法不是加提示词（加过，不听），而是**不让模型决定切分粒度**：
1. JD 先按行切成候选（确定性），模型只对每一行做 must/plus/skip 分类，编号一一对应
2. 简历句子也用规则切（按标点），逐字可靠，不经过模型
这样模型只做「分类」这件它擅长的事，粒度由代码掌握。
"""
import os
import re
import time

from state import State, append

TECH = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{1,15}")

SYSTEM = """你是招聘条件分类器。下面给你一份 JD 按行切好的候选行（带编号），请逐行判断它属于哪一类，只输出 JSON。

分类标准：
- must：硬性条件——不满足就不该投（学历、专业、年限、必备技能、到岗时间、实习期）
- plus：加分项——没有也能投（出现「优先」「加分」「更好」等）
- skip：公司介绍、岗位职责、团队文化、福利宣传等非条件内容

硬规矩：
1. 每行必须给出一个判断，不许合并、不许拆分、不许跳过任何编号
2. 判断只能基于该行原文，不许脑补
3. why 用 8 个字以内说明依据

输出格式（严格 JSON）：
{"items": [{"cid": "c1", "type": "must|plus|skip", "why": "含'必须'"}]}"""

NUM_PREFIX = re.compile(r"^\s*(?:[（(]?\d+[）).、]\s*|[-*·•]\s*)")


def _lines(text: str):
    """把文本切成候选行：去标题、去序号、过滤过短行"""
    for raw in text.splitlines():
        line = raw.strip(" \t　")
        if line.startswith("#"):
            continue
        line = NUM_PREFIX.sub("", line).strip()
        if len(line) >= 4:
            yield line


def _candidates(jd: str) -> list[dict]:
    out = []
    for line in _lines(jd):
        i = jd.find(line)
        out.append({"cid": f"c{len(out) + 1}", "text": line,
                    "span": [i, i + len(line)]})
        if len(out) >= 60:          # 防超长 JD 把提示词撑爆
            break
    return out


def _split_resume(resume: str) -> list[dict]:
    """简历按标点切句：确定性、逐字，保证 evidence 一定能回溯"""
    sentences, section, seen = [], "其他", set()
    for raw in resume.splitlines():
        line = raw.strip(" \t　#")
        if not line:
            continue
        for key in ("教育", "技能", "项目", "实习"):
            if line.startswith(key):
                section = key
                line = line[len(key):].strip("：: ")
                break
        for seg in re.split(r"[。！？；]", line):
            seg = seg.strip()
            if len(seg) >= 6 and seg not in seen:
                seen.add(seg)
                sentences.append({"sid": f"s{len(sentences) + 1}",
                                  "text": seg, "section": section})
    return sentences


def _classify(cands: list[dict], hint: str = "") -> tuple[dict, dict]:
    from llm import chat_json
    lines = "\n".join(f"{c['cid']}｜{c['text']}" for c in cands)
    user = f"【候选行】\n{lines}\n\n请逐行分类，输出 JSON。{hint}"
    return chat_json(SYSTEM, user, temperature=0.0)


def _build(cands: list[dict], data: dict) -> tuple[list, list, list]:
    by_cid = {str(it.get("cid")): it for it in (data.get("items") or [])}
    must, plus, missing = [], [], []
    for c in cands:
        it = by_cid.get(c["cid"])
        if it is None:
            missing.append(c["cid"])
            continue
        kind = it.get("type")
        if kind == "must":
            must.append({"id": f"m{len(must) + 1}", "text": c["text"],
                         "why": str(it.get("why", ""))[:20], "span": c["span"]})
        elif kind == "plus":
            plus.append({"id": f"p{len(plus) + 1}", "text": c["text"],
                         "why": str(it.get("why", ""))[:20]})
    return must, plus, missing


def run_llm(state: State) -> dict:
    t0 = time.time()
    jd, resume = state["input"]["jd"], state["input"]["resume"]
    errors: list[dict] = []

    cands = _candidates(jd)
    data, usage = _classify(cands)
    must, plus, missing = _build(cands, data)

    if missing:
        errors.append({"step": "parser", "type": "unclassified_lines",
                       "detail": f"模型漏判了 {len(missing)} 行：{missing[:5]}",
                       "recovered": True})

    # must 为空 = schema 合法但没干活，带纠正提示重试一次
    if not must:
        errors.append({"step": "parser", "type": "semantic_invalid_retry",
                       "detail": "第一轮没有任何 must，带纠正提示重试", "recovered": True})
        data2, usage2 = _classify(cands, "注意：上一轮你没有标出任何 must，这次必须把硬性条件全部标出来。")
        usage = {"prompt": usage["prompt"] + usage2["prompt"],
                 "completion": usage["completion"] + usage2["completion"]}
        must, plus, _ = _build(cands, data2)
        if not must:
            errors.append({"step": "parser", "type": "semantic_invalid",
                           "detail": "重试后仍无 must，交人工确认点 1 补充", "recovered": False})

    sentences = _split_resume(resume)
    years_m = re.search(r"(\d+(?:\.\d+)?)\s*年", resume)
    edu = "本科" if "本科" in resume else ("硕士" if "硕士" in resume else "未标注")
    skills = []
    for w in TECH.findall(resume):
        if w.lower() not in [k.lower() for k in skills]:
            skills.append(w)

    job = {"rev": 1, "title": (next(iter(_lines(jd)), "") or "")[:40],
           "must": must, "plus": plus,
           "keywords": skills[:20],
           "truncated": len(jd) > 6000,
           "semantic_invalid": not must}
    res = {"skills": skills[:20],
           "years": float(years_m.group(1)) if years_m else 0.0,
           "edu": edu, "sentences": sentences,
           "projects": [{"name": s["text"][:20], "sid_range": [s["sid"]]}
                        for s in sentences if s["section"] == "项目"][:5]}

    return {
        "job": job, "resume": res,
        "errors": list(state.get("errors", [])) + errors,
        "trace": append(state, "trace", {
            "step": "parser", "ms": int((time.time() - t0) * 1000),
            "tokens": usage["prompt"] + usage["completion"],
            "tokens_in": usage["prompt"], "tokens_out": usage["completion"],
            "note": f"候选行={len(cands)} must={len(must)} plus={len(plus)} 句子={len(sentences)}"
                    + ("（语义空转）" if job["semantic_invalid"] else ""),
        }),
    }


# ============ 规则版（P7_STUB=1，离线自测用） ============
MUST_WORDS = ("必须", "要求", "本科", "硕士", "年以上", "熟悉", "掌握", "能够",
              "具备", "以上", "不少于", "能使用", "每周")
PLUS_WORDS = ("加分", "优先", "更好", "nice to have")


def run_stub(state: State) -> dict:
    t0 = time.time()
    jd, resume = state["input"]["jd"], state["input"]["resume"]
    must, plus = [], []
    for line in _lines(jd):
        if any(w in line.lower() for w in PLUS_WORDS):
            plus.append({"id": f"p{len(plus) + 1}", "text": line, "why": "含加分关键词"})
        elif any(w in line for w in MUST_WORDS):
            i = jd.find(line)
            must.append({"id": f"m{len(must) + 1}", "text": line,
                         "why": "含硬性关键词", "span": [i, i + len(line)]})

    sentences = _split_resume(resume)
    job = {"rev": 1, "title": next(iter(_lines(jd)), "")[:40],
           "must": must, "plus": plus, "keywords": TECH.findall(jd)[:20],
           "truncated": False, "semantic_invalid": not must}
    res = {"skills": TECH.findall(resume)[:20], "years": 0.0,
           "edu": "本科" if "本科" in resume else "未标注",
           "sentences": sentences,
           "projects": [{"name": s["text"][:20], "sid_range": [s["sid"]]}
                        for s in sentences if s["section"] == "项目"][:5]}
    return {
        "job": job, "resume": res,
        "trace": append(state, "trace", {
            "step": "parser", "ms": int((time.time() - t0) * 1000), "tokens": 0,
            "note": f"[规则版] must={len(must)} plus={len(plus)}",
        }),
    }


def run(state: State) -> dict:
    """分级开关：P7_STUB_PARSER=1 单独让本节点走规则版，P7_STUB=1 则全部走规则"""
    if os.environ.get("P7_STUB") == "1" or os.environ.get("P7_STUB_PARSER") == "1":
        return run_stub(state)
    return run_llm(state)
