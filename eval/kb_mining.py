#!/usr/bin/env python3
"""Step 5 — 검색 실패 사례를 활용한 오프라인 KB 확장 (CRAG-inspired corrective KB augmentation). **LLM 호출 0회.**

KB 밖 이름(링킹 MISS)의 87%는 person_master에도 없다(`kb_coverage.py`) — KB 범위를 넓혀서는 덮이지 않는다.
남은 출처는 병렬 코퍼스의 **전문가 국역**이다: 원문에서 NER이 찾은 인명(MISS)이 같은 행 국역에 어떤 표기로
나오는지 모아, 여러 행에서 합의되는 표기를 KB 후보로 만든다. 런타임 외부 검색이 아니라 오프라인 후보 수집 →
신뢰도 등급 → 승격이다 (CRAG의 "검색 결과 평가 → 교정 행동"을 실시간이 아닌 KB 구축 단계로 옮긴 것).

행 단위 투표 (NER PER 멘션 1개, 2자 이상만 — 1자 멘션은 국역에서 이름인지 가를 수 없다):
  exact    `hanja` 라이브러리 독음(성 독음 보정·두음 포함, name_gold.lib_readings)이 국역의 이름 자리에 있다
  aligned  독음 후보가 없고(희귀자 등), 국역에 성·길이가 같은 이름 토큰이 **하나만** 남는다 — 같은 행의 다른
           멘션이 설명하는 표기(KB 주입명, 다른 멘션의 exact 표기)를 뺀 뒤. 3자 이상이면 이름 글자 1자 이상 일치
  (이름 자리 = 어절 첫머리 + 뒤에 조사만, name_gold.name_tokens)

신뢰도 등급 — **결과를 보기 전에 고정** (2026-10-04):
  High    투표 ≥ 2, 최다 표기 합의율 ≥ 0.8, 그리고 (최다 표기에 exact 투표 ≥ 1 또는 투표 ≥ 3) → 자동 승격
  Medium  High가 아니고 투표 ≥ 1, 합의율 ≥ 0.6 → 검수 대기열 (전문가 부재 — 승격하지 않는다)
  Low     그 밖 → Unknown 유지
  일반 낱말 필터 — **개발 실행 후 추가** (부분 NER 캐시 7,782행의 후보 목록을 보고: 李進 → "이와"(이와 같으니),
  夏衍 → "하니"). 최다 표기에 exact 투표가 없으면(aligned로만 모였으면), 그 표기가 마이닝 입력 국역에서 어절
  첫머리로 나오는 행 수가 max(5 × 멘션 행 수, 20)를 넘을 때 Low로 내린다 — 진짜 이름이면 토큰 빈도가 멘션 빈도를
  따라간다(申櫄 → 신훈: 멘션 8행, 토큰 12행). 등급 기준(투표·합의율)은 바꾸지 않았다.

평가 (모두 LLM 0회):
  A. 유사-OOV 정밀도 — KB에 있는 이름(SINGLE)을 KB 없이 같은 방법으로 캐서 KB 표기와 맞는지. 표본이 크다(수천 키).
     KB 이름은 KB 밖 이름보다 자주·크게 나오는 사람들이라 분포가 다르다는 점은 경계로 남긴다.
  B. 골든셋 KB 밖 이름(국역 정답지, name_gold.scored) — 등급별 커버리지와 정답 일치율.
     `--train-years 1-20`이면 평가는 재위 21–27년 문장만(시기 분할, leakage.test_ids).
     정답지가 라이브러리 독음 후보로 찾은 표기라 exact 투표에 유리하다 — 상태별(lib / rare·given 등)로 나눠 낸다.
  C. 코퍼스 MISS 멘션 중 High 키가 덮는 비율 (학습 행 안 — 표본 내).
  D. (시기 분할일 때) 시험 연차 코퍼스 행(규칙 1·2·4 통과)의 MISS 멘션 중 학습 연차 High 키가 덮는 비율과, 덮인
     멘션 중 시험 행 자신의 국역 투표가 있는 것에서 표기가 맞는 비율 — 골든셋 36명보다 큰 표본의 표본 외 지표.

누출 방지 (eval/leakage.py): 평가 문장·같은 날짜 행·밀림 의심 행을 마이닝 입력에서 뺀다(규칙 1·2·4), assert_no_leak.

사용법:
  python3 eval/ner_corpus_cache.py                                               # 선행: 코퍼스 NER 캐시 (~80분)
  uv run --with hanja python eval/kb_mining.py --tag all                       # 전 연차(평가 문장·날짜 제외)
  uv run --with hanja python eval/kb_mining.py --tag y01-20 --train-years 1-20  # 시기 분할
  ... --write-kb injo_aug                                                      # High 승격분을 합친 KB 파일 쓰기
"""
import argparse
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from leakage import (EvalSet, assert_no_leak, default_corpus, filter_rows, kb_name_keys,  # noqa: E402
                     misaligned_rows, reign_year, test_ids)
