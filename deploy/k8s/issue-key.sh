#!/usr/bin/env bash
# 테넌트 API 키 발급 (§5.0 1-1) — 관리 API 대신 이 스크립트. 랜덤 키 → SHA-256만 DB에 INSERT,
# 평문은 여기서 한 번만 출력된다 (TenantRepository와 같은 해시: UTF-8 바이트의 SHA-256 hex).
# 같은 tenant id로 다시 돌리면 키가 교체된다(이전 키 즉시 무효).
#
#   deploy/k8s/issue-key.sh <tenant-id> <daily-call-limit> [--operator]
#     --operator : 운영자 키(GEMINI_API_KEY) 경로(비동기 job·배치) 허가. 공개 사용자에겐 주지 말 것.
#
# 대상 DB (기본 = 클러스터의 postgres StatefulSet):
#   로컬 compose:  PSQL="docker exec -i sjw-postgres psql -U sjw -d sjw" deploy/k8s/issue-key.sh alice 50
set -euo pipefail

TENANT="${1:-}"; LIMIT="${2:-}"; OPERATOR=false
[[ "${3:-}" == "--operator" ]] && OPERATOR=true
if [[ ! "$TENANT" =~ ^[a-z0-9][a-z0-9_-]{1,39}$ ]] || [[ ! "$LIMIT" =~ ^[1-9][0-9]{0,5}$ ]]; then
  echo "usage: $0 <tenant-id: [a-z0-9_-], 2~40자> <daily-call-limit: 1~999999> [--operator]" >&2
  exit 2
fi
if [[ "$TENANT" == "default" ]]; then
  echo "default는 운영자 자신이다 — 키를 붙이지 않는다" >&2
  exit 2
fi

PSQL="${PSQL:-kubectl exec -i statefulset/sjw-postgres -- psql -U sjw -d sjw}"

KEY="sjw_$(openssl rand -hex 24)"
HASH="$(printf '%s' "$KEY" | shasum -a 256 | cut -d' ' -f1)"

# 평문 키는 psql로 넘기지 않는다 — 해시만. (tenant·limit은 위 정규식으로 검증된 값)
$PSQL -v ON_ERROR_STOP=1 -q <<SQL
INSERT INTO tenant (id, display_name, api_key_hash, daily_call_limit, operator_access)
VALUES ('$TENANT', '$TENANT', '$HASH', $LIMIT, $OPERATOR)
ON CONFLICT (id) DO UPDATE
   SET api_key_hash = EXCLUDED.api_key_hash,
       daily_call_limit = EXCLUDED.daily_call_limit,
       operator_access = EXCLUDED.operator_access;
SQL

echo "tenant=$TENANT daily_call_limit=$LIMIT operator_access=$OPERATOR" >&2
echo "X-Api-Key (다시 볼 수 없다 — 지금 전달할 것):" >&2
echo "$KEY"
