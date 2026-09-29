# ADR-018: 포트/어댑터 경계 (Translator / EntityRecognizer / KnowledgeSource)와 그 한계

- 상태: 승인 (M2.5 S2~S4에서 구현) · **개정 (2026-09-21): Translator provider 축 2구현 — `fake` 추가**
- 날짜: 2026-09-01

## 배경

M3은 캐시 무효화를 `kb_version`에, M4는 모델 스위칭에 의존하는데, M2까지의 코드는
`ChatClient`가 생성자에 고정, NER는 구체 클래스, KB는 파일명 하드코딩 + 자유 문자열 버전이었다.
**둘 다 존재하지 않는 추상화 위에 설계된 상태** — 그래서 M2.5가 M3 앞에 삽입됐다 (2026-09-01 계획 개정).

## 선택지

1. **포트 3종, 각각 실구현 2개 이상** — 교체가 실제로 되는 최소 경계
2. 헥사고날 풀세트 (모든 의존을 포트로, 모듈 재편)
3. 추상화 없이 M3·M4에서 조건 분기로 처리

## 결정

**선택지 1.** 경계와 구현:

| 포트 | 경계 | 구현 (전부 실동작) | 교체 스위치 |
|---|---|---|---|
| `Translator` | 프롬프트 in → 텍스트+usage out. 파싱은 도메인 | `GoogleGenAiTranslator` × 레지스트리 3모델 / **`FakeTranslator`**(provider `fake`, 네트워크 0·quota 0) | `GEMINI_MODEL` (어느 어댑터인지는 레지스트리 항목의 `provider`) |
| `EntityRecognizer` | 빈 결과 ≠ 장애 (`NerUnavailableException`) | `HttpOnnxRecognizer` / `RulePatternRecognizer` | `NER_MODE` |
| `KnowledgeSource` | version은 데이터 파생값 (체크섬) | `FileKnowledgeSource`(injo/jeongjo) / `NoOp` | `KB_NAME` / `KB_MODE` |

원칙: **구현이 1개뿐인 인터페이스는 만들지 않는다.** 포트의 목적은 추상화가 아니라 교체다.
전부 라이브 검증됨 (docs/benchmarks.md): 3모델 설정 교체, rule NER E2E(골든셋 recall 26.9% vs ONNX 100%), 정조 KB 기동(`jeongjo-2abe1183`) — 코드 수정 0줄.

**모델 레지스트리**(`sjw.llm.models[]`)가 Translator 포트의 짝이다: rate 버킷 키 · 원장 counterfactual 단가 · M4 티어 매핑의 단일 출처. 모델 지식이 코드에 없어야 "교체 = 설정"이 성립한다.

## 한계 (이 경계가 안 해주는 것)

- ~~`Translator`의 provider 축은 구현 1개다(google-genai).~~ **개정 (2026-09-21):** 두 번째 provider `fake`가 생겼다 — 다른
  벤더가 아니라 **실측이 불가능한 조건을 만드는 장치**다(`FakeProvider`/`FakeTranslator`, M6 mock). 실사용처가 생겨서
  만들었다: M2 수용 기준 ②의 라이브 429를 60RPM×2워커로도 못 만들었고(M6 mock으로 이관), 부하 테스트·CI E2E는
  무료 quota를 태울 수 없다. 가짜는 LLM을 흉내 내지 않고 파이프라인이 LLM에 기대는 **계약만** 지킨다 — Structured
  Output JSON, 확정 인명 반영(→ VERIFIED; `dropNameRate`로 REJECTED 유발), 등장 순서 보존(L2 슬롯팅 성립), usage
  동반, 그리고 429 본문은 실측 로그 형식(quotaId PerMinute/PerDay, "retry in Ns") — `FailureClassifier`·`RetryAfterHint`가
  실 provider와 **같은 분기**를 타는지 워커 테스트로 고정했다. 어느 어댑터인지는 코드가 아니라 레지스트리 `provider`가
  정하고(모르는 값은 기동 실패), 가짜 결과는 **캐시에 적재하지 않는다**(키에 모델이 없어 실 요청이 가짜를 받게 된다).
  OpenAI 등 실제 벤더 교체는 여전히 어댑터 신작이 필요하다 — 그 실사용처는 아직 없다.
- 프롬프트 템플릿은 아직 Java 상수다 (S7에서 외부화). 포트를 갈아도 프롬프트가 모델 특성에 결합돼 있으면 교체 품질은 별개 문제다.
- `EntityRecognizer` 교체는 품질 게이트(S5)와 결합해야 안전하다 — rule 모드는 recall 27%라 무게이트 운영 시 KB 주입 누락이 조용히 늘어난다.

## 버린 대안과 그 이유

- **헥사고날 풀세트(2)**: Redis·Postgres·큐까지 포트화하면 인터페이스 수가 구현 수를 초과한다. §7 마이크로서비스 배제와 같은 논지 — 교체 계획이 없는 경계는 읽기 비용만 낸다.
- **조건 분기(3)**: M4 티어 라우팅이 `if (model == ...)` 사슬이 되고, 모델 추가마다 도메인 코드가 열린다. 레지스트리 1곳 수정으로 끝나는 지금 구조와의 차이가 유지보수 비용의 전부다.

## 재검토 조건

- ~~두 번째 provider가 실제로 필요해지면~~ → `fake`로 시험됐다 (2026-09-21): `Translator` 구현 1개 + `TranslatorFactory` 분기 1곳 + 레지스트리 항목으로 흡수됐고 도메인 코드는 열리지 않았다. 실제 벤더(예: Gemini 무료 정책 종료)도 같은 경로여야 한다.
- BYOK(S6)가 요청 단위 클라이언트를 요구할 때 `LlmConfig`의 싱글턴 조립이 어떻게 바뀌는지 기록할 것 (ADR-020).