from name_gold import (default_master, dueum, lib_readings, load_master, name_tokens,  # noqa: E402
                       normalize_span, scored)
from simulate_cache import MAX_TEXT, link, normalized_hash, reign_year_to_ad  # noqa: E402

HIGH_MIN_VOTES, HIGH_AGREE = 2, 0.8
MED_MIN_VOTES, MED_AGREE = 1, 0.6
COMMON_RATIO, COMMON_MIN = 5, 20       # 일반 낱말 필터 (aligned 전용)


def parse_years(spec):
    if not spec:
        return None
    a, b = spec.split("-")
    return range(int(a), int(b) + 1)


def token_df(rows, lengths=(2, 3, 4, 5)):
    """국역의 어절 첫머리 토큰별 출현 행 수 — 일반 낱말 필터용. 마이닝 입력 행에서만 센다(평가 국역 제외)."""
    import re
    df = collections.Counter()
    for r in rows:
        toks = set()
        for run in set(re.findall(r"[가-힣]+", r.get("translation") or "")):
            toks.update(run[:L] for L in lengths if len(run) >= L)
        df.update(toks)
    return df


def common_word(top, exact, rows, df):
    return df is not None and exact[top] == 0 and df.get(top, 0) > max(COMMON_RATIO * rows, COMMON_MIN)


def tier(votes: collections.Counter, exact: collections.Counter, rows=0, df=None):
    total = sum(votes.values())
    if not total:
        return "Low", None, 0.0
    top, n = votes.most_common(1)[0]
    agree = n / total
    if common_word(top, exact, rows, df):
        return "Low", top, agree
    if total >= HIGH_MIN_VOTES and agree >= HIGH_AGREE and (exact[top] >= 1 or total >= 3):
        return "High", top, agree
    if total >= MED_MIN_VOTES and agree >= MED_AGREE:
        return "Medium", top, agree
    return "Low", top, agree


