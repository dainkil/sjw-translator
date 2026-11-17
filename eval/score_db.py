"""
score_db.py — 골든셋 채점의 DB 어댑터 (§5.4-(2), ADR-019).

score_300.py(연구 시절 jsonl CLI)의 지표를 시스템 결과(Postgres)에 직접 붙인다.
매칭 키는 normalized_hash (TextHash.java와 동일 규칙: NFC + 공백 제거 + SHA-256).

지표 3개 — 서로 다른 것을 잰다 (M3.5-S1에서 구분을 명시):
  - chrF            : 전문가 번역 대비 문자 n-gram 유사도. 문체·유창성의 표면 지표.
  - 확정 인명 반영률 : 시스템이 KB로 확정해 **프롬프트에 주입한** 한글명이 번역문에 있는 비율.
                      주입 소스와 채점 소스가 같으므로 **순환 지표**다 — 번역 정확도가 아니라 지시
                      이행률이며, 캐시·라우팅처럼 "싼 경로가 비싼 경로의 것을 잃었나"를 묻는 비열등
                      게이트로만 쓴다 (선행 연구 보고서 §0 "닫힌 고리"가 지적한 바로 그 구조).
  - ETS             : 독립 정답지(ner_groundtruth_300.json — 국편 사이트의 역사학자 인명 어노테이션,
                      SillokBERT·KB와 무관)의 인명이 번역문에 있는 비율. 시스템이 모르는 이름(KB MISS)도
                      분모에 든다. **이것이 품질 헤드라인이다.** 엄격판(전체형 포함, 연구 보고서 §5의
                      정의)을 저장하고, 게이트 규칙(QualityGate.nameReflected — 성 생략 허용)의 관대판은
                      참고로만 출력한다.

비열등 임계 (사전 고정 — 사후에 정하면 게이트가 아니다):
  - chrF: 기준선 대비 -2.0 이내
  - 확정 인명 반영률: 하락 0 (동등 요구)
  - ETS: 이 슬라이스에서는 **보고 지표**. 허용 하락폭은 M3.5-S2의 대조군 3라운드 test-retest 폭으로,
    변형 채점 *전에* 고정한다(eval/prompt_ablation.py). 그 전에 정하면 추정이고 변형을 본 뒤 정하면
    사후 임계다.

사용법:
  uv run --with sacrebleu --with "psycopg[binary]" python eval/score_db.py                 # 채점 + 기준선 비교
  uv run --with sacrebleu --with "psycopg[binary]" python eval/score_db.py --save-baseline
  ... --batch-id <uuid> [--batch-id ...] --json --no-gate   # 배치(=라운드) 단위 채점, 기계 출력 (S2 드라이버)
  ... --prompt-version main-xxxxxxxx --save-baseline        # 프롬프트 변경 확정 시 그 버전의 결과만으로 기준선
  ... --self-check-reference                                # 전문가 번역을 가설 자리에 넣어 정답지 자체를 점검
  ... --corpus <path> --groundtruth <path>                  # 다른 골든셋 (BatchController의 sjw.eval.corpus와 같은 규칙)
  ... --batch-id A --report [out.tsv]                       # 문장 단위 리포트 (sentence chrF 오름차순 = 나쁜 문장부터)
  ... --batch-id A --compare-batch B --report               # A 대 B 짝 비교 — ΔchrF가 어느 문장에 몰렸는지, 어느 이름이 빠졌는지
기준선 파일: eval/baseline_scores.json. CI(M2.5-S8)는 종료 코드로 판정한다 (위반=1, 표본 부족=2).
"""

import argparse
import hashlib
import json
import re
import statistics
import sys
import unicodedata
from datetime import date
from pathlib import Path

import sacrebleu

DSN = "host=localhost port=5433 dbname=sjw user=sjw password=sjw"
HERE = Path(__file__).parent
BASELINE_PATH = HERE / "baseline_scores.json"
DEFAULT_CORPUS = HERE / "eval300_1925.json"
DEFAULT_GROUNDTRUTH = HERE / "ner_groundtruth_300.json"

CHRF_TOLERANCE = 2.0          # 기준선 대비 허용 하락폭
NAME_RECALL_TOLERANCE = 0.0   # 인명은 동등 요구 — 하락 0
MIN_ITEMS = 10


_JAVA_WS = re.compile(r"[ \t\n\x0b\f\r]+")


