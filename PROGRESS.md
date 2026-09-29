# 개발 기록

> 마일스톤별 진행과 실측을 시간순으로 남긴 기록이다. 무엇을 재고 무엇을 버렸는지가 여기 있다.
> 전체 설계·수용 기준은 [PROJECT_PLAN.md](PROJECT_PLAN.md), 결정 기록은 [docs/adr/](docs/adr/).

## 1. 로드맵 현황

| 마일스톤 | 상태 | 증거 |
|---|---|---|
| M0 리프레이밍 + 비용/시간 모델 | ✅ 완료 | `docs/cost-model.md` (파라미터화, 토큰 실측 반영) |
| M1 단건 동기 경로 | ✅ 완료 | `docs/benchmarks.md` — LLM이 p95의 99.7~99.9% 확정 |
| M2 비동기 + 배치 엔진 | ✅ 완료 (2026-09-01) | 아래 §3 — 수용 기준 3종 증거 상태 포함 |
| M2.5 교체 가능성 + 품질 게이트 + 배포 기반 | ✅ 완료 (2026-09-01) | §5.1 수용 기준 판정 — 포트 3종·품질 게이트·BYOK·compose 전 스택 |
| M3 템플릿 슬롯 캐싱 | ✅ **완료 (2026-09-11)** | S1 슬롯 로직 / S2 L1 라이브 / S3 L2 라이브 / S4 히트율 **17.36% 실측** / S5 비열등 **통과** — 수용 기준 판정은 §5.1.2 |
| M3.5 평가 신뢰도 + 프롬프트 실측 (삽입) | ✅ **완료 (2026-09-24)** | S1: 독립 정답지 ETS 도입(종전 99.09%는 순환 지표). S2: 서빙 프롬프트 6변형 ablation → **v4 채택**(chrF +2.42, 토큰 −11.2%, 인명 +1) — §5.4. KB 주입 A/B: **LLM 단독 ETS 0.9038 → 파이프라인 0.9712** |
| M4 NER 비용 라우팅 | 진행 중 (S1 완료, 2026-09-11 · S2 ① gemma-4 탐침 2026-09-21: 측정 불가 — 출력 형식 계약 위반) | 신호 분포 실측 → **계획서 §5.1 표가 성립하지 않음을 확인**(T2 29.86%가 quota의 8배, 완역 124→926일). 난이도/배정 분리로 재설계, ADR-010 |
| **공개 서빙 1차** (최소 하드닝 + skala-gj 배포) | ✅ **배포 완료 (2026-09-29)** — `https://skala-gj4-sjw.skala-gj.com` 가동 | §5.0 — BYOK 필수, SKALA EKS `skala-gj4`, `https://skala-gj4-sjw.skala-gj.com`. 배포 전 검증에서 타임아웃 오분류 1건 발견·수정 |
| M5 비용 SLI + 예산 서킷브레이커 | 예정 | |
| M6 배포/CI/부하테스트 | 선행 1건 완료 (2026-09-21) | **가짜 LLM provider** — 429·503·REJECTED를 quota 0으로 유발, M2 수용 기준 ② 닫힘 — §5.5 |

## 2. 지금까지의 핵심 실측 (전부 재현 커맨드 포함, docs/benchmarks.md)

- **LLM이 전체 p95의 99.7~99.9%** — 모든 아키텍처 결정의 근거 (§2.1 실측 완료)
- NER ONNX INT8: p50 8.3ms, PER recall 100%, 178MB (GPU 배제 근거, ADR-003)
- 입력 토큰의 ~85%가 프롬프트 오버헤드 (820 중 700) → **v4 채택으로 597** (M3.5-S2) — 다음 레버는 컨텍스트 캐싱
- 완역 비용: 유료 $803≈112만 원/0.9일 (캐시 17.4% 반영 $663, v4 프롬프트 기준) vs **무료 단일 모델(RPD 20 실측) 173년**
- 무료 quota는 **모델별 독립** → 티어 라우팅(M4) = quota 풀링
- 무료 모델 현황: `gemini-3.5-flash`(T1, RPD 20 — 아껴 쓸 것) / `gemini-3.1-flash-lite`(개발 기본, p50 3.0s) / `gemma-4-26b-a4b-it`(혼잡 변동 큼) / `gemini-2.5-flash`(신규 프로젝트 단종 — 404)
- 배치 처리량 **23 jobs/min** (flash-lite) — 워커 1대와 2대가 동일 (23.4 vs 23.1), 중복 호출 0건 → D11(단일 워커)의 실측 근거
- 골든셋 인명 정확도 **ETS 98.21%** (독립 정답지, n=70) — 종전 99.09%는 주입 반영률(순환)이라 게이트 지표로 강등 (M3.5-S1)
- **KB 주입 효과: LLM 단독 ETS 0.9038 → 파이프라인 0.9712** (층화 60·104명, 인명 오류 10 → 3, chrF 동일) — 프로젝트 핵심 주장의 첫 정량 근거

## 3. M2 완료분 (S1~S6)

| 단계 | 내용 | 실증 |
|---|---|---|
| S1 | 멀티모듈(common/api/worker) + docker-compose(Redis·Postgres) + §8.2 스키마 4테이블 | 테스트 7건 |
| S2 | Redis Streams 큐: 202 발행→Consumer Group 소비→XACK, 멱등 키, cost_ledger 원장, stale claim 회수 | 비동기 E2E |
| S3 | 배치 엔진: BatchPump(커서 발행), pause/resume, 예산 사전 검증(422) | **`deploy/demo-resume.sh`: kill -9 → 재개 → 12/12, job당 호출 최대 1회 (중복 0건 증명)** |
| S4 | 실패 분류 9종(429를 SPEND_CAP/QUOTA_DAILY/RATE_LIMITED로 3분할), DLQ 적재, Resilience4j retry+서킷, 파싱 실패 시에도 토큰 원장 기록, QUOTA_DAILY 시 배치 자동 일시정지 | 분류기 테스트 8건 + DLQ 실동작 데모(404 유발) |
| S5 | 적응형 rate control: Redis Lua 토큰버킷(멀티워커 공유) + AIMD(429→절반, 연속 성공→+1 RPM), 고정 페이싱 제거 | `deploy/demo-rate-control.sh`, 리미터 테스트 (ADR-017) |
| S6 | 단건 SSE 스트리밍(entities→token→done), 배치 처리량 실측, rate 데모 판정 | benchmarks.md M2 섹션 — 처리량 23 jobs/min, 워커 2배에도 동일, 중복 0건 |

**M2 수용 기준 증거 상태:** ① 강제 종료→재개 중복 0건 = `demo-resume.sh` 라이브 증명 ✅ ② 429→하향→상향 = ~~라이브 유발 실패~~ (60 RPM·2워커에서도 429 0건 — provider 유효 한도 미달) **→ 2026-09-21 가짜 provider로 유발 성공: 429 2건, rate 39→19·23→11 RPM, 완주 (§5.5)**, AIMD 자체는 실측 429 원문 기반 단위 테스트로 검증, 강제 429 라이브 검증은 M6 mock provider로 이관 ③ DLQ 분류 적재 = 404 유발 라이브 증명 ✅. 상세는 benchmarks.md.

## 4. 실행

