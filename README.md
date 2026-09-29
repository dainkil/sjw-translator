# 인물 지식베이스(KB)로 LLM 환각을 억제한 《승정원일기》 번역 시스템

> 한문 원문의 인명을 NER과 인물 KB로 확정해 LLM에 주입하고, 번역문에 올바르게 반영됐는지 매 응답 검증하는 번역 서빙 시스템

| 항목 | 내용 |
|---|---|
| 기간 | 2026.03.01 – 2026.09.26 |
| 구성 | 4인 팀 프로젝트 |
| 담당 | KB 구축 · 모델링 · 백엔드 · 모델 서빙 |
| 기술 | Java 21 · Spring Boot · Spring AI · Redis Streams · PostgreSQL · Python · ONNX Runtime · Gemini API · Docker · Kubernetes |

## 핵심 결과

LLM 단독 번역은 드문 한자로 된 조선 인물의 이름을 그럴듯한 다른 음으로 읽는 환각을 보였습니다. 인물 KB로 인명을 확정해 주입한 결과, 동일 조건에서 **인명 정확도가 90.4%에서 97.1%로 향상되고 인명 오류는 10건에서 3건으로 감소**했으며 문장 품질(chrF)은 유지되었습니다. 또한 프롬프트 구성 통제 실험으로 **입력 토큰을 11.2% 줄이면서 번역 품질을 +2.42 개선**했고, NER 모델을 경량화해 **GPU 없이 CPU만으로 서빙**할 수 있게 했습니다.

| 항목 | 기준 | 결과 |
|---|---|---|
| 인명 정확도 (ETS) | LLM 단독 90.4% | **97.1%** (인명 오류 10 → 3건, 3회 반복 동일) |
| 문장 품질 (chrF) | 37.90 | **40.32** (+2.42, 프롬프트 v4 채택) |
| 입력 토큰 | 917.9 tok | **−11.2%** |
| NER 추론 지연 (p50) | PyTorch 17.9ms | **8.3ms** (ONNX INT8, 인명 재현율 100% 유지) |
| NER 모델 크기 | 709MB | **178MB** |
| 대량 번역 재개 | — | 강제 종료 후 재개 시 **중복 LLM 호출 0건** |
| 완역 비용 추정 (잔여 1.5억 자) | — | 유료 기준 **약 93만 원 · 0.7일** |

## 1. 프로젝트 목적

- 《승정원일기》(2억 4,250만 자)는 국역률 37.4%로, 현 속도로는 완역에 2062년까지 소요될 것으로 추산됩니다.
- 기존 한문 번역 모델의 고유명사 번역 정확도는 8.3%에 불과하며, 역사 번역에서 인명 오역은 기록된 사실을 왜곡하는 오류입니다.
- 이에 **언어 변환은 LLM에, 인명 확정은 검증 가능한 KB에** 맡기도록 역할을 분리해, 인명 환각을 구조적으로 억제하는 번역 시스템을 목표로 했습니다.

## 2. 데이터와 제약

| 데이터 | 규모 | 용도 |
|---|---|---|
| 병렬 코퍼스 (한문 원문 ↔ 전문가 국역, 인조 연간) | 62,476쌍 | 통계 · 캐시/라우팅 시뮬레이션 |
| 인물 KB (한자 표기 → 한글명 · 활동 시기 · 관직) | 2,690명 · 역색인 9,403키 | 인명 확정 |
| 골든셋 / 인명 정답지 | 300문장 / 383명 | 품질 평가 (국사편찬위원회 인물 어노테이션 기반 독립 정답) |

- **전처리:** 코퍼스의 11.24%에서 원문과 번역이 한 행씩 밀린 정렬 결함을 검출해 평가에서 제외했습니다.
- **라벨:** NER 학습 데이터는 신뢰도 0.9 이상의 자동 생성 라벨(silver label)이므로, NER 결과를 정형문 패턴 매칭과 결합해 보완했습니다.
- **편향·제약:** 인조 연간(1623–1649) 데이터에 한정되며, 전체 인명 멘션의 36.5%가 KB에 없습니다. 무료 API 한도 내 운영(모델별 일일 호출 제한)을 전제로 설계했습니다.

