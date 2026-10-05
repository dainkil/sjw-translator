#!/usr/bin/env python3
"""KB 밖 이름의 정체 — 범위만 넓히면 덮이는가, 마스터에도 없는가. **LLM 호출 0회.**

런타임 KB는 인조 연간 2,690명 slice다(`kb/id_lookup_injo.json`). 원천인 person_master는 27,329명(전 시대)이다.
KB 밖 이름(링킹 MISS)을 마스터에 대 보면 두 가지 개선 경로의 크기가 갈린다:
  (a) 마스터에 있음   → KB 범위 확장(slice를 넓히기)으로 덮인다
  (b) 마스터에도 없음 → 원천 DB 밖의 인물 — 병렬 코퍼스 마이닝(Step 5) 같은 다른 출처가 필요하다

모집단은 NER 출력이 아니라 **국편 인명 어노테이션**(`name_gold_300.json`, 골든셋 300문장)이다 — NER 오탐·
성 없는 조각이 섞이지 않은 "실제로 사람인 것"만 센다.

마스터의 원천은 관직 이력 CSV(`인물_관직_이력.csv`)라 관직 기록이 없는 사람(노비·죄인·환관·명나라 인물·
시호 등)은 애초에 없다 — (b)의 상당 부분이 이 구조에서 온다.

사용법:
  python3 eval/kb_coverage.py [--json]
"""
import argparse
import collections
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
INJO = (1623, 1649)


def default_master() -> Path:
    for p in (ROOT / "malmoi" / "kb" / "person_master.json", ROOT.parent / "malmoi" / "kb" / "person_master.json"):
        if p.exists():
            return p
    return ROOT / "malmoi" / "kb" / "person_master.json"


def classify(rows, master_by_hanja, years=INJO):
    """KB 밖 이름 → (a) in_master / in_master_active(활동기간이 인조 연간과 겹침) / (b) not_in_master."""
    out = [r for r in rows if not r["in_kb"]]
    res = collections.Counter()
    examples = collections.defaultdict(list)
    for r in out:
        ps = master_by_hanja.get(r["hanja"], [])
        if not ps:
            key = "not_in_master"
        elif any((p.get("활동_시작") or 0) <= years[1] and (p.get("활동_종료") or 9999) >= years[0] for p in ps):
            key = "in_master_active"
        else:
            key = "in_master_other_era"
        res[key] += 1
        if len(examples[key]) < 12:
            examples[key].append(r["hanja"])
    length = collections.Counter(len(r["hanja"]) for r in out if not master_by_hanja.get(r["hanja"]))
    return {
        "names": len(rows),
        "out_kb": len(out),
        "out_kb_share": round(len(out) / len(rows), 4) if rows else None,
        "breakdown": dict(res),
        "expansion_ceiling": round((res["in_master_active"] + res["in_master_other_era"]) / len(out), 4) if out else None,
        "not_in_master_length": dict(sorted(length.items())),
        "examples": dict(examples),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gold", type=Path, default=HERE / "name_gold_300.json")
    ap.add_argument("--master", type=Path, default=default_master())
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = json.loads(args.gold.read_text(encoding="utf-8"))["names"]
    by_hanja = collections.defaultdict(list)
    for p in json.loads(args.master.read_text(encoding="utf-8")):
        if p.get("한자_명"):
            by_hanja[p["한자_명"]].append(p)

    res = classify(rows, by_hanja)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    b = res["breakdown"]
    print(f"국편 어노테이션 인명 {res['names']} 중 KB 밖 {res['out_kb']} ({100 * res['out_kb_share']:.1f}%)")
    print(f"  (a) 마스터에 있음, 인조 연간 활동  {b.get('in_master_active', 0):>4}")
    print(f"  (a) 마스터에 있음, 다른 시대       {b.get('in_master_other_era', 0):>4}")
    print(f"  (b) 마스터에도 없음                {b.get('not_in_master', 0):>4}")
    print(f"  → KB 범위 확장의 상한: KB 밖 이름의 {100 * res['expansion_ceiling']:.1f}%")
    print(f"  (b) 글자 수 분포: {res['not_in_master_length']}")
    for k, v in res["examples"].items():
        print(f"  예 {k}: {' '.join(v)}")


if __name__ == "__main__":
    main()
