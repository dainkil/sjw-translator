#!/usr/bin/env python3
"""인명 정답지를 KB에서 떼어 내 전문가 국역에 다시 묶는다. **LLM 호출 0회.**

기존 `ner_groundtruth_300.json`(research/06_NER/build_groundtruth.py)의 두 가지 문제:
  1. 한글 표기를 런타임 KB 역색인의 첫 후보(`pids[0]`)에서 붙였다 — KB에 있는 이름의 정답은 곧 KB 값이라
     ETS는 "KB대로 썼는가"를 잰다. 성 없는 키(珙)가 다른 사람(윤공)에 잘못 링크되면 정답지도 그 사람을 적는다.
  2. 국편 어노테이션 528건 중 383건만 남았고, 빠진 145건은 거의 전부 KB에 없는 이름이다(걸러 낸 코드는
     레포에 없다). KB가 도울 수 없는 이름이 채점에서 빠져 있었다.

이 스크립트는 `crawl_idx_person.py`가 받은 **어노테이션 원본 전체**를 쓰고, 한글 표기는 같은 문장의
**전문가 국역(reference)에 실제로 나온 표기**로 붙인다. 한문을 읽을 필요가 없다 — 판정은 한글끼리의 비교다.

후보 표기(이 순서로 국역에서 찾는다; 이름은 어절 첫머리에서만 인정):
  kb      런타임 링킹(simulate_cache.link = FileKnowledgeSource 이식)이 주입하는 이름, 그다음 동명이인 후보
  master  같은 한자의 person_master(27,329명, 전 시대) 표기
  lib     `hanja` 라이브러리 독음 + 성 독음 보정(person_master에서 그 한자가 성일 때 가장 흔한 음: 沈→심)
          + 첫 글자 두음법칙(리→이, 류→유, 로→노 …)

상태(status):
  agree        주입될 이름이 국역에 있다 — KB 표기가 국역으로 검증됨
  kb_alt       주입될 이름은 없고 다른 KB 후보(동명이인) 표기가 있다
  master       KB에 없고 person_master(다른 시대 인물 포함) 표기가 국역에 있다
  lib          KB·마스터에 없고 라이브러리 독음(보정 포함)이 국역에 있다
  given        원문이 이름만 쓴 KB 밖 인물이고 국역에 "성 + 그 이름 독음"이 하나만 있다 → 정답은 이름부 독음
               (예: 克明 → 국역 이극명 → 정답 "극명")
  surname_fix  원문이 이름만이고 KB가 성을 붙여 주입하는데, 국역에는 "다른 성 + 같은 이름"이 하나만 있다
               (성 없는 키의 오링킹 — 예: 尙安 KB 이상안 / 국역 김상안)
  rare         라이브러리가 읽지 못하는 희귀 한자(BMP 밖)가 든 이름이고, 국역에 성·길이가 같은 표기가
               하나만 있다 → 그 표기 (예: 沈𪗆 → 심제, 朴𥶇 → 박로)
  ref_reading  성·길이가 같고 이름 글자가 다른 표기가 국역에 하나만 있다 — **채점 제외, 검토 목록에만**
               (실측: 국역 저본이 다른 글자인 경우(李必崇 → 이필영)와 일반 낱말(毛督 → 모두)이 섞여 있어
               정답으로 쓰기에 믿을 수 없다)
  unresolved   국역에서 표기를 찾지 못함 → 채점에서 제외하고 개수만 보고

사용법:
  uv run --with hanja python eval/name_gold.py                                    # eval/name_gold_300.json
  uv run --with hanja python eval/name_gold.py --review eval/name_gold_review.tsv  # agree 아닌 건을 TSV로
"""
import argparse
import collections
import html
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from simulate_cache import link, reign_year_to_ad  # noqa: E402

HANGUL_RUN = re.compile(r"[가-힣]+")
HANGUL_ONLY = re.compile(r"[가-힣]+")
# CJK 통합 한자 + 확장 A + 호환 + 확장 B 이후(BMP 밖 희귀 한자) — 코드포인트로 적는다
_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x3134F))
CJK = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _CJK_RANGES) + "]")
SURROGATE_PAIR = re.compile(r"&#(\d{5});&#(\d{5});")


def decode_entities(span: str) -> str:
    """국편 페이지는 BMP 밖 희귀 한자를 서로게이트 쌍 엔티티로 싣는다(`&#55381;&#57050;`) — 한 글자로 되돌린다."""
    def pair(m):
        hi, lo = int(m.group(1)), int(m.group(2))
        if 0xD800 <= hi <= 0xDBFF and 0xDC00 <= lo <= 0xDFFF:
            return chr(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00))
        return m.group(0)
    return html.unescape(SURROGATE_PAIR.sub(pair, span))
