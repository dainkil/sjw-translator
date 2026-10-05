#!/usr/bin/env python3
"""파이프라인 대 날것의 LLM: KB 주입(NER→링킹→[등장 인물] 블록) 유무 A/B.

**대체됨 (2026-10-04): `name_ablation.py`.** 이 드라이버는 eval60·종전 정답지(KB 표기, KB 밖 이름 대부분 누락)를
쓰고 대조군을 다른 머신의 v0 결과로 재사용한다. 상태 파일 `kb_ablation.json`에 nokb 3라운드가 완료로 남아 있어
`--run`은 아무것도 돌리지 않는다. 기록 보존용으로만 둔다.

질문: 지식 주입이 LLM 단독보다 인명 정확도(ETS)·chrF를 얼마나 올리나. README 헤드라인의 ETS 98%는 주입을 켠
상태의 절대값일 뿐, 주입 없는 번역과 비교한 적이 없었다 (선행 연구 AB는 정성 사례뿐, research/README 실험 결과).

설계 (prompt_ablation.py와 같은 규약 — 표본·라운드·채점 함수를 그대로 쓴다):
  - 표본: eval60_stratified.json (n=60, 정답지 인명 104)
  - 대조군(kb): prompt_ablation.json의 v0 3라운드를 그대로 가져온다 — 같은 생산 프롬프트(main-d5ac24e9)·표본·
    모델·캐시 off·승격 off. 새로 돌리지 않는다 (quota 180회 절약). 측정일이 다르다는 점은 결과에 기록한다.
  - 실험군(nokb): 워커를 KB_MODE=noop으로 — 모든 링킹이 MISS라 [등장 인물] 블록이 빠진다. 나머지(역할·원칙·
    문체 표·예시·[반드시 사용할 표현])는 같다. 즉 이 A/B가 재는 것은 "NER+KB 주입"의 몫이다.
  - self-check: DB의 prompt_version = v0, kb_version = noop 이 아니면 배치 pause 후 exit 2.
  - 반영률(주입 인명)은 nokb에서 정의되지 않는다(주입 0) — ETS와 chrF가 비교 지표다.

절차:
  PROMPT_TEMPLATE= KB_MODE=noop CACHE_L1_ENABLED=false CACHE_L2_ENABLED=false TIER_UP_ENABLED=false \\
    docker compose -f deploy/docker-compose.yml up -d --no-deps worker
  uv run --with sacrebleu --with "psycopg[binary]" python eval/kb_ablation.py --run      # nokb 3라운드 (재개 가능)
  ...                                                                  --report   # kb vs nokb 표
종료 코드: 0 / 2 측정 불가(self-check 실패, 배치 정지)
"""
import argparse
import json
import math
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import prompt_ablation as pa                      # 제출·대기·채점·버전 계산 공유
import score_db

STATE_PATH = HERE / "kb_ablation.json"
ROUNDS = pa.ROUNDS
METRICS = ("chrf", "ets", "ets_lenient", "ets_macro", "mean_tokens_in")


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"created": pa.now(), "sample": pa.SAMPLE_PATH.name, "rounds": ROUNDS,
            "control": "prompt_ablation.json variants.v0 (KB 주입 on, 같은 프롬프트·표본·모델)",
            "nokb": {"prompt_version": pa.expected_version(pa.PROD_TEMPLATE), "kb_version": "noop", "rounds": []}}


def save_state(st):
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean(scores):
    """nokb는 주입 인명이 0건이라 반영률이 NaN — JSON에는 null로."""
    return {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in scores.items()}


def check_kb_version(conn, batch_id):
    seen = [r[0] for r in conn.execute(
        "SELECT DISTINCT kb_version FROM translation_job WHERE batch_id = %s::uuid AND status = 'SUCCEEDED'",
        (batch_id,)).fetchall()]
    if seen and seen != ["noop"]:
        pa.http("POST", f"/api/v1/batches/{batch_id}/pause")
        print(f"self-check 실패: kb_version={seen} ≠ noop — 워커를 KB_MODE=noop으로 재기동할 것. 배치 pause.", file=sys.stderr)
        sys.exit(2)


