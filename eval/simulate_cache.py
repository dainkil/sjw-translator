#!/usr/bin/env python3
"""M3-S4: L1/L2 캐시 히트율 시뮬레이션 + 캐시 on/off 비용 비교 (계획서 §10 M3 수용 기준).

**LLM 호출 0회.** 히트율은 코퍼스·NER·KB가 정하는 성질이고 번역 LLM과 무관하다 —
캐시 키는 원문 해시와 슬롯화된 원문 해시로만 만들어지기 때문이다(ADR-009). 따라서 1년치
전수를 실제로 번역하지 않고도 히트율을 실측할 수 있다. 무료 티어(RPD 20)에서 1,913건을
번역해 보는 것은 불가능하므로 이것이 유일하게 가능한 측정 경로다.

방법: **순차 재생.** 코퍼스를 문서 순서대로 훑으며 생산 코드와 같은 순서로 판정한다.
  ① L1 조회(원문 정규화 해시) → ② L2 조회(슬롯화 원문 해시) → ③ 미스면 LLM 호출 1회로 계상
  → 적재 정책(ADR-009)에 따라 저장: L1은 게이트 통과분만, L2는 VERIFIED + 전체형 출현분만.
이 순서가 워밍업 효과까지 반영한다 — 첫 등장은 항상 미스이므로 단순 중복 개수보다 낮게 나온다.

품질 등급은 **전문가 번역**으로 판정한다 (프록시). 시스템의 LLM 출력과 같지 않지만, 이
프로젝트는 게이트 오탐률(3.9%)도 같은 프록시로 측정했다(QualityGateGoldensetTest) — 방법론 일관.
키 일치로 결정되는 '구조적 히트율'은 프록시와 무관하게 정확하고, 적재 정책을 반영한 '실효
히트율'만 프록시에 의존한다. 둘 다 보고한다.

전제: NER 서버(:8100) 기동. 인물 KB는 kb/. 코퍼스는 malmoi/ (커밋 안 됨 — docs/asset-inventory.md).

사용법:
  python3 eval/simulate_cache.py                    # A01 (인조 1년치, 수용 기준)
  python3 eval/simulate_cache.py --years all        # 전수 27년
  python3 eval/simulate_cache.py --years A01,A03
NER 결과는 --ner-cache 파일에 캐시되어 재실행이 공짜다.
"""
import argparse
import collections
import concurrent.futures as cf
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_TEXT = 2000          # TranslateRequest @Size(max=2000) — 넘으면 API가 받지 않는다
MAX_AMBIGUOUS = 3        # FileKnowledgeSource.MAX_AMBIGUOUS
PAREN = re.compile(r"\(.*?\)")
HANJA_IN_PAREN = re.compile(r"\((.*?)\)")

# ── 생산 코드 이식 (common/) ──────────────────────────────────────────────
# 아래 4개는 Java 구현의 이식이다. 이식 충실도는 --self-check가 생산 DB의 job 행
# (normalized_hash / template_hash)과 대조해 검증한다.

# Java의 `\s`는 기본 플래그에서 ASCII 공백만 매칭한다 ([ \t\n\x0B\f\r]) — Python의 `\s`와 달리
# U+00A0(비분리 공백)을 잡지 않는다. 코퍼스에는 U+00A0가 2,191회(문장 2.8%) 있으므로 Python 기본
# 의미론을 쓰면 생산 코드와 다른 키가 나온다. 시뮬레이션은 실제 시스템을 기술해야 하므로 Java를 따른다.
# (이 차이가 히트율에 미치는 영향은 전수 0건으로 측정됐다 — docs/benchmarks.md. 그래서 생산 코드는
#  고치지 않는다: 키를 전면 무효화할 만한 수혜가 없다.)
JAVA_WHITESPACE = re.compile(r"[ \t\n\x0b\f\r]+")


def normalized_hash(text):
    """TextHash.normalizedHash — NFC 정규화 + 공백 제거 + SHA-256."""
    t = JAVA_WHITESPACE.sub("", unicodedata.normalize("NFC", text))
    return hashlib.sha256(t.encode()).hexdigest()