class Miner:
    def __init__(self, inv, ids, surname_reading, korean_names, translate):
        self.inv, self.ids = inv, ids
        self.sr, self.names, self.translate = surname_reading, korean_names, translate
        # key → {"rows", "votes", "exact", "years", "examples", "gold"(유사-OOV의 KB 표기)}
        self.miss = collections.defaultdict(self._new)
        self.pseudo = collections.defaultdict(self._new)
        self.miss_mentions = 0

    @staticmethod
    def _new():
        return {"rows": 0, "votes": collections.Counter(), "exact": collections.Counter(),
                "years": collections.Counter(), "examples": [], "gold": collections.Counter()}

    def expected_surname(self, key, libs):
        if len(key) >= 2 and key[0] in self.sr:
            return self.sr[key[0]]
        return dueum(libs[0][0]) if libs else None

    def row(self, r, mentions):
        text, trans = r["original"], r.get("translation") or ""
        year = reign_year_to_ad(r["id"]) or 10 ** 9
        tok_cache = {}

        def toks(L):
            if L not in tok_cache:
                tok_cache[L] = name_tokens(trans, L, self.names)
            return tok_cache[L]

        ments = []
        for m in mentions:
            if m.get("type") != "PER":
                continue
            key = normalize_span(m["surface"])
            if len(key) < 2:
                continue
            stage, cand = link(key, year, text, self.inv, self.ids)
            kb_names = list(dict.fromkeys(self.ids[i]["한글_명"] for i in cand if i in self.ids))
            injected = kb_names[0] if stage in ("SINGLE", "TIME", "OFFICE") and kb_names else None
            ments.append({"key": key, "stage": stage, "injected": injected,
                          "libs": lib_readings(key, self.translate, self.sr)})
        explained_kb = {m["injected"] for m in ments if m["injected"]}

        # 1차: exact
        voted = {}
        for m in ments:
            for k in m["libs"]:
                if k in toks(len(k)):
                    voted[id(m)] = (k, "exact")
                    break
        explained_exact = {v[0] for v in voted.values()}

        # 2차: aligned
        for m in ments:
            if id(m) in voted:
                continue
            L = len(m["key"])
            s = self.expected_surname(m["key"], m["libs"])
            if not s:
                continue
            own = {m["injected"]} if m["injected"] else set()
            blocked = (explained_kb - own) | explained_exact
            hits = [t for t in toks(L) if t[0] == s and t not in blocked]
            if L >= 3 and m["libs"]:
                hits = [t for t in hits if any(a == b for a, b in zip(t[1:], m["libs"][0][1:]))]
            if len(hits) == 1:
                voted[id(m)] = (hits[0], "aligned")

        for m in ments:
            if m["stage"] == "MISS":
                bucket = self.miss[m["key"]]
                self.miss_mentions += 1
            elif m["stage"] == "SINGLE" and m["injected"]:
                bucket = self.pseudo[m["key"]]
                bucket["gold"][m["injected"]] += 1
            else:
                continue
            bucket["rows"] += 1
            bucket["years"][reign_year(r["id"])] += 1
            if id(m) in voted:
                k, how = voted[id(m)]
                bucket["votes"][k] += 1
                if how == "exact":
                    bucket["exact"][k] += 1
                if len(bucket["examples"]) < 2:
                    i = trans.find(k)
                    bucket["examples"].append(f"{r['id']}: …{trans[max(0, i - 15): i + 20]}…")


def summarize_keys(bucket_map, df=None):
    out = {}
    for key, b in bucket_map.items():
        t, top, agree = tier(b["votes"], b["exact"], b["rows"], df)
        out[key] = {"tier": t, "reading": top, "agree": round(agree, 3), "votes": sum(b["votes"].values()),
                    "exact": sum(b["exact"].values()), "rows": b["rows"],
                    "readings": dict(b["votes"].most_common(4)), "examples": b["examples"],
                    "common_word": bool(top) and common_word(top, b["exact"], b["rows"], df),
                    "gold": b["gold"].most_common(1)[0][0] if b["gold"] else None}
    return out


def eval_pseudo(pseudo):
    """A. 유사-OOV: KB 표기를 맞히는가 (등급별)."""
    res = {}
    for t in ("High", "Medium", "Low"):
        ks = [v for v in pseudo.values() if v["tier"] == t and v["reading"]]
        ok = sum(1 for v in ks if v["reading"] == v["gold"])
        res[t] = {"keys": len(ks), "precision": round(ok / len(ks), 4) if ks else None}
    res["keys_total"] = len(pseudo)
    res["keys_no_vote"] = sum(1 for v in pseudo.values() if not v["votes"])
    return res


def eval_gold(mined, gold_rows, ids_filter=None):
    """B. 골든셋 KB 밖 이름: 등급별 커버리지와 국역 정답 일치."""
    rows = [r for r in gold_rows if not r["in_kb"] and scored(r) and len(r["hanja"]) >= 2
            and (ids_filter is None or r["id"] in ids_filter)]
    res = {"names": len(rows), "by_tier": {}, "by_status": {}}
    for t in ("High", "Medium", "Low", "absent"):
        sel = [r for r in rows if (mined.get(r["hanja"], {}).get("tier") or "absent") == t]
        ok = sum(1 for r in sel if mined.get(r["hanja"], {}).get("reading") == r["gold"])
        res["by_tier"][t] = {"names": len(sel), "correct": ok,
                             "precision": round(ok / len(sel), 4) if sel and t != "absent" else None}
    for st in sorted({r["status"] for r in rows}):
        sel = [r for r in rows if r["status"] == st]
        hi = [r for r in sel if mined.get(r["hanja"], {}).get("tier") == "High"]
        res["by_status"][st] = {"names": len(sel), "high": len(hi),
                                "high_correct": sum(1 for r in hi if mined[r["hanja"]]["reading"] == r["gold"])}
    n = res["names"]
    hi = res["by_tier"]["High"]
    res["high_coverage"] = round(hi["names"] / n, 4) if n else None
    res["high_correct_share"] = round(hi["correct"] / n, 4) if n else None    # KB 밖 이름 중 올바르게 덮이는 몫
    return res