## 3. 담당 역할

| 영역 | 내용 |
|---|---|
| **KB 구축** | 인조 연간 인물 2,690명 KB와 표기 변형 역색인 구축, 역색인 → 활동 시기 → 관직 3단계 엔티티 링킹 |
| **모델링** | NER 도메인 적응 및 파인튜닝, 프롬프트 설계와 구성 요소별 통제 실험, 인명 정확도 지표(ETS) 설계 |
| **백엔드** | Spring Boot API/Worker, Redis Streams 비동기 큐, 체크포인트 기반 배치 재개, 실패 유형 분류와 적응형 호출 속도 제어, 번역 캐시, 품질 게이트 |
| **모델 서빙** | NER ONNX INT8 경량화 및 CPU 서빙, Docker Compose 전 스택, 공개 서빙용 접근 제어(API 키 · 사용자 LLM 키), Kubernetes 배포 구성 |

## 4. 접근 방법

```
원문 → NER(인명 위치) → KB 링킹(인물 확정) → 프롬프트에 인명 주입 → LLM 번역 → 품질 게이트(인명 반영 검증)
```

- **기준 모델 대비 비교:** 모든 개선은 명시적 기준 모델과 비교했습니다. KB 효과는 LLM 단독, 프롬프트는 기존 생산 프롬프트, NER 경량화는 PyTorch 원본을 기준으로 했습니다.
- **실험 설계:** LLM 출력의 비결정성을 고려해 판정 기준(chrF −2.0 이내, 인명 정확도 하락 없음)을 실험 전에 확정하고, 조건별 3회 반복 중앙값으로 판정했습니다. 모델 · 평가셋 · 캐시 조건을 고정해 비교 변수만 달리했습니다.
- **품질 게이트:** 확정된 인명이 번역문에 반영됐는지 추가 LLM 호출 없이 판정해 모든 응답에 VERIFIED / DEGRADED / REJECTED 등급을 부여합니다(오탐률 3.9%).

| 기술 선택 | 이유 |
|---|---|
| 벡터 DB/RAG 대신 **결정론적 역색인** | 유사도 검색은 이름 경계 · 동음이의 모호성을 다시 유발함. 인명 확정에는 정확 일치가 필요 |
| NER **ONNX INT8 + CPU** | 전체 지연의 99.7% 이상이 외부 LLM으로, GPU 도입 효과가 없음 |
| **Redis Streams 비동기 큐** | 외부 LLM 지연에 요청 스레드가 묶이지 않도록 분리 |

## 5. 트러블슈팅과 한계

| 문제 | 해결 |
|---|---|
| 동일한 429 응답이 분당 한도 · 일일 한도 · 월 지출 상한의 세 원인으로 발생 | 응답 본문 기반 실패 분류로 재시도 / 배치 일시정지 / 즉시 격리를 구분 |
| 외부 LLM 무응답으로 작업이 장시간 정체 | 하드 타임아웃을 도입해 재시도 · 서킷 브레이커 경로로 편입 |
| 코퍼스 정렬 결함으로 품질 측정 왜곡 | 결함 검출 규칙으로 11.24%를 제외해 측정 정합성 확보 |

- **한계:** KB 미등재 인물(인명 멘션의 36.5%)은 주입 효과를 받지 못하므로 KB 확장이 다음 과제입니다. 통제 실험 표본은 60문장 · 인명 104명입니다.

## 실행 방법

```bash
cp .env.example .env                      # GEMINI_API_KEY 설정
cd ner-server && uv sync && uv run python scripts/export_onnx.py && cd ..   # NER 모델 변환 (최초 1회)
docker compose -f deploy/docker-compose.yml up -d --build

curl -s -X POST localhost:8080/api/v1/translations/sync \
  -H 'Content-Type: application/json' -d '{"text":"以金瑬爲承旨","year":1623}'
```

API 문서(Swagger UI)는 <http://localhost:8080/> 에서 열린다.

## 공개 엔드포인트

SKALA EKS `skala-gj4`에 배포한다 (ADR-022 개정). 주소: `https://skala-gj4-sjw.skala-gj.com`
— 루트를 열면 Swagger UI다. 가용성은 교육 과정 기간에 한정된다.