로컬 실행과 공개 엔드포인트 사용법은 [README](README.md#실행-방법)에 있다.
전 스택은 `docker compose -f deploy/docker-compose.yml up -d --build` 한 줄이고,
NER 모델만 최초 1회 변환이 필요하다 (`ner-server/scripts/export_onnx.py`, 재현성은 ADR-024).

대용량 원천 데이터(`malmoi/` 180MB — 병렬 코퍼스 등)는 저장소에 없다. 서빙에는 불필요하며,
필요한 KB·골든셋은 `kb/`·`eval/`에 있다. 소재는 [docs/asset-inventory.md](docs/asset-inventory.md).

## 5. 마일스톤별 기록

> **2026-09-01 계획 개정.** 계획 검토 결과 M3·M4가 둘 다 아직 없는 추상화(모델 레지스트리, 검증된 `kb_version`, 품질 게이트)에 의존한다는 점이 확인되어, **M2.5를 M3 앞에 삽입**했다. 개정 전문은 [PROJECT_PLAN.md](PROJECT_PLAN.md) §5.4 / §10 / §15.

### 5.0 공개 서빙 1차 — 최소 하드닝 + skala-gj 배포 (계획 확정 2026-09-23 · 배포 완료 2026-09-29)

> **상태 (2026-09-28).** Step 1(하드닝) · Step 3(매니페스트) · Step 4(문서·ADR) 완료.
> Step 2(이미지)는 스크립트만 작성됨 — **Harbor 푸시와 클러스터 적용이 남은 전부다** (자격증명 필요).
>
> 배포 전 검증에서 확인한 것 (로컬 compose + 가짜 provider):
> - `FAKE_RPM=5` 연타 → 5회 후 429 + `Retry-After` 감소 ✓
> - 하드 타임아웃 → 504 `LLM_TIMEOUT`, 지연 70초를 5.2초에 절단, RUNNING 고착 0건 ✓
> - 공개 모드 401 / 403 `BYOK_REQUIRED` / 401 `UNKNOWN_API_KEY` / 403 `OPERATOR_ACCESS_REQUIRED` ✓
> - `/actuator/prometheus` 200, 앱 포트(8080)로는 actuator 404 ✓
> - readiness에 db·redis 포함 — redis 중단 시 readiness 503 / liveness 200 ✓
>
> **발견·수정한 결함:** `FailureClassifier`가 `contains("500")`으로 상태 코드를 찾아,
> 타임아웃 값 `5000ms`·`15000ms`의 숫자에 걸려 504 대신 502로 오분류했다(7000ms에서는 정상).
> 상태 코드를 단어 경계(`\b500\b`)로 찾도록 고치고 회귀 테스트 2개를 추가했다.
> §5.0 1-3의 "넘기면 TIMEOUT" 수용 기준이 실제로는 깨져 있었다.
>
> 추가한 것: OpenAPI/Swagger UI(`/`가 문서로 리다이렉트 — 공개 주소 첫 화면이 404이던 문제),
> Ingress 경로 `/api` → `/`, `.env.example`에 DB 비밀번호 항목 문서화,
> `argocd-app.yaml`에 "kubectl로는 적용 불가" 실측 주석,
> **ADR-024(산출물 재현성)** — 점검해 보니 "같은 환경이면 똑같이 만들 수 있다"가 성립하지 않았다:
> HF 모델 리비전 미고정, 서빙 의존성이 `>=`(2026-09-29 빌드가 transformers를 4.x가 아닌 5.17.0으로
> 받았다), `uv.lock`은 export 환경만 고정. 층을 나눠 정리했다 — 층 1은 Harbor 이미지 다이제스트
> (강한 보장), 층 2는 소스 재빌드(리비전·핀·지문 검증). `export_onnx.py`가 산출 지문을 검증하고
> 불일치면 exit 1 한다. 고정 후 재빌드 실측: `onnx-d66923a5` 동일.
> `kustomization.yaml`의 이미지 자리표시자를 `latest` → `unset-run-build-push-sh`
> (`latest`는 Harbor에 없는데도 정상 태그처럼 보여 ImagePullBackOff 원인이 안 드러난다).
>
> **데모 경로 (`deploy/preheat-cache.py`).** 공개 모드에서 캐시 미스는 403이라, 평가자에게
> Gemini 키 발급을 요구하면 그 시점에 이탈한다. 반면 캐시 히트는 `X-Api-Key`만으로 200이다
> (BYOK 검사가 캐시 조회 뒤에 있으므로 — ADR-020 개정). 골든셋 앞쪽 몇 문장을 미리 적재해
> 키 하나로 시연 가능하게 한다. L1 TTL 30일. 실측(공개 모드 로컬): 적재된 문장 →
> `HIT L1_EXACT` 200(BYOK 없이), 미적재 문장 → 403 `BYOK_REQUIRED`.
> `--dry-run`으로 대상 확인, `--verify`로 예열 상태 점검(미스·실패 0이어야 시연 가능).
>
> **배포 실측 (2026-09-29).** 파드 5개 Running, Flyway V1~V5 성공, cert-manager가 `sjw-tls` 발급
> (Ingress 경로를 `/`로 넓힌 것이 ACME challenge를 막지 않았다). 공개 URL에서:
> 키 없음 401 / 미등록 키 401 / 미캐시 403 `BYOK_REQUIRED` / 비운영자 비동기 403
> `OPERATOR_ACCESS_REQUIRED` / `/actuator` 404(관리 포트 미노출). 예열 5문장은 데모 키만으로
> `HIT L1_EXACT` 200, 지연 **1ms**(첫 호출 2,018ms 대비).
>
> **배포 중 드러난 것 둘.**
> ① 공유 네임스페이스 이름 충돌 — `svc/postgres`가 이미 rai 프로젝트 소유여서 apply가 거부됐다.
> 덮어쓰이진 않았으나 그 이름은 엔드포인트 없는 남의 서비스로 해석되므로, 방치했다면 api가
> 자기 DB에 닿지 못하는 조용한 오작동이 됐다. `namePrefix: sjw-`로 전부 접두했다.
> ② 예열은 배치 경로여야 한다 — 동기 경로는 `byok == null`일 때만 캐시에 적재한다
> ("요청자가 자기 키로 산 번역을 다른 테넌트가 공짜로 받아가는 모양이 된다").
> BYOK로 예열을 시도해 LLM 5회를 쓰고 캐시는 비었다. 워커는 운영자 키로 번역하고 적재하므로
> `preheat-cache.py`를 배치 생성 + 완료 대기로 다시 썼다.

**결정 (사용자, 2026-09-23):** 추가 실험을 기다리지 않고 지금 품질(M3.5-S2 v4 프롬프트)로 외부 사용자에게 서빙한다.
계획서 §13("공개 호스팅은 M6 이후")보다 앞당긴 것이며, 범위를 공개에 꼭 필요한 최소분으로 좁혔다.
- **LLM 비용: BYOK 필수** — 사용자 Gemini 키(`X-Llm-Key`). 운영자 비용 0 유지 (ADR-016·020, 계획서 §15.3과 일치).
- **호스팅: SKALA 실습 클러스터 skala-gj (EKS, ap-northeast-2), 네임스페이스 `skala-gj4`** — ADR-022의 Oracle A1을 대체.
  환경(과정 안내 PDF 기준): Harbor 레지스트리 `harbor.skala-gj.com/skala-gj4/*`, ArgoCD AppProject `skala-gj4`(GitOps),
  nginx Ingress + `*.skala-gj.com` 와일드카드 DNS, cert-manager `letsencrypt-prod`, StorageClass `ebs-sc`(기본, RWO),
  **노드 x86_64**, 권한은 네임스페이스 범위만. **자격증명(AWS 키·Harbor·ArgoCD 비밀번호)은 PDF에만 있다 — 저장소·문서·
  명령 기록에 절대 남기지 않는다.**
- **노출:** `https://skala-gj4-sjw.skala-gj.com` (Ingress 하나). HTTP 우선도 검토했으나 이 환경에선 TLS가
  어노테이션 한 줄이고 BYOK 키가 헤더로 오가므로 처음부터 HTTPS (발급 실패 시 HTTP로 먼저 열고 이어서 처리).
- **범위 밖(이후):** M5의 3단계 예산 저하·Grafana 전 지표·ADR-011, 동기 경로 원장 기록, CI 이미지 푸시.
- **주의:** 실습 클러스터라 노드가 내려가 있을 수 있고 과정 종료 후 유지 보장이 없다 → 상시 서비스가 아니라
  **"공개 데모"**로 문서화. compose 셀프호스트 경로는 그대로 유지.

**왜 하드닝이 먼저인가 — 지금 스택은 "나 혼자 로컬" 전제다 (2026-09-23 조사):**
키 없는 요청이 default 테넌트로 통과(→ 운영자 무료 quota를 누구나 소진) / batch pause·resume에 소유권 검사 없음 /
동기 경로 429가 500으로 나감 / LLM 하드 타임아웃 없음(26분 RUNNING 선례) / `/actuator/prometheus` 404(레지스트리 없음) /
redis 무비밀번호·postgres `sjw/sjw` 하드코딩·전 포트 호스트 노출 / K8s 매니페스트 없음.

**0. 접속 준비 (자격증명은 저장소·기록에 남기지 않는다)**
- `aws configure --profile skala-gj4` → `aws eks update-kubeconfig --name skala-gj --region ap-northeast-2 --profile skala-gj4`
  → `kubectl config set-context --current --namespace=skala-gj4`.
- Harbor: `docker login harbor.skala-gj.com -u skala-gj4 --password-stdin` (**`https://` 없이** — 붙이면 push 401).
  `harbor-cred` docker-registry Secret도 직접 생성한다.
- 확인: `kubectl get nodes`(여유 메모리 — 전 스택 요청 ~2.5GB), `kubectl auth can-i create ingress`, `kubectl get sc`.
- ArgoCD가 GitHub 저장소를 읽을 수 있는지 (비공개면 저장소 자격증명 등록 필요).

**1. 하드닝 (코드)**
- **1-1 공개 모드 접근 제어** — `sjw.public-mode`(기본 false, K8s에서 true).
  - `TenantGuard.resolve`: 공개 모드면 `X-Api-Key` 없을 때 default 테넌트로 떨어지지 않고 401.
  - `TranslationController` `/sync`·`/stream`: `X-Llm-Key` 없으면 **L1 캐시 히트만 응답**, 미스는 403 `BYOK_REQUIRED`
    (§15.3 "미인증은 캐시 히트만").
  - 비동기 `/translations` POST·`/batches`: 운영자 키를 쓰므로 `tenant.operator_access=true` 테넌트만 (V5 마이그레이션,
    기본 false). BYOK 배치는 ADR-020대로 범위 밖.
  - `BatchController` get/pause/resume: 배치 `tenant_id` 소유권 검사 (타 테넌트 404).
  - 키 발급: 관리 API 대신 `deploy/k8s/issue-key.sh <tenant> <daily_limit>` — 랜덤 키 → SHA-256을 psql로 INSERT,
    평문은 한 번만 출력 (`TenantRepository` 해시 방식 재사용).
- **1-2 동기 경로 실패 처리** — `FailureClassifier`·`ErrorClass`를 worker → common으로 이동. api `@RestControllerAdvice`:
  RATE_LIMITED/QUOTA_DAILY → 429 + `Retry-After`, NER_UNAVAILABLE → 503, SPEND_CAP·잘못된 BYOK 키 → 4xx, 나머지 502.
  BYOK 키는 응답·로그에서 마스킹(§8.2). api에도 `spring.ai.retry.max-attempts: 1`(이중 재시도 방지).
- **1-3 LLM 하드 타임아웃** — `sjw.llm.timeout-ms`(기본 60000). BYOK 클라이언트(`TranslatorFactory`)와 운영자 기본 클라이언트
  (`LlmConfig`)에 `HttpOptions.timeout`. 타임아웃은 `FailureClassifier`가 이미 일시 오류로 분류 → 워커 재시도/서킷 경로 그대로.
- **1-4 관측 최소분** — `micrometer-registry-prometheus`를 api·worker에 추가, actuator는 **별도 관리 포트**(api 9080 /
  worker 9081, 공개 Service에는 앱 포트만). api에도 `tenant.calls`·`llm.tokens`·`translation.quality.grade` 카운터.
- **1-5 비밀·설정 외부화** — DB·redis 비밀번호를 env(`SPRING_DATASOURCE_PASSWORD`, `SPRING_DATA_REDIS_PASSWORD`)로.
  compose 기본값은 유지(로컬 개발 무변경).

**2. 이미지 — Harbor.** `deploy/k8s/build-push.sh <tag>`: `docker buildx build --platform linux/amd64`(Apple Silicon → amd64
필수)로 `harbor.skala-gj.com/skala-gj4/sjw-{api,worker,ner}:<git short SHA>` push. ner 이미지는 모델(gitignore)이 필요해
로컬 빌드·푸시. 푸시 후 `deploy/k8s/kustomization.yaml`의 `images:` 태그를 커밋 → ArgoCD 동기화 (배포 = 커밋).

**3. K8s 매니페스트 — `deploy/k8s/`** (kustomize, Helm·오퍼레이터 없음) + ArgoCD Application
- `postgres` StatefulSet + PVC 5Gi(`ebs-sc`) / `redis`(`--requirepass`) / `ner` — 전부 ClusterIP.
- `api` Deployment(앱 8080, 관리 9080, readiness on 9080) + ClusterIP 80→8080.
- **Ingress** `nginx`, host `skala-gj4-sjw.skala-gj.com`, `cert-manager.io/cluster-issuer: letsencrypt-prod` + tls.
  공개 진입점은 이것 하나. SSE용 `proxy-buffering: off`·`proxy-read-timeout` 어노테이션.
- `worker` **replicas 1, strategy `Recreate`** (ADR-021 — 롤링 중 2대가 겹치면 이중 과금 위험).
- 모든 Pod `imagePullSecrets: harbor-cred`. resources requests/limits (JVM `-XX:MaxRAMPercentage`).
- ConfigMap: 생산 기본값 (v4 프롬프트, 캐시 L1 on, 승격 on, 라우팅 off, `SJW_PUBLIC_MODE=true`).
- Secret `sjw-secrets`(GEMINI_API_KEY·DB·redis 비밀번호)는 **git에 넣지 않는다** — `deploy/k8s/create-secrets.sh`가 로컬
  `.env`로 `kubectl create secret` (ArgoCD 관리 밖). `GEMINI_API_KEY`는 운영자 테넌트 배치용.
- `deploy/k8s/argocd-app.yaml` (project·dest-namespace `skala-gj4`, path `deploy/k8s`, automated sync).
- Prometheus/Grafana는 넣지 않음 — `/actuator/prometheus`만 열어 두고 대시보드는 M5.

**4. 문서·ADR** — ADR-022 개정(타겟 → skala-gj4, 가용성은 과정 기간 한정) / ADR-012 주석(K8s 배제는 "이력서용 도입"을 막는
것 — 제공된 클러스터에 평범한 kustomize로 올리는 것은 예외, Helm·오퍼레이터·HPA·다중 워커 없음) / ADR-020 개정(공개 모드) /
README에 공개 엔드포인트 사용법(`X-Api-Key` + `X-Llm-Key` curl 예시).

**재사용:** `TenantGuard`(resolve/charge), `TenantRepository`(해시), `FailureClassifier`/`ErrorClass`,
`TranslatorFactory.forModelWithKey`, `TranslationCache`(L1 조회), 가짜 provider(`GEMINI_MODEL=fake-flash-lite`, `FAKE_RPM`,
`FAKE_LATENCY_MS`)로 quota 없이 검증.

**검증.**
1. `./gradlew build` + 신규 테스트: 공개 모드 401 / 캐시 히트만 / 403 `BYOK_REQUIRED` / operator_access 없는 배치 403 /
   배치 소유권 404 / 예외 → 429 `Retry-After`·503 매핑.
2. 로컬 compose + 가짜 provider: `FAKE_RPM=5` 동기 연타 → 429 + `Retry-After`; `FAKE_LATENCY_MS` > timeout → 타임아웃 분류,
   RUNNING 고착 없음; `curl :9080/actuator/prometheus` 200.
3. EKS: `kubectl kustomize deploy/k8s` 렌더 → ArgoCD sync(첫 회는 `kubectl apply -k`) → `kubectl rollout status` →
   `https://skala-gj4-sjw.skala-gj.com`에서 키 없음 401 / 발급 키 + BYOK 없음 + 미캐시 문장 403 / 발급 키 + 실제 BYOK 키 200
   (사용자 키로 1~2회만). 관리 포트는 외부에서 접근 불가 확인.
4. 커밋 단위: ① 하드닝 / ② 이미지·매니페스트·배포 / ③ 문서·ADR.

### 5.1 M3 진행

계획서 §10의 M3가 정본. 5슬라이스로 쪼갰다.

- **S1 완료 (2026-09-01, `01a0e0f`)** — `TemplateSlotter`(확정 PER만 슬롯화, 등장 위치 순 번호,
  조사 보정 5쌍, 경계 검사) + `CacheKeys`/`CacheLevel` + **ADR-009**(무효화 A안: 버전을 캐시 키에
  포함 → 무효화 코드 0줄, 적재 정책 = L1은 게이트 통과분만 / L2는 VERIFIED + 전체형 출현분만).
- **S2 완료 (2026-09-10)** — L1 캐시 라이브. 워커·동기·SSE 경로 통합, `cache_hit_level` 기록,
  `translation.cache.hit/miss` 계측, `deploy/demo-cache.sh`.
  실측은 `docs/benchmarks.md` — **job 2건에 원장 1행**, 히트 지연 0~12ms vs 미스 1,665ms.

  S2에서 **ADR-009를 개정**했다. 원결정의 키는 `kb_version`·`prompt_version`뿐이었는데, 품질을
  결정하는 축은 넷(인물 사전·지시문·**NER 모델**·번역 LLM)이고 NER 모델에는 버전 개념이 아예
  없었다. NER 재학습본으로 갈아끼워도 캐시가 옛 결과를 계속 내주면 **개선분이 캐시에 막혀
  사라지고 등급으로도 안 드러난다.** 그래서:
  - 키를 `{kb}:{prompt}:{ner}:{epoch}` 파이프라인 버전으로 확장 (`PipelineVersions`)
  - NER 서버가 `/healthz`에 `model_version`(가중치 SHA-256 앞 8자리) 노출 → 자동 무효화
  - 번역 LLM은 여전히 키에서 제외하고 **`CACHE_EPOCH` 수동 손잡이**로 전면 재구축
  - `NER_MODE`/`KB_NAME`/`CACHE_EPOCH`를 compose passthrough로 노출 (교체 실증 수단)

  결정 근거(사용자, 2026-09-10): 캐시는 **테넌트 간 전역 공유**하되 BYOK 결과는 적재 제외,
  동기 경로도 게이트를 붙여 적재, 캐시 히트는 테넌트 일일 상한을 소모하지 않는다(예산 단위 =
  LLM 호출 수).

- **S3 완료 (2026-09-11)** — L2 템플릿 슬롯 캐시 라이브 (`CACHE_L2_ENABLED=true`, **기본 off**).
  워커·동기·SSE 3경로 통합. 실측은 `docs/benchmarks.md` "L2 템플릿 슬롯 캐시".

  라이브 증거 (`deploy/demo-l2-cache.sh`, `以{인물}爲承旨`의 인물만 교체한 쌍):
  - `以兪榥爲承旨` → `유황을 승지로 제수하였다.` 적재 → 템플릿 `⟪PER1⟫을 승지로 제수하였다.`
  - `以金瑬爲承旨` → **`김류를 승지로 제수하였다.`** — LLM 0회, **조사 `을`→`를` 보정 라이브 확인**
  - job 2건에 `cost_ledger` **1행**, 히트 등급 **DEGRADED**, `tokens_in` NULL
  - 마커 제거 고장 주입 → `reinject.abort{structure_mismatch}` + 전체 파이프라인 fallback (원장 1행)
  - **opt-in 실증**: 틀이 Redis에 있는 상태로 L2를 끄고 요청 → LLM이 처리(797 tok),
    `hit/miss{level=L2}` 둘 다 미생성(꺼둔 것은 미스가 아니다), `template_hash`는 기록됨
  - 단계별 지연: L2 히트 ≈42ms (**ner 31 + cache 4.8**, 워밍 후 ≈23ms) vs 미스 2,450ms.
    **L1(0~12ms)보다 느린 것이 정상** — L2는 슬롯화에 링크 확정 PER이 필요해 NER을 건너뛰지 못한다.

  ADR-009는 개정하지 않고 **구현 메모**를 덧붙였다 (원결정이 정하지 않았던 4건):
  ① 재주입 결과에도 게이트를 통과시킨다(공짜 결정론 검사 — 불합격은 재주입 로직 회귀 신호)
  ② `template_hash`는 L2 on/off와 무관하게 기록한다(S4가 기능을 켜지 않고 효과를 추정하므로)
  ③ L2 히트를 L1으로 승격 적재하지 않는다(껐을 때 즉시 멈춰야 한다 — 스위치의 의미)
  ④ 테넌트 과금을 LLM 호출 직전으로 내렸다(`TenantGuard.ensureWithinCap`이 문 앞을 막는다).
  새 메트릭: `translation.cache.reinject.abort{reason}` (§9.1 표에 추가).

- **S4 완료 (2026-09-11)** — 히트율 실측 + 비용 비교표. `eval/simulate_cache.py` (LLM 호출 **0회**).
  히트율은 코퍼스·NER·KB의 성질이고 번역 LLM과 무관하므로, 6만 문장을 실제 번역하지 않고
  **순차 재생**(생산 코드와 같은 L1→L2→LLM 순서 + 적재 정책)으로 측정했다. 전문은
  `docs/benchmarks.md` "캐시 히트율 + 캐시 on/off 비용 비교".

  | | A01 (1,911문장) | 인조 전수 (62,056문장) |
  |---|---|---|
  | L1 구조적 반복 | 17.84% | 15.65% |
  | L2 구조적 반복 (전체 중) | 0.73% | 2.77% |
  | **실효 히트율 L1+L2** | **18.21%** | **17.36%** |
  | L2 순 기여 | +0.37%p | **+1.74%p** |
  | 비용 (Flash counterfactual) | 2,437 → 1,993원 | 79,146 → 65,409원 |

  - **L2는 코퍼스가 커질수록 기여가 커진다** (1년치 0.37%p → 23년치 1.74%p). 1년치만 보고 판단하면
    과소평가한다. L1은 규모와 함께 단조 증가하며 6.2만에서도 상승 중 → 잔여 코퍼스엔 보수적 하한.
  - **L2가 L1 히트를 317건 잡아먹는다** (승격 적재를 안 하므로). 순효과 +1,075 절감 —
    "L2를 끄면 즉시 멈춘다"는 보장의 대가가 호출 0.51%로 측정됐다.
  - `docs/cost-model.md`의 `캐시 히트율` 파라미터를 0%(가정 40%) → **17.4%(실측)** 로 교체.
    **가정 40%는 반증됐다.** 캐시만으로 무료 축은 173년 → 143년(17%)뿐이고,
    **완역 시간의 지배 변수는 캐시가 아니라 quota다 → 남은 레버는 M4 티어 라우팅.**
  - **이식 충실도 2중 검증**: 생산 DB 대조(`normalized_hash` 96건, `template_hash` 9건 전부 일치,
    `--self-check`는 불일치 시 exit 1) + 골든셋 게이트 오탐률 **3.9% 정확 재현**.

  **부수 발견 3건:**
  1. **NER 모델에 POS(관직) 클래스가 없다** — 라벨은 `PER/LOC/DAT/POH`. 계획서 §5.2의
     "인명/날짜/관직"과 `命{PER}爲{POS}` 예시가 모델과 불일치 → §5.2에 정정 주석.
  2. **슬롯 확장의 기회는 관직이 아니라 지명이다** — 반복 틀 상한 PER 2.77% / PER+LOC **13.86%**(5배)
     / +관직 15.63%(+1.8%p). **ADR-009의 재검토 조건을 개정**했다(전제도 역어 사전이 아니라 지명 KB).
  3. **원천 코퍼스의 원문↔번역 짝이 밀려 있다** (전수 11.24%, A01 20.67%). 게이트 REJECTED가
     골든셋(3.9%)과 7배 어긋나 추적한 결과 데이터 결함. 밀림 제외 후 4.36%로 일치.
     골든셋 300은 영향 없다. 선례: `docs/troubleshooting.md` §5. 검출기는 시뮬레이터에 포함.
     **런타임 품질 게이트가 코퍼스 정합성 검사기로 동작한 사례.**

  재현: `python3 eval/simulate_cache.py --years all --self-check` (NER 서버 필요, 첫 실행 846초 —
  NER 결과는 `eval/.ner_cache_*.json`에 캐시되어 재실행은 즉시. 캐시는 gitignore).

### 5.1.1 L2 활성화 결정 — **off 유지** (2026-09-11)

S4 실측을 보고 **L2를 기본 off로 유지**하기로 했다. 근거는 교환 조건의 크기다:

| L2가 사는 것 (전수 62,056문장) | L2가 내는 대가 |
|---|---|
| LLM 호출 −1,075회 (+1.74%p) | **DEGRADED 등급 1,392건** — LLM이 쓰지 않은 문장이 서빙된다 |
| 비용 66,780 → 65,409원 (−2.1%) | L1 히트 317건 잠식 (승격 적재를 안 하므로) |
| 완역 유료 99.6 → 97.5만원 | 재주입 실패·조사 보정이라는 추가 실패 표면의 유지 비용 |
| 완역 무료 **146년 → 143년 (3년)** | |

**173년 중 3년을 위해 "LLM이 쓰지 않은 번역"을 서빙하는 교환은, 인명 정확도가 존재 이유인
프로젝트에 맞지 않는다.** 코드·테스트·데모·계측은 전부 보존한다 — 기능을 못 만든 것이 아니라
만들어서 재보고 이 조건에서는 수혜가 작다고 판정한 것이고, 그 판정이 계획서 §13이 말하는
유효한 결과다. 코퍼스가 커지면 기여가 커지므로(1년치 0.37%p → 23년치 1.74%p) 재검토 여지는 남는다.

**L2의 값은 현재 슬롯 범위가 아니라 범위 확장에 있다** — 지명(LOC)까지 슬롯화하면 반복 틀이
2.77% → 13.86%(5배)다. 막힌 것은 인식이 아니라(NER이 LOC를 33,469건 검출한다) **한자 지명 →
한글 지명 정본**이고, 지명 KB가 새로 필요하다. ADR-009 재검토 조건에 반영했다. M3 범위 밖.

- **S5 완료 (2026-09-11)** — L2 비열등 판정 **통과**. `eval/score_l2.py` (신규).
  **생산 코드 경로로 측정했다** — L2 결과를 이식으로 근사하지 않고 실행 중인 api의
  `TranslationCache.lookupL2`+`TemplateSlotter.reinject`+`QualityGate`가 만들게 하고
  `meta.cacheHit == "L2_TEMPLATE"` 확인분만 표본에 넣었다. 쌍당 3요청(F를 LLM 번역→틀 적재 /
  R은 L2 히트 / 틀 삭제 후 R을 LLM 직접 = 대조군), LLM 2회.

  | 지표 | 임계(§5.4) | 실측 중앙값 | 판정 |
  |---|---|---|---|
  | chrF | −2.0 이내 | **+0.04** (2차) / −1.28 (1차) / 6라운드 −1.13 | ✅ |
  | 확정 인명 재현율 | **하락 0** | **±0.0000** (6라운드 전부 양쪽 1.0000) | ✅ |

  - 표본 풀 = S4 L2 히트 1,392쌍 중 **정렬 검증 통과 1,220쌍**(`--dump-l2-pairs`). 층화 패턴×길이 7개 층.
  - **"3회 중앙값" 규정이 실제로 일했다**: 1차 R1이 −2.20으로 임계를 넘었다. 단일 측정이면 위반이
    나왔을 것이다. 6라운드 범위 −2.20~+0.70 = LLM 비결정성이 chrF 3포인트 폭을 만든다.
  - 기록 `eval/l2_noninferiority.json`, 종료 코드 계약은 `score_db.py`와 동일(위반=1) → M6 CI 연결 가능.
  - **판정 통과에도 L2는 off 유지** (§5.1.1 결정 불변). 품질이 아니라 수혜 크기가 근거이며,
    ADR-009 "운영 상태"에 "품질이 나빠서 끈 것이 아니다"를 명시했다.

  **S5 부수 실측 2건 (헤드라인을 바꿨다):**
  1. **`gemini-3.1-flash-lite` 무료 quota 실측 — RPD 500 / RPM 15.** 429 응답 본문의 `quotaValue`가
     직접 알려줬다(3.5-flash RPD 20을 확정한 것과 같은 방법). 42건 전부 `quotaDimensions.model`이
     flash-lite다. **무료 축 완역 시간이 173년 → 6.9년(캐시 17.4% 반영 5.7년)으로 25배 바뀐다** —
     기존 173년은 RPD 20짜리 T1 단독이라는 최악 조건이었다. `docs/cost-model.md` 갱신.
     **이것이 M4의 값을 구체화한다**: quota가 모델별 독립이므로 풀링하면 500+20+(gemma-4 미측정)이
     하루 예산이고, 완역 시간은 캐시가 아니라 이 합계가 정한다.
     또 **M2에서 유발 실패했던 라이브 429가 여기서 자연 발생했다**(M6 mock 이관 항목의 근거 데이터).
     **미해소 모순**: M2의 배치 처리량 23 jobs/min이 RPM 15를 넘는데 그때는 429 0건이었다
     (quota 변경/버스트 허용/측정 창 중 미확증) → M6 재측정. AIMD `initial-rpm: 20`·`max-rpm: 60`도
     실측 15를 넘어 **M4 재조정 후보**다.
  2. **동기 경로에 rate limiter·재시도·실패 분류가 없다** — 429 42건이 전부 HTTP 500으로 나갔다.
     워커만 ADR-017+Resilience4j+`FailureClassifier` 9종을 갖고 있다. 측정 도구가 페이싱·재시도를
     대신 지고 있고, **429를 본문 문자열로 판별해야 한다는 것이 갭의 증거**다.
     처리 방향(`FailureClassifier`를 common으로 + `@ExceptionHandler`로 429/503 매핑)은
     `docs/troubleshooting.md` §6. 곁가지로 **`cost_ledger`가 job 단위(`job_id NOT NULL`)라 동기 경로
     호출은 원장에 안 남는다** — 예산은 테넌트 카운터가 지키지만 **M5 비용 SLI는 두 출처 합산 필요**.

### 5.1.2 M3 수용 기준 최종 판정 (2026-09-11)

계획서 §10 M3 기준 3종 + 추가 1종.

1. **인조 1년치 실측 L1/L2 히트율 확보** — ✅ A01 L1 17.84% / L2 0.73% / 실효 18.21%.
   전수 23년치도 확보(L1 15.65% / L2 2.77% / 실효 17.36%) + 배치 규모별 추이.
2. **캐시 on/off 비용 비교표** — ✅ 전수 62,056문장: off 79,146원 / L1만 66,780원 / L1+L2 65,409원.
   `cost-model.md`의 캐시 히트율 파라미터를 0%(가정 40%)에서 **17.4% 실측**으로 교체.
3. **재주입 실패 탐지 → fallback** — ✅ 라이브 고장 주입으로 증명(`deploy/demo-l2-cache.sh`):
   마커 제거 → `reinject.abort{structure_mismatch}` + 전체 파이프라인 재실행(원장 1행).
   게이트를 재주입 결과에도 걸어 두 번째 방어선을 만들었다(ADR-009 구현 메모 1).
4. **추가: L2 히트가 §5.4 비열등 임계 통과** — ✅ chrF +0.04 / 인명 재현율 ±0.
   **단, 통과에도 L2는 off로 유지**한다(수혜 1.74%p). 계획서 §13의 "시도했고 이 조건에서는
   수혜가 작다"는 결과 형태로 기록했다.

### 5.3 M4 진행

계획서 §10의 M4가 정본. **단, S1 실측이 §5.1의 전제를 뒤집었다 — ADR-010이 그 기록이다.**

- **S1 완료 (2026-09-11)** — 신호 분포 실측 + 재설계 + 도메인 구현. **LLM 호출 0회.**

  **① 계획서 §5.1 표가 성립하지 않음을 실측으로 확인** (`eval/simulate_routing.py`, 전수 62,056문장)
  - 표가 문장의 **35.87%를 어느 티어로도 보내지 않는다** (엔티티0+패턴없음 9,526 / PER없고 LOC·DAT만
    9,366 / PER 3개+ 전건확정 3,366).
  - **표대로 묶으면 라우팅이 완역을 124일 → 926일로 7.5배 늦춘다.** T2(MISS 또는 모호) 수요
    29.86%가 상위 모델 quota 몫 3.8%(RPD 20/520)의 8배라 배치가 T2에서 정체된다.
  - 이미 켜져 있는 품질 기반 상향도 같은 문제: REJECTED 4.36% × 62,056 = **2,705건 vs RPD 20 =
    quota의 135배.** 문이 없으면 상향이 quota를 즉시 태우고 이후 모든 상향이 실패한다.
  - 원인: §5.1의 전제는 "저가↔고가 단가 차이"인데 무료 티어에서 단가는 전부 0이고 차이는 **quota**다.

  **② 재설계 — 난이도 분류와 예산 인식 배정의 분리 (ADR-010)**
  - `TierRouter`(common/routing): 신호 → T0/T1/T2. 결정론적, 추가 비용 0.
  - `ModelAllocator`(common/routing): 티어 + **provider quota 잔량** → 실제 모델. 상위 모델 배정 전
    `quota:daily:{model}:{date}`를 원자적으로 예약(INCR 후 한도 비교), 자리 없으면 기본 모델로 강등.
    **예약 카운터는 테넌트가 아니라 모델 단위** (무료 quota가 프로젝트×모델이다). 실패한 예약은 DECR로 되돌린다.
  - **품질 기반 상향도 같은 문을 지난다**(`reserveForPromotion`) → 135배 오버서브스크립션 해소.
  - **T2 = 동명이인 모호만 (1.83%).** MISS를 뺀 근거는 quota만이 아니다 — 역색인에 없는 인물은
    **모델을 올려도 알게 되지 않는다**(지식원은 KB지 LLM이 아니다, ADR-006 전제와 동일).
  - **T1을 기본값으로** 두어 미정의 구간 35.87%를 없앴다. 채택 분포: T0 13.02% / T1 85.15% / T2 1.83%.
    (재현: `simulate_routing.py` "채택한 규칙" 절 — M4-S1 커밋 시점엔 원안 표만 구현돼 있던 갭을 2026-09-21에 닫음, 정확 일치.)
  - **T0 정형문 패턴을 프롬프트 어휘 목록에서 분리**(`routing/formulaic-patterns.tsv`).
    `positive-patterns.tsv`에는 `傳曰`(전체 31.49%)·`答曰`(24.95%)이 없어 T0이 5.90%에 머물렀다 —
    그건 "쓸 어휘" 목록이고 구조 탐지기가 아니다. 분리 후 13.02%.
  - **T0과 T1은 현재 같은 모델로 간다** (flash-lite가 최대 quota 보유자라 하향 대상이 없다).
    숨기지 않고 명시했다 — 지금 라우팅의 실질 효과는 "T2 1.83%를 quota 한도 내에서 상위 모델로"뿐이다.
  - **기본 off** (`ROUTING_ENABLED`, L2와 같은 규칙).

  **③ 실측 RPM을 모델 레지스트리로** — `ModelSpec.rpm` 추가(실측만, 미실측 null).
  버킷은 모델별인데 탐색 경계가 전역 상수 하나였다 → 실측 15인 모델도 20에서 출발해 기동마다 429를
  한 번 사서 배웠고 상한 60은 실측의 4배였다. 실측값이 있으면 **시작점·상한**을 대체하고,
  없는 모델만 전역 기본값으로 탐색한다(AIMD는 유지 — provider가 바꿀 수 있으므로).

  **④ MISS의 정체 = KB 확장 작업 목록.** MISS 멘션 44,082건이 표면형 13,586개에 몰려 있고,
  10회 이상 등장 표면형만 채우면 42.21% 해소. 1자 표면형 필터 가설은 값이 없었다(MISS의 7.95%,
  제거해도 DEGRADED 28.84%→27.39%이고 링크되는 1자 1,695건을 잃는다). 87%가 2~3자 실명 = 진짜 공백.

  테스트 100/100 (신규 21건: TierRouter 7 / ModelAllocator 8 / ModelSpec·리미터 6).

- 남은 범위: M4-S2 — 라우팅 on/off 비용·품질 곡선 (M4 수용 기준). LLM 호출이 필요하다.
  ① ~~gemma-4 등 나머지 모델 RPD/RPM을 429 `quotaValue`로 탐침~~ → **2026-09-21 탐침 결과: 측정 불가, 그 전 단계에서 막힘.**
     `eval/probe_model.py`로 골든셋 60 배치 → 25분에 응답 12건, **전부 PARSE_ERROR**(응답이 JSON이 아니라 `*` 마크다운),
     출력 평균 64 tok, 응답 간격 30초~11분, 429 0건, 1건은 26분 RUNNING(하드 타임아웃 부재). gemma-4는 Structured Output
     계약을 안 지키므로 quota 이전에 **모델별 출력 모드(평문+결정론 추출)**가 설계 항목으로 필요하다. 그 전까지
     **M4 quota 풀 = 500 + 20.** benchmarks 마지막 절.
     부수 갭: pause된 배치의 재시도 메시지(PARSE_ERROR는 미ACK)가 모델 교체 재기동 후 새 모델로 처리됨 — 4건이
     flash-lite·v5로 섞여 들어감(quota 4회). 수동 정리(XACK/XDEL). **모델 교체 전 `XPENDING` 0 확인.** 대책은 M5/M6.
  ② 라우팅 on/off로 골든셋 배치를 돌려 품질(chrF·인명 재현율)과 호출 수 비교. T2가 1.83%뿐이라
     **전체 품질 변화는 작을 것이고, 봐야 할 것은 T2 문장군의 품질 차이**다 — 층화해서 재야 한다.
  ③ 상향 라우팅 발동률·추가 비용 시계열 (M4 추가 기준).
  주의: 오늘 flash-lite RPD 500을 한 번 소진했다(회복됨). 탐침은 quota를 쓴다.

**S2에서 발견한 별건 갭 (M5로 이월):** `/actuator/prometheus`가 404다 —
`micrometer-registry-prometheus`가 없어 노출 설정만 있고 레지스트리가 없다. 카운터 자체는
`/actuator/metrics`로 확인된다. Grafana 대시보드를 세우는 M5에서 붙인다.

### 5.4 M3.5 평가 신뢰도 + 프롬프트 실측 (삽입, 2026-09-21)

M2.5처럼 계획 중간에 삽입한 마일스톤. 계기는 AI 엔지니어링 채용자 관점의 검토 — 두 가지가 걸렸다.
① 헤드라인 "인명 재현율 99.09%"가 **순환 지표**였다: `score_db.py`가 시스템이 프롬프트에 주입한
`resolvedName`이 번역문에 있는지 셌다. 선행 연구 보고서(§0)가 정확히 이 구조를 "닫힌 고리"라 부르고
독립 정답지(`eval/ner_groundtruth_300.json`)로 ETS를 만들어 1.000→0.888을 보였는데, 서빙 채점기는 그
정답지를 안 쓰고 있었다. ② 입력 토큰의 85%가 프롬프트인데 서빙 프롬프트를 골든셋으로 ablation한 적이
없고, 연구가 "효과 없음"이라 한 요소(역할 지정·번역 원칙)가 그대로 들어 있다.

- **S1 완료 (2026-09-21) — ETS를 서빙 채점기에. LLM 호출 0회.**
  - `eval/score_db.py`: 독립 정답지 기반 **ETS**(엄격/관대/macro) 추가, `--batch-id`·`--prompt-version`·
    `--model` 필터, `--json`·`--no-gate`(S2 드라이버용), `--self-check-reference`(정답 번역 자체 채점),
    `--corpus`·`--groundtruth`(다른 왕대 골든셋 — 교체 축을 채점기에도 적용). 종료 코드 계약 불변.
  - 기존 n=70 결과 재채점: chrF 41.52 / 반영률 0.9909 **정확히 재현**(리팩터링 무해 확인), **ETS 0.9821**
    (110/112, 문장 50). 전문가 번역 자체의 ETS 0.9713(383건) — 정답지 표기와 번역가 표기 차이, 100%가
    상한이 아님. `eval/baseline_scores.json`에 ETS·prompt_versions·batch_ids·mean_tokens_in(877) 추가.
  - 문서: README 헤드라인·presentation §0/§5.11/§6.2/부록·benchmarks에 "반영률=게이트 지표(순환) /
    ETS=품질 지표" 구분 명시. **ETS 허용 하락폭은 아직 없다** — S2 대조군 3라운드 폭으로 변형 채점 전에
    고정한다(사후 임계 금지).
  - 발견: 이 n=70에서 정답지 인명 112 vs 주입 인명 110 — 골든셋의 KB 커버리지가 전수(MISS 36.51%)보다
    훨씬 높다. MISS 문장군의 ETS는 별도 측정 대상.
- **S2 완료 (2026-09-21~24) — 서빙 프롬프트 ablation, 생산 코드 경로. v4 채택.** 최종: 여섯 변형 모두 통과, 사전 규칙(토큰 최소)은 v2, **채택은 v4**(품질 세 지표 모두 상승, v2 대비 +22 tok) — 사유 ADR-013 적용 기록. `translate-main.st` = `v4-minimal.st`(`main-4a1cb192`), 기준선 재저장(층화 60, chrF 40.32 / ETS 0.9808)으로 프롬프트 체크섬 게이트 활성, cost-model 오버헤드 700 → 597 tok(완역 $695 → $663). 아래는 진행 기록.
- (진행 기록) **S2 (2026-09-21) — 서빙 프롬프트 ablation, 생산 코드 경로.** 도구는 커밋됨(f1aafb0):
  `PromptAssembler` 리소스 경로 설정화(`PROMPT_TEMPLATE`, 버전은 바이트 체크섬 — Python 계산값이 생산
  `main-d5ac24e9`와 일치), 변형 5종 `eval/prompts/`, 층화 표본 `eval/eval60_stratified.json`(15층, 정답지 인명
  104), 드라이버 `eval/prompt_ablation.py`(v0 먼저 강제·self-check·재개 가능). 결과 정본 `eval/prompt_ablation.json`,
  표·해석은 `docs/benchmarks.md` 마지막 절.
  - **v0 3라운드 완료 → 임계 고정: ETS 허용 하락 0.0000**(세 라운드 ETS 동일 0.9712). chrF 37.60~38.29.
  - **v1(페르소나·원칙 제거) 통과:** tokens −12.2%, 인명 지표·등급 동일, chrF −1.48(연구의 "효과 없음"과 달리
    문체에 값을 한다, 임계 안). **중간 선두 — 최종 아님** (v2~v5 미측정).
  - 부수: **REJECTED 첫 라이브 자연 발생**(兪榥→유황, 승격 → 3.5-flash VERIFIED). 1차 시도는 승격 때문에 모델이
    섞여 폐기 → 이후 `TIER_UP_ENABLED=false`(compose passthrough 추가)·예산 2n.
  - 발견: 정답 코퍼스 300문장 전부 `아뢰기를,“…”하니,`(curly·공백 없음)인데 서빙 예시는 `아뢰기를, "…" 하니,` →
    구두점만 바꾼 v5 추가. 6변형 × 3 × 60 = 1,080회 (RPD 500 → 하루 2변형).
  - **v5(구두점만) 라운드 1: chrF 39.62** — v0 세 라운드보다 위, 인명 동일. 라운드 2는 16/60에서 **QUOTA_PAUSED**
    (batch `e95b8d16-9fdb-4453-be8d-528f755bc754`). 워커는 지금 v5 프롬프트·캐시 off·승격 off 상태로 떠 있다.
  - v5 라운드 2가 QUOTA_PAUSED로 멈췄다 — 무료 일일 한도를 실험이 실제로 밀어냈다는 기록이다.
    이후 v5 r2~3 · v2 · v3 · v4를 하루 한도에 맞춰 나눠 측정했다 (총 1,080회, 사흘).
  - **막간 작업 (2026-09-21, quota 대기 중, LLM 0회):**
    ① `simulate_routing.py`에 채택 규칙(`tier_adopted` = `TierRouter.classify` 이식) 추가 → T0 8,080 / T1 52,843 /
    T2 1,133 **정확 재현**(M4-S1의 "알려진 갭" 닫힘, benchmarks M4-S1 절에 재현 커맨드).
    ② `score_db.py --report [PATH]` 문장 단위 TSV + `--compare-batch B` 짝 비교(movers·인명 변화) — v1 손실이
    짧은 정형문에 몰려 있음을 확인(benchmarks S2 "읽는 법").
    ③ `eval/tests/` 11건 + CI `eval-tests` 잡(uv+pytest, DB 불필요) + **프롬프트 체크섬 회귀 게이트**
    (`test_prompt_version_gate.py` — 기준선 `prompt_versions`에 없는 체크섬이면 실패; 기준선이 pre-V4라 지금은 skip,
    S2 종료 시 `--save-baseline`이 켠다) + **ADR-013 작성**(비결정 출력 회귀 검증 — 결정론 축 CI / 확률 축 오프라인).
  - 채택 시: `translate-main.st` 교체 → `score_db.py --prompt-version <new> --save-baseline` → cost-model 프롬프트
    오버헤드 행(448·700 tok) 실측 교체 → README/presentation의 `main-d5ac24e9` 갱신 → ADR-013. 미채택 시 결과만 기록.
- **KB 주입 효과 A/B 완료 (2026-09-24) — 파이프라인 대 LLM 단독.** `KB_MODE=noop`(compose passthrough 추가)으로 같은
  층화 60문장·생산 프롬프트·캐시/승격 off 3라운드 → **ETS 0.9038(94/104) vs 0.9712(101/104), 인명 오류 10 → 3**, chrF 동일
  (37.73 vs 37.90), 토큰 +5.8%. LLM 단독의 오류는 드문 이름 한자 오독(崔葕→최헌, 朴頵→박윤, 沈詻→심악)과 인명 누락.
  대조군은 v0 라운드 재사용(9/21). 도구 `eval/kb_ablation.py`, 정본 `eval/kb_ablation.json`, 표는 benchmarks 마지막 절.
  부수: 9/23 v3 자동 재개 스크립트가 Mac 절전 중 `sleep`이 멈춰 발화하지 않았다 — 장시간 대기는 절전 영향을 받는다.

### 5.5 M6 선행 — 가짜 LLM provider (2026-09-21)

`FakeTranslator`/`FakeProvider`/`FakeLlmProperties`(common/llm) 배선·검증·데모.
`Translator` 포트의 **두 번째 provider** — ADR-018 재검토 조건("두 번째 provider가 필요해지면") 이행. 네트워크 0, quota 0.

- **배선:** `TranslatorFactory`가 레지스트리 `provider`로 분기(`google-genai` | `fake`, 모르는 값은 `ModelSpec`이 기동
  시 거부), `LlmConfig`에 `FakeProvider` 싱글턴 빈(quota 창·난수가 프로세스 상태), 레지스트리에 `fake-flash-lite`(T0)·
  `fake-flash`(T1, 승격 데모용) + `sjw.llm.fake.*` 손잡이, compose에 `GEMINI_MODEL`·`FAKE_*`·`TIER_UP_MODEL` passthrough.
- **안전장치:** 가짜 결과는 L1/L2에 적재하지 않는다(`JobProcessor`·`TranslationController` — 캐시 키에 모델이 없어
  실 요청이 가짜를 받게 된다). `score_db.py`는 `fake-*` 결과를 기본 제외(`--include-fake`).
- **테스트 +18:** `FakeProviderTest`(시계 조작으로 분당·일일 창, 모델별, 503, 시드) / `FakeTranslatorTest`(실 조립 프롬프트
  파싱, 동명이인, JSON·usage, 스트림, **규칙 NER + 인조 KB + 게이트 E2E: VERIFIED ↔ REJECTED**) / `TranslatorFactoryTest` /
  worker `FakeProviderFailureContractTest`(가짜 429·503이 RATE_LIMITED·QUOTA_DAILY·SERVER_ERROR로 읽히고 재시도 힌트가
  파싱됨). common 92 / worker 27 / api 통과, eval 11.
- **라이브 데모 `deploy/demo-fake-provider.sh A|B|C`** (benchmarks 마지막 절): A 60/60 120s 실 호출 0·캐시 0 /
  **B `FAKE_RPM=20` → 429 2건, 리미터 39→19·23→11 RPM(힌트 쿨다운) → 완주** / C `FAKE_DROP_NAME_RATE=0.3` → REJECTED 7 →
  `fake-flash` 승격 7회. **M2 수용 기준 ②가 닫혔다.**
- ADR-018 개정(provider 축 2구현 + 재검토 조건 이행 기록). 부수: `up -d worker`가 api를 기본 env로 재생성하는 함정 →
  재기동은 항상 `--no-deps`.
- 이후 범위: 부하 테스트(M6)에서 `FAKE_LATENCY_MS`·`FAKE_ERROR_RATE`로 처리량 상한·서킷 동작 실측, CI E2E에 fake 스택.

### 5.2 M2.5 완료 기록

계획서 §10 M2.5가 정본이다. **진행 (2026-09-01): S1(Flyway + ADR 6건) · S2(모델 레지스트리 + Translator 포트, 3모델 설정 교체 실증) · S3(EntityRecognizer 포트 http/rule, 골든셋 A/B 수치 확보) · S4(KnowledgeSource 포트, 정조 KB 기동 실증, 체크섬 버전, ADR-018) · S5(품질 게이트: quality_grade + T1 승격 + 오탐률 3.9% 선측정 + score_db.py 기준선 chrF 41.52/인명 99.09%, ADR-019) · S6(BYOK/테넌트: X-Api-Key 해시 식별 + 일일 상한 429 + rate:bucket:{tenant}:{model} + X-Llm-Key 요청 단위 클라이언트·비저장 검증, ADR-020 — 단, BYOK는 동기/SSE만, 배치는 운영자 키) · S7(프롬프트 외부화: 템플릿·패턴 리소스 파일 + prompt_version 체크섬 파생, 외부화 전후 프롬프트 바이트 동일 확인 tokens_in 754 불변) · S8(Dockerfile 3종 — NER은 INT8 174MB 동봉, compose 전 스택 라이브 E2E, GitHub Actions CI 테스트→빌드, §9.1 메트릭 이름 정렬 + cost/tokens/latency/ner.unavailable 계측, ADR-022 배포 타겟 = Oracle Always Free 우선) 완료.**

**M2.5 수용 기준 최종 판정 (2026-09-01):**
1. KB 정조 교체 기동, 코드 0줄 — ✅ 라이브 (`jeongjo-2abe1183`, 蔡濟恭 링크)
2. NER 규칙 교체 + 품질 차이 수치 — ✅ 골든셋 recall 26.9% vs ONNX 100% (`NER_MODE=rule` 라이브 E2E)
3. 모델 3종 설정 교체 — ✅ flash-lite·3.5-flash 라이브 200 (gemma-4는 라우팅 성공·응답은 혼잡 무응답 — 실측된 provider 특성, 교체 경로는 동일)
4. REJECTED 검출 + 상위 티어 승격 + 오탐률 측정 — 게이트·승격 경로 구현 + 단위 테스트, 오탐률 3.9% 실측 ✅. **라이브 REJECTED 자연 발생은 2026-09-21 관측** (M3.5-S2 ablation 중 兪榥→유황 누락 → 3.5-flash 승격 → VERIFIED, §5.4)
5. compose 전 스택 기동 — ✅ 라이브 (5컨테이너, 컨테이너 파이프라인 E2E SUCCEEDED. 이미지: api 604MB / worker 600MB / ner 973MB)
6. 골든셋 채점 DB 직결 + 비열등 임계 판정 — ✅ `score_db.py` (위반 시 exit 1). **CI 스케줄 연결은 결과 DB가 생기는 배포(M6) 이후** — ci.yml 주석에 명시

요약:

1. **포트 3종** `Translator` / `EntityRecognizer` / `KnowledgeSource` — 각각 실구현 2개 이상. 구현체 1개짜리 인터페이스는 만들지 않는다
2. **모델 레지스트리** `sjw.llm.models[]{id, provider, tier, rpd, 단가}` — rate 버킷 키·원장 단가·M4 티어 매핑·quota 풀링의 단일 출처. `CostLedgerRepository`의 단가 0 하드코딩을 유료 환산값으로 교체
3. **품질 게이트** (§5.4) — 런타임 `quality_grade`(VERIFIED/DEGRADED/REJECTED) + `eval/score_300.py`의 DB 어댑터 + 비열등 임계(chrF −2.0, 인명 재현율 하락 0, 층화 n=60 3회 중앙값)
4. **BYOK / 테넌트 격리** — 요청 단위 LLM 클라이언트, `rate:bucket:{tenant}:{model}`, 키는 저장하지 않음
5. **프롬프트 외부화** — `PromptAssembler`의 Java 상수 템플릿을 리소스 파일로, `prompt_version` 기록
6. **배포 기반** — Dockerfile 3종(NER은 INT8 178MB를 이미지에 굽는다), 최소 CI(테스트·빌드), Flyway, §9.1 메트릭 계측 착수
7. **배포 타겟 확정** — Oracle Cloud Always Free(ARM) 우선, 불가 시 self-host+CI로 축소 → ADR-022

**현재 코드의 착수 지점 (검토에서 확인된 갭):**

| 대상 | 파일 | 문제 |
|---|---|---|
| ~~모델 하드바인딩~~ | `TranslationService` | ✅ S2 해소 — `Translator` 포트 + `common/llm` 레지스트리, 모델은 호출마다 옵션 지정 |
| ~~NER 교체 불가~~ | `EntityRecognizer` | ✅ S3 해소 — http/rule 2구현 + `NerUnavailableException`(장애≠빈 결과) + `NER_UNAVAILABLE` 분류 |
| ~~KB 파일명 고정~~ | `KnowledgeSource` | ✅ S4 해소 — file(injo/jeongjo)/noop 구현, version = 파일 체크섬 파생 |
| ~~품질 게이트 부재~~ | `QualityGate` | ✅ S5 해소 — quality_grade 판정 + REJECTED→T1 승격 + 오탐률 3.9% 선측정 + score_db.py 비열등 판정 |
| ~~단가 미기록~~ | `CostLedgerRepository` | ✅ S2 해소 — 레지스트리 단가로 counterfactual 원화 기록 (행 단위) |

### 5.3 미작성 ADR

**작성 완료 (2026-09-01, M2.5-S1):** 004(KB in-memory) / 007(Tool Calling 배제) / 008(ChatMemory 배제) / 012(Kafka·MSA·K8s 배제) / 021(단일 워커 — 처리량 실측 근거) / 023(Flyway).
**남은 M2.5 산출물:** 022(배포 타겟) — 018(S4)·019(S5)·020(S6) 작성 완료. 009(M3)·010(M4)·013(M3.5, 2026-09-21) 작성 완료 — 011은 M5에서.