def eval_temporal_corpus(mined, test_rows, cache, miner_args):
    """D. 시험 연차 행에서 학습 연차 High 키의 커버리지·정밀도 (멘션 단위)."""
    test_miner = Miner(*miner_args)
    per_mention = []                                   # (key, 시험 행 투표 표기 or None)
    orig_row = test_miner.row

    for r in test_rows:
        before = {k: (b["rows"], b["votes"].copy()) for k, b in test_miner.miss.items()}
        orig_row(r, cache[normalized_hash(r["original"])])
        for k, b in test_miner.miss.items():
            rows0, votes0 = before.get(k, (0, collections.Counter()))
            if b["rows"] > rows0:
                diff = b["votes"] - votes0
                per_mention.append((k, next(iter(diff)) if diff else None))
    n = len(per_mention)
    hi = [(k, v) for k, v in per_mention if mined.get(k, {}).get("tier") == "High"]
    med = [(k, v) for k, v in per_mention if mined.get(k, {}).get("tier") == "Medium"]
    checkable = [(k, v) for k, v in hi if v]
    ok = sum(1 for k, v in checkable if mined[k]["reading"] == v)
    return {"test_rows": len(test_rows), "miss_mentions": n,
            "high_coverage": round(len(hi) / n, 4) if n else None,
            "medium_coverage": round(len(med) / n, 4) if n else None,
            "high_checkable": len(checkable),
            "high_precision_vs_test_translation": round(ok / len(checkable), 4) if checkable else None}


