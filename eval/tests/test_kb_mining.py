"""kb_mining.py 단위 테스트 — 행 단위 투표, 신뢰도 등급, 일반 낱말 필터, KB 파일 형식. LLM 0회.

hanja 라이브러리 대신 작은 독음표를 넣는다 (CI 의존성 밖).
"""
import collections
import json

import kb_mining as km

READ = {"沈悅": "침열", "申櫄": "신춘", "李進": "리진", "金瑬": "금류"}
SURNAME = {"沈": "심", "申": "신", "李": "이", "金": "김"}
NAMES = frozenset({"김류"})


def _miner(inv=None, ids=None):
    return km.Miner(inv or {}, ids or {}, SURNAME, NAMES, READ.get)


def _per(surface):
    return {"type": "PER", "surface": surface}


def test_exact_vote_uses_surname_fixed_reading():
    m = _miner()
    m.row({"id": "SJW-A05010010-00100", "original": "沈悅啓曰", "translation": "심열이 아뢰기를"}, [_per("沈悅")])
    b = m.miss["沈悅"]
    assert b["votes"] == collections.Counter({"심열": 1}) and b["exact"]["심열"] == 1


def test_aligned_vote_for_unreadable_reading_and_blocking():
    # 申櫄: 라이브러리 신춘, 국역 신훈 — 같은 성·길이의 유일한 남은 토큰으로 정렬
    m = _miner(inv={"金瑬": ["k1"]}, ids={"k1": {"한글_명": "김류", "활동_시작": 1580}})
    m.row({"id": "SJW-A05010010-00200", "original": "金瑬·申櫄啓曰", "translation": "김류와 신훈이 아뢰기를"},
          [_per("金瑬"), _per("申櫄")])
    assert m.miss["申櫄"]["votes"] == collections.Counter({"신훈": 1})
    assert m.miss["申櫄"]["exact"]["신훈"] == 0
    assert m.pseudo["金瑬"]["gold"]["김류"] == 1          # KB SINGLE은 유사-OOV 평가로 간다
    # 같은 성 토큰이 둘이면 정렬하지 않는다
    m2 = _miner()
    m2.row({"id": "SJW-A05010010-00300", "original": "申櫄啓曰", "translation": "신훈과 신경이 아뢰기를"}, [_per("申櫄")])
    assert not m2.miss["申櫄"]["votes"]


def test_single_char_mentions_are_skipped():
    m = _miner()
    m.row({"id": "SJW-A05010010-00400", "original": "珙", "translation": "이공이"}, [_per("珙")])
    assert not m.miss and not m.pseudo


def test_tier_rules():
    C = collections.Counter
    assert km.tier(C({"심열": 3}), C({"심열": 1}))[0] == "High"
    assert km.tier(C({"신훈": 2}), C())[0] == "Medium"                # aligned 2표는 High 아님
    assert km.tier(C({"신훈": 3}), C())[0] == "High"
    assert km.tier(C({"a": 3, "b": 1}), C({"a": 3}))[0] == "Medium"   # 합의율 0.75 < 0.8
    assert km.tier(C({"a": 1, "b": 1}), C({"a": 1}))[0] == "Low"      # 합의율 0.5 < 0.6
    assert km.tier(C(), C())[0] == "Low"


def test_common_word_filter_only_for_aligned():
    C = collections.Counter
    df = {"이와": 2337, "신훈": 12, "심열": 375}
    assert km.tier(C({"이와": 3}), C(), rows=10, df=df)[0] == "Low"           # 2337 > max(50, 20)
    assert km.tier(C({"신훈": 8}), C(), rows=8, df=df)[0] == "High"           # 12 ≤ 40
    assert km.tier(C({"심열": 11}), C({"심열": 11}), rows=12, df=df)[0] == "High"  # exact는 필터 밖


def test_token_df_counts_rows_once():
    rows = [{"translation": "이와 같으니 이와 같다"}, {"translation": "이와"}]
    assert km.token_df(rows)["이와"] == 2


def test_write_kb_adds_high_only(tmp_path, monkeypatch):
    monkeypatch.setattr(km, "ROOT", tmp_path)
    (tmp_path / "kb").mkdir()
    mined = {"沈悅": {"tier": "High", "reading": "심열", "votes": 11, "agree": 1.0},
             "李進": {"tier": "Medium", "reading": "이진", "votes": 1, "agree": 1.0}}
    added = km.write_kb({"金瑬": ["k1"]}, {"k1": {"한글_명": "김류"}}, mined, "t_aug", "t")
    inv = json.loads((tmp_path / "kb" / "inverted_index_t_aug.json").read_text())
    ids = json.loads((tmp_path / "kb" / "id_lookup_t_aug.json").read_text())
    assert added == 1 and set(inv) == {"金瑬", "沈悅"}
    pid = inv["沈悅"][0]
    assert ids[pid]["한글_명"] == "심열" and ids[pid]["관직_리스트"] == [] and ids[pid]["출처"] == "corpus_mining"
