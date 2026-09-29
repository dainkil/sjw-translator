#!/usr/bin/env python3
"""공개 데모용 캐시 예열 — 골든셋 앞쪽 몇 문장을 미리 번역해 L1에 적재한다.

왜 필요한가 (§5.0 Step 4): 공개 모드에서 캐시 미스는 403 BYOK_REQUIRED다. 평가자에게
"본인 Gemini 키를 발급받아 오세요"를 요구하는 순간 이탈한다. 반면 **캐시 히트는
X-Api-Key만으로 200을 돌려준다** — BYOK 검사가 캐시 조회 뒤에 있기 때문이다(ADR-020 개정).

**반드시 배치(비동기) 경로로 예열한다.** 동기 경로에 BYOK 키를 주면 번역은 되지만
캐시에 들어가지 않는다 — `TranslationController`가 `byok == null`일 때만 적재하기 때문이다
("요청자가 자기 키로 산 번역을 다른 테넌트가 공짜로 받아가는 모양이 된다"). 반면
워커는 운영자 키로 번역하고 `JobProcessor`가 L1·L2에 적재한다. 그래서 예열에는
`operator_access` 테넌트가 필요하다:

    deploy/k8s/issue-key.sh preheat 50 --operator

비용: 예열 1문장 = LLM 1회. 무료 RPD가 20이므로 기본값을 5로 두고, 넘길 때 경고한다.
L1 TTL은 30일이라 한 번 채우면 시연 기간 내내 유지된다 (CACHE_EPOCH를 올리면 무효화).

사용:
  # 대상 확인 (네트워크 호출 없음)
  deploy/preheat-cache.py --base-url https://skala-gj4-sjw.skala-gj.com \\
      --operator-key sjw_... --dry-run

  # 예열 (배치 생성 → 워커가 번역·적재할 때까지 대기)
  deploy/preheat-cache.py --base-url https://skala-gj4-sjw.skala-gj.com \\
      --operator-key sjw_... --count 5

  # 확인 — 데모 키로 BYOK 없이 전부 히트여야 한다 (LLM 0회)
  deploy/preheat-cache.py --base-url ... --api-key sjw_demo... --verify --count 5
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

SYNC_PATH = "/api/v1/translations/sync"
BATCH_PATH = "/api/v1/batches"
DEFAULT_CORPUS = "eval/eval300_1925.json"
FREE_RPD = 20  # gemini 무료 일일 상한 실측 (ADR-016)
TERMINAL = {"COMPLETED", "FAILED", "PAUSED", "QUOTA_PAUSED", "BUDGET_EXHAUSTED"}


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


def call(url, api_key, body=None, llm_key=None, method=None, timeout=60):
    headers = {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if api_key:
        headers["X-Api-Key"] = api_key
    if llm_key:
        headers["X-Llm-Key"] = llm_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r), None
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, None, (e.headers.get("Retry-After"), raw[:300])
    except urllib.error.URLError as e:
        return 0, None, (None, str(e))


def preheat(a, corpus):
    """배치를 만들고 워커가 다 처리할 때까지 기다린다 — 이 경로만 캐시에 적재된다."""
    base = a.base_url.rstrip("/")
    n = len(corpus)
    code, body, err = call(base + BATCH_PATH, a.operator_key,
                           {"offset": a.offset, "limit": n, "budgetLimitCalls": n})
    if code == 403:
        sys.exit("403 — 이 키에 operator_access가 없다. "
                 "deploy/k8s/issue-key.sh <id> <limit> --operator 로 발급한다.")
    if code == 401:
        sys.exit("401 — X-Api-Key가 없거나 등록되지 않았다.")
    if code not in (200, 201, 202) or not body:   # 배치 생성은 202 Accepted다
        sys.exit(f"배치 생성 실패: HTTP {code} {err[1] if err else ''}")

    bid = body.get("batchId")
    print(f"  배치 생성: {bid}  (offset {a.offset}, {n}문장, 예산 {n}회)")

    last = None
    deadline = time.monotonic() + a.wait
    while time.monotonic() < deadline:
        time.sleep(a.poll)
        code, view, err = call(f"{base}{BATCH_PATH}/{bid}", a.operator_key)
        if code != 200 or not view:
            print(f"  상태 조회 실패: HTTP {code}")
            continue
        cur = (view.get("status"), view.get("done"), view.get("failed"))
        if cur != last:
            print(f"    status={cur[0]}  done={cur[1]}/{view.get('total')}  failed={cur[2]}")
            last = cur
        if view.get("done", 0) + view.get("failed", 0) >= view.get("total", n):
            break
        if view.get("status") in TERMINAL and view.get("status") != "COMPLETED":
            print(f"  배치가 {view.get('status')}로 멈췄다.")
            break
    else:
        print(f"  {a.wait}초 안에 끝나지 않았다 — 배치는 계속 돌 수 있다.")
    return bid


def verify(a, corpus):
    """데모 키로 BYOK 없이 호출해 전부 캐시 히트인지 본다."""
    base = a.base_url.rstrip("/")
    hit = miss = failed = 0
    for i, it in enumerate(corpus, 1):
        if i > 1:
            time.sleep(a.delay)
        body = {"text": it["original"]}
        y = reign_year(it["id"])
        if y is not None:
            body["year"] = y
        code, resp, err = call(base + SYNC_PATH, a.api_key, body, timeout=a.timeout)
        tag = f"  {i:2d}. [{it['id']}]"
        if code == 200:
            c = (resp.get("meta") or {}).get("cacheHit")
            if c:
                hit += 1
                print(f"{tag} HIT {c}")
            else:
                miss += 1
                print(f"{tag} 200이지만 캐시 미스 — 운영자 키로 번역됐다(공개 모드가 아닌가?)")
        elif code == 403:
            failed += 1
            print(f"{tag} 403 BYOK_REQUIRED — 아직 예열되지 않았다")
        elif code == 401:
            sys.exit(f"{tag} 401 — 데모 키(--api-key)가 등록되지 않았다.")
        else:
            failed += 1
            print(f"{tag} HTTP {code} {err[1] if err else ''}")
    print(f"\n합계: 히트 {hit} / 캐시미스 {miss} / 실패 {failed}")
    print("확인 기준: 실패 0 · 캐시미스 0 이어야 키 하나로 시연 가능하다.")
    return 0 if (failed == 0 and miss == 0) else 1


def main():
    p = argparse.ArgumentParser(description="공개 데모용 L1 캐시 예열 (배치 경로)")
    p.add_argument("--base-url", required=True, help="예: https://skala-gj4-sjw.skala-gj.com")
    p.add_argument("--operator-key", help="operator_access 테넌트 키 — 예열에 필요")
    p.add_argument("--api-key", help="데모 테넌트 키 — --verify에 필요")
    p.add_argument("--corpus", default=DEFAULT_CORPUS)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--count", type=int, default=5, help="예열할 문장 수 (기본 5 — 무료 RPD 20)")
    p.add_argument("--wait", type=int, default=600, help="배치 완료 대기 상한(초)")
    p.add_argument("--poll", type=float, default=5, help="상태 조회 간격(초)")
    p.add_argument("--delay", type=float, default=0.3, help="--verify 호출 간격(초)")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--dry-run", action="store_true", help="호출 없이 대상만 출력")
    p.add_argument("--verify", action="store_true", help="예열 확인 모드")
    a = p.parse_args()

    corpus = load_corpus(a.corpus, a.offset, a.count)
    mode = "확인" if a.verify else "예열"
    print(f"{mode}: {len(corpus)}문장 (offset {a.offset}) → {a.base_url}")

    if a.dry_run:
        for i, it in enumerate(corpus, 1):
            print(f"  {i:2d}. [{it['id']}] {reign_year(it['id'])} {it['original'][:40]}")
        print("(dry-run — 호출하지 않았다)")
        return 0

    if a.verify:
        if not a.api_key:
            sys.exit("--verify에는 --api-key(데모 키)가 필요하다.")
        return verify(a, corpus)

    if not a.operator_key:
        sys.exit("예열에는 --operator-key가 필요하다. 동기 경로 + BYOK로는 캐시에 적재되지 않는다 "
                 "(TranslationController: byok == null 일 때만 store).")
    if a.count > FREE_RPD // 2:
        print(f"! {a.count}문장을 예열하면 LLM을 {a.count}회 부른다. "
              f"무료 일일 상한은 {FREE_RPD}회다 (ADR-016).", file=sys.stderr)
    preheat(a, corpus)
    print("\n예열 요청 완료. --verify 로 확인한다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
