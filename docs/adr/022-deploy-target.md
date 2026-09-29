# ADR-022: 배포 타겟 및 호스팅 선택 (§15)

- 상태: 승인 (M2.5-S8에서 확정 — M6로 미루지 않는 이유는 §15.2) · **개정 (공개 서빙 1차, 2026-09-28 — 타겟을 SKALA EKS `skala-gj4`로 변경. 맨 아래 개정 절 참조)**
- 날짜: 2026-09-01

## 배경

배포 타겟이 미정이면 M6에서 배포 자체가 불가하고(§10 리스크 표), 무엇보다 **ARM 여부가 지금의
컨테이너 이미지 전략을 정한다**: NER 서버가 기동 시 700MB를 내려받는 구조로는 무료 인스턴스
컨테이너화가 성립하지 않아 INT8(174MB)을 이미지에 굽는 결정(S8 Dockerfile)이 선행돼야 했다.
예산은 0원이다 (ADR-016).

## 선택지

1. **Oracle Cloud Always Free (Ampere A1, ARM aarch64 — 4 OCPU / 24GB / 200GB)**
2. Self-host (개인 장비) + `docker compose up` + CI
3. HuggingFace Spaces / 기타 무료 PaaS

## 결정

**A(1) 우선, 계정 확보 실패 시 C(2)로 축소.**

- A1 무료 셰이프는 이 스택(JVM 2개 + ONNX CPU 추론 + Redis + Postgres)에 충분하고 24시간 상시라
  배치·시계열 메트릭(§9.1)이 실제로 쌓인다. 단, 용량 부족으로 프로비저닝이 거부되는 사례가 흔해
  확보를 보장할 수 없다 — 그래서 fallback이 있는 결정이다.
- **ARM 대응은 이미 끝났다**: base 이미지 전부 multi-arch(eclipse-temurin, python-slim, redis, postgres),
  onnxruntime은 aarch64 휠 제공. Dockerfile 수정 없이 ARM에서 빌드된다.
- self-host fallback도 같은 산출물을 쓴다: `docker compose -f deploy/docker-compose.yml up -d --build`
  한 줄이 전 스택이다 (M2.5 수용 기준 5). 잃는 것은 상시성뿐, 재현성은 동일.

## 버린 대안과 그 이유

- **HF Spaces(3)**: 무료 Space는 영구 디스크가 없고 무활동 시 잠든다. Postgres 원장·배치 체크포인트가
  재시작마다 소실되면 M2의 재개·멱등·예산 원장이 전부 무의미해진다. 결정적으로 본인 키 공개 = RPD 20
  즉시 소진 — 호스팅이 아니라 BYOK(ADR-020)로 풀 문제다 (§15.2 원문).
- 기타 무료 PaaS(Fly/Render 등 무료 축소 추세): 상시 프로세스 + 영구 DB + 멀티 컨테이너를 무료로
  주는 곳이 사실상 없다. 조건 변동 리스크에 포폴 마감을 걸 수 없다.

## 재검토 조건

- A1 프로비저닝이 2주 내 확보되지 않으면 즉시 C로 확정하고 M6 계획에서 원격 배포 단계를 제거한다.
- 프로젝트가 실사용(연구자 다수)으로 넘어가면 유료 최소 인스턴스 재평가 — 그때는 ADR-016도 함께 갱신된다.

---

## 개정 (공개 서빙 1차, 2026-09-28) — 타겟을 SKALA EKS `skala-gj4`로

**위 재검토 조건의 첫 항목이 발동했다.** Oracle A1을 확보하지 못한 채, 그 사이 교육 과정에서
쿠버네티스 네임스페이스가 제공됐다. 선택지 A는 만료됐고 C(self-host)보다 나은 것이 생겼다.

**새 결정: SKALA 광주캠퍼스 EKS 클러스터 `skala-gj`의 네임스페이스 `skala-gj4`.**