def run():
    import psycopg
    st = load_state()
    entry = st["nokb"]
    expected = entry["prompt_version"]
    n = json.loads(pa.SAMPLE_PATH.read_text(encoding="utf-8"))["n"]
    print(f"[nokb] prompt_version {expected}, kb_version noop")
    with psycopg.connect(score_db.DSN) as conn:
        while sum(r["status"] == "COMPLETED" for r in entry["rounds"]) < ROUNDS:
            rec = next((r for r in entry["rounds"] if r["status"] != "COMPLETED"), None)
            if rec:
                print(f"  라운드 {rec['round']} 이어서 대기 (batch {rec['batch_id']})")
            else:
                rec = {"round": len(entry["rounds"]) + 1, "batch_id": pa.submit_batch(n), "status": "RUNNING",
                       "submitted_at": pa.now(), "completed_at": None, "scores": None}
                entry["rounds"].append(rec)
                save_state(st)
                print(f"  라운드 {rec['round']} 제출: batch {rec['batch_id']} ({n}건)")
            b = pa.wait_batch(conn, rec["batch_id"], expected)
            check_kb_version(conn, rec["batch_id"])
            rec.update(status="COMPLETED", completed_at=pa.now(), failed=b["failed"],
                       scores=clean(pa.score_batch(conn, rec["batch_id"], expected)))
            save_state(st)
            s = rec["scores"]
            print(f"  라운드 {rec['round']} 완료: chrF {s['chrf']:.2f} ETS {s['ets']:.4f} "
                  f"tokens_in {s['mean_tokens_in']:.1f} 등급 {s['grades']}")


def medians(rounds):
    rs = [r["scores"] for r in rounds if r["status"] == "COMPLETED"]
    if len(rs) < ROUNDS:
        return None, rs
    return {k: statistics.median(r[k] for r in rs) for k in METRICS}, rs


def report():
    st = load_state()
    control = json.loads(pa.STATE_PATH.read_text(encoding="utf-8"))["variants"]["v0"]["rounds"]
    mk, rk = medians(control)
    mn, rn = medians(st["nokb"]["rounds"])
    if mk is None or mn is None:
        print(f"라운드 부족: kb {len(rk)}/{ROUNDS}, nokb {len(rn)}/{ROUNDS}", file=sys.stderr)
        sys.exit(2)
    names = rk[0]["ets_names"]
    print(f"표본 n=60, 정답지 인명 {names}건 — 중앙값 (3라운드)\n")
    print(f"{'지표':14s} {'LLM 단독(nokb)':>14s} {'파이프라인(kb)':>14s} {'차이':>9s}")
    for k in METRICS:
        print(f"{k:14s} {mn[k]:14.4f} {mk[k]:14.4f} {mk[k] - mn[k]:+9.4f}")
    print(f"\nETS 인명 수: nokb {round(mn['ets'] * names)}/{names} → kb {round(mk['ets'] * names)}/{names}")
    print("라운드별 chrF  nokb: " + " / ".join(f"{r['chrf']:.2f}" for r in rn)
          + "   kb: " + " / ".join(f"{r['chrf']:.2f}" for r in rk))
    print("라운드별 ETS   nokb: " + " / ".join(f"{r['ets']:.4f}" for r in rn)
          + "   kb: " + " / ".join(f"{r['ets']:.4f}" for r in rk))
    st["verdict"] = {"at": pa.now(), "nokb_median": mn, "kb_median": mk,
                     "delta": {k: mk[k] - mn[k] for k in METRICS}, "ets_names": names,
                     "note": "kb = prompt_ablation v0 라운드(2026-09-21), nokb = 이 파일의 라운드"}
    save_state(st)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", action="store_true", help="nokb 남은 라운드를 돈다")
    g.add_argument("--report", action="store_true", help="kb vs nokb 비교")
    a = ap.parse_args()
    run() if a.run else report()


if __name__ == "__main__":
    main()