def write_kb(base_inv, base_ids, mined, name, tag):
    inv = {k: list(v) for k, v in base_inv.items()}
    ids = dict(base_ids)
    added = 0
    for key, v in sorted(mined.items()):
        if v["tier"] != "High" or key in inv:
            continue
        pid = f"AUG_{tag}_{added + 1:05d}"
        ids[pid] = {"한글_명": v["reading"], "한자_명": key, "본관_표준": None, "활동_시작": None, "활동_종료": None,
                    "관직_리스트": [], "출처": "corpus_mining", "투표": v["votes"], "합의율": v["agree"]}
        inv[key] = [pid]
        added += 1
    (ROOT / "kb" / f"inverted_index_{name}.json").write_text(json.dumps(inv, ensure_ascii=False), encoding="utf-8")
    (ROOT / "kb" / f"id_lookup_{name}.json").write_text(json.dumps(ids, ensure_ascii=False), encoding="utf-8")
    return added


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--train-years", help="예: 1-20 — 이 재위 연차 행에서만 캔다(시기 분할)")
    ap.add_argument("--test-years", help="기본: train-years 이후 ~27")
    ap.add_argument("--corpus", type=Path, default=default_corpus())
    ap.add_argument("--ner-cache", type=Path, default=HERE / ".ner_cache_all.json")
    ap.add_argument("--kb-name", default="injo")
    ap.add_argument("--master", type=Path, default=default_master())
    ap.add_argument("--gold", type=Path, default=HERE / "name_gold_300.json")
    ap.add_argument("--write-kb", metavar="NAME", help="High 승격분을 합친 KB를 kb/에 쓴다")
    ap.add_argument("--allow-partial", action="store_true", help="NER 캐시에 있는 행만 쓴다 (개발용 — 결과 기록 금지)")
    args = ap.parse_args()

    import hanja as hanja_lib

    def translate(s):
        return hanja_lib.translate(s, "substitution")

    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))["corpus"]
    cache = json.loads(args.ner_cache.read_text(encoding="utf-8"))
    inv = json.loads((ROOT / "kb" / f"inverted_index_{args.kb_name}.json").read_text(encoding="utf-8"))
    ids = json.loads((ROOT / "kb" / f"id_lookup_{args.kb_name}.json").read_text(encoding="utf-8"))
    _, surname_reading, korean_names = load_master(args.master)

    ev = EvalSet.load(corpus_rows=corpus)
    train = parse_years(args.train_years)
    bad = misaligned_rows(corpus, kb_name_keys(inv, ids))
    rows = filter_rows(corpus, ev, train_years=train, exclude_ids=bad)
    assert_no_leak(rows, ev)
    missing_ner = sum(1 for r in rows if normalized_hash(r["original"]) not in cache)
    if missing_ner and not args.allow_partial:
        sys.exit(f"NER 캐시에 {missing_ner}행이 없다 — eval/ner_corpus_cache.py를 먼저 끝낼 것")
    if missing_ner:
        print(f"경고: NER 캐시 없는 {missing_ner}행을 건너뛴다 (--allow-partial)", file=sys.stderr)
        rows = [r for r in rows if normalized_hash(r["original"]) in cache]

    miner = Miner(inv, ids, surname_reading, korean_names, translate)
    for r in rows:
        miner.row(r, cache[normalized_hash(r["original"])])

    df = token_df(rows)
    mined = summarize_keys(miner.miss, df)
    pseudo = summarize_keys(miner.pseudo, df)
    gold_rows = json.loads(args.gold.read_text(encoding="utf-8"))["names"]
    from eval_exclusions import load_exclusions
    excluded = load_exclusions()
    gold_rows = [r for r in gold_rows if r["id"] not in excluded]
    if train is not None:
        test = parse_years(args.test_years) or range(max(train) + 1, 28)
        gold_ids = test_ids(ev, test)
    else:
        gold_ids = None

    tiers = collections.Counter(v["tier"] for v in mined.values())
    high_keys = {k for k, v in mined.items() if v["tier"] == "High"}
    high_mentions = sum(miner.miss[k]["rows"] for k in high_keys)
    summary = {
        "tag": args.tag, "train_years": args.train_years, "rows_used": len(rows), "partial": bool(missing_ner),
        "rows_excluded": {"eval_and_same_date": len(ev.ids) + sum(1 for r in corpus if r["date"] in ev.dates
                                                                 and r["id"] not in ev.ids),
                          "misaligned": len(bad)},
        "ner_truncated_rows": sum(1 for r in rows if len(r["original"]) > MAX_TEXT),
        "miss_mentions": miner.miss_mentions, "miss_keys": len(mined), "tiers": dict(tiers),
        "common_word_demoted": sum(1 for v in mined.values() if v["common_word"]),
        "C_high_mention_coverage": round(high_mentions / miner.miss_mentions, 4) if miner.miss_mentions else None,
        "A_pseudo_oov": eval_pseudo(pseudo),
        "B_gold_out_kb": eval_gold(mined, gold_rows, gold_ids),
        "thresholds": {"high": [HIGH_MIN_VOTES, HIGH_AGREE], "medium": [MED_MIN_VOTES, MED_AGREE]},
    }
    if train is not None:
        test_rows = filter_rows(corpus, ev, train_years=test, exclude_ids=bad)
        assert_no_leak(test_rows, ev)
        test_rows = [r for r in test_rows if normalized_hash(r["original"]) in cache]
        summary["D_temporal_corpus"] = eval_temporal_corpus(
            mined, test_rows, cache, (inv, ids, surname_reading, korean_names, translate))
    if args.write_kb:
        summary["kb_written"] = {"name": args.write_kb,
                                 "added": write_kb(inv, ids, mined, args.write_kb, args.tag)}

    out = HERE / f"kb_mining_{args.tag}.json"
    out.write_text(json.dumps({"summary": summary, "candidates": mined}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    review = HERE / f"kb_mining_{args.tag}_review.tsv"
    with review.open("w", encoding="utf-8") as f:
        f.write("tier\thanja\treading\tagree\tvotes\texact\trows\treadings\texample\n")
        for key, v in sorted(mined.items(), key=lambda kv: (kv[1]["tier"] != "Medium", -kv[1]["votes"])):
            if v["tier"] == "Low":
                continue
            f.write("\t".join([v["tier"], key, v["reading"] or "", str(v["agree"]), str(v["votes"]), str(v["exact"]),
                               str(v["rows"]), json.dumps(v["readings"], ensure_ascii=False),
                               (v["examples"] or [""])[0]]) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
