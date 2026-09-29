#!/usr/bin/env python3
"""SillokBERT-NER → ONNX fp32 → INT8 동적 양자화 (ADR-003).

산출물 (ner-server/models/, gitignore 대상):
  models/onnx-fp32/  — optimum export 결과 + 토크나이저
  models/onnx-int8/  — quantize_dynamic 결과 (서빙 기본)

재현성 (ADR-024): 업스트림 리비전을 고정한다. 고정하지 않으면 저자가 가중치를 갱신했을 때
같은 명령이 다른 모델을 만들고, 그 순간 캐시 키(`model_version`)와 벤치마크가 조용히 어긋난다.
아래 REVISION에서 만든 INT8 산출물의 지문은 `onnx-d66923a5`다 (model.onnx + config.json SHA-256 앞 8자리).
export 환경 자체는 `ner-server/uv.lock`이 고정한다 — `uv sync` 후 실행할 것.
"""
from pathlib import Path

MODEL_ID = "ddokbaro/SillokBert-NER"
# 2025-11-29 기준 main. 갱신할 때는 산출 지문(model_version)이 바뀌므로 CACHE_EPOCH도 함께 본다.
REVISION = "6b1b724cbb688032906264517332dbe5003284a3"
EXPECTED_VERSION = "onnx-d66923a5"  # 이 리비전 + uv.lock 환경의 산출물 지문
BASE = Path(__file__).resolve().parent.parent / "models"


def main():
    from optimum.onnxruntime import ORTModelForTokenClassification
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from transformers import AutoTokenizer

    fp32_dir = BASE / "onnx-fp32"
    int8_dir = BASE / "onnx-int8"
    int8_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/3] {MODEL_ID}@{REVISION[:8]} 다운로드 + ONNX export → {fp32_dir}")
    model = ORTModelForTokenClassification.from_pretrained(MODEL_ID, export=True, revision=REVISION)
    model.save_pretrained(fp32_dir)
    tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=REVISION)
    tok.save_pretrained(fp32_dir)

    print(f"[2/3] INT8 동적 양자화 → {int8_dir}")
    quantize_dynamic(fp32_dir / "model.onnx", int8_dir / "model.onnx", weight_type=QuantType.QInt8)
    # 토크나이저·config는 fp32와 동일
    for f in fp32_dir.iterdir():
        if f.name != "model.onnx":
            (int8_dir / f.name).write_bytes(f.read_bytes())

    fp32_mb = (fp32_dir / "model.onnx").stat().st_size / 1e6
    int8_mb = (int8_dir / "model.onnx").stat().st_size / 1e6
    print(f"[3/3] 완료. 모델 크기: fp32 {fp32_mb:.0f}MB → int8 {int8_mb:.0f}MB")

    # 산출 지문 검증 (ADR-024) — 서빙이 쓰는 것과 같은 계산이다 (app/ner.py::_model_version).
    # 다르면 캐시 키가 갈라지고 벤치마크 수치가 이 산출물에 대해 성립하지 않는다.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.ner import _model_version
    got = _model_version(int8_dir)
    if got == EXPECTED_VERSION:
        print(f"      지문 {got} — 기록된 산출물과 일치한다.")
    else:
        print(f"      ! 지문 {got} ≠ 기록된 {EXPECTED_VERSION}")
        print("      업스트림 리비전·uv.lock·onnxruntime 버전 중 무엇이 달라졌는지 확인하고,")
        print("      의도한 교체라면 EXPECTED_VERSION과 docs/benchmarks.md를 함께 갱신한다.")
        sys.exit(1)


if __name__ == "__main__":
    main()
