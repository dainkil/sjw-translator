#!/usr/bin/env python3
"""골든셋 문장의 국편 인명 어노테이션(`idx_person`)을 원문 그대로 다시 받는다. LLM 호출 0회.

`ner_groundtruth_300.json`은 이 어노테이션에서 만들었지만 한글 표기를 KB에서 붙였고, 연구 보고서의 527건과 달리
383건만 남아 있다(걸러 낸 스크립트는 레포에 없다). 여기서는 **한자 표면형만** 받아 KB와 무관한 원자료로 둔다.
한글 표기는 `name_gold.py`가 국역에서 붙인다.

마크업에는 인물 ID가 없다 — `<span class="idx_wrap idx_person tooltip2" title="趙廷虎">趙廷虎</span>` (2026-10-04 확인).
그래서 링킹 정답(인물 ID)은 이 소스로 만들 수 없다.

사용법:
  python3 eval/crawl_idx_person.py            # eval/idx_person_300.json (이어받기 지원)
"""
import argparse
import json
import re
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPAN = re.compile(r'<span[^>]*class="[^"]*idx_person[^"]*"[^>]*>([^<]+)</span>')


def fetch(doc_id, timeout=20, tries=4):
    req = urllib.request.Request(f"https://sjw.history.go.kr/id/{doc_id}", headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                html = r.read().decode("utf-8")
            spans = []
            for s in SPAN.findall(html):
                s = s.strip()
                if s and s not in spans:          # 문장 안 중복 제거, 등장 순서 유지
                    spans.append(s)
            return spans
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, default=HERE / "eval300_1925.json")
    ap.add_argument("--out", type=Path, default=HERE / "idx_person_300.json")
    ap.add_argument("--delay", type=float, default=0.5)
    args = ap.parse_args()

    ids = json.loads(args.corpus.read_text(encoding="utf-8"))["ids"]
    done = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {}
    failed = []
    for i, doc_id in enumerate(ids):
        if doc_id in done:
            continue
        try:
            done[doc_id] = fetch(doc_id)
        except Exception as e:
            failed.append((doc_id, str(e)))
            continue
        if (i + 1) % 25 == 0:
            args.out.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  {i + 1}/{len(ids)}", flush=True)
        time.sleep(args.delay)
    args.out.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(v) for v in done.values())
    print(f"문장 {len(done)} / 인명 있는 문장 {sum(1 for v in done.values() if v)} / 인명 {total} / 실패 {len(failed)}")
    for f in failed:
        print("  실패", *f)


if __name__ == "__main__":
    main()