def link(mention, current_year, context, inv, ids):
    """FileKnowledgeSource.link — 역색인 → 활동시기 → 관직-문맥. (stage, candidate_ids)."""
    cands = inv.get(mention)
    if not cands:
        return "MISS", []
    if len(cands) == 1:
        return "SINGLE", cands

    time_filtered = [i for i in cands
                     if ids.get(i) is not None and (ids[i].get("활동_시작") or 0) <= current_year]
    if len(time_filtered) == 1:
        return "TIME", time_filtered

    job_filtered = []
    for i in time_filtered:
        for job in (ids[i].get("관직_리스트") or []):
            hangul = PAREN.sub("", job).strip()
            m = HANJA_IN_PAREN.search(job)
            if (hangul and hangul in context) or (m and m.group(1).strip() and m.group(1) in context):
                job_filtered.append(i)
                break
    if len(job_filtered) == 1:
        return "OFFICE", job_filtered

    remaining = job_filtered or time_filtered[:MAX_AMBIGUOUS]
    return "AMBIGUOUS", list(remaining)


def slot_source(source_text, entities):
    """TemplateSlotter.slotSource — 링크 확정 PER만 슬롯화. (template, slots) 또는 None."""
    confirmed = {}
    for e in entities:
        if (e["type"] == "PER" and e.get("kb_id") and e.get("resolved_name")
                and e["surface"] in source_text and e["surface"] not in confirmed):
            confirmed[e["surface"]] = e["resolved_name"]
    if not confirmed:
        return None

    by_position = sorted(confirmed, key=source_text.index)        # 등장 위치 순 번호
    slots = [{"marker": f"⟪PER{i + 1}⟫", "surface": s, "resolved_name": confirmed[s]}
             for i, s in enumerate(by_position)]
    marker_of = {s["surface"]: s["marker"] for s in slots}

    template = source_text
    for surface in sorted(confirmed, key=len, reverse=True):      # 긴 표면형부터 (부분 문자열 오염 방지)
        template = template.replace(surface, marker_of[surface])
    return template, slots


def office_hanja_set(ids, min_len=2):
    """KB의 관직_리스트에서 한자 관직명을 모은다 — "선공참봉(繕工參奉)" → 繕工參奉.

    NER 모델에는 POS(관직) 클래스가 없다(라벨은 PER/LOC/DAT/POH). 계획서 §5.2와 ADR-009는
    POS 슬롯화를 확장 후보로 적어뒀지만 그 전제가 없으므로, 상한을 재려면 사전 마스킹을 쓴다.
    """
    out = set()
    for p in ids.values():
        for job in (p.get("관직_리스트") or []):
            m = HANJA_IN_PAREN.search(job)
            if m and len(m.group(1).strip()) >= min_len:
                out.add(m.group(1).strip())
    return out


def slot_source_extended(source_text, entities, use_loc=False, offices=None):
    """참고용 상한: 확정 PER에 LOC·관직(사전)까지 슬롯화한 틀.

    ADR-009가 v1에서 배제한 확장이다 — 재주입할 한국어 지명·관직명의 정본이 없어(같은 관직도
    문맥별 역어가 갈린다) **실현 불가**다. 히트율 상한을 재두면 ADR-009의 재검토 조건
    ("역어 사전이 생기면 재평가")에 숫자가 붙는다. 달성 가능한 히트율이 아니라 기회의 크기다.
    """
    spans = {}
    for e in entities:
        if e["surface"] not in source_text:
            continue
        if e["type"] == "PER" and e.get("kb_id") and e.get("resolved_name"):
            spans.setdefault(e["surface"], "PER")
        elif use_loc and e["type"] == "LOC":
            spans.setdefault(e["surface"], "LOC")
    if offices:
        for office in offices:
            if office in source_text:
                spans.setdefault(office, "POS")
    if not spans:
        return None
    by_position = sorted(spans, key=source_text.index)
    marker_of = {sfc: f"⟪{spans[sfc]}{i + 1}⟫" for i, sfc in enumerate(by_position)}
    template = source_text
    for surface in sorted(spans, key=len, reverse=True):
        template = template.replace(surface, marker_of[surface])
    return template


def templateize(translation, slots):
    """TemplateSlotter.templateizeTranslation — 모든 슬롯의 전체형이 있어야 성립 (보수적 적재)."""
    if not translation or not translation.strip():
        return None
    out = translation
    for s in sorted(slots, key=lambda s: len(s["resolved_name"]), reverse=True):
        if s["resolved_name"] not in out:
            return None
        out = out.replace(s["resolved_name"], s["marker"])
    return out