INJECTING_STAGES = ("SINGLE", "TIME", "OFFICE")
# 국역에서 이름 바로 뒤(같은 어절)에 올 수 있는 것 — 조사·서술격·호칭. 그 밖의 글자가 이어지면 더 긴 다른 낱말이다
# ("이봉진이"의 "이봉", "공의가"의 "공").
PARTICLES = ("이", "가", "은", "는", "을", "를", "의", "에", "과", "와", "도", "로", "으로", "만", "께", "라",
             "이라", "인", "였", "이었", "입니다", "이다", "등", "까지", "부터", "처럼", "보다", "씨", "형제", "부자")

# 두음법칙 — 초성 ㄹ/ㄴ + 이 모음들이면 ㅇ, 그 밖의 ㄹ은 ㄴ
_YI_VOWELS = {2, 6, 7, 12, 17, 20}          # ㅑ ㅕ ㅖ ㅛ ㅠ ㅣ (중성 인덱스)
_L, _N, _O = 5, 2, 11                        # 초성 인덱스 ㄹ ㄴ ㅇ


def dueum(ch: str) -> str:
    code = ord(ch) - 0xAC00
    if not 0 <= code < 11172:
        return ch
    cho, jung, jong = code // 588, (code % 588) // 28, code % 28
    if cho == _L:
        cho = _O if jung in _YI_VOWELS else _N
    elif cho == _N and jung in _YI_VOWELS:
        cho = _O
    else:
        return ch
    return chr(0xAC00 + cho * 588 + jung * 28 + jong)


def default_master() -> Path:
    for p in (ROOT / "malmoi" / "kb" / "person_master.json", ROOT.parent / "malmoi" / "kb" / "person_master.json"):
        if p.exists():
            return p
    return ROOT / "malmoi" / "kb" / "person_master.json"


def load_master(path: Path):
    """한자명 → 한글 표기 집합 / 성 한자 → 가장 흔한 성 독음 / 한글명 전체 집합."""
    readings = collections.defaultdict(set)
    surname_votes = collections.defaultdict(collections.Counter)
    names = set()
    for p in json.loads(path.read_text(encoding="utf-8")):
        h, k = p.get("한자_명"), p.get("한글_명")
        if h and k:
            readings[h].add(k)
            names.add(k)
            if len(h) == len(k) and len(h) >= 2:
                surname_votes[h[0]][k[0]] += 1
    surname_reading = {h: c.most_common(1)[0][0] for h, c in surname_votes.items()}
    return readings, surname_reading, names


def normalize_span(span: str) -> str:
    """어노테이션 표면형 → 이름 한자. 구두점 제거, 봉군 칭호 접두(昇平府院君金瑬 → 金瑬) 제거."""
    s = "".join(CJK.findall(decode_entities(span)))
    if "君" in s and not s.endswith("君"):
        tail = s.rsplit("君", 1)[1]
        tail = tail[1:] if tail.startswith("臣") else tail     # 昇平府院君臣金瑬 — 상소의 자칭
        if 2 <= len(tail) <= 4:
            s = tail
    return s


def lib_readings(hanja: str, translate, surname_reading):
    """라이브러리 독음 + 성 보정 + 첫 글자 두음 변형 (중복 제거, 순서 유지)."""
    base = translate(hanja)
    if not base or base == hanja or len(base) != len(hanja):
        return []
    out = [base]
    if len(hanja) >= 2 and hanja[0] in surname_reading:
        out.append(surname_reading[hanja[0]] + base[1:])
    out += [dueum(r[0]) + r[1:] for r in list(out)]
    return list(dict.fromkeys(out))


def name_tokens(reference: str, length: int, korean_names=frozenset()):
    """국역에서 이름 자리에 올 수 있는 길이 `length`의 한글 토큰.

    이름은 어절 첫머리에서 시작하고 뒤에는 조사·호칭만 붙는다 — "진심을"의 "심을"(어절 중간),
    "이봉진이"의 "이봉"(뒤에 다른 글자)은 받지 않는다. 토큰 뒤 한 글자까지가 인물 마스터의 더 긴
    이름이면("조위" + "한") 그 긴 이름의 앞부분이므로 버린다.
    """
    out = set()
    if length < 2:          # 한 글자 "이름"은 국역에서 이름인지 낱말인지 가를 수 없다
        return out
    for run in HANGUL_RUN.findall(reference):
        tok, rest = run[:length], run[length:]
        if len(tok) < length or (rest and tok + rest[0] in korean_names):
            continue
        if rest and not rest.startswith(PARTICLES):
            continue
        out.add(tok)
    return out