def normalized_hash(text: str) -> str:
    """translation_job.normalized_hash와 같은 값 — TextHash.normalizedHash(Java)를 그대로 재현한다.

    Java의 `\\s`는 ASCII 공백 6자뿐이다. 파이썬 `str.split()`은 U+00A0(NBSP) 등 유니코드 공백까지 지워서,
    원문에 NBSP가 든 문장은 DB 결과와 짝지어지지 않고 채점에서 조용히 빠졌다 (eval300에서 7문장, 2026-11-17 발견).
    """
    normalized = _JAVA_WS.sub("", unicodedata.normalize("NFC", text))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_goldenset(path: Path):
    ev = json.loads(path.read_text(encoding="utf-8"))
    return {normalized_hash(r["original"]): r for r in ev["corpus"]}


def load_groundtruth(path: Path):
    """id → 정답지 PER 한글명 목록. 형식: {id: [["PER", "趙廷虎", "조정호"], ...]}"""
    gt = json.loads(path.read_text(encoding="utf-8"))
    return {doc_id: [e[2] for e in ents if e[0] == "PER"] for doc_id, ents in gt.items()}


def fetch_results(conn, hashes, batch_ids=None, prompt_version=None, model=None, include_fake=False):
    """골든셋 문장과 매칭되는 최신 SUCCEEDED 결과 (필터 안에서 hash당 1건)."""
    sql = """
        SELECT DISTINCT ON (j.normalized_hash)
               j.normalized_hash, r.translated_text, r.entities, j.model_used, j.quality_grade,
               j.prompt_version, j.batch_id, j.tokens_in
        FROM translation_job j
        JOIN translation_result r ON r.job_id = j.id
        WHERE j.status = 'SUCCEEDED' AND j.normalized_hash = ANY(%s)
    """
    params = [list(hashes)]
    if not include_fake:
        sql += " AND (j.model_used IS NULL OR j.model_used NOT LIKE 'fake-%%')"   # 가짜 provider 결과는 품질 채점 대상이 아니다
    if batch_ids:
        sql += " AND j.batch_id = ANY(%s::uuid[])"
        params.append(list(batch_ids))
    if prompt_version:
        sql += " AND j.prompt_version = %s"
        params.append(prompt_version)
    if model:
        sql += " AND j.model_used = %s"
        params.append(model)
    sql += " ORDER BY j.normalized_hash, j.completed_at DESC"
    rows = conn.execute(sql, params).fetchall()
    return {h: {"hypothesis": t, "entities": e or [], "model": m, "grade": g,
                "prompt_version": pv, "batch_id": str(b) if b else None, "tokens_in": ti}
            for h, t, e, m, g, pv, b, ti in rows}


def confirmed_name_recall(items) -> tuple[float, int]:
    """링크 확정 인물(kbId 보유)의 주입 한글명이 번역문에 반영된 비율. (§5.4의 게이트 지표 — 순환)"""
    hits = total = 0
    for it in items:
        for e in it["entities"]:
            if e.get("kbId") and e.get("resolvedName"):
                total += 1
                if e["resolvedName"] in it["hypothesis"]:
                    hits += 1
    return (hits / total if total else float("nan")), total


def name_reflected(text: str, name: str) -> bool:
    """QualityGate.nameReflected 이식 — 전체 이름 또는 이름부(2자 이상) 포함이면 반영."""
    if name in text:
        return True
    given = name[1:] if len(name) >= 3 else None
    return bool(given) and len(given) >= 2 and given in text


def ets(items, groundtruth) -> dict:
    """독립 정답지 기반 인명 보존률. micro(연구 보고서 §5 공식) + macro(score_300.py 집계) 둘 다 계산."""
    hits = hits_lenient = total = sentences = 0
    per_item = []
    for it in items:
        names = groundtruth.get(it["id"])
        if not names:
            continue
        sentences += 1
        item_hits = 0
        for n in names:
            total += 1
            if n in it["hypothesis"]:
                hits += 1
                item_hits += 1
            if name_reflected(it["hypothesis"], n):
                hits_lenient += 1
        per_item.append(item_hits / len(names))
    return {
        "ets": (hits / total) if total else None,
        "ets_lenient": (hits_lenient / total) if total else None,
        "ets_macro": statistics.fmean(per_item) if per_item else None,
        "ets_names": total,
        "ets_sentences": sentences,
    }


