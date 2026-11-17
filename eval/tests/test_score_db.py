"""score_db.py 단위 테스트 — 채점 규칙이 바뀌면 여기서 잡힌다. LLM 0회, DB 불필요.

경계: Java↔Python 해시 파리티는 생산 DB가 Java(TextHash)로 써 둔 행을 픽스처로 쓴다.
프롬프트 체크섬 파리티는 단위 테스트로 증명할 수 없다(Java 값이 필요) — 그 역할은 prompt_ablation.py의
라이브 self-check(DB 행의 prompt_version 대조)가 하며, 2026-09-21 실측으로 main-d5ac24e9 일치를 확인했다.
"""
import json
import math

import pytest

import score_db

# 생산 DB의 translation_job 행 — normalized_hash는 Java TextHash가 계산한 값 (2026-09-21 조회)
JAVA_HASHES = {
    "○ 傳于兪榥曰, 政事, 明日爲之。": "52cddcc2abeec9ec9895b01ee8381d38acf1dad8e824333dd0b0811135842d9f",
    "傳于李馨長曰知道": "33665f0407f46081bbe45b968793c170706448ac9406572b3fc379ad5c50828b",
}


def test_normalized_hash_matches_java_textHash():
    for text, h in JAVA_HASHES.items():
        assert score_db.normalized_hash(text) == h


def test_normalized_hash_ignores_whitespace_and_nfd():
    a = score_db.normalized_hash("傳于 李馨長曰 知道")
    assert a == JAVA_HASHES["傳于李馨長曰知道"]
    import unicodedata
    nfd = unicodedata.normalize("NFD", "김류가 아뢰었다")
    assert score_db.normalized_hash(nfd) == score_db.normalized_hash("김류가 아뢰었다")


def test_normalized_hash_keeps_nbsp_like_java():
    # Java `\s`는 U+00A0을 지우지 않는다. 기댓값은 Java(TextHash)가 translation_job에 쓴 값 (SJW-A07010170-00700).
    # str.split()으로 지우면 이 문장이 DB 결과와 짝지어지지 않아 채점에서 빠졌다.
    text = ("○ 答院曰, 依啓。李溟運削去仕版,\xa0"
            "泰川縣監成戒愼越署經遞差事")
    assert score_db.normalized_hash(text) == "0cb45291470e1ac855a47e86a288d176b8a4bedb5cc45b09804ddf3b5bcea583"


def test_name_reflected_rules():
    assert score_db.name_reflected("김류를 승지로 제수하였다", "김류")           # 전체형
    assert score_db.name_reflected("문회가 아뢰기를", "정문회")                  # 성 생략, 이름부 2자
    assert not score_db.name_reflected("공이 아뢰기를", "윤공")                  # 이름부 1자는 우연 일치 — 거부
    assert score_db.name_reflected("이원익이 아뢰기를", "이원")                  # 2자 이름은 전체형 포함 검사뿐 — 긴 이름의 부분 문자열도 잡힌다 (게이트와 동일한 현 규칙, 알려진 한계)


GT = {"s1": ["김류", "이원익"], "s2": ["정경세"], "s3": []}
ITEMS = [
    {"id": "s1", "reference": "김류와 이원익이 아뢰었다", "hypothesis": "김류와 이원익이 아뢰었다",
     "entities": [{"kbId": "1", "resolvedName": "김류"}, {"kbId": "2", "resolvedName": "이원익"}], "tokens_in": 100},
    {"id": "s2", "reference": "정경세가 아뢰었다", "hypothesis": "경세가 아뢰었다",
     "entities": [{"kbId": "3", "resolvedName": "정경세"}], "tokens_in": 120},
    {"id": "s3", "reference": "비가 내렸다", "hypothesis": "비가 내렸다", "entities": [], "tokens_in": None},
]


def test_ets_strict_lenient_macro():
    e = score_db.ets(ITEMS, GT)
    assert e["ets_names"] == 3 and e["ets_sentences"] == 2      # s3는 정답지 인명 없음 → 분모 제외
    assert math.isclose(e["ets"], 2 / 3)                        # 엄격: 정경세 누락
    assert math.isclose(e["ets_lenient"], 1.0)                  # 관대: '경세' 인정
    assert math.isclose(e["ets_macro"], 0.5)                    # 문장별 (1.0 + 0.0) / 2


def test_confirmed_name_recall_counts_injected_names_only():
    r, n = score_db.confirmed_name_recall(ITEMS)
    assert n == 3 and math.isclose(r, 2 / 3)
    assert math.isnan(score_db.confirmed_name_recall([ITEMS[2]])[0])   # 확정 인명 0건 → nan


def test_score_items_shape_and_chrf_ceiling():
    sc = score_db.score_items(ITEMS, GT)
    assert sc["n"] == 3 and sc["n_confirmed"] == 3
    assert 0 < sc["chrf"] <= 100
    assert math.isclose(sc["mean_tokens_in"], 110.0)            # None은 제외
    identical = [dict(it, hypothesis=it["reference"]) for it in ITEMS]
    assert math.isclose(score_db.score_items(identical, GT)["chrf"], 100.0)


def test_reference_self_check_reproduces_recorded_ceiling():
    """전문가 번역 자체의 ETS = 정답지·채점 규칙의 상한. docs/benchmarks.md에 0.9713 / 383건 / 187문장으로 기록."""
    gold = score_db.load_goldenset(score_db.DEFAULT_CORPUS)
    gt = score_db.load_groundtruth(score_db.DEFAULT_GROUNDTRUTH)
    items = [{"id": r["id"], "hypothesis": r["reference"]} for r in gold.values()]
    e = score_db.ets(items, gt)
    assert (e["ets_names"], e["ets_sentences"]) == (383, 187)
    assert round(e["ets"], 4) == 0.9713
    assert round(e["ets_lenient"], 4) == 0.9791


def test_baseline_file_has_required_fields():
    base = json.loads(score_db.BASELINE_PATH.read_text(encoding="utf-8"))
    for k in ("chrf", "confirmed_name_recall", "ets", "prompt_versions", "n"):
        assert k in base, k
