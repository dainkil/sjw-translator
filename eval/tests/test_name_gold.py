"""name_gold.py · name_metrics.py 단위 테스트 — 정답지를 국역에서 찾는 규칙과 구간 채점. LLM 0회, DB 불필요.

픽스처는 골든셋에서 실제로 나온 사례를 줄인 것이다(eval/name_gold_review.tsv).
"""
import pytest

import name_gold as ng
import name_metrics as nm

NAMES = frozenset({"조위한", "김상안", "이상안", "이공", "윤공"})


def test_dueum_initial_sound_law():
    assert ng.dueum("리") == "이"
    assert ng.dueum("류") == "유"
    assert ng.dueum("로") == "노"
    assert ng.dueum("녀") == "여"
    assert ng.dueum("김") == "김"          # 바뀌지 않는 초성


def test_normalize_span_strips_punct_titles_and_decodes_rare_hanja():
    assert ng.normalize_span("金起宗,") == "金起宗"
    assert ng.normalize_span("昇平府院君金瑬") == "金瑬"
    assert ng.normalize_span("昇平府院君臣金瑬") == "金瑬"
    rare = ng.normalize_span("沈&#55401;&#56774;")      # 서로게이트 쌍 엔티티 → BMP 밖 한 글자
    assert len(rare) == 2 and rare[0] == "沈" and ord(rare[1]) > 0xFFFF


def test_lib_readings_fix_surname_and_dueum():
    table = {"沈詻": "침액", "李溟運": "리명운"}
    out = ng.lib_readings("沈詻", table.get, {"沈": "심"})
    assert "심액" in out
    out = ng.lib_readings("李溟運", table.get, {})
    assert "이명운" in out


def test_name_tokens_word_start_particles_and_longer_names():
    ref = "진심을 다해, 홍방ㆍ조위한이 입시하였다. 나장 이봉진이 도망쳤다. 공의가 행해지면 이공의 몸도"
    toks2 = ng.name_tokens(ref, 2, NAMES)
    assert "심을" not in toks2          # 어절 중간 조각
    assert "조위" not in toks2          # 마스터의 더 긴 이름(조위한)의 앞부분
    assert "이봉" not in toks2          # 뒤에 조사가 아닌 글자(진)
    assert "이공" in toks2              # 이공 + 의(조사)
    assert ng.name_tokens(ref, 1, NAMES) == set()     # 한 글자는 낱말과 가를 수 없다


def test_resolve_agree_when_injected_name_in_reference():
    st, gold = ng.resolve([("심액", "kb")], "심액", "제조 심액이 아뢰기를", NAMES, "심", 2)
    assert (st, gold) == ("agree", "심액")


def test_resolve_surname_fix_for_surnameless_key():
    # 尙安: KB가 성 없는 키를 이상안으로 링크, 국역은 김상안
    ref = "첨정 김상안은 순창 현감에 제수되어"
    st, gold = ng.resolve([("이상안", "kb"), ("상안", "lib")], "이상안", ref, NAMES, "이", 2)
    assert (st, gold) == ("surname_fix", "김상안")


def test_resolve_lib_reading_overrides_kb_reading():
    # 尹爓: KB 윤염, 라이브러리·국역 윤섬
    st, gold = ng.resolve([("윤염", "kb"), ("윤섬", "lib")], "윤염", "의금부가 올린 윤섬에 대한", NAMES, "윤", 2)
    assert (st, gold) == ("lib", "윤섬")


def test_resolve_given_name_only_scores_given_part():
    st, gold = ng.resolve([("극명", "lib")], None, "이극명이 형을 위해 자복하였으니", NAMES, "극", 2)
    assert (st, gold) == ("given", "극명")


def test_resolve_rare_vs_unreliable_ref_reading():
    ref = "정언 심제가 와서 아뢰기를"
    st, gold = ng.resolve([("침\U0002A5C6", "lib")], None, ref, NAMES, "심", 2)
    assert (st, gold) == ("rare", "심제")
    # 읽을 수 있는 독음이 국역과 다르면 정답으로 쓰지 않는다 (李必崇 → 국역 이필영)
    st, _ = ng.resolve([("이필숭", "lib")], None, "동지사 이필영은 식가 중이고", NAMES, "이", 3)
    assert st == "ref_reading"
    assert not ng.scored({"gold": "이필영", "status": "ref_reading"})


def test_resolve_unresolved_when_nothing_found():
    st, gold = ng.resolve([("심집", "kb")], "심집", "집에 틀어박혀 송구해하며", NAMES, "심", 2)
    assert (st, gold) == ("unresolved", None)


# ── name_metrics ──────────────────────────────────────────────────────────

def _row(doc_id, gold, in_kb=True, injected=None, old=True):
    return {"id": doc_id, "gold": gold, "status": "agree", "in_kb": in_kb,
            "injected": injected, "in_old_gold": old}


def test_relinked_ignores_surname_omitted_reference():
    assert nm.relinked(_row("a", "김상안", injected="이상안"))
    assert not nm.relinked(_row("a", "문회", injected="정문회"))     # 국역이 성을 생략했을 뿐
    assert not nm.relinked(_row("a", "심액", injected="심액"))
    assert not nm.relinked(_row("a", "만월개", in_kb=False))


def test_score_rounds_segments_and_wrong_injected():
    gold = {"s1": [_row("s1", "심액", injected="심액"), _row("s1", "만월개", in_kb=False, old=False)],
            "s2": [_row("s2", "김상안", injected="이상안")]}
    rounds = [{"s1": "제조 심액과 만월개가", "s2": "첨정 이상안은"},
              {"s1": "제조 심액과 만월가가", "s2": "첨정 이상안은"}]
    per, wrong = nm.score_rounds(gold, rounds)
    assert nm.micro(per, per, "in_kb") == (pytest.approx(0.5), 2)      # 심액 1.0 + 김상안 0.0
    assert nm.micro(per, per, "out_kb") == (pytest.approx(0.5), 1)     # 라운드 평균
    assert nm.micro(per, per, "old_gold")[1] == 2
    assert wrong == [pytest.approx(1.0), 1]                           # 틀린 주입 표기를 그대로 씀


def test_bootstrap_ci_is_deterministic_and_brackets_point():
    per = {f"s{i}": {s: [float(i % 2), 1] for s in nm.SEGMENTS} for i in range(40)}
    a = nm.bootstrap_ci(per, "all", iters=500, seed=1)
    b = nm.bootstrap_ci(per, "all", iters=500, seed=1)
    assert a == b and a[0] <= 0.5 <= a[1]
    d = nm.bootstrap_ci(per, "all", iters=500, other=per)
    assert d == (0.0, 0.0)
