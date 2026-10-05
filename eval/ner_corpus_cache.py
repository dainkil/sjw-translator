#!/usr/bin/env python3
"""병렬 코퍼스 전수의 NER 결과를 캐시한다 — kb_mining.py · simulate_routing.py의 입력. LLM 호출 0회.

`simulate_cache.ner_all`과 같은 캐시 형식(`normalized_hash(원문) → entities`, `eval/.ner_cache_all.json`, gitignore)
이지만 2,000건마다 중간 저장해서 끊겨도 이어서 돈다. 고유 원문 52,790건, 로컬 ONNX 서버 8병렬 약 80분(2026-10-04).
원문이 2,000자를 넘는 행(420행)은 API 상한대로 앞 2,000자만 추론한다(simulate_cache.ner_one).

사용법:
  docker compose -f deploy/docker-compose.yml up -d ner
  python3 eval/ner_corpus_cache.py [--workers 8]
"""
import argparse
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from leakage import default_corpus  # noqa: E402
from simulate_cache import ner_one, normalized_hash  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, default=default_corpus())
    ap.add_argument("--cache", type=Path, default=HERE / ".ner_cache_all.json")
    ap.add_argument("--url", default="http://localhost:8100")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--save-every", type=int, default=2000)
    args = ap.parse_args()

    cache = json.loads(args.cache.read_text()) if args.cache.exists() else {}
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))["corpus"]
    todo = list(dict.fromkeys(r["original"] for r in corpus if normalized_hash(r["original"]) not in cache))
    print(f"캐시 {len(cache)} / 남은 원문 {len(todo)}", flush=True)

    def one(text):
        for attempt in range(4):
            try:
                return text, ner_one(text, args.url)
            except Exception:
                time.sleep(2 ** attempt)
        return text, None

    t0, failed = time.time(), 0
    with cf.ThreadPoolExecutor(args.workers) as ex:
        for n, (text, ents) in enumerate(ex.map(one, todo), 1):
            if ents is None:
                failed += 1
            else:
                cache[normalized_hash(text)] = ents
            if n % args.save_every == 0:
                args.cache.write_text(json.dumps(cache, ensure_ascii=False))
                print(f"  {n}/{len(todo)} {time.time() - t0:.0f}s", flush=True)
    args.cache.write_text(json.dumps(cache, ensure_ascii=False))
    print(f"완료: 캐시 {len(cache)}건, 실패 {failed}, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
