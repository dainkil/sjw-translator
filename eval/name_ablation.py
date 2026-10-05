#!/usr/bin/env python3
"""KB 주입 A/B 재측정 — 같은 프롬프트(v4)·eval300·교차 라운드. 사전 등록: docs/benchmarks.md "사전 등록 — KB 주입 A/B 재측정".

종전 `kb_ablation.py`(eval60, 대조군은 다른 머신의 v0 결과 재사용)를 대체한다. 다른 점:
  - 표본 eval300 (n=300, 국편 어노테이션 인명 527 — KB 밖 이름 포함), 채점은 `name_metrics`(국역 정답지)
  - 두 arm 모두 이 드라이버가 새로 돌린다 — 같은 프롬프트(현 생산 = translate-main.st), 같은 모델·기간
  - 라운드를 arm끼리 **교차**한다(r1 kb→nokb, r2 nokb→kb, r3 kb→nokb) — 측정 기간 중 모델·provider 변동이
    한 arm에만 실리지 않게. 워커를 arm마다 재기동한다(`--orchestrate`) 또는 사람이 재기동한다
  - 상태 파일은 `name_ablation.json` — 종전 `kb_ablation.json`과 섞이지 않는다
  - 행 밀림 문장(`eval_exclusions.json`)은 이름·chrF 모두에서 뺀다

arm (워커 환경변수; 공통: 캐시 L1·L2 off, 승격 off, 라우팅 off, 프롬프트 classpath 기본):
  kb    KB_MODE=file  — NER → KB 링킹 → [등장 인물] 블록
  nokb  KB_MODE=noop  — 모든 링킹이 MISS, 블록이 빠진다. 나머지 프롬프트는 같다

self-check (어긋나면 배치 pause 후 exit 2 — 그 배치는 버린다):
  prompt_version = translate-main.st 체크섬, kb_version = injo-… (kb) / noop (nokb), model_used = 활성 모델 1종

사용법:
  uv run --with sacrebleu --with "psycopg[binary]" python eval/name_ablation.py --plan                    # 일정·사전 점검 (LLM 0회)
  uv run --with sacrebleu --with "psycopg[binary]" python eval/name_ablation.py --run --orchestrate       # 남은 라운드 실행, arm마다 워커 재기동 (재개 가능)
  uv run --with sacrebleu --with "psycopg[binary]" python eval/name_ablation.py --report [--json]

비용: 300 × 2 arm × 3라운드 = 1,800 호출. 테넌트 일일 상한(기본 1,000)은 배치 생성 시 선과금이므로 하루 3배치까지 —
`--plan`이 확인한다. 워커 rpm 버킷(15)이 처리량을 묶어 배치당 약 20분.
"""
import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import prompt_ablation as pa  # noqa: E402  — http·대기·버전 계산 공유

STATE_PATH = HERE / "name_ablation.json"
SAMPLE_PATH = HERE / "eval300_1925.json"
EVAL60_PATH = HERE / "eval60_stratified.json"
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
WORKER_HEALTH = "http://localhost:9081/actuator/health"
ROUNDS = 3
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
COMMON_ENV = {"CACHE_L1_ENABLED": "false", "CACHE_L2_ENABLED": "false", "TIER_UP_ENABLED": "false",
              "ROUTING_ENABLED": "false", "PROMPT_TEMPLATE": "classpath:prompts/translate-main.st",
              "GEMINI_MODEL": MODEL, "KB_NAME": "injo", "KB_DIR": "/app/kb"}   # 확장 KB(injo_aug)가 섞이지 않게
ARMS = {"kb": {"KB_MODE": "file"}, "nokb": {"KB_MODE": "noop"}}
CHRF_ITERS = 1000
MAX_MISSING = 0.05          # 라운드에서 빠진 문장(FAILED)이 이보다 많으면 그 배치는 무효 — 사전 등록
FROZEN = ("name_gold_300.json", "eval_exclusions.json")     # 실행 전에 고정하는 채점 입력
BOTTLENECK_SHARE = 0.5      # H4: kb arm 인명 오류 중 KB 밖 이름의 몫이 이 이상이면 "병목 이동" — 사전 등록


def sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schedule(rounds=ROUNDS):
    """교차 순서 — 홀수 라운드 kb 먼저, 짝수 라운드 nokb 먼저."""
    out = []
    for r in range(1, rounds + 1):
        order = ("kb", "nokb") if r % 2 else ("nokb", "kb")
        out += [{"round": r, "arm": a} for a in order]
    return out


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"created": pa.now(), "sample": SAMPLE_PATH.name, "n": json.loads(SAMPLE_PATH.read_text())["n"],
            "prompt_version": pa.expected_version(pa.PROD_TEMPLATE), "model": MODEL,
            "preregistration": "docs/benchmarks.md#사전-등록--kb-주입-ab-재측정",
            "frozen_sha256": {f: sha256(HERE / f) for f in FROZEN},
            "runs": [dict(s, batch_id=None, status="PENDING") for s in schedule()]}


def save_state(st):
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ── 워커 환경 ─────────────────────────────────────────────────────────────

def container_env(name):
    out = subprocess.run(["docker", "exec", name, "env"], capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def worker_matches(arm):
    env = container_env("sjw-worker")
    want = {**COMMON_ENV, **ARMS[arm]}
    return {k: (env.get(k), v) for k, v in want.items() if env.get(k) != v}


def restart_worker(arm, timeout_s=240):
    env = {**os.environ, **COMMON_ENV, **ARMS[arm]}
    print(f"  워커 재기동: {ARMS[arm]}")
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), "up", "-d", "--no-deps", "worker"], env=env, check=True)
    time.sleep(5)                       # 이전 컨테이너가 아직 UP을 답하는 창을 건너뛴다
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            import urllib.request
            with urllib.request.urlopen(WORKER_HEALTH, timeout=5) as r:
                if json.load(r).get("status") == "UP" and not worker_matches(arm):
                    return
        except Exception:
            pass
        time.sleep(3)
    sys.exit(f"워커가 {timeout_s}s 안에 {arm} 설정으로 뜨지 않았다: {worker_matches(arm)}")


