"""Re-grade our 60Q hypotheses with the OFFICIAL LongMemEval judge protocol (gpt-4o), so the number
is comparable to Zep/Mem0. Faithful reimplementation of xiaowu0162/LongMemEval evaluate_qa.py:
per-type judge prompt, gpt-4o, temperature 0, max_tokens 10, label = 'yes' in response.lower().
Our custom gpt-5-mini judge is explicitly NON-comparable per the official repo; this fixes that.

    OPENAI_API_KEY=... python official_judge_v073.py
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import json
import os
from urllib import error, request

SRC = Path("runs/graph-v3.0-retrieval-executor-v2")
OUT = SRC / "official-judge.jsonl"
JUDGE_MODEL = "gpt-4o-2024-08-06"


def get_anscheck_prompt(qtype, question, answer, response):
    if "_abs" in qtype:  # abstention
        return ("I will give you an unanswerable question, an explanation, and a response from a "
                "model. Please answer yes if the model correctly identifies the question as "
                "unanswerable. The model could say that the information is incomplete, or some other "
                "information is given but the asked information is not.\n\n"
                f"Question: {question}\n\nExplanation: {answer}\n\nModel Response: {response}\n\n"
                "Does the model correctly identify the question as unanswerable? Answer yes or no only.")
    base = ("I will give you a question, a correct answer, and a response from a model. Please answer "
            "yes if the response contains the correct answer. Otherwise, answer no. If the response is "
            "equivalent to the correct answer or contains all the intermediate steps to get the correct "
            "answer, you should also answer yes. If the response only contains a subset of the "
            "information required by the answer, answer no. ")
    extra = ""
    if qtype == "temporal-reasoning":
        extra = ("In addition, do not penalize off-by-one errors for the number of days. If the "
                 "question asks for the number of days/weeks/months, etc., and the model makes off-by-one "
                 "errors (e.g., predicting 19 days when the answer is 18), the model's response is "
                 "still correct. ")
    elif qtype == "knowledge-update":
        extra = ("In addition, if the response contains some previous information along with an updated "
                 "answer, the response should be considered as correct as long as the updated answer is "
                 "the required answer. ")
    elif qtype == "single-session-preference":
        base = ("I will give you a question, a rubric for desired personalized response, and a response "
                "from a model. Please answer yes if the response satisfies the desired response. "
                "Otherwise, answer no. The model does not need to reflect all the points in the rubric. "
                "The response is correct as long as it recalls and utilizes the user's personal "
                "information correctly. ")
    return (base + extra + f"\n\nQuestion: {question}\n\nCorrect Answer: {answer}\n\n"
            f"Model Response: {response}\n\nIs the model response correct? Answer yes or no only.")


def judge(prompt, key):
    body = {"model": JUDGE_MODEL, "messages": [{"role": "user", "content": prompt}],
            "n": 1, "temperature": 0, "max_tokens": 10}
    req = request.Request("https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with request.urlopen(req, timeout=60) as r:
        payload = json.loads(r.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def _load_jsonl(path, key):
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line); out[r[key]] = r
    return out


def run():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY_REQUIRED")
    questions = {r["question_id"]: r for r in json.load((SRC / "questions.json").open(encoding="utf-8"))}
    answers = _load_jsonl(SRC / "answers.jsonl", "question_id")
    # a verdict that failed (timeout, HTTP error) is asked again, not counted as judged
    done = {qid: row for qid, row in (_load_jsonl(OUT, "question_id") if OUT.exists() else {}).items()
            if "label" in row}
    with OUT.open("a", encoding="utf-8") as h:
        for qid, q in questions.items():
            if qid in done:
                continue
            if answers.get(qid, {}).get("status", "ok") != "ok" or qid not in answers:
                continue    # judged after its answer is retried, never as "UNKNOWN" before it
            a = answers.get(qid, {}).get("result", {})
            hypothesis = str(a.get("answer", "")) or "UNKNOWN"
            prompt = get_anscheck_prompt(qid if "_abs" in qid else q["question_type"],
                                         q["question"], str(q["answer"]), hypothesis)
            try:
                resp = judge(prompt, key)
                row = {"question_id": qid, "type": ("abstention" if "_abs" in qid else q["question_type"]),
                       "label": "yes" in resp.lower(), "judge_raw": resp.strip()}
            except (error.HTTPError, error.URLError, TimeoutError) as exc:
                row = {"question_id": qid, "error": type(exc).__name__}
            h.write(json.dumps(row, ensure_ascii=False) + "\n"); h.flush()
    graded = _load_jsonl(OUT, "question_id")
    agg = defaultdict(lambda: [0, 0])
    for r in graded.values():
        if "label" in r:
            agg[r["type"]][0] += int(r["label"]); agg[r["type"]][1] += 1
            agg["ALL"][0] += int(r["label"]); agg["ALL"][1] += 1
    report = {"judge": JUDGE_MODEL, "note": "OFFICIAL LongMemEval protocol judge (comparable). "
              "Same 60 pilot answers as the custom-judge 87%.",
              "by_type": {k: {"correct": v[0], "n": v[1], "acc": round(v[0]/v[1], 4) if v[1] else 0}
                          for k, v in sorted(agg.items())}}
    (SRC / "official-judge-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(run()["by_type"], ensure_ascii=False, indent=2))
