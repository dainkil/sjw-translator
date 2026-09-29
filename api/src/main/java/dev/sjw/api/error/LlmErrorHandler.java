package dev.sjw.api.error;

import dev.sjw.common.failure.ErrorClass;
import dev.sjw.common.failure.FailureClassifier;
import dev.sjw.common.failure.RetryAfterHint;
import dev.sjw.common.util.SecretMasker;
import jakarta.servlet.http.HttpServletRequest;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageConversionException;
import org.springframework.web.ErrorResponse;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/**
 * 동기·스트림 경로의 실패 → HTTP 상태. 워커와 <b>같은 분류기</b>를 쓴다 —
 * 429가 지출 상한·일일 quota·순간 rate 셋으로 갈리는 것은 경로와 무관한 사실이다.
 * 이전에는 전부 500이었다: 클라이언트가 "기다리면 되는지(429)"와 "고쳐야 하는지(4xx)"를 구분할 수 없었다.
 *
 * <p>테넌트 예외({@code TenantErrorHandler})보다 뒤에 선다 — 여기의 RuntimeException 포괄 처리가
 * 그쪽 401/403/429를 삼키면 안 된다. 스프링 자체 예외(본문 파싱 실패 등)는 기본 처리로 돌려보낸다.
 *
 * <p>응답 본문에 provider 원문 메시지를 싣지 않는다 — 키·요청 URL이 되비칠 수 있다. 로그에만, 가려서.
 */
@RestControllerAdvice
@Order(Ordered.LOWEST_PRECEDENCE)
public class LlmErrorHandler {

    private static final Logger log = LoggerFactory.getLogger(LlmErrorHandler.class);

    /** provider가 재시도 힌트를 주지 않았을 때의 Retry-After (분당 창의 길이). */
    static final Duration DEFAULT_RETRY_AFTER = Duration.ofSeconds(60);
    static final Duration NER_RETRY_AFTER = Duration.ofSeconds(10);

    private final FailureClassifier classifier;

    public LlmErrorHandler(FailureClassifier classifier) {
        this.classifier = classifier;
    }

    @ExceptionHandler(RuntimeException.class)
    public ResponseEntity<Map<String, Object>> handle(RuntimeException e, HttpServletRequest request) {
        if (e instanceof ErrorResponse || e instanceof HttpMessageConversionException) {
            throw e;   // 원래 예외를 다시 던지면 다음 resolver(스프링 기본 400/404 등)가 처리한다
        }
        String llmKey = request.getHeader("X-Llm-Key");
        boolean byok = llmKey != null && !llmKey.isBlank();
        ErrorClass c = classifier.classify(e);

        Mapping m = map(c, byok);
        String detail = SecretMasker.mask(rootMessage(e), llmKey);
        if (m.status().is5xxServerError()) {
            log.error("{} {} 실패 [{}] → {}: {}", request.getMethod(), request.getRequestURI(), c,
                    m.status().value(), detail);
        } else {
            log.warn("{} {} 거절 [{}] → {}: {}", request.getMethod(), request.getRequestURI(), c,
                    m.status().value(), detail);
        }

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", m.code());
        body.put("errorClass", c.name());
        var resp = ResponseEntity.status(m.status()).contentType(MediaType.APPLICATION_JSON);
        if (m.retryAfter()) {
            Duration wait = c == ErrorClass.NER_UNAVAILABLE ? NER_RETRY_AFTER
                    : RetryAfterHint.parse(e).orElse(DEFAULT_RETRY_AFTER);
            long seconds = Math.max(1, (wait.toMillis() + 999) / 1000);   // 올림 — 너무 일찍 두드리지 않게
            resp.header(HttpHeaders.RETRY_AFTER, Long.toString(seconds));
            body.put("retryAfterSeconds", seconds);
        }
        return resp.body(body);
    }

    record Mapping(HttpStatus status, String code, boolean retryAfter) {}

    /**
     * 분류 → 상태. 키 거부는 BYOK면 요청자 잘못(400), 운영자 키면 서버 설정 잘못(502)이다.
     * 지출 상한도 같은 축 — 운영자 키는 결제 미연동(ADR-016)이라 사실상 BYOK 프로젝트의 상한이다.
     */
    static Mapping map(ErrorClass c, boolean byok) {
        return switch (c) {
            case RATE_LIMITED -> new Mapping(HttpStatus.TOO_MANY_REQUESTS, "LLM_RATE_LIMITED", true);
            case QUOTA_DAILY -> new Mapping(HttpStatus.TOO_MANY_REQUESTS, "LLM_QUOTA_DAILY", true);
            case SPEND_CAP -> byok
                    ? new Mapping(HttpStatus.FORBIDDEN, "LLM_SPEND_CAP", false)
                    : new Mapping(HttpStatus.BAD_GATEWAY, "LLM_SPEND_CAP", false);
            case AUTH_FAILED -> byok
                    ? new Mapping(HttpStatus.BAD_REQUEST, "LLM_KEY_REJECTED", false)
                    : new Mapping(HttpStatus.BAD_GATEWAY, "LLM_UPSTREAM_AUTH", false);
            case CONTENT_FILTERED -> new Mapping(HttpStatus.UNPROCESSABLE_ENTITY, "LLM_CONTENT_FILTERED", false);
            case NER_UNAVAILABLE -> new Mapping(HttpStatus.SERVICE_UNAVAILABLE, "NER_UNAVAILABLE", true);
            case TIMEOUT -> new Mapping(HttpStatus.GATEWAY_TIMEOUT, "LLM_TIMEOUT", false);
            case SERVER_ERROR -> new Mapping(HttpStatus.BAD_GATEWAY, "LLM_UPSTREAM_ERROR", false);
            case MODEL_UNAVAILABLE -> new Mapping(HttpStatus.BAD_GATEWAY, "LLM_MODEL_UNAVAILABLE", false);
            case PARSE_ERROR -> new Mapping(HttpStatus.BAD_GATEWAY, "LLM_PARSE_ERROR", false);
            case UNKNOWN -> new Mapping(HttpStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", false);
        };
    }

    private static String rootMessage(Throwable e) {
        Throwable root = e;
        while (root.getCause() != null && root.getCause() != root) {
            root = root.getCause();
        }
        String m = root.getClass().getSimpleName() + ": " + root.getMessage();
        return m.length() > 300 ? m.substring(0, 300) : m;
    }
}
