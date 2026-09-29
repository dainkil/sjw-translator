package dev.sjw.api.tenant;

import dev.sjw.common.queue.QueueKeys;
import dev.sjw.common.tenant.Tenant;
import dev.sjw.common.tenant.TenantRepository;
import java.time.Duration;
import java.time.LocalDate;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

/**
 * 테넌트 식별(X-Api-Key) + 일일 사용량 상한 (D10, 계획서 §13 "타인의 quota 소진" 방어).
 * BYOK LLM 키(X-Llm-Key)는 여기를 지나지 않는다 — 식별·과금과 키는 별개 축이다
 * ({@link #requireByok}는 키의 <b>유무</b>만 본다).
 *
 * <p>공개 모드({@code sjw.public-mode}, ADR-020 개정)는 "나 혼자 로컬" 전제를 걷어낸다:
 * 키 없는 요청이 default 테넌트(운영자)로 통과하지 않고, 운영자 키로 도는 경로는 허가된 테넌트만,
 * 동기·스트림의 캐시 미스는 BYOK만. 로컬 기본값(false)의 동작은 그대로다.
 */
@Component
public class TenantGuard {

    public static class UnknownApiKeyException extends RuntimeException {}

    /** 공개 모드에서 X-Api-Key 없음 → 401. */
    public static class ApiKeyRequiredException extends RuntimeException {}

    /** 운영자 키 경로(비동기·배치)를 허가받지 않은 테넌트 → 403. */
    public static class OperatorAccessRequiredException extends RuntimeException {}

    /** 공개 모드에서 캐시 미스인데 X-Llm-Key 없음 → 403 (§15.3 "미인증은 캐시 히트만"). */
    public static class ByokRequiredException extends RuntimeException {}

    public static class DailyLimitExceededException extends RuntimeException {
        public final int limit;

        public DailyLimitExceededException(int limit) {
            this.limit = limit;
        }
    }

    private final TenantRepository tenants;
    private final StringRedisTemplate redis;
    private final boolean publicMode;

    public TenantGuard(TenantRepository tenants, StringRedisTemplate redis,
                       @Value("${sjw.public-mode:false}") boolean publicMode) {
        this.tenants = tenants;
        this.redis = redis;
        this.publicMode = publicMode;
    }

    /**
     * 헤더 없음 → default 테넌트 (개발·단독 운영). 공개 모드면 401 — default는 운영자 자신이라
     * 거기로 떨어지면 누구나 운영자 quota를 쓴다. 미등록 키 → 401.
     */
    public Tenant resolve(String apiKeyHeader) {
        if (apiKeyHeader == null || apiKeyHeader.isBlank()) {
            if (publicMode) {
                throw new ApiKeyRequiredException();
            }
            return tenants.defaultTenant();
        }
        return tenants.findByApiKey(apiKeyHeader).orElseThrow(UnknownApiKeyException::new);
    }

    /** 운영자 키(GEMINI_API_KEY)로 도는 경로의 문 — 비동기 job·배치 생성. */
    public void requireOperatorAccess(Tenant tenant) {
        if (!tenant.operatorAccess()) {
            throw new OperatorAccessRequiredException();
        }
    }

    /**
     * 동기·스트림의 캐시 미스 지점에서 호출한다. 로컬 모드는 운영자 키로 폴백하므로 통과,
     * 공개 모드는 X-Llm-Key가 있어야 LLM을 부른다. 키 값은 보지도 남기지도 않는다.
     */
    public void requireByok(String llmKeyHeader) {
        if (publicMode && (llmKeyHeader == null || llmKeyHeader.isBlank())) {
            throw new ByokRequiredException();
        }
    }

    /** 리소스 소유권 — 남의 배치·job은 존재 자체를 드러내지 않는다(404로 답할 것). */
    public boolean owns(Tenant tenant, String resourceTenantId) {
        return tenant.id().equals(resourceTenantId);
    }

    /**
     * 소모 없이 상한 초과 여부만 확인한다 (M3-S3).
     *
     * <p>과금(charge)은 LLM 호출 직전으로 내려갔다 — 예산 단위가 LLM 호출 수이므로 캐시 히트와
     * 전처리 단계 실패는 상한을 소모해선 안 된다(§8.2, ADR-009). 그러면 이미 초과한 테넌트가
     * 전처리(NER 추론)를 공짜로 쓰게 되므로 그 앞에 이 빠른 거절을 둔다. 경합으로 1~2건 새는 것은
     * 허용한다: 정확한 방어선은 charge의 INCR이고 이쪽은 문 앞에서 돌려보내는 용도다.
     */
    public void ensureWithinCap(Tenant tenant) {
        String used = redis.opsForValue()
                .get(QueueKeys.budgetDaily(tenant.id(), LocalDate.now().toString()));
        if (used != null && Long.parseLong(used) >= tenant.dailyCallLimit()) {
            throw new DailyLimitExceededException(tenant.dailyCallLimit());
        }
    }

    /**
     * 일일 카운터에 호출 수를 선과금한다 (수용 시점 — 배치는 job 수만큼).
     * 초과분은 남겨둔다: 오늘은 이미 초과 상태라는 사실 자체가 정확한 기록이다.
     */
    public void charge(Tenant tenant, int calls) {
        String key = QueueKeys.budgetDaily(tenant.id(), LocalDate.now().toString());
        Long used = redis.opsForValue().increment(key, calls);
        redis.expire(key, Duration.ofDays(2));
        if (used != null && used > tenant.dailyCallLimit()) {
            throw new DailyLimitExceededException(tenant.dailyCallLimit());
        }
    }
}
