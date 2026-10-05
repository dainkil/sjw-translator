#!/usr/bin/env python3
"""인명 정확도를 KB 포함/미포함 구간으로 나눠 채점한다 — 정답은 `name_gold.py`의 국역 표기. LLM 호출 0회.

ETS(score_db.py)와 같은 엄격 규칙(정답 표기가 번역문에 그대로 있는가)을 쓰되 두 가지가 다르다:
정답이 KB 값이 아니라 전문가 국역에 실제로 나온 표기이고, 국편 어노테이션 전체(KB 밖 이름 포함)가 분모다.
`unresolved`·`ref_reading`(국역 표기를 믿을 수 있게 찾지 못한 이름)은 분모에서 뺀다 — name_gold.scored.

구간:
  in_kb     런타임 KB 링킹이 MISS가 아닌 이름 — 주입을 받을 수 있는 이름
  out_kb    MISS — 어떤 경우에도 주입을 받지 못하는 이름
  relinked  KB가 확정해 주입하는 표기가 국역 표기와 다른 이름 — KB가 *틀린 이름*을 주입하는 자리
  old_gold  기존 정답지(ner_groundtruth_300.json)에 남아 있던 이름 — 종전 ETS의 모집단 재현용

추가 지표 wrong_injected: relinked 이름에서 번역문이 KB의 틀린 표기를 쓴 비율 — 검증 게이트가 통과시키는 오류.

민감도 하한 acc_lower: 채점에서 뺀 이름(unresolved·ref_reading)을 전부 오답으로 친 값. 뺀 이름은 국역 표기가
후보와 갈리는 어려운 이름에 몰려 있어(대부분 KB 밖) 제외만 하면 정확도가 낙관적으로 나온다 — 두 값을 함께 본다.
행 밀림 문장(`eval_exclusions.json`)은 이름·chrF 모두에서 통째로 뺀다.

라운드가 여럿이면 이름별 적중을 라운드 평균으로 합친다. 신뢰구간은 문장 단위 재표집 bootstrap(기본 2,000회),
두 arm 비교는 같은 문장을 짝지어 재표집한 paired bootstrap이다.

사용법:
  uv run --with "psycopg[binary]" python eval/name_metrics.py \\
      --arm nokb=a93b8817-...,a3563a39-...,0fa1887e-... --arm kb=<batch>,<batch> [--json]
"""
import argparse
import collections
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SEGMENTS = ("all", "in_kb", "out_kb", "relinked", "old_gold")


def load_gold(path: Path, exclude=frozenset()):
    """(id → 채점 대상 이름 목록, id → 채점 제외 이름 목록). 행 밀림 문장(exclude)은 양쪽에서 뺀다."""
    from name_gold import scored
    by_id = collections.defaultdict(list)
    unscored = collections.defaultdict(list)
    for r in json.loads(path.read_text(encoding="utf-8"))["names"]:
        if r["id"] in exclude:
            continue
        (by_id if scored(r) else unscored)[r["id"]].append(r)
    return by_id, unscored


def relinked(name_row) -> bool:
    return bool(name_row["injected"]) and name_row["injected"] != name_row["gold"] \
        and name_row["gold"] not in name_row["injected"]          # 文晦: 주입 정문회 ⊃ 국역 문회 — 오류 아님


def segments_of(name_row):
    segs = ["all", "in_kb" if name_row["in_kb"] else "out_kb"]
    if relinked(name_row):
        segs.append("relinked")
    if name_row.get("in_old_gold"):
        segs.append("old_gold")
    return segs


def score_rounds(gold_by_id, rounds, unscored_by_id=None):
    """rounds: [{id: hypothesis}] → 문장별 {seg: [hit, total, unscored]} 와 wrong_injected (라운드 평균 적중)."""
    unscored_by_id = unscored_by_id or {}
    common = set(gold_by_id) | set(unscored_by_id)      # 제외 이름만 있는 문장도 하한의 분모에 든다
    for r in rounds:
        common &= set(r)
    per_sentence = {}
    wrong = [0.0, 0]
    for doc_id in sorted(common):
        acc = {s: [0.0, 0, 0] for s in SEGMENTS}
        for n in unscored_by_id.get(doc_id, []):
            for s in ("all", "in_kb" if n["in_kb"] else "out_kb") + (("old_gold",) if n.get("in_old_gold") else ()):
                acc[s][2] += 1
        for n in gold_by_id.get(doc_id, []):
            hit = sum(1 for r in rounds if n["gold"] in r[doc_id]) / len(rounds)
            for s in segments_of(n):
                acc[s][0] += hit
                acc[s][1] += 1
            if relinked(n):
                wrong[0] += sum(1 for r in rounds if n["injected"] in r[doc_id]) / len(rounds)
                wrong[1] += 1
        per_sentence[doc_id] = acc
    return per_sentence, wrong


def micro(per_sentence, ids, seg):
    hit = sum(per_sentence[i][seg][0] for i in ids)
    tot = sum(per_sentence[i][seg][1] for i in ids)
    return (hit / tot if tot else None), tot


def micro_lower(per_sentence, ids, seg):
    """채점 제외 이름을 오답으로 친 하한."""
    hit = sum(per_sentence[i][seg][0] for i in ids)
    den = sum(per_sentence[i][seg][1] + (per_sentence[i][seg][2] if len(per_sentence[i][seg]) > 2 else 0)
              for i in ids)
    return hit / den if den else None


