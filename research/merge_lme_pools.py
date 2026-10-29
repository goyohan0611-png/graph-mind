"""Join lme_pools.py --part files into the one file rerank_v2_check.py reads.

    python research/merge_lme_pools.py research/runs/graph-v9.20-learning/lme_pools-NAME
"""
from pathlib import Path
import json
import sys

stem = Path(sys.argv[1])
merged = {"turns": {}, "questions": []}
for part in sorted(stem.parent.glob(stem.name + "-part*.json"),
                   key=lambda p: int(p.stem.rsplit("-part", 1)[1].split("-")[0])):
    data = json.loads(part.read_text(encoding="utf-8"))
    merged["turns"].update(data["turns"])
    merged["questions"].extend(data["questions"])
    print(part.name, len(data["questions"]))
stem.with_suffix(".json").write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
print(len(merged["questions"]), "questions")