def resolve(candidates, injected, reference, korean_names, expected_surname=None, expected_len=None):
    """candidates: [(표기, 출처)] → (status, gold)."""
    by_len = {}

    def present(k):
        if len(k) not in by_len:
            by_len[len(k)] = name_tokens(reference, len(k), korean_names)
        return k in by_len[len(k)]

    for k, src in candidates:
        if present(k):
            if k == injected:
                return "agree", k
            return {"lib": "lib", "master": "master"}.get(src, "kb_alt"), k

    if injected and expected_len and len(injected) == expected_len + 1:
        given = injected[1:]
        toks = name_tokens(reference, len(injected), korean_names)
        hits = sorted(t for t in toks if t[1:] == given and t[0] != injected[0])
        if len(hits) == 1:
            return "surname_fix", hits[0]

    if not injected and expected_len and expected_len >= 2:
        # 원문이 이름만 쓴 KB 밖 인물(克明): 국역은 성을 붙여 쓴다(이극명). 원문에 없는 성은 채점하지 않고
        # 이름부 독음이 맞는지만 본다 — 정답은 이름부, 국역의 성 붙은 표기는 근거로만 남긴다.
        toks = name_tokens(reference, expected_len + 1, korean_names)
        for k, src in candidates:
            if src == "lib" and len(k) == expected_len:
                full = sorted(t for t in toks if t[1:] == k)
                if len(full) == 1:
                    return "given", k

    ref_len = len(injected) if injected else expected_len
    if expected_surname and ref_len and ref_len >= 2:
        toks = name_tokens(reference, ref_len, korean_names)
        hits = [t for t in toks if t[0] == expected_surname and all(t != k for k, _ in candidates)]
        if ref_len >= 3:
            gives = [k[1:] for k, _ in candidates if len(k) == ref_len]
            hits = [t for t in hits if any(any(a == b for a, b in zip(t[1:], g)) for g in gives)]
        if len(hits) == 1:
            # 후보 독음이 전부 읽지 못한 글자를 품고 있으면(BMP 밖 희귀 한자) 국역이 유일한 근거다 — 채점에 쓴다
            if candidates and all(not HANGUL_ONLY.fullmatch(k) for k, _ in candidates):
                return "rare", hits[0]
            return "ref_reading", hits[0]

    return "unresolved", None


def build(corpus, spans_by_id, old_gold, inv, ids, master_readings, surname_reading, korean_names, translate):
    rows = []
    for doc_id, spans in spans_by_id.items():
        rec = corpus.get(doc_id)
        if rec is None:
            continue
        year = reign_year_to_ad(doc_id) or 10 ** 9
        old_by_hanja = {e[1]: e[2] for e in old_gold.get(doc_id, []) if e[0] == "PER"}
        for surface in spans:
            hanja = normalize_span(surface)
            if not hanja:
                continue
            stage, cand_ids = link(hanja, year, rec["original"], inv, ids)
            kb_names = list(dict.fromkeys(ids[i]["한글_명"] for i in cand_ids if i in ids))
            injected = kb_names[0] if stage in INJECTING_STAGES and kb_names else None

            cands = [(k, "kb") for k in kb_names]
            cands += [(k, "master") for k in sorted(master_readings.get(hanja, ()))]
            libs = lib_readings(hanja, translate, surname_reading)
            cands += [(k, "lib") for k in libs]
            cands = list(dict.fromkeys(cands))
            seen, uniq = set(), []
            for k, src in cands:
                if k not in seen:
                    seen.add(k)
                    uniq.append((k, src))

            if injected:
                exp_surname = injected[0]
            elif len(hanja) >= 2 and hanja[0] in surname_reading:
                exp_surname = surname_reading[hanja[0]]
            else:
                exp_surname = dueum(libs[0][0]) if libs else None
            status, gold = resolve(uniq, injected, rec["reference"], korean_names,
                                   expected_surname=exp_surname, expected_len=len(hanja))
            rows.append({
                "id": doc_id, "surface": surface, "hanja": hanja, "gold": gold, "status": status,
                "stage": stage, "in_kb": stage != "MISS", "injected": injected, "kb_names": kb_names,
                "lib": libs[0] if libs else None,
                "old": old_by_hanja.get(surface) or old_by_hanja.get(hanja),
                "in_old_gold": surface in old_by_hanja or hanja in old_by_hanja,
            })
    return rows


