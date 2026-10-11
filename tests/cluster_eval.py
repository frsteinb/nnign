"""Bewertet die Themenbildung auf den Referenzdaten (paarweise Präzision/Trefferquote).

Aufruf: python -m tests.cluster_eval   (zeigt auch falsche Zusammenlegungen und Trennungen)
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NNIGN_DISABLE_SCHEDULER", "1")

from tests.data.cluster_cases import CASES, HOLDOUT  # noqa: E402

BASE = datetime(2025, 9, 8, 8, tzinfo=timezone.utc)


def run(verbose: bool = False, cases=None) -> dict:
    cases = CASES if cases is None else cases
    from app import config, db, pipeline
    from app.fetcher import Item
    items = []
    for i, (story, day, dom, title, summary) in enumerate(cases):
        items.append(Item(url=f"https://{dom}/a{i}", title=title, summary=summary,
                          published=BASE + timedelta(days=day, hours=i % 9), source_name=dom, source_domain=dom,
                          via="rss"))
    config.set("collect_since", "2000-01-01")
    ctx = pipeline.Ctx("eval")
    pipeline.ingest(items, ctx, allow_llm=False)
    rows = db.query("SELECT url, topic_id FROM articles")
    topic_of = {r["url"]: r["topic_id"] for r in rows}
    gold = {f"https://{dom}/a{i}": (story, title) for i, (story, day, dom, title, summary) in enumerate(cases)}
    tp = fp = fn = 0
    wrong_merge, wrong_split = [], []
    for a, b in itertools.combinations(gold, 2):
        same_gold = gold[a][0] == gold[b][0]
        same_pred = topic_of.get(a) is not None and topic_of.get(a) == topic_of.get(b)
        if same_gold and same_pred:
            tp += 1
        elif same_pred:
            fp += 1
            wrong_merge.append((gold[a], gold[b]))
        elif same_gold:
            fn += 1
            wrong_split.append((gold[a], gold[b]))
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    n_topics = len({t for t in topic_of.values() if t})
    n_gold = len({g[0] for g in gold.values()})
    if verbose:
        print(f"Themen: {n_topics} (soll {n_gold})  Präzision {prec:.2f}  Trefferquote {rec:.2f}  F1 {f1:.2f}")
        for (sa, ta), (sb, tb) in wrong_merge:
            print(f"  FALSCH ZUSAMMEN  [{sa}] {ta[:60]}  <->  [{sb}] {tb[:60]}")
        seen = set()
        for (sa, ta), (sb, tb) in wrong_split:
            if (ta, sa) not in seen:
                seen.add((ta, sa))
        by_story: dict = {}
        for url, (story, title) in gold.items():
            by_story.setdefault(story, {}).setdefault(topic_of.get(url), []).append(title)
        for story, groups in sorted(by_story.items()):
            if len(groups) > 1:
                print(f"  GETRENNT [{story}]: " + " | ".join(f"{len(v)}× {v[0][:40]}" for v in groups.values()))
    return {"precision": prec, "recall": rec, "f1": f1, "topics": n_topics, "gold": n_gold}


if __name__ == "__main__":
    from app import db
    tmp = tempfile.mkdtemp()
    db.DB_PATH = os.path.join(tmp, "eval.sqlite3")
    db.DATA_DIR = tmp
    db.init_db()
    which = sys.argv[1] if len(sys.argv) > 1 else "cases"
    run(verbose=True, cases=HOLDOUT if which == "holdout" else CASES)
