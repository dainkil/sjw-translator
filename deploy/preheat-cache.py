#!/usr/bin/env python3
"""공개 데모용 캐시 예열 — 골든셋 앞쪽 몇 문장을 미리 번역해 L1에 적재한다.

왜 필요한가 (§5.0 Step 4): 공개 모드에서 캐시 미스는 403 BYOK_REQUIRED다. 평가자에게
"본인 Gemini 키를 발급받아 오세요"를 요구하는 순간 이탈한다. 반면 **캐시 히트는
X-Api-Key만으로 200을 돌려준다** — BYOK 검사가 캐시 조회 뒤에 있기 때문이다(ADR-020 개정).
따라서 몇 문장만 미리 채워 두면 키 하나로 진짜 응답을 보여줄 수 있다.

비용: 예열 1문장 = LLM 1회. 무료 RPD가 20이므로 기본값을 5로 두고, 넘길 때 경고한다.
L1 TTL은 30일이라 한 번 채우면 시연 기간 내내 유지된다 (CACHE_EPOCH를 올리면 무효화).

사용:
  # 무엇을 호출할지 먼저 확인 (네트워크 호출 없음)
  deploy/preheat-cache.py --base-url https://skala-gj4-sjw.skala-gj.com \\
      --api-key sjw_... --llm-key <본인 Gemini 키> --dry-run

  # 실제 예열
  deploy/preheat-cache.py --base-url https://skala-gj4-sjw.skala-gj.com \\
      --api-key sjw_... --llm-key <본인 Gemini 키> --count 5

  # 예열 결과 확인 — 키 하나로 전부 히트여야 한다 (LLM 0회)
  deploy/preheat-cache.py --base-url ... --api-key sjw_... --verify
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

SYNC_PATH = "/api/v1/translations/sync"
DEFAULT_CORPUS = "eval/eval300_1925.json"
FREE_RPD = 20  # gemini-3.5-flash 무료 일일 상한 실측 (ADR-016)


def reign_year(doc_id):
    """문서 id → 서기 연도. eval/score_l2.py와 같은 규칙."""
    try:
        if doc_id[4] == "A":
            return 1623 + int(doc_id[5:7]) - 1
    except (IndexError, ValueError):
        pass
    return None


def load_corpus(path, offset, count):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    corpus = data["corpus"][offset:offset + count]
    if not corpus:
        sys.exit(f"코퍼스에서 뽑을 문장이 없다 (offset={offset}, n={data.get('n')})")
    return corpus


def post_sync(base_url, text, year, api_key, llm_key, timeout):
    body = {"text": text}
    if year is not None:
        body["year"] = year
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    if llm_key:
        headers["X-Llm-Key"] = llm_key
    req = urllib.request.Request(base_url.rstrip("/") + SYNC_PATH,
                                 data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r), None
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, None, (e.headers.get("Retry-After"), raw[:200])


def main():
    p = argparse.ArgumentParser(description="공개 데모용 L1 캐시 예열")
    p.add_argument("--base-url", required=True, help="예: https://skala-gj4-sjw.skala-gj.com")
    p.add_argument("--api-key", required=True, help="발급받은 테넌트 키 (X-Api-Key)")
    p.add_argument("--llm-key", help="본인 Gemini 키 (X-Llm-Key). --verify에는 넣지 않는다")
    p.add_argument("--corpus", default=DEFAULT_CORPUS)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--count", type=int, default=5, help="예열할 문장 수 (기본 5 — 무료 RPD 20)")
    p.add_argument("--delay", type=float, default=4.5, help="호출 간 간격 초 (분당 RPM 보호)")
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--dry-run", action="store_true", help="호출 없이 대상만 출력")
    p.add_argument("--verify", action="store_true",
                   help="예열 확인 모드 — BYOK 없이 호출해 전부 캐시 히트인지 본다")
    a = p.parse_args()

    if a.verify:
        a.llm_key = None  # 히트가 아니면 403이 나야 검증이 성립한다
    elif not a.llm_key and not a.dry_run:
        sys.exit("예열에는 --llm-key가 필요하다 (캐시 미스는 본인 키로 번역된다). "
                 "확인만 하려면 --verify.")

    corpus = load_corpus(a.corpus, a.offset, a.count)

    if not a.verify and a.count > FREE_RPD // 2:
        print(f"! {a.count}문장을 예열하면 LLM을 {a.count}회 부른다. "
              f"무료 일일 상한은 {FREE_RPD}회다 (ADR-016).", file=sys.stderr)

    mode = "확인" if a.verify else "예열"
    print(f"{mode}: {len(corpus)}문장 (offset {a.offset}) → {a.base_url}")
    if a.dry_run:
        for i, it in enumerate(corpus, 1):
            print(f"  {i:2d}. [{it['id']}] {reign_year(it['id'])} {it['original'][:40]}")
        print("(dry-run — 호출하지 않았다)")
        return 0

    hit = miss = failed = 0
    for i, it in enumerate(corpus, 1):
        if i > 1:
            time.sleep(a.delay)
        year = reign_year(it["id"])
        code, body, err = post_sync(a.base_url, it["original"], year,
                                    a.api_key, a.llm_key, a.timeout)
        tag = f"  {i:2d}. [{it['id']}]"
        if code == 200:
            cache = (body.get("meta") or {}).get("cacheHit")
            if cache:
                hit += 1
                print(f"{tag} HIT {cache}")
            else:
                miss += 1
                print(f"{tag} MISS → 번역 적재 (LLM 1회)")
        elif code == 429:
            retry = err[0] if err else "?"
            print(f"{tag} 429 — rate/quota 소진. Retry-After: {retry}s. 중단한다.")
            failed += 1
            break
        elif code == 403:
            print(f"{tag} 403 — 캐시에 없고 BYOK 키가 없다. "
                  f"{'(확인 모드에서는 아직 예열되지 않았다는 뜻)' if a.verify else ''}")
            failed += 1
        elif code == 401:
            sys.exit(f"{tag} 401 — X-Api-Key가 없거나 등록되지 않았다. issue-key.sh로 발급한다.")
        else:
            print(f"{tag} HTTP {code} {err[1] if err else ''}")
            failed += 1

    print(f"\n합계: 히트 {hit} / 미스(적재) {miss} / 실패 {failed}")
    if a.verify:
        print("확인 모드 기준: 실패 0 · 미스 0 이어야 키 하나로 시연 가능하다.")
        return 0 if (failed == 0 and miss == 0) else 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
