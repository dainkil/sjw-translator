"""leakage.py · eval_exclusions.py · name_ablation.py(일정·판정) 단위 테스트. LLM 0회, DB·Docker 불필요.

픽스처는 실제 사례를 줄인 것이다 — 權大進 행(부분 밀림, 2026-10-04 골든셋에서 검출).
"""
import json

import pytest

import eval_exclusions as ex
import leakage as lk
import name_ablation as na
import name_metrics as nm


def _row(i, date, original="", translation=""):
    return {"id": i, "date": date, "original": original, "translation": translation}


CORPUS = [
    _row("SJW-A09020070-00200", "1631-02-07", "○ 慈殿, 藥房問安。", "죄인 권대진과 그 아들 권계와 권락이 들어왔으므로"),
    _row("SJW-A09020070-00300", "1631-02-07", "○ 罪人權大進及其子繼·絡入來。傳曰, 禁府…", "전교하기를,“금부와 추국청에…"),
    _row("SJW-A09030010-00100", "1631-03-01", "○ 沈詻啓曰", "심액이 아뢰기를"),
    _row("SJW-A22010010-00100", "1644-01-01", "○ 有政。", "정사가 있었다."),
]


def test_filter_rows_drops_eval_ids_and_same_date_rows():
    ev = lk.EvalSet(ids={"SJW-A09020070-00300"})
    kept = [r["id"] for r in lk.filter_rows(CORPUS, ev)]
    assert "SJW-A09020070-00300" not in kept         # 규칙 1: 평가 문장
    assert "SJW-A09020070-00200" not in kept         # 규칙 2: 같은 날짜
    assert kept == ["SJW-A09030010-00100", "SJW-A22010010-00100"]
    lk.assert_no_leak(lk.filter_rows(CORPUS, ev), ev)
    with pytest.raises(AssertionError):
        lk.assert_no_leak(CORPUS, ev)


def test_temporal_split_keeps_train_years_only():
    ev = lk.EvalSet(ids={"SJW-A09020070-00300", "SJW-A22099999-00100"})
    kept = [r["id"] for r in lk.filter_rows(CORPUS, ev, train_years=range(1, 21))]
    assert kept == ["SJW-A09030010-00100"]           # 22년 행은 학습에서 빠진다
    assert lk.test_ids(ev, range(21, 28)) == {"SJW-A22099999-00100"}
    assert lk.reign_year("SJW-A09020070-00300") == 9 and lk.reign_year("bad") is None


def test_kb_name_keys_keeps_full_unambiguous_names_only():
    inv = {"沈詻": ["a"], "詻": ["a"], "金浚": ["b", "c"], "李球": ["d", "e"]}
    ids = {"a": {"한글_명": "심액"}, "b": {"한글_명": "김준"}, "c": {"한글_명": "김준"},
           "d": {"한글_명": "이구"}, "e": {"한글_명": "이추"}}
    keys = lk.kb_name_keys(inv, ids)
    assert keys == {"沈詻": "심액", "金浚": "김준"}   # 성 없는 키(詻)·독음이 갈리는 동명이인 키(李球)는 뺀다


def test_misaligned_rows_flags_name_found_in_neighbor_only():
    rows = [_row("r0", "d", "○ 有政。", "형조 판서 심액이 아뢰기를"),
            _row("r1", "d", "○ 判書沈詻啓曰", "정사가 있었다."),          # 원문의 심액이 이웃 번역에 가 있다
            _row("r2", "d", "○ 沈詻啓曰", "심액이 아뢰기를")]            # 정상
    assert lk.misaligned_rows(rows, {"沈詻": "심액"}) == {"r1"}


def test_exclusions_detect_partial_misalignment():
    gold_rows = [{"id": "SJW-A09020070-00300", "hanja": "權大進", "status": "unresolved",
                  "injected": None, "kb_names": []}]
    found = ex.detect(gold_rows, CORPUS, {"權大進": "권대진"}.get, {"權": "권"})
    assert found["SJW-A09020070-00300"]["neighbor_offset"] == -1


def test_load_exclusions_missing_file_means_none(tmp_path):
    assert ex.load_exclusions(tmp_path / "none.json") == set()
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"excluded": ["a", "b"]}))
    assert ex.load_exclusions(p) == {"a", "b"}


def test_name_metrics_lower_bound_counts_unscored_as_wrong(tmp_path):
    rows = [{"id": "s1", "gold": "심액", "status": "agree", "in_kb": True, "injected": "심액", "in_old_gold": True},
            {"id": "s1", "gold": None, "status": "unresolved", "in_kb": False, "injected": None, "in_old_gold": False},
            {"id": "s2", "gold": None, "status": "ref_reading", "in_kb": False, "injected": None, "in_old_gold": False},
            {"id": "bad", "gold": "권대진", "status": "lib", "in_kb": False, "injected": None, "in_old_gold": False}]
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"names": rows}, ensure_ascii=False))
    gold, unscored = nm.load_gold(p, exclude={"bad"})
    assert "bad" not in gold and "bad" not in unscored
    per, _ = nm.score_rounds(gold, [{"s1": "심액이", "s2": "아무 말"}], unscored)
    assert set(per) == {"s1", "s2"}                                 # 제외 이름만 있는 문장도 하한 분모에 든다
    assert nm.micro(per, per, "all") == (pytest.approx(1.0), 1)
    assert nm.micro_lower(per, per, "all") == pytest.approx(1 / 3)
    assert nm.micro_lower(per, per, "out_kb") == pytest.approx(0.0)


def test_schedule_is_counterbalanced():
    s = [(r["round"], r["arm"]) for r in na.schedule(3)]
    assert s == [(1, "kb"), (1, "nokb"), (2, "nokb"), (2, "kb"), (3, "kb"), (3, "nokb")]


def test_verdict_applies_preregistered_rules():
    def seg(acc, names):
        return {"acc": acc, "names": names}
    names = {"deltas": [{"in_kb": {"ci95": [0.02, 0.10]}, "out_kb": {"ci95": [-0.03, 0.04]}}],
             "arms": [{"arm": "kb", "in_kb": seg(0.99, 100), "out_kb": seg(0.70, 30)}]}
    chrf = {"deltas": {"kb-nokb": {"ci95": [-1.5, 0.8]}}}
    v = na.verdict(names, chrf)
    assert v["H1_in_kb_gain"] and v["H2_out_kb_no_gain"] and v["H3_chrf_noninferior"]
    assert v["H4_bottleneck_share_out_kb"] == pytest.approx(9 / 10) and v["H4_bottleneck_moved"]
    chrf_bad = {"deltas": {"kb-nokb": {"ci95": [-2.4, 0.1]}}}
    assert not na.verdict(names, chrf_bad)["H3_chrf_noninferior"]