def preflight(st):
    """LLM 0회 사전 점검 — 문제는 모두 모아서 보고한다."""
    problems = []
    api = container_env("sjw-api")
    if not api.get("SJW_EVAL_CORPUS", "").endswith("eval300_1925.json"):
        problems.append(f"api SJW_EVAL_CORPUS={api.get('SJW_EVAL_CORPUS')} — eval300_1925.json이어야 한다")
    import hashlib
    local = hashlib.sha256(SAMPLE_PATH.read_bytes()).hexdigest()
    remote = subprocess.run(["docker", "exec", "sjw-api", "sha256sum", api.get("SJW_EVAL_CORPUS", "")],
                            capture_output=True, text=True).stdout.split(" ")[0]
    if remote != local:
        problems.append(f"api 컨테이너의 골든셋이 레포 {SAMPLE_PATH.name}와 다르다 (sha256 {remote[:12]} ≠ {local[:12]})")
    if st["prompt_version"] != pa.expected_version(pa.PROD_TEMPLATE):
        problems.append("translate-main.st가 상태 파일 생성 이후 바뀌었다 — 새 상태 파일로 다시 시작할 것")
    today = subprocess.run(["docker", "exec", "sjw-api", "date", "+%F"], capture_output=True, text=True).stdout.strip()
    used = subprocess.run(["docker", "exec", "sjw-redis", "redis-cli", "get", f"budget:daily:default:{today}"],
                          capture_output=True, text=True).stdout.strip()
    limit = subprocess.run(["docker", "exec", "sjw-postgres", "psql", "-U", "sjw", "-d", "sjw", "-tAc",
                            "SELECT daily_call_limit FROM tenant WHERE id='default'"],
                           capture_output=True, text=True).stdout.strip()
    used_n = int(used) if used.isdigit() else 0
    limit_n = int(limit) if limit.isdigit() else 0
    pending = sum(1 for r in st["runs"] if r["status"] != "COMPLETED")
    fit_today = max(0, (limit_n - used_n) // st["n"])
    return problems, {"date": today, "tenant_used": used_n, "tenant_limit": limit_n,
                      "pending_batches": pending, "batches_fit_today": fit_today}


# ── 실행 ─────────────────────────────────────────────────────────────────

def self_check(conn, batch_id, arm, expected_version):
    kb = [r[0] for r in conn.execute("SELECT DISTINCT kb_version FROM translation_job "
                                     "WHERE batch_id = %s::uuid AND status = 'SUCCEEDED'", (batch_id,)).fetchall()]
    models = [r[0] for r in conn.execute("SELECT DISTINCT model_used FROM translation_job "
                                         "WHERE batch_id = %s::uuid AND status = 'SUCCEEDED'", (batch_id,)).fetchall()]
    pv = pa.db_prompt_versions(conn, batch_id)
    bad = []
    if pv and pv != [expected_version]:
        bad.append(f"prompt_version={pv}")
    if kb and (kb != ["noop"] if arm == "nokb" else not all(k.startswith("injo-") for k in kb)):
        bad.append(f"kb_version={kb}")
    if models and models != [MODEL]:
        bad.append(f"model_used={models}")
    return bad, {"kb_version": kb, "models": models, "prompt_version": pv}


def run(orchestrate):
    import psycopg
    st = load_state()
    save_state(st)
    problems, info = preflight(st)
    if problems:
        sys.exit("사전 점검 실패:\n  " + "\n  ".join(problems))
    with psycopg.connect(pa.score_db.DSN) as conn:
        for rec in st["runs"]:
            if rec["status"] == "COMPLETED":
                continue
            arm = rec["arm"]
            print(f"[r{rec['round']} {arm}]")
            mismatch = worker_matches(arm)
            if mismatch and rec["status"] == "RUNNING":
                sys.exit(f"진행 중 배치 {rec['batch_id']}가 있는데 워커 설정이 {arm}이 아니다: {mismatch} — "
                         f"워커를 {arm}으로 되돌린 뒤 다시 실행할 것")
            if mismatch:
                if not orchestrate:
                    env = " ".join(f"{k}={v}" for k, v in {**COMMON_ENV, **ARMS[arm]}.items())
                    sys.exit(f"워커가 {arm} 설정이 아니다: {mismatch}\n재기동: {env} docker compose -f "
                             f"deploy/docker-compose.yml up -d --no-deps worker  (또는 --orchestrate)")
                restart_worker(arm)
            if rec["status"] == "PENDING":
                code, body = pa.http("POST", "/api/v1/batches",
                                     {"offset": 0, "limit": st["n"], "budgetLimitCalls": 2 * st["n"]})
                if code != 202:
                    sys.exit(f"배치 생성 실패 HTTP {code}: {body} — 429 TENANT_DAILY_LIMIT이면 내일 같은 명령으로 "
                             f"재개하거나 tenant.daily_call_limit을 올릴 것 (--plan 참고)")
                rec.update(batch_id=body["batchId"], status="RUNNING", submitted_at=pa.now())
                save_state(st)
            pa.wait_batch(conn, rec["batch_id"], st["prompt_version"])
            bad, seen = self_check(conn, rec["batch_id"], arm, st["prompt_version"])
            if bad:
                pa.http("POST", f"/api/v1/batches/{rec['batch_id']}/pause")
                rec.update(status="DISCARDED", discarded_reason=bad)
                st["runs"].append({"round": rec["round"], "arm": arm, "batch_id": None, "status": "PENDING"})
                save_state(st)
                sys.exit(f"self-check 실패 {bad} — 배치를 버리고 같은 라운드를 다시 잡았다. 원인을 고친 뒤 재실행")
            rec.update(status="COMPLETED", completed_at=pa.now(), **seen)
            save_state(st)
    print("모든 라운드 완료 — --report")


# ── 채점 ─────────────────────────────────────────────────────────────────

def chrf_by_arm(arm_rounds, refs, ids):
    """arm별 corpus chrF(라운드 중앙값)와 첫 arm 대비 paired bootstrap Δ."""
    import statistics
    import sacrebleu

    def corpus_chrf(rounds, sample):
        vals = [sacrebleu.corpus_chrf([r[i] for i in sample], [[refs[i] for i in sample]]).score for r in rounds]
        return statistics.median(vals)

    ids = sorted(ids)
    labels = list(arm_rounds)
    out = {k: round(corpus_chrf(arm_rounds[k], ids), 2) for k in labels}
    deltas = {}
    rng = random.Random(0)
    base = labels[0]
    for b in labels[1:]:
        stats = []
        for _ in range(CHRF_ITERS):
            sample = [rng.choice(ids) for _ in ids]
            stats.append(corpus_chrf(arm_rounds[b], sample) - corpus_chrf(arm_rounds[base], sample))
        stats.sort()
        deltas[f"{b}-{base}"] = {"delta": round(out[b] - out[base], 2),
                                 "ci95": [round(stats[int(0.025 * len(stats))], 2),
                                          round(stats[int(0.975 * len(stats)) - 1], 2)]}
    return {"chrf": out, "deltas": deltas}


def verdict(names, chrf):
    """사전 등록 판정 규칙 (docs/benchmarks.md) — 사후에 바꾸지 않는다."""
    d = names["deltas"][0]
    seg = lambda s: d[s]["ci95"]                                   # noqa: E731
    out = {
        "H1_in_kb_gain": seg("in_kb") is not None and seg("in_kb")[0] > 0,
        "H2_out_kb_no_gain": seg("out_kb") is not None and seg("out_kb")[0] <= 0 <= seg("out_kb")[1],
        "H3_chrf_noninferior": chrf["deltas"]["kb-nokb"]["ci95"][0] > -2.0,
    }
    kb = next(a for a in names["arms"] if a["arm"] == "kb")
    out["H4_bottleneck_share_out_kb"] = None
    out["H4_bottleneck_moved"] = None
    errs_in = kb["in_kb"]["names"] * (1 - kb["in_kb"]["acc"]) if kb["in_kb"]["acc"] is not None else 0
    errs_out = kb["out_kb"]["names"] * (1 - kb["out_kb"]["acc"]) if kb["out_kb"]["acc"] is not None else 0
    if errs_in + errs_out:
        out["H4_bottleneck_share_out_kb"] = round(errs_out / (errs_in + errs_out), 4)
        out["H4_bottleneck_moved"] = out["H4_bottleneck_share_out_kb"] >= BOTTLENECK_SHARE
    return out


def report(as_json):
    import psycopg
    import name_metrics as nm
    from eval_exclusions import load_exclusions
    from score_db import DSN, normalized_hash

    st = load_state()
    drift = {f: (h[:12], sha256(HERE / f)[:12]) for f, h in st.get("frozen_sha256", {}).items()
             if sha256(HERE / f) != h}
    if drift:
        print(f"경고: 실행 시점에 고정한 채점 입력이 바뀌었다 {drift} — 사전 등록 위반. 결과에 기록된다.", file=sys.stderr)
    done = [r for r in st["runs"] if r["status"] == "COMPLETED"]
    corpus = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))["corpus"]
    hash_to_id = {normalized_hash(r["original"]): r["id"] for r in corpus}
    refs = {r["id"]: r["reference"] for r in corpus}
    excluded = load_exclusions()
    gold, unscored = nm.load_gold(HERE / "name_gold_300.json", excluded)
    eval60 = set(json.loads(EVAL60_PATH.read_text(encoding="utf-8"))["ids"])

    arm_rounds = {"nokb": [], "kb": []}             # 첫 arm이 Δ의 기준 — nokb
    with psycopg.connect(DSN) as conn:
        for r in done:
            arm_rounds[r["arm"]].append(nm.fetch_batch(conn, r["batch_id"], hash_to_id))
    if not all(arm_rounds.values()):
        sys.exit(f"완료된 라운드가 부족하다: { {k: len(v) for k, v in arm_rounds.items()} }")
    short = {f"{k}[{i}]": len(r) for k, rounds in arm_rounds.items() for i, r in enumerate(rounds)
             if len(r) < (1 - MAX_MISSING) * st["n"]}
    if short:
        print(f"경고: 문장이 {MAX_MISSING:.0%} 넘게 빠진 라운드 {short} — 사전 등록상 그 배치는 다시 돌려야 한다",
              file=sys.stderr)

    all_ids = set.intersection(*(set(r) for rounds in arm_rounds.values() for r in rounds)) - excluded
    result = {"sample": st["sample"], "prompt_version": st["prompt_version"], "model": st["model"],
              "rounds": {k: len(v) for k, v in arm_rounds.items()}, "excluded_sentences": sorted(excluded),
              "frozen_drift": drift, "short_rounds": short}
    # eval60은 v4 프롬프트를 고른 표본이다 — 고정 예시 쪽의 홈 이점을 보려고 나머지 240과 따로 낸다
    for label, ids in (("all", all_ids), ("eval60", all_ids & eval60), ("rest240", all_ids - eval60)):
        if not ids:
            continue
        result[label] = {"names": nm.evaluate(gold, unscored, arm_rounds, ids=ids),
                         "chrf": chrf_by_arm(arm_rounds, refs, ids)}
    result["verdict"] = verdict(result["all"]["names"], result["all"]["chrf"])

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return
    for label in ("all", "eval60", "rest240"):
        if label not in result:
            continue
        print(f"\n════ {label} — 문장 {result[label]['names']['sentences']}")
        nm.print_result(result[label]["names"])
        c = result[label]["chrf"]
        print(f"  chrF {c['chrf']}  Δ {c['deltas']}")
    print("\n판정 (사전 등록):", json.dumps(result["verdict"], ensure_ascii=False))