def score_items(items, groundtruth) -> dict:
    """chrF + 확정 인명 반영률 + ETS + tokens_in — CLI와 드라이버(prompt_ablation.py)가 같은 함수를 쓴다."""
    hyps = [it["hypothesis"] for it in items]
    refs = [it["reference"] for it in items]
    name_recall, n_confirmed = confirmed_name_recall(items)
    e = ets(items, groundtruth)
    tokens = [it["tokens_in"] for it in items if it.get("tokens_in") is not None]
    return {"n": len(items), "chrf": sacrebleu.corpus_chrf(hyps, [refs]).score,
            "confirmed_name_recall": name_recall, "n_confirmed": n_confirmed,
            "ets": e["ets"], "ets_lenient": e["ets_lenient"], "ets_macro": e["ets_macro"],
            "ets_names": e["ets_names"], "ets_sentences": e["ets_sentences"],
            "mean_tokens_in": statistics.fmean(tokens) if tokens else None,
            "_ets": e, "_tokens": tokens}


def sentence_rows(items, groundtruth):
    """문장 단위 행 — corpus 집계 한 줄이 어느 문장에서 오는지 보기 위한 것. sentence chrF 오름차순."""
    rows = []
    for it in items:
        hyp, names = it["hypothesis"], groundtruth.get(it["id"], [])
        inj_miss = [e["resolvedName"] for e in it.get("entities", [])
                    if e.get("kbId") and e.get("resolvedName") and e["resolvedName"] not in hyp]
        rows.append({
            "id": it["id"],
            "chrf": sacrebleu.sentence_chrf(hyp, [it["reference"]]).score if it.get("reference") else None,
            "ets_hit": sum(1 for n in names if n in hyp), "ets_total": len(names),
            "ets_miss": [n for n in names if n not in hyp], "inj_miss": inj_miss,
            "grade": it.get("grade"), "tokens_in": it.get("tokens_in"),
            "hyp": hyp, "ref": it.get("reference", ""),
        })
    rows.sort(key=lambda r: (r["chrf"] is None, r["chrf"]))
    return rows


def write_report(rows, path, compare=None):
    """TSV. compare가 있으면(B 배치의 id→row) B 열과 Δ를 붙이고 movers 요약을 stderr에 낸다."""
    out = sys.stdout if path in (None, "-") else open(path, "w", encoding="utf-8")
    cols = ["id", "chrf"] + (["chrf_b", "delta"] if compare else []) + \
           ["ets", "ets_miss", "inj_miss", "grade", "tokens_in", "hyp", "ref"]
    print("\t".join(cols), file=out)
    paired = []
    for r in rows:
        b = compare.get(r["id"]) if compare else None
        vals = [r["id"], f"{r['chrf']:.2f}" if r["chrf"] is not None else ""]
        if compare:
            d = (b["chrf"] - r["chrf"]) if (b and r["chrf"] is not None) else None
            vals += [f"{b['chrf']:.2f}" if b else "", f"{d:+.2f}" if d is not None else ""]
            if d is not None:
                paired.append((d, r, b))
        vals += [f"{r['ets_hit']}/{r['ets_total']}" if r["ets_total"] else "",
                 " ".join(r["ets_miss"]), " ".join(r["inj_miss"]), r["grade"] or "",
                 str(r["tokens_in"] or ""), r["hyp"][:80].replace("\t", " "), r["ref"][:80].replace("\t", " ")]
        print("\t".join(vals), file=out)
    if out is not sys.stdout:
        out.close()
    if compare and paired:
        paired.sort()
        total = sum(d for d, _, _ in paired)
        worse = sum(1 for d, _, _ in paired if d < -0.5)
        better = sum(1 for d, _, _ in paired if d > 0.5)
        top10 = sum(d for d, _, _ in paired[:10])
        print(f"\n짝 {len(paired)}문장: 문장 chrF Δ 합 {total:+.1f} (평균 {total / len(paired):+.2f}) — "
              f"나빠짐(<−0.5) {worse} / 좋아짐(>+0.5) {better} / 나머지 {len(paired) - worse - better}", file=sys.stderr)
        print(f"하위 10문장 Δ 합 {top10:+.1f} = 총 하락의 {100 * top10 / total:.0f}% (100% 초과 = 나머지 문장은 합쳐서 개선)"
              if total < 0 else f"상위 10문장 Δ 합 {sum(d for d, _, _ in paired[-10:]):+.1f}", file=sys.stderr)
        print("가장 나빠진 5:", file=sys.stderr)
        for d, r, b in paired[:5]:
            print(f"  {d:+6.2f}  {r['id']}  A={r['chrf']:.1f} B={b['chrf']:.1f}  {r['ref'][:40]}", file=sys.stderr)
        print("가장 좋아진 5:", file=sys.stderr)
        for d, r, b in paired[-5:][::-1]:
            print(f"  {d:+6.2f}  {r['id']}  A={r['chrf']:.1f} B={b['chrf']:.1f}  {r['ref'][:40]}", file=sys.stderr)
        lost = [(r["id"], n) for _, r, b in paired for n in b["ets_miss"] if n not in r["ets_miss"]]
        gained = [(r["id"], n) for _, r, b in paired for n in r["ets_miss"] if n not in b["ets_miss"]]
        print(f"B에서만 빠진 정답지 인명: {lost or '없음'} / B에서만 살아난 인명: {gained or '없음'}", file=sys.stderr)