def bootstrap_ci(per_sentence, seg, iters=2000, seed=0, other=None):
    """문장 재표집 95% CI. other가 있으면 같은 표본으로 (self − other) 차이의 CI."""
    ids = sorted(per_sentence if other is None else set(per_sentence) & set(other))
    rng = random.Random(seed)
    stats = []
    for _ in range(iters):
        sample = [rng.choice(ids) for _ in ids]
        a, _ = micro(per_sentence, sample, seg)
        if other is not None:
            b, _ = micro(other, sample, seg)
            if a is not None and b is not None:
                stats.append(a - b)
        elif a is not None:
            stats.append(a)
    if not stats:
        return None
    stats.sort()
    return stats[int(0.025 * len(stats))], stats[int(0.975 * len(stats)) - 1]


def arm_report(name, per_sentence, wrong, iters):
    ids = list(per_sentence)
    out = {"arm": name, "sentences": len(ids)}
    for seg in SEGMENTS:
        acc, tot = micro(per_sentence, ids, seg)
        ci = bootstrap_ci(per_sentence, seg, iters) if tot else None
        low = micro_lower(per_sentence, ids, seg)
        out[seg] = {"acc": round(acc, 4) if acc is not None else None, "names": tot,
                    "ci95": [round(x, 4) for x in ci] if ci else None,
                    "acc_lower": round(low, 4) if low is not None else None,
                    "unscored": sum(per_sentence[i][seg][2] for i in ids if len(per_sentence[i][seg]) > 2)}
    out["wrong_injected"] = {"rate": round(wrong[0] / wrong[1], 4) if wrong[1] else None, "names": wrong[1]}
    return out


def fetch_batch(conn, batch_id, hash_to_id):
    rows = conn.execute(
        """SELECT j.normalized_hash, r.translated_text FROM translation_job j
           JOIN translation_result r ON r.job_id = j.id
           WHERE j.status = 'SUCCEEDED' AND j.batch_id = %s::uuid""", [batch_id]).fetchall()
    return {hash_to_id[h]: t for h, t in rows if h in hash_to_id}


def evaluate(gold, unscored, arm_rounds, ids=None, iters=2000):
    """arm_rounds: {label: [{id: hypothesis}, ...]} → 구간별 arm 보고와 첫 arm 대비 paired Δ.

    ids가 주어지면 그 문장만 (예: eval60 부분집합 / 나머지 240). 모든 arm이 공통으로 가진 문장만 비교한다.
    """
    arms = {k: score_rounds(gold, rounds, unscored) for k, rounds in arm_rounds.items()}
    common = set.intersection(*(set(ps) for ps, _ in arms.values()))
    if ids is not None:
        common &= set(ids)
    arms = {k: ({i: ps[i] for i in common}, w) for k, (ps, w) in arms.items()}
    reports = [arm_report(k, ps, w, iters) for k, (ps, w) in arms.items()]
    labels = list(arms)
    deltas = []
    for b in labels[1:]:
        a = labels[0]
        d = {"a": a, "b": b}
        for seg in SEGMENTS:
            pa, _ = micro(arms[a][0], common, seg)
            pb, _ = micro(arms[b][0], common, seg)
            ci = bootstrap_ci(arms[b][0], seg, iters, other=arms[a][0]) if common else None
            d[seg] = {"delta": round(pb - pa, 4) if pa is not None and pb is not None else None,
                      "ci95": [round(x, 4) for x in ci] if ci else None}
        deltas.append(d)
    return {"sentences": len(common), "arms": reports, "deltas": deltas}


def print_result(result):
    for r in result["arms"]:
        print(f"\n[{r['arm']}] 문장 {r['sentences']}")
        for seg in SEGMENTS:
            s = r[seg]
            if s["names"]:
                ci = f"[{s['ci95'][0]:.3f}, {s['ci95'][1]:.3f}]" if s["ci95"] else ""
                low = f"하한 {s['acc_lower']:.4f} (제외 {s['unscored']})" if s.get("unscored") else ""
                print(f"  {seg:<9} {s['acc']:.4f}  {ci:<17} 이름 {s['names']:<4} {low}")
        w = r["wrong_injected"]
        if w["names"]:
            print(f"  wrong_injected {w['rate']:.4f}  (KB의 틀린 표기를 쓴 비율, 이름 {w['names']})")
    for d in result["deltas"]:
        print(f"\nΔ {d['b']} − {d['a']}")
        for seg in SEGMENTS:
            if d[seg]["delta"] is not None:
                ci = d[seg]["ci95"]
                print(f"  {seg:<9} {d[seg]['delta']:+.4f}  " + (f"[{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gold", type=Path, default=HERE / "name_gold_300.json")
    ap.add_argument("--corpus", type=Path, default=HERE / "eval300_1925.json")
    ap.add_argument("--exclusions", type=Path, default=HERE / "eval_exclusions.json")
    ap.add_argument("--arm", action="append", required=True, metavar="LABEL=BATCH[,BATCH...]")
    ap.add_argument("--iters", type=int, default=2000)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    import psycopg
    from eval_exclusions import load_exclusions
    from score_db import DSN, normalized_hash

    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))["corpus"]
    hash_to_id = {normalized_hash(r["original"]): r["id"] for r in corpus}
    excluded = load_exclusions(args.exclusions)
    gold, unscored = load_gold(args.gold, excluded)

    arm_rounds = {}
    with psycopg.connect(DSN) as conn:
        for spec in args.arm:
            label, batches = spec.split("=", 1)
            arm_rounds[label] = [fetch_batch(conn, b.strip(), hash_to_id) for b in batches.split(",") if b.strip()]

    result = {"gold": args.gold.name, "excluded_sentences": sorted(excluded),
              **evaluate(gold, unscored, arm_rounds, iters=args.iters)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print_result(result)


if __name__ == "__main__":
    main()
