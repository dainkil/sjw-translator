# NER 서버 이미지 (M2.5-S8). INT8 모델(174MB)을 이미지에 굽는다 — 기동 시 700MB 다운로드 구조로는
# 무료 인스턴스에서 컨테이너화가 성립하지 않는다 (계획서 §10 M2.5-(6), §15.2).
# 전제: ner-server/models/onnx-int8 이 존재해야 한다 (최초 1회: uv run python scripts/export_onnx.py).
# 서빙 런타임 의존성만 설치한다 — torch/optimum은 export 전용이라 이미지에 넣지 않는다 (pyproject 주석 참조).
FROM python:3.11-slim
WORKDIR /app
# 버전 고정 (ADR-024). `>=`로 두면 다시 빌드할 때마다 다른 런타임이 들어온다 — 2026-09-29 빌드는
# transformers를 4.x가 아니라 5.17.0으로 받았다(메이저 상승). 아래는 그날 실제로 배포된 조합이고,
# 벤치마크(docs/benchmarks.md)는 이 조합에 대해 성립한다. 올릴 때는 NER recall을 다시 잰다.
RUN pip install --no-cache-dir \
    "fastapi==0.141.1" "uvicorn==0.54.0" "onnxruntime==1.30.0" \
    "transformers==5.17.0" "tokenizers==0.23.2" "numpy==2.4.6"
COPY ner-server/app app
COPY ner-server/models/onnx-int8 models/onnx-int8
ENV NER_MODEL_DIR=/app/models/onnx-int8
EXPOSE 8100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