def r4(x):
    return None if x is None or x != x else round(x, 4)


def emit(msg, machine):
    print(msg, file=sys.stderr if machine else sys.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--save-baseline", action="store_true",
                        help="현재 점수를 기준선으로 저장 (모델·프롬프트 변경 확정 시에만)")
    parser.add_argument("--batch-id", action="append", default=[],
                        help="이 배치의 결과만 (반복 가능). 라운드 단위 채점용")
    parser.add_argument("--prompt-version", help="이 prompt_version의 결과만")
    parser.add_argument("--model", help="이 model_used의 결과만")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--groundtruth", type=Path, default=DEFAULT_GROUNDTRUTH)
    parser.add_argument("--json", action="store_true", help="기계 출력 (stdout에 JSON만, 사람용 줄은 stderr)")
    parser.add_argument("--include-fake", action="store_true", help="fake provider(fake-*) 결과도 포함 — 데모·플럼빙 점검용")
    parser.add_argument("--no-gate", action="store_true", help="기준선 비교를 생략 (채점만; 드라이버가 자체 판정할 때)")
    parser.add_argument("--self-check-reference", action="store_true",
                        help="전문가 번역을 가설로 넣어 정답지 자체를 점검 (LLM·DB 불필요)")
    parser.add_argument("--report", nargs="?", const="-", metavar="PATH",
                        help="문장 단위 TSV 리포트 (경로 생략 시 stdout). 사람용 요약은 stderr")
    parser.add_argument("--compare-batch", metavar="UUID",
                        help="이 배치(B)를 같은 문장으로 짝지어 A(기본 필터) 대비 ΔchrF·인명 변화를 낸다")
    args = parser.parse_args()

    gold = load_goldenset(args.corpus)
    groundtruth = load_groundtruth(args.groundtruth)

    if args.self_check_reference:
        # 정답 번역의 ETS = 정답지·채점 규칙의 상한. 100%가 아닌 몫은 정답지 표기(한글명)와 전문가
        # 번역 표기의 차이(성 생략·이칭 등)이지 번역 오류가 아니다 — 게이트 오탐률 3.9%와 같은 성격.
        items = [{"id": r["id"], "hypothesis": r["reference"], "reference": r["reference"]} for r in gold.values()]
        e = ets(items, groundtruth)
        if args.report:
            write_report([r for r in sentence_rows(items, groundtruth) if r["ets_miss"]], args.report)
        out = {"mode": "self-check-reference", "n": len(items), **{k: r4(v) if isinstance(v, float) else v for k, v in e.items()}}
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(f"전문가 번역 자체의 ETS: 엄격 {e['ets']:.4f} / 관대 {e['ets_lenient']:.4f} / macro {e['ets_macro']:.4f}"
                  f"  (정답지 인명 {e['ets_names']}건, 문장 {e['ets_sentences']}건)")
        return

    import psycopg  # DB 경로에서만 필요
    with psycopg.connect(DSN) as conn:
        found = fetch_results(conn, gold.keys(), args.batch_id, args.prompt_version, args.model, args.include_fake)

    items = [{"id": gold[h]["id"], "reference": gold[h]["reference"], "hypothesis": res["hypothesis"],
              "entities": res["entities"], "tokens_in": res["tokens_in"], "grade": res["grade"]}
             for h, res in found.items()]
    if len(items) < MIN_ITEMS:
        emit(f"매칭된 시스템 결과가 {len(items)}건뿐 — 채점 불가 (골든셋 배치를 먼저 돌릴 것)", args.json)
        sys.exit(2)

    sc = score_items(items, groundtruth)
    chrf, name_recall, n_confirmed, e = sc["chrf"], sc["confirmed_name_recall"], sc["n_confirmed"], sc["_ets"]
    tokens = sc["_tokens"]
    models = sorted({found[h]["model"] for h in found if found[h]["model"]})
    prompt_versions = sorted({found[h]["prompt_version"] or "(pre-V4: null)" for h in found})
    batch_ids = sorted({found[h]["batch_id"] for h in found if found[h]["batch_id"]})

    emit(f"n={len(items)} (확정 인명 {n_confirmed}건 / 정답지 인명 {e['ets_names']}건·문장 {e['ets_sentences']}건) "
         f"모델={models} prompt={prompt_versions}", args.json)
    emit(f"chrF                = {chrf:.2f}", args.json)
    emit(f"확정 인명 반영률    = {name_recall:.4f}   (게이트 지표 — 주입한 이름의 반영 여부, 순환)", args.json)
    if e["ets"] is not None:
        emit(f"ETS (독립 정답지)   = {e['ets']:.4f}   (관대 {e['ets_lenient']:.4f} / macro {e['ets_macro']:.4f})", args.json)
    if tokens:
        emit(f"tokens_in 평균      = {statistics.fmean(tokens):.1f}", args.json)

    current = {"date": str(date.today()), "n": len(items), "models": models,
               "prompt_versions": prompt_versions, "batch_ids": batch_ids,
               "corpus": args.corpus.name, "groundtruth": args.groundtruth.name,
               "chrf": round(chrf, 2), "confirmed_name_recall": r4(name_recall), "n_confirmed": n_confirmed,
               "ets": r4(e["ets"]), "ets_lenient": r4(e["ets_lenient"]), "ets_macro": r4(e["ets_macro"]),
               "ets_names": e["ets_names"], "ets_sentences": e["ets_sentences"],
               "mean_tokens_in": round(statistics.fmean(tokens), 1) if tokens else None}

    if args.report:
        compare = None
        if args.compare_batch:
            with psycopg.connect(DSN) as conn:
                found_b = fetch_results(conn, gold.keys(), batch_ids=[args.compare_batch], include_fake=args.include_fake)
            items_b = [{"id": gold[h]["id"], "reference": gold[h]["reference"], "hypothesis": r["hypothesis"],
                        "entities": r["entities"], "tokens_in": r["tokens_in"], "grade": r["grade"]}
                       for h, r in found_b.items()]
            compare = {r["id"]: r for r in sentence_rows(items_b, groundtruth)}
            emit(f"비교 배치 B: n={len(items_b)}", True)
        write_report(sentence_rows(items, groundtruth), args.report, compare)
        if args.report == "-":
            return   # stdout을 표로 썼으니 판정 줄과 섞지 않는다

    if args.save_baseline or not BASELINE_PATH.exists():
        BASELINE_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        emit(f"기준선 저장: {BASELINE_PATH.name}", args.json)
        if args.json:
            print(json.dumps({**current, "verdict": "baseline-saved"}, ensure_ascii=False))
        return

    verdict, violations = None, []
    if not args.no_gate:
        base = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        chrf_drop = base["chrf"] - chrf
        recall_drop = base["confirmed_name_recall"] - name_recall
        emit(f"기준선({base['date']}, n={base['n']}) 대비: chrF {-chrf_drop:+.2f}, 확정 인명 반영률 {-recall_drop:+.4f}"
             + (f", ETS {e['ets'] - base['ets']:+.4f} (보고만)" if e["ets"] is not None and base.get("ets") is not None else ""),
             args.json)
        if chrf_drop > CHRF_TOLERANCE:
            violations.append(f"chrF 하락 {chrf_drop:.2f} > 허용 {CHRF_TOLERANCE}")
        if recall_drop > NAME_RECALL_TOLERANCE:
            violations.append(f"확정 인명 반영률 하락 {recall_drop:.4f} > 허용 {NAME_RECALL_TOLERANCE}")
        verdict = "violation" if violations else "pass"

    if args.json:
        print(json.dumps({**current, "verdict": verdict, "violations": violations}, ensure_ascii=False))
    if violations:
        emit("비열등 위반: " + "; ".join(violations), args.json)
        sys.exit(1)
    if verdict == "pass":
        emit("비열등 판정: 통과", args.json)


if __name__ == "__main__":
    main()