공개 모드에서는 키가 두 개다. 축이 다르다:

| 헤더 | 무엇 | 없으면 |
|---|---|---|
| `X-Api-Key` | 발급받은 테넌트 키. 일일 호출 상한이 여기 걸린다 | 401 `API_KEY_REQUIRED` |
| `X-Llm-Key` | 본인 Gemini API 키. **저장·로깅하지 않는다** | 403 `BYOK_REQUIRED` (캐시 미스일 때만) |

**본인 키를 받는 이유:** 운영자 무료 quota가 하루 20회(RPD 20 실측)라 공개하면 한 사람이 오전에
소진한다. 번역 비용은 요청자 키로 나간다 (ADR-020).

```bash
# 캐시에 있는 문장 — X-Api-Key만으로 응답한다 (LLM을 부르지 않으므로)
curl -s -X POST https://skala-gj4-sjw.skala-gj.com/api/v1/translations/sync \
  -H 'X-Api-Key: sjw_...' \
  -H 'Content-Type: application/json' -d '{"text":"以金瑬爲承旨","year":1623}'

# 캐시에 없는 문장 — 본인 Gemini 키가 필요하다
curl -s -X POST https://skala-gj4-sjw.skala-gj.com/api/v1/translations/sync \
  -H 'X-Api-Key: sjw_...' -H 'X-Llm-Key: <본인 Gemini 키>' \
  -H 'Content-Type: application/json' -d '{"text":"上曰予不敏","year":1623}'
```

비동기 잡·배치 생성은 운영자 키로 LLM을 호출하므로 `operator_access` 테넌트만 쓸 수 있다
(없으면 403 `OPERATOR_ACCESS_REQUIRED`). 테넌트 키 발급은 `deploy/k8s/issue-key.sh`.

### 바로 해보기 (Gemini 키 불필요)

골든셋 앞 5문장은 미리 번역해 캐시에 넣어 두었다(`deploy/preheat-cache.py`). 캐시 히트는
BYOK 없이 응답하므로 아래를 그대로 붙여넣으면 실제 출력이 나온다.

```bash
curl -s -X POST https://skala-gj4-sjw.skala-gj.com/api/v1/translations/sync \
  -H 'X-Api-Key: <데모 키>' \
  -H 'Content-Type: application/json' \
  -d '{"text":"○ 吏曹參議鄭太和上疏。疏辭缺, 批答缺","year":1638}'
```

```json
{
  "translatedText": "이조참의 정태화가 상소하였다. 소사(상소의 내용)는 빠졌고, 비답(임금의 답변)도 빠졌다.",
  "entities": [
    { "surface": "鄭太和", "type": "PER", "kbId": "M_0005933",
      "resolvedName": "정태화", "confidence": 0.9948, "linkStage": "SINGLE" }
  ],
  "meta": {
    "model": "gemini-3.1-flash-lite", "kbVersion": "injo-fffabc78",
    "promptVersion": "main-4a1cb192", "cacheHit": "L1_EXACT",
    "latencyMs": { "cache": 1, "total": 1 }
  }
}
```

`entities`가 KB 주입의 실물이다 — `鄭太和`를 인물 `M_0005933`(정태화)로 확정했기 때문에 LLM이
이름을 지어내지 않는다. 캐시 미스일 때의 지연 분해는 `{"ner":197,"link":0,"prompt":19,"llm":1802}`로,
외부 LLM 호출은 전체의 일부다.

데모 키는 일일 50회 상한이며 캐시 히트만 가능하다 — 캐시에 없는 문장은 요청자의 `X-Llm-Key`가
필요하므로 이 키만으로는 운영자 quota를 소모시킬 수 없다.

## 문서

[설계서](PROJECT_PLAN.md) · [설계 결정 기록(ADR)](docs/adr/) · [재현성 정책](docs/adr/024-artifact-reproducibility.md) · [실측 기록](docs/benchmarks.md) · [비용 모델](docs/cost-model.md) · [트러블슈팅](docs/troubleshooting.md) · [연구 방법론](research/README.md)