| 항목 | 값 |
|---|---|
| 클러스터 | `skala-gj` (AWS `ap-northeast-2`) |
| 네임스페이스 | `skala-gj4` (다른 반 네임스페이스 접근 불가) |
| 레지스트리 | Harbor `harbor.skala-gj.com/skala-gj4/*` |
| 공개 주소 | `https://skala-gj4-sjw.skala-gj.com` (와일드카드 DNS + nginx Ingress) |
| TLS | cert-manager `letsencrypt-prod` |
| 스토리지 | `ebs-sc` (RWO, 기본) — postgres 5Gi / redis 1Gi |
| CD | ArgoCD (AppProject `skala-gj4`) |

**이 타겟을 고른 이유** (A·C와의 비교):
- **상시성이 있다.** A1의 장점이었던 "24시간 상시라 시계열 메트릭이 실제로 쌓인다"(§9.1)가
  여기서 성립한다. C(self-host)는 노트북을 닫으면 끝이라 이 속성이 없었다.
- **비용 0을 유지한다** (ADR-016). 과정 제공 자원이다.
- **ARM 대응이 불필요해졌다.** 노드가 x86_64라 A1 때 준비한 multi-arch는 쓰이지 않지만,
  Apple Silicon에서 빌드하므로 반대 방향 제약이 생겼다 — `--platform linux/amd64` 강제
  (`deploy/k8s/build-push.sh`). 빼면 `exec format error`.

**네임스페이스가 공유라는 점이 이름 규칙을 강제한다.** `skala-gj4`는 반 전체가 쓴다. 매니페스트가
`api`·`postgres`·`redis` 같은 맨 이름을 쓰면 충돌한다 — 실제로 `svc/postgres`가 같은 계정의 이전
프로젝트(`rai`) 소유여서 첫 apply가 거부됐다(`clusterIPs: may not change once set`). 불변 필드가
막아줬지만, 막히지 않았다면 더 나빴다: 그 이름은 엔드포인트 없는 남의 서비스로 해석되므로
파드가 전부 떠도 api가 자기 DB에 닿지 못하는 **조용한 오작동**이 된다. `kustomization.yaml`의
`namePrefix: sjw-`로 전부 접두한다. ConfigMap의 호스트명은 문자열이라 kustomize가 고쳐주지
않으므로 `config.yaml`에서 직접 맞춘다 — 이 둘이 어긋나면 같은 실패로 돌아온다.

**한계 (정직하게):**
- **가용성이 과정 기간에 한정된다.** 과정이 끝나면 네임스페이스가 회수될 수 있다. 영구 데모가
  아니며, 포트폴리오 링크로 쓸 때 이 사실을 함께 적어야 한다.
- 공유 클러스터다. 다른 반 수강생의 워크로드와 노드를 공유하므로 이웃 부하가 지연에 섞인다 —
  여기서 잰 p95를 단독 인스턴스 수치로 제시하면 안 된다.
- 권한이 네임스페이스 범위다. 노드·CRD·클러스터 범위 자원을 만들 수 없다. 실측(2026-09-28):
  `kubectl auth can-i create applications.argoproj.io` → **no**. ArgoCD Application은 kubectl이
  아니라 ArgoCD 자체 계정(CLI·UI)으로 등록한다 (`deploy/k8s/argocd-app.yaml` 주석).
- 재현성은 C가 여전히 낫다 — `docker compose up` 한 줄은 그대로 유지된다 (ADR 본문의 근거 그대로).

**재검토 조건 (개정판):**
- 과정 종료로 네임스페이스가 회수되면 C(self-host compose)로 되돌리거나, 그때 유료 최소
  인스턴스를 재평가한다 (ADR-016 동반 갱신).
- 실사용자가 생겨 상시성이 요구사항이 되면 과정 자원에 의존할 수 없다 — 그 시점에 이 ADR을 다시 연다.