def grade(translation, entities):
    """QualityGate.grade — 확정 인명이 번역문에 반영됐는지. VERIFIED/DEGRADED/REJECTED."""
    if not translation or not translation.strip():
        return "REJECTED"
    missing, kb_miss = False, False
    for e in entities:
        if e.get("kb_id") and e.get("resolved_name"):
            name = e["resolved_name"]
            reflected = name in translation
            if not reflected and len(name) >= 3:                  # 이름부(2자 이상) 허용
                given = name[1:]
                reflected = len(given) >= 2 and given in translation
            if not reflected:
                missing = True
        elif e.get("link_stage") == "MISS":
            kb_miss = True
    if missing:
        return "REJECTED"
    return "DEGRADED" if kb_miss else "VERIFIED"


def name_reflected(name, translation):
    """QualityGate.nameReflected — 전체 이름 또는 이름부(2자 이상)."""
    if name in translation:
        return True
    given = name[1:] if len(name) >= 3 else ""
    return len(given) >= 2 and given in translation


def detect_misalignment(items, corpus_rows, window=2):
    """원천 코퍼스의 원문↔번역 정렬 밀림 검출.

    확정 인명이 자기 번역에는 없는데 **이웃 행의 번역에는 전부 있으면** 짝이 밀린 것이다.
    `malmoi/Merged_Corpus_Final.json`은 크롤링 산출물이고 이 결함이 실재한다 (A01 표본에서
    19.5%p). 게이트 등급을 전문가 번역으로 판정하는 이 시뮬레이션에서 밀린 짝은 전부 REJECTED로
    오판되므로, 검출해서 적재 판정에서 보정한다. 히트율의 키 계산에는 영향이 없다(원문만 쓴다).

    반환: 밀린 것으로 판정된 인덱스 집합.
    """
    trans = [r["translation"] or "" for r in corpus_rows]
    bad = set()
    for i, it in enumerate(items):
        names = [e["resolved_name"] for e in it["entities"]
                 if e.get("kb_id") and e.get("resolved_name")]
        if not names:
            continue
        missing = [n for n in names if not name_reflected(n, trans[i])]
        if not missing:
            continue
        for off in range(-window, window + 1):
            j = i + off
            if off == 0 or not (0 <= j < len(trans)):
                continue
            if all(name_reflected(n, trans[j]) for n in missing):
                bad.add(i)
                break
    return bad


def reign_year_to_ad(doc_id):
    """BatchController.reignYearToAd — SJW-A01… → 1623."""
    try:
        if doc_id[4] == "A":
            return 1623 + int(doc_id[5:7]) - 1
    except (IndexError, ValueError):
        pass
    return None


# ── NER (라이브 서버 + 디스크 캐시) ────────────────────────────────────────

