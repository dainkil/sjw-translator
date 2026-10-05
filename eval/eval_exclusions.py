#!/usr/bin/env python3
"""골든셋에서 원문↔국역이 어긋난 문장을 찾아 채점 제외 목록을 만든다. **LLM 호출 0회.**

원천 코퍼스(`Merged_Corpus_Final.json`)에는 짝이 행 단위로 밀린 결함이 있다(docs/benchmarks.md "원천 병렬 코퍼스의
원문↔번역 정렬 결함", 전수 11.24%). 골든셋 300문장에서는 **부분 밀림**으로 나타난다 — 원문 일부의 국역이 이웃 행에
가 있어 국역이 원문을 다 덮지 못한다. 이런 문장은 인명 채점(정답 표기가 국역에 없다)과 chrF(참조가 짧다)를 함께
왜곡하므로 양쪽에서 뺀다.

검출 (두 방법의 합집합):
  a. 국편 어노테이션 인명 중 자기 국역에서 표기를 못 찾은 것(name_gold의 unresolved·ref_reading)의 후보 표기가
     **이웃 행(±2) 국역에는 전부 있으면** 밀림 (simulate_cache.detect_misalignment와 같은 규칙, 정답지 기준)
  b. `leakage.misaligned_rows` — 원문의 KB 성명 키 독음이 자기 국역에 없고 이웃 국역에 있음 (코퍼스 전수에 쓰는 규칙)
  2026-10-04: a 2건, b 1건(a에 포함). b의 전수 표본 5건은 모두 실제 밀림이었다.

경계: 인명이 없는 문장의 밀림은 이 방법으로 잡을 수 없다. 원문 한자음과 국역의 글자 겹침(chrF)과 국역/원문
길이 비율도 시험했지만 신호가 되지 못했다 — 알려진 밀림 사례를 못 잡았고, 길이 비율 하위는 전부 정상인 인명
나열문(제수·입시 명단)이었다 (2026-10-04).

사용법:
  uv run --with hanja python eval/eval_exclusions.py      # eval/eval_exclusions.json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from name_gold import default_master, lib_readings, load_master  # noqa: E402

DEFAULT_PATH = HERE / "eval_exclusions.json"


def default_corpus() -> Path:
    for p in (ROOT / "malmoi" / "Merged_Corpus_Final.json", ROOT.parent / "malmoi" / "Merged_Corpus_Final.json"):
        if p.exists():
            return p
    return ROOT / "malmoi" / "Merged_Corpus_Final.json"


def load_exclusions(path: Path = DEFAULT_PATH) -> set:
    """채점에서 뺄 문장 id 집합. 파일이 없으면 빈 집합 (제외 없음)."""
    if not path.exists():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8"))["excluded"])


def candidates(row, translate, surname_reading):
    out = ([row["injected"]] if row.get("injected") else []) + list(row.get("kb_names") or [])
    out += lib_readings(row["hanja"], translate, surname_reading)
    return [k for k in dict.fromkeys(out) if k and len(k) >= 2]


def detect(gold_rows, corpus_rows, translate, surname_reading, window=2):
    idx = {r["id"]: i for i, r in enumerate(corpus_rows)}
    trans = [r.get("translation") or "" for r in corpus_rows]
    by_id = {}
    for r in gold_rows:
        if r["status"] in ("unresolved", "ref_reading"):
            by_id.setdefault(r["id"], []).append(r)
    found = {}
    for doc_id, rows in sorted(by_id.items()):
        i = idx.get(doc_id)
        if i is None:
            continue
        missing = [(r["hanja"], candidates(r, translate, surname_reading)) for r in rows]
        missing = [(h, c) for h, c in missing if c]
        if not missing:
            continue
        for off in sorted(range(-window, window + 1), key=abs):
            j = i + off
            if off == 0 or not 0 <= j < len(trans):
                continue
            hits = {h: next((k for k in c if k in trans[j]), None) for h, c in missing}
            if all(hits.values()):
                found[doc_id] = {"reason": "partial_misalignment", "neighbor_offset": off,
                                 "names": {h: k for h, k in hits.items()}}
                break
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gold", type=Path, default=HERE / "name_gold_300.json")
    ap.add_argument("--corpus", type=Path, default=default_corpus())
    ap.add_argument("--master", type=Path, default=default_master())
    ap.add_argument("--out", type=Path, default=DEFAULT_PATH)
    args = ap.parse_args()

    import hanja as hanja_lib

    def translate(s):
        return hanja_lib.translate(s, "substitution")

    gold_rows = json.loads(args.gold.read_text(encoding="utf-8"))["names"]
    corpus_rows = json.loads(args.corpus.read_text(encoding="utf-8"))["corpus"]
    _, surname_reading, _ = load_master(args.master)
    found = detect(gold_rows, corpus_rows, translate, surname_reading)

    from leakage import kb_name_keys, misaligned_rows
    inv = json.loads((ROOT / "kb" / "inverted_index_injo.json").read_text(encoding="utf-8"))
    ids = json.loads((ROOT / "kb" / "id_lookup_injo.json").read_text(encoding="utf-8"))
    eval_ids = {r["id"] for r in gold_rows}
    eval_ids |= set(json.loads((HERE / "eval300_1925.json").read_text(encoding="utf-8"))["ids"])
    for doc_id in sorted(misaligned_rows(corpus_rows, kb_name_keys(inv, ids)) & eval_ids):
        found.setdefault(doc_id, {"reason": "kb_key_misalignment"})
        found[doc_id].setdefault("also", []).append("kb_key_misalignment")
    args.out.write_text(json.dumps({"rule": "unresolved 인명의 후보 표기가 이웃 행(±2) 국역에 전부 있음",
                                    "excluded": sorted(found), "detail": found},
                                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"제외 {len(found)}건")
    for k, v in found.items():
        print(f"  {k}  {v['reason']}  " + (f"이웃 {v['neighbor_offset']:+d} {v['names']}" if "names" in v else "")
              + (f"  (+{','.join(v['also'])})" if v.get("also") else ""))


if __name__ == "__main__":
    main()