def plan():
    st = load_state()
    problems, info = preflight(st)
    print(f"표본 {st['sample']} n={st['n']} / prompt_version {st['prompt_version']} / 모델 {st['model']}")
    print("일정:")
    for r in st["runs"]:
        print(f"  r{r['round']} {r['arm']:<5} {r['status']:<10} {r.get('batch_id') or ''}")
    print(f"테넌트 {info['date']}: 사용 {info['tenant_used']} / 상한 {info['tenant_limit']} → 오늘 가능한 배치 "
          f"{info['batches_fit_today']} / 남은 배치 {info['pending_batches']}")
    if info["batches_fit_today"] < info["pending_batches"]:
        print("  하루에 다 돌리려면: docker exec sjw-postgres psql -U sjw -d sjw -c "
              f"\"UPDATE tenant SET daily_call_limit = {info['tenant_used'] + st['n'] * info['pending_batches']} "
              "WHERE id='default'\"  (유료 키일 때만)")
    for k in ARMS:
        mm = worker_matches(k)
        print(f"  워커 현재 설정 {'= ' + k if not mm else '≠ ' + k + ' ' + str(mm)}")
    print("사전 점검:", "통과" if not problems else problems)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true")
    g.add_argument("--run", action="store_true")
    g.add_argument("--report", action="store_true")
    ap.add_argument("--orchestrate", action="store_true", help="arm마다 워커를 docker compose로 재기동")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.plan:
        plan()
    elif args.run:
        run(args.orchestrate)
    else:
        report(args.json)


if __name__ == "__main__":
    main()