def ner_one(text, url):
    req = urllib.request.Request(f"{url}/v1/ner",
                                 data=json.dumps({"text": text[:MAX_TEXT]}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["entities"]


def ner_all(sentences, url, cache_path, workers):
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
        print(f"  NER 캐시 {len(cache)}건 재사용: {cache_path.name}")
    todo = [s for s in sentences if normalized_hash(s) not in cache]
    if todo:
        print(f"  NER 추론 {len(todo)}건 (병렬 {workers})…", end="", flush=True)
        t0 = time.perf_counter()
        with cf.ThreadPoolExecutor(workers) as ex:
            for s, ents in zip(todo, ex.map(lambda x: ner_one(x, url), todo)):
                cache[normalized_hash(s)] = ents
        print(f" {time.perf_counter() - t0:.0f}초")
        cache_path.write_text(json.dumps(cache, ensure_ascii=False))
    return cache


# ── 시뮬레이션 ───────────────────────────────────────────────────────────

def replay(items, l1_on, l2_on, grade_key="grade", pairs_out=None):
    """생산 코드 순서대로 재생: L1 조회 → L2 조회 → 미스면 LLM 1회 + 적재.

    pairs_out가 주어지면 L2 히트마다 (틀을 적재한 문장 인덱스, 히트한 문장 인덱스)를 append한다 —
    S5 비열등 판정의 표본 풀이다.
    """
    l1_store = set()
    l2_store = {}          # template_hash → 그 틀을 적재한 item 인덱스
    stat = collections.Counter()
    for idx, it in enumerate(items):
        if l1_on and it["l1_key"] in l1_store:
            stat["hit_l1"] += 1
            continue
        if l2_on and it["template_hash"] and it["template_hash"] in l2_store:
            stat["hit_l2"] += 1
            if pairs_out is not None:
                pairs_out.append((l2_store[it["template_hash"]], idx))
            continue
        stat["llm_calls"] += 1
        g = it[grade_key]
        stat[f"grade_{g.lower()}"] += 1
        if l1_on and g != "REJECTED":
            l1_store.add(it["l1_key"])
        if l2_on and g == "VERIFIED" and it["translation_template"]:
            l2_store.setdefault(it["template_hash"], idx)
    return stat


def pct(n, d):
    return f"{100.0 * n / d:.2f}%" if d else "—"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="A01", help="A01 | A01,A03 | all")
    ap.add_argument("--corpus", default=str(ROOT / "malmoi" / "Merged_Corpus_Final.json"))
    ap.add_argument("--kb", default=str(ROOT / "kb"))
    ap.add_argument("--kb-name", default="injo")
    ap.add_argument("--ner-url", default=os.environ.get("NER_URL", "http://localhost:8100"))
    ap.add_argument("--ner-cache", default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dump-l2-pairs", default=None,
                    help="L2 히트 쌍(틀 적재 문장, 히트 문장)을 JSON으로 저장 — S5 표본 풀")
    ap.add_argument("--self-check", action="store_true",
                    help="생산 DB의 job 행과 해시를 대조해 이식 충실도를 검증한다")
    args = ap.parse_args()

    corpus = json.load(open(args.corpus))["corpus"]
    if args.years == "all":
        items_raw = corpus
        label = f"인조 전체 {len(set(i['date'][:3] for i in corpus))}년치"
    else:
        years = set(args.years.split(","))
        items_raw = [i for i in corpus if i["date"][:3] in years]
        label = args.years
    if not items_raw:
        sys.exit(f"해당 슬라이스가 비어 있다: {args.years}")

    inv = json.load(open(Path(args.kb) / f"inverted_index_{args.kb_name}.json"))
    ids = json.load(open(Path(args.kb) / f"id_lookup_{args.kb_name}.json"))
    offices = office_hanja_set(ids)

    print(f"\n{'=' * 72}\nM3-S4 캐시 히트율 시뮬레이션 — {label}  (LLM 호출 0회)\n{'=' * 72}")
    print(f"코퍼스: {len(items_raw)}건, KB: {args.kb_name} (인물 {len(ids)}명 / 역색인 {len(inv)}키)")

    oversize = [i for i in items_raw if len(i["original"]) > MAX_TEXT]
    items_raw = [i for i in items_raw if len(i["original"]) <= MAX_TEXT]
    if oversize:
        print(f"  API 상한(2,000자) 초과 {len(oversize)}건 제외 — 분할 입력이 필요한 문장")

    cache_path = Path(args.ner_cache) if args.ner_cache else \
        ROOT / "eval" / f".ner_cache_{args.years.replace(',', '_')}.json"
    ner_cache = ner_all([i["original"] for i in items_raw], args.ner_url, cache_path, args.workers)

    # 파이프라인 전처리 (NER → 링킹 → 슬롯화 → 등급)
    items, stage_count = [], collections.Counter()
    for it in items_raw:
        text = it["original"]
        year = reign_year_to_ad(it["id"]) or 10 ** 9
        entities = []
        for e in ner_cache[normalized_hash(text)]:
            if e["type"] != "PER":
                entities.append({"surface": e["surface"], "type": e["type"],
                                 "kb_id": None, "resolved_name": None, "link_stage": None})
                continue
            stage, cands = link(e["surface"], year, text, inv, ids)
            stage_count[stage] += 1
            kb_id = cands[0] if stage in ("SINGLE", "TIME", "OFFICE") else None
            entities.append({"surface": e["surface"], "type": "PER", "kb_id": kb_id,
                             "resolved_name": ids[kb_id]["한글_명"] if kb_id else None,
                             "link_stage": stage})
        slotted = slot_source(text, entities)
        tpl_hash = normalized_hash(slotted[0]) if slotted else None
        tpl_trans = templateize(it["translation"], slotted[1]) if slotted else None
        ext_loc = slot_source_extended(text, entities, use_loc=True)
        ext_all = slot_source_extended(text, entities, use_loc=True, offices=offices)
        items.append({"id": it["id"], "l1_key": normalized_hash(text),
                      "template_hash": tpl_hash, "translation_template": tpl_trans,
                      "ext_loc_hash": normalized_hash(ext_loc) if ext_loc else None,
                      "ext_all_hash": normalized_hash(ext_all) if ext_all else None,
                      "grade": grade(it["translation"], entities), "entities": entities})

    n = len(items)
    with_slot = sum(1 for i in items if i["template_hash"])
    print(f"\n── 파이프라인 전처리 ──")
    print(f"  PER 링킹 단계 분포: {dict(stage_count.most_common())}")
    print(f"  L2 대상 문장(링크 확정 PER ≥1): {with_slot} / {n}  ({pct(with_slot, n)})")
    print(f"  게이트 등급(전문가 번역 기준): "
          f"{dict(collections.Counter(i['grade'] for i in items).most_common())}")
    tpl_ok = sum(1 for i in items if i["translation_template"])
    print(f"  L2 템플릿화 성립(전체형 출현): {tpl_ok} / {with_slot}  ({pct(tpl_ok, with_slot)})")

    # 구조적 상한: 키 중복만 본다 (적재 정책·프록시와 무관하게 정확)
    l1_dup = n - len(set(i["l1_key"] for i in items))
    l2_keys = [i["template_hash"] for i in items if i["template_hash"]]
    l2_dup = len(l2_keys) - len(set(l2_keys))
    print(f"\n── 구조적 상한 (키 중복만 — 적재 정책 미반영, 프록시 무관) ──")
    print(f"  L1 반복 문장: {l1_dup} / {n}  ({pct(l1_dup, n)})")
    print(f"  L2 반복 틀  : {l2_dup} / {len(l2_keys)} (L2 대상 중)  ({pct(l2_dup, len(l2_keys))})"
          f"  = 전체의 {pct(l2_dup, n)}")

    if l2_dup:
        seen, dups = {}, []
        for i in items:
            k = i["template_hash"]
            if not k:
                continue
            if k in seen:
                dups.append((seen[k], i))
            else:
                seen[k] = i
        by_id = {i["id"]: i for i in items_raw}
        print(f"  반복된 틀 실물 (최대 5쌍):")
        for first, again in dups[:5]:
            print(f"    · {by_id[first['id']]['original'][:46]}")
            print(f"      {by_id[again['id']]['original'][:46]}")

    # 배치 규모별 히트율 — 비용 모델 외삽의 방향을 정하는 수치.
    # 캐시는 "이미 본 문장"만 맞출 수 있으므로 히트율은 배치 범위에 종속된다. 잔여 코퍼스
    # (약 120만 문장)로 외삽하려면 규모가 커질 때 오르는지 내리는지를 알아야 한다.
    if len(items) > 5000:
        print(f"\n── 배치 규모별 L1/L2 히트율 (문서 순서 누적) ──")
        print(f"  {'문장 수':>10}{'L1 반복':>10}{'L1 %':>8}{'L2 반복':>10}{'L2 %(전체)':>12}")
        for size in (1000, 2000, 5000, 10000, 20000, 40000, len(items)):
            if size > len(items):
                continue
            sub = items[:size]
            l1 = size - len(set(i["l1_key"] for i in sub))
            tks = [i["template_hash"] for i in sub if i["template_hash"]]
            l2 = len(tks) - len(set(tks))
            print(f"  {size:>10,}{l1:>10,}{pct(l1, size):>8}{l2:>10,}{pct(l2, size):>12}")

    print(f"\n── 참고: 슬롯 범위 확장 상한 (ADR-009가 배제 — 역어 정본이 없어 실현 불가) ──")
    print(f"  {'슬롯 범위':<26}{'반복 틀':>10}{'대상 문장':>10}{'대상 중':>10}{'전체 중':>10}")
    print(f"  {'PER만 (현행 v1)':<26}{l2_dup:>10,}{len(l2_keys):>10,}"
          f"{pct(l2_dup, len(l2_keys)):>10}{pct(l2_dup, n):>10}")
    for label, key in (("PER+LOC", "ext_loc_hash"), ("PER+LOC+관직(KB 사전)", "ext_all_hash")):
        ks = [i[key] for i in items if i[key]]
        dup = len(ks) - len(set(ks))
        print(f"  {label:<26}{dup:>10,}{len(ks):>10,}{pct(dup, len(ks)):>10}{pct(dup, n):>10}")
    print(f"  NER 모델에 POS 클래스가 없다 (라벨 PER/LOC/DAT/POH) — 관직은 KB 사전으로 마스킹했다.")

    # 원천 코퍼스 정렬 결함 보정 — 밀린 짝은 REJECTED로 오판되어 적재율을 과소평가한다
    misaligned = detect_misalignment(items, items_raw)
    for idx, it in enumerate(items):
        g = it["grade"]
        if idx in misaligned and g == "REJECTED":
            # 누락 신호가 데이터 결함에서 온 것이므로 제거하고 나머지 축(KB_MISS)으로만 판정
            g = "DEGRADED" if any(e.get("link_stage") == "MISS" for e in it["entities"]) else "VERIFIED"
        it["grade_aligned"] = g
    n_conf = sum(1 for i in items
                 if any(e.get("kb_id") for e in i["entities"]))
    print(f"\n── 원천 코퍼스 정렬 결함 (게이트가 검출) ──")
    print(f"  확정 PER 보유 문장 {n_conf}건 중 짝이 밀린 것으로 판정: {len(misaligned)}건"
          f"  ({pct(len(misaligned), n_conf)})")
    print(f"  보정 후 등급: "
          f"{dict(collections.Counter(i['grade_aligned'] for i in items).most_common())}")
    print(f"  → 밀림 제외 게이트 오탐률: "
          f"{pct(sum(1 for i in items if i['grade_aligned'] == 'REJECTED'), n_conf)}"
          f"  (골든셋 실측 3.9%와 대조)")

    print(f"\n── 순차 재생 (적재 정책 반영 = 실효 히트율) ──")
    print(f"{'구성':<14}{'LLM 호출':>10}{'L1 히트':>10}{'L2 히트':>10}{'히트율':>10}{'호출 절감':>10}")
    base = None
    rows = {}
    for name, l1, l2 in (("캐시 off", False, False), ("L1만", True, False), ("L1+L2", True, True)):
        s = replay(items, l1, l2, "grade_aligned")
        calls = s["llm_calls"]
        base = base or calls
        hits = s["hit_l1"] + s["hit_l2"]
        rows[name] = s
        print(f"{name:<14}{calls:>10,}{s['hit_l1']:>10,}{s['hit_l2']:>10,}"
              f"{pct(hits, n):>10}{pct(base - calls, base):>10}")
    raw = replay(items, True, True, "grade")
    print(f"  (정렬 보정 없이 원천 등급 그대로: L1+L2 히트 "
          f"{raw['hit_l1'] + raw['hit_l2']:,} = {pct(raw['hit_l1'] + raw['hit_l2'], n)} — "
          f"밀린 짝이 적재를 막아 과소평가된다)")

    # 비용 환산 (docs/cost-model.md 파라미터)
    TOK_IN, TOK_OUT = 820, 266          # 실측 E2E usage (입력 820 = 오버헤드 700 + 원문 ~120)
    P_IN, P_OUT, KRW = 0.30, 2.50, 1400  # Flash 공시가 $/1Mtok, 환율
    print(f"\n── 비용 환산 (Flash 공시가 counterfactual, 호출당 in {TOK_IN} / out {TOK_OUT} tok) ──")
    print(f"{'구성':<14}{'LLM 호출':>10}{'비용(원)':>12}{'문장당(원)':>12}")
    for name, s in rows.items():
        krw = s["llm_calls"] * (TOK_IN * P_IN + TOK_OUT * P_OUT) / 1e6 * KRW
        print(f"{name:<14}{s['llm_calls']:>10,}{krw:>12,.0f}{krw / n:>12.2f}")

    if args.dump_l2_pairs:
        pairs = []
        replay(items, True, True, "grade_aligned", pairs_out=pairs)
        out = []
        for first_i, hit_i in pairs:
            # 두 문장 모두 정렬이 정상이어야 한다 — reference를 정답으로 쓰는 chrF 판정의 전제
            if first_i in misaligned or hit_i in misaligned:
                continue
            out.append({
                "first": {"id": items_raw[first_i]["id"],
                          "original": items_raw[first_i]["original"],
                          "reference": items_raw[first_i]["translation"]},
                "hit": {"id": items_raw[hit_i]["id"],
                        "original": items_raw[hit_i]["original"],
                        "reference": items_raw[hit_i]["translation"]},
                "template_hash": items[hit_i]["template_hash"],
                "slots": len([e for e in items[hit_i]["entities"] if e.get("kb_id")]),
            })
        Path(args.dump_l2_pairs).write_text(
            json.dumps({"pairs": out, "total_hits": len(pairs),
                        "dropped_misaligned": len(pairs) - len(out)},
                       ensure_ascii=False, indent=1))
        print(f"\n── L2 히트 쌍 덤프 ──")
        print(f"  히트 {len(pairs)}건 중 정렬 정상 {len(out)}건 저장 → {args.dump_l2_pairs}")
        print(f"  (정렬 결함으로 제외 {len(pairs) - len(out)}건 — reference를 정답으로 쓸 수 없다)")

    if args.self_check:
        self_check(items_raw, inv, ids, ner_cache)
    print()


def self_check(items_raw, inv, ids, ner_cache):
    """생산 DB의 job 행과 해시를 대조 — 이 스크립트의 이식이 Java와 같은 값을 내는지."""
    import subprocess
    print(f"\n── 이식 충실도 자기검증 (생산 DB의 job 행과 대조) ──")
    # 원문에 개행이 있어 구분자 파싱이 깨진다 — JSON으로 받는다
    sql = ("SELECT coalesce(json_agg(json_build_object('t', source_text, 'y', doc_year, "
           "'l1', normalized_hash, 'l2', template_hash))::text, '[]') FROM ("
           "SELECT source_text, doc_year, normalized_hash, template_hash FROM translation_job "
           "WHERE normalized_hash IS NOT NULL ORDER BY created_at DESC LIMIT 200) s")
    try:
        out = subprocess.run(["docker", "exec", "sjw-postgres", "psql", "-U", "sjw", "-d", "sjw",
                              "-t", "-A", "-c", sql],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        rows = json.loads(out.strip())
    except Exception as e:
        print(f"  건너뜀 (DB 접근 불가: {e})")
        return
    ok = l1_bad = l2_bad = l2_checked = 0
    for r in rows:
        text, year, want_l1, want_l2 = r["t"], r["y"], r["l1"], r["l2"] or ""
        if normalized_hash(text) != want_l1:
            l1_bad += 1
            continue
        if want_l2:
            ents = ner_cache.get(normalized_hash(text))
            if ents is None:
                ents = ner_one(text, "http://localhost:8100")
                ner_cache[normalized_hash(text)] = ents
            entities = []
            for e in ents:
                if e["type"] != "PER":
                    continue
                stage, cands = link(e["surface"], year if year else 10 ** 9, text, inv, ids)
                kb_id = cands[0] if stage in ("SINGLE", "TIME", "OFFICE") else None
                entities.append({"surface": e["surface"], "type": "PER", "kb_id": kb_id,
                                 "resolved_name": ids[kb_id]["한글_명"] if kb_id else None,
                                 "link_stage": stage})
            slotted = slot_source(text, entities)
            got = normalized_hash(slotted[0]) if slotted else ""
            l2_checked += 1
            if got != want_l2:
                l2_bad += 1
                print(f"  !! template_hash 불일치: {text[:40]} 기대 {want_l2[:12]} 실제 {got[:12]}")
        ok += 1
    print(f"  normalized_hash: {ok}건 일치, {l1_bad}건 불일치")
    print(f"  template_hash  : {l2_checked}건 검증, {l2_bad}건 불일치")
    if l1_bad or l2_bad:
        sys.exit("이식이 생산 코드와 다른 값을 낸다 — 시뮬레이션 수치를 신뢰할 수 없다")
    print("  ✅ 생산 코드와 동일한 키를 생성한다")


if __name__ == "__main__":
    main()
