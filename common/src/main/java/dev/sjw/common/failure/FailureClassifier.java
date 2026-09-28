package dev.sjw.common.failure;

import dev.sjw.common.ner.NerUnavailableException;
import dev.sjw.common.translate.LlmParseException;
import java.util.Locale;
import java.util.regex.Pattern;
import org.springframework.stereotype.Component;

/**
 * 예외 → ErrorClass. HTTP 코드가 같아도(429) 본문 메시지로 갈라야 한다 —
 * 지출 상한/일일 quota/순간 rate가 전부 429로 온다 (2026-08-31 실측, docs/troubleshooting.md §2).
 */
@Component
public class FailureClassifier {

    // 숫자 상태 코드는 단어 경계로 찾는다. contains("500")은 "5000ms"에도 걸려서,
    // 하드 타임아웃이 SERVER_ERROR로 오분류됐다 (2026-09-28 실측: timeout-ms 5000·15000에서
    // 504 LLM_TIMEOUT 대신 502 LLM_UPSTREAM_ERROR. 7000ms에서는 정상 — §5.0 1-3 수용 기준 위반).
    private static final Pattern CODE_429 = Pattern.compile("\\b429\\b");
    private static final Pattern CODE_404 = Pattern.compile("\\b404\\b");
    private static final Pattern CODE_503 = Pattern.compile("\\b503\\b");
    private static final Pattern CODE_500 = Pattern.compile("\\b500\\b");

    public ErrorClass classify(Throwable e) {
        if (find(e, LlmParseException.class) != null) {
            return ErrorClass.PARSE_ERROR;
        }
        // 메시지 매칭보다 먼저 — NER 타임아웃이 "timeout" 문자열로 LLM TIMEOUT에 오귀속되는 것을 막는다
        if (find(e, NerUnavailableException.class) != null) {
            return ErrorClass.NER_UNAVAILABLE;
        }
        String msg = messages(e).toLowerCase(Locale.ROOT);

        if (CODE_429.matcher(msg).find() || msg.contains("resource_exhausted")) {
            if (msg.contains("spending cap")) {
                return ErrorClass.SPEND_CAP;
            }
            if (msg.contains("perday") || msg.contains("per day")) {
                return ErrorClass.QUOTA_DAILY;
            }
            return ErrorClass.RATE_LIMITED;
        }
        // 404보다 먼저 — 키 거부 본문에 모델 경로가 섞여 와도 키 문제로 본다
        if (msg.contains("api_key_invalid") || msg.contains("api key not valid")
                || msg.contains("api key expired") || msg.contains("permission_denied")
                || msg.contains("unauthenticated")) {
            return ErrorClass.AUTH_FAILED;
        }
        if (CODE_404.matcher(msg).find() || msg.contains("not_found")
                || msg.contains("no longer available")) {
            return ErrorClass.MODEL_UNAVAILABLE;
        }
        if (CODE_503.matcher(msg).find() || msg.contains("unavailable")
                || CODE_500.matcher(msg).find() || msg.contains("internal")) {
            return ErrorClass.SERVER_ERROR;
        }
        if (msg.contains("timed out") || msg.contains("timeout")) {
            return ErrorClass.TIMEOUT;
        }
        if (msg.contains("safety") || msg.contains("blocked") || msg.contains("prohibited")) {
            return ErrorClass.CONTENT_FILTERED;
        }
        return ErrorClass.UNKNOWN;
    }

    private static String messages(Throwable e) {
        StringBuilder sb = new StringBuilder();
        for (Throwable t = e; t != null; t = t.getCause()) {
            sb.append(t.getClass().getSimpleName()).append(' ')
              .append(String.valueOf(t.getMessage())).append(' ');
            if (t == t.getCause()) {
                break;
            }
        }
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static <T extends Throwable> T find(Throwable e, Class<T> type) {
        for (Throwable t = e; t != null; t = t.getCause()) {
            if (type.isInstance(t)) {
                return (T) t;
            }
            if (t == t.getCause()) {
                break;
            }
        }
        return null;
    }
}
