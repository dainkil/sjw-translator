#!/usr/bin/env python3
"""병렬 코퍼스를 검색 인덱스(Step 4 동적 few-shot)나 KB 마이닝(Step 5)에 쓸 때의 누출 방지 규칙. LLM 호출 0회.

코퍼스(`Merged_Corpus_Final.json`, 인조 연간 62,476쌍)는 골든셋 300문장의 국역(=정답)을 그대로 품고 있다.
아무 규칙 없이 쓰면 검색이 정답 국역을 예시로 가져오고, 마이닝이 정답 표기를 KB에 넣는다.

규칙 (모두 행 단위 필터 — 조합해서 쓴다):
  1. 평가 문장 자체를 뺀다 (id)
  2. 평가 문장과 **같은 날짜**의 행을 전부 뺀다 — 같은 날 기사는 같은 인물·사안이 이어져 국역에 정답 표기가
     거의 그대로 나온다 (골든셋 300문장 → 같은 날짜 3,695행, 2026-10-04 실측)
  3. **시기 분할**(선택) — 마이닝·인덱스는 재위 연차 `train_years`에서만, 평가는 `test_years` 문장만.
     "번역된 구간에서 배운 것을 새 구간에 쓴다"는 주장을 재려면 이것이 필요하다: 날짜만 빼면 같은 인물이 다른
     날짜에 계속 나오므로(인조 연간 안에서) 미번역 시대로의 전이를 과대평가한다
  4. 행 밀림 의심 행을 뺀다 — KB 한자명이 원문에 있는데 그 독음이 자기 국역에는 없고 이웃 행(±2) 국역에는 있는
     행 (simulate_cache.detect_misalignment와 같은 규칙을 NER 없이 역색인 문자열 매칭으로)

사용법 (라이브러리):
  from leakage import EvalSet, filter_rows, misaligned_rows
  ev = EvalSet.load()                       # eval300 + 그 날짜들
  rows = filter_rows(corpus, ev, train_years=range(1, 21))
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def default_corpus() -> Path:
    for p in (ROOT / "malmoi" / "Merged_Corpus_Final.json", ROOT.parent / "malmoi" / "Merged_Corpus_Final.json"):
        if p.exists():
            return p
    return ROOT / "malmoi" / "Merged_Corpus_Final.json"


def reign_year(doc_id: str):
    """SJW-A09020070-00300 → 9 (인조 재위 연차). 형식이 다르면 None."""
    m = re.match(r"SJW-A(\d{2})", doc_id)
    return int(m.group(1)) if m else None


@dataclass
class EvalSet:
    ids: set
    dates: set = field(default_factory=set)

    @classmethod
    def load(cls, eval_path: Path = HERE / "eval300_1925.json", corpus_rows=None):
        ids = set(json.loads(eval_path.read_text(encoding="utf-8"))["ids"])
        dates = set()
        if corpus_rows is not None:
            dates = {r["date"] for r in corpus_rows if r["id"] in ids}
        return cls(ids=ids, dates=dates)


def filter_rows(corpus_rows, ev: EvalSet, train_years=None, exclude_ids=frozenset()):
    """규칙 1–3(+임의 제외 id)을 적용한 행 목록. ev.dates가 비어 있으면 corpus_rows로 채운다."""
    if not ev.dates:
        ev.dates = {r["date"] for r in corpus_rows if r["id"] in ev.ids}
    years = set(train_years) if train_years is not None else None
    out = []
    for r in corpus_rows:
        if r["id"] in ev.ids or r["date"] in ev.dates or r["id"] in exclude_ids:
            continue
        if years is not None and reign_year(r["id"]) not in years:
            continue
        out.append(r)
    return out


def test_ids(ev: EvalSet, test_years):
    """시기 분할의 평가 쪽 — test_years에 속한 평가 문장 id."""
    years = set(test_years)
    return {i for i in ev.ids if reign_year(i) in years}


def kb_name_keys(inv, ids, min_len=2):
    """원문 문자열 매칭에 쓸 KB 한자명 → 한글명 (동명이인 표기가 갈리는 키는 뺀다; 2자 이상 성명 키만)."""
    out = {}
    for key, pids in inv.items():
        if len(key) < min_len or not re.fullmatch(r"[一-鿿\U00020000-\U0003134f]+", key):
            continue
        names = {ids[p]["한글_명"] for p in pids if p in ids}
        if len(names) == 1:
            name = next(iter(names))
            if len(name) == len(key):          # 성 포함 전체 표기 키만 (성 없는 키는 오링킹 위험 — name_gold 실측)
                out[key] = name
    return out


def misaligned_rows(corpus_rows, name_keys, window=2, max_key_len=4):
    """규칙 4 — 밀림 의심 행 id 집합. 원문에서 KB 성명 키를 찾고, 그 독음이 자기 국역에 없는데 이웃 국역에 전부 있으면."""
    trans = [r.get("translation") or "" for r in corpus_rows]
    lens = sorted({len(k) for k in name_keys if len(k) <= max_key_len}, reverse=True)
    bad = set()
    for i, r in enumerate(corpus_rows):
        text = r.get("original") or ""
        found = set()
        for L in lens:
            for j in range(len(text) - L + 1):
                k = text[j:j + L]
                if k in name_keys:
                    found.add(name_keys[k])
        if not found:
            continue
        missing = [n for n in found if n not in trans[i]]
        if not missing:
            continue
        for off in range(-window, window + 1):
            j = i + off
            if off and 0 <= j < len(trans) and all(n in trans[j] for n in missing):
                bad.add(r["id"])
                break
    return bad


def assert_no_leak(rows, ev: EvalSet):
    """인덱스·마이닝 입력에 평가 문장이나 같은 날짜 행이 남아 있으면 실패 — Step 4·5 스크립트의 마지막 관문."""
    leaked = [r["id"] for r in rows if r["id"] in ev.ids or r["date"] in ev.dates]
    if leaked:
        raise AssertionError(f"누출: 평가 문장·같은 날짜 행 {len(leaked)}건 (예: {leaked[:3]})")


if __name__ == "__main__":
    corpus = json.loads(default_corpus().read_text(encoding="utf-8"))["corpus"]
    ev = EvalSet.load(corpus_rows=corpus)
    inv = json.loads((ROOT / "kb" / "inverted_index_injo.json").read_text(encoding="utf-8"))
    ids = json.loads((ROOT / "kb" / "id_lookup_injo.json").read_text(encoding="utf-8"))
    keys = kb_name_keys(inv, ids)
    bad = misaligned_rows(corpus, keys)
    base = filter_rows(corpus, ev)
    split = filter_rows(corpus, ev, train_years=range(1, 21))
    print(f"코퍼스 {len(corpus):,}행 / 평가 {len(ev.ids)}문장, 같은 날짜 {len(ev.dates)}일")
    print(f"  규칙 1–2 적용 후 {len(base):,}행 (−{len(corpus) - len(base):,})")
    print(f"  + 시기 분할 학습 1–20년 {len(split):,}행 / 평가 21–27년 문장 {len(test_ids(ev, range(21, 28)))}")
    print(f"  밀림 의심 {len(bad):,}행 (KB 성명 키 {len(keys):,}개 기준) → 규칙 1–2 후 남는 행 중 "
          f"{sum(1 for r in base if r['id'] in bad):,}")