def scored(r) -> bool:
    """채점에 쓰는 이름 — 국역 표기를 믿을 수 있는 규칙으로 찾은 것."""
    return bool(r["gold"]) and r["status"] != "ref_reading"


def rate(num, den):
    return round(num / den, 4) if den else None


def summarize(rows):
    def seg(rs):
        return {"names": len(rs), "status": dict(collections.Counter(r["status"] for r in rs))}

    in_kb = [r for r in rows if r["in_kb"]]
    out_kb = [r for r in rows if not r["in_kb"]]
    injected = [r for r in rows if r["injected"] and scored(r)]
    old = [r for r in rows if r["in_old_gold"]]
    return {
        "names": len(rows),
        "sentences": len({r["id"] for r in rows}),
        "all": seg(rows),
        "in_kb": seg(in_kb),
        "out_kb": seg(out_kb),
        "out_kb_share": rate(len(out_kb), len(rows)),
        "old_gold": {"names": len(old), "out_kb": sum(1 for r in old if not r["in_kb"]),
                     "dropped": len(rows) - len(old),
                     "dropped_out_kb": sum(1 for r in rows if not r["in_old_gold"] and not r["in_kb"])},
        # KB가 확정해 주입하는 이름 중 국역 표기와 다른 비율 = KB 주입의 국역 대비 오류율 (오링킹 + 독음 차이)
        "injected_disagree": {"rate": rate(sum(1 for r in injected if r["gold"] != r["injected"]), len(injected)),
                              "names": len(injected)},
        "scored": sum(1 for r in rows if scored(r)),
        "scored_rate": rate(sum(1 for r in rows if scored(r)), len(rows)),
    }


def write_review(rows, corpus, path: Path, window=30):
    with path.open("w", encoding="utf-8") as f:
        f.write("id\tstatus\tsurface\thanja\tgold\tinjected\tstage\tlib\told\treference_context\n")
        for r in rows:
            if r["status"] == "agree":
                continue
            ref = corpus[r["id"]]["reference"]
            anchor = r["gold"] or (r["injected"] or r["lib"] or "")[-1:]
            i = ref.find(anchor) if anchor else -1
            ctx = ref[max(0, i - window): i + window] if i >= 0 else ref[:2 * window]
            f.write("\t".join([r["id"], r["status"], r["surface"], r["hanja"], r["gold"] or "",
                               r["injected"] or "", r["stage"], r["lib"] or "", r["old"] or "",
                               ctx.replace("\t", " ").replace("\n", " ")]) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", type=Path, default=HERE / "eval300_1925.json")
    ap.add_argument("--spans", type=Path, default=HERE / "idx_person_300.json")
    ap.add_argument("--old-groundtruth", type=Path, default=HERE / "ner_groundtruth_300.json")
    ap.add_argument("--kb", type=Path, default=ROOT / "kb")
    ap.add_argument("--kb-name", default="injo")
    ap.add_argument("--master", type=Path, default=default_master())
    ap.add_argument("--out", type=Path, default=HERE / "name_gold_300.json")
    ap.add_argument("--review", type=Path, help="agree가 아닌 건을 국역 문맥과 함께 TSV로")
    args = ap.parse_args()

    import hanja as hanja_lib

    def translate(s):
        return hanja_lib.translate(s, "substitution")

    corpus = {r["id"]: r for r in json.loads(args.corpus.read_text(encoding="utf-8"))["corpus"]}
    spans = json.loads(args.spans.read_text(encoding="utf-8"))
    old_gold = json.loads(args.old_groundtruth.read_text(encoding="utf-8")) if args.old_groundtruth.exists() else {}
    inv = json.loads((args.kb / f"inverted_index_{args.kb_name}.json").read_text(encoding="utf-8"))
    ids = json.loads((args.kb / f"id_lookup_{args.kb_name}.json").read_text(encoding="utf-8"))
    master_readings, surname_reading, korean_names = load_master(args.master)

    rows = build(corpus, spans, old_gold, inv, ids, master_readings, surname_reading, korean_names, translate)
    summary = summarize(rows)
    args.out.write_text(json.dumps({"spans": args.spans.name, "kb": args.kb_name, "summary": summary,
                                    "names": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.review:
        write_review(rows, corpus, args.review)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
