package dev.sjw.common.failure;

import static org.junit.jupiter.api.Assertions.assertEquals;

import dev.sjw.common.translate.LlmParseException;
import org.junit.jupiter.api.Test;

/**
 * 분류 근거 문자열은 2026-08-31 실제 발생 로그에서 가져왔다 (docs/troubleshooting.md).
 * HTTP 429 하나가 세 가지 전혀 다른 운영 상황을 실어 나른다 — 이 테스트가 그 증거.
 */
class FailureClassifierTest {

    private final FailureClassifier c = new FailureClassifier();

    @Test
    void 지출상한_429는_SPEND_CAP() {
        var e = new RuntimeException("429 . Your project has exceeded its monthly spending cap. "
                + "Please go to AI Studio at https://ai.studio/spend");
        assertEquals(ErrorClass.SPEND_CAP, c.classify(e));
    }

    @Test
    void 일일quota_429는_QUOTA_DAILY() {
        var e = new RuntimeException("429 RESOURCE_EXHAUSTED. You exceeded your current quota. "
                + "quota_metric: GenerateRequestsPerDayPerProjectPerModel, quotaValue: 20");
        assertEquals(ErrorClass.QUOTA_DAILY, c.classify(e));
    }

    @Test
    void 분당_429는_RATE_LIMITED() {
        var e = new RuntimeException("429 RESOURCE_EXHAUSTED. quota_metric: "
                + "GenerateRequestsPerMinutePerProjectPerModel. Please retry in 55.6s");
        assertEquals(ErrorClass.RATE_LIMITED, c.classify(e));
    }

    @Test
    void 모델단종_404는_MODEL_UNAVAILABLE() {
        var e = new RuntimeException("404 NOT_FOUND. This model models/gemini-2.5-flash "
                + "is no longer available");
        assertEquals(ErrorClass.MODEL_UNAVAILABLE, c.classify(e));
    }

    @Test
    void 혼잡_503은_SERVER_ERROR() {
        var e = new RuntimeException("503 UNAVAILABLE. This model is currently experiencing high demand");
        assertEquals(ErrorClass.SERVER_ERROR, c.classify(e));
    }

    @Test
    void 타임아웃() {
        var e = new RuntimeException("The read operation timed out");
        assertEquals(ErrorClass.TIMEOUT, c.classify(e));
    }

    @Test
    void 파싱실패는_원인체인_어디에_있어도_PARSE_ERROR() {
        var e = new RuntimeException("wrapper",
                new LlmParseException("m", 800, 100, new IllegalStateException("bad json")));
        assertEquals(ErrorClass.PARSE_ERROR, c.classify(e));
    }

    @Test
    void 미분류는_UNKNOWN() {
        assertEquals(ErrorClass.UNKNOWN, c.classify(new RuntimeException("???")));
    }

    @Test
    void NER장애는_메시지에_timeout이_있어도_NER_UNAVAILABLE() {
        // "timeout" 문자열 매칭보다 타입 검사가 먼저여야 LLM TIMEOUT으로 오귀속되지 않는다
        var e = new RuntimeException("wrapper", new dev.sjw.common.ner.NerUnavailableException(
                "NER 서버 호출 실패: request timed out", new RuntimeException("timed out")));
        assertEquals(ErrorClass.NER_UNAVAILABLE, c.classify(e));
    }

    @Test
    void 타임아웃_값의_숫자가_상태코드로_오인되지_않는다() {
        // 2026-09-28 실측 회귀: contains("500")이 "5000ms"에 걸려 504가 아니라 502가 나갔다.
        // 하드 타임아웃을 5000·15000으로 두면 재현된다 — §5.0 1-3의 "넘기면 TIMEOUT" 위반.
        for (int ms : new int[] {500, 1500, 5000, 15000, 50000, 7000, 60000}) {
            var e = new RuntimeException("Request timed out after " + ms
                    + "ms [fake provider, model: fake-flash-lite]");
            assertEquals(ErrorClass.TIMEOUT, c.classify(e), ms + "ms 타임아웃");
        }
    }

    @Test
    void 상태코드는_단어경계로_찾는다() {
        // 진짜 상태 코드는 계속 잡혀야 한다 (단어 경계 도입의 반대 방향 회귀)
        assertEquals(ErrorClass.SERVER_ERROR, c.classify(new RuntimeException("500 Internal Server Error")));
        assertEquals(ErrorClass.SERVER_ERROR, c.classify(new RuntimeException("[503] backend overloaded")));
        assertEquals(ErrorClass.MODEL_UNAVAILABLE, c.classify(new RuntimeException("HTTP 404 on models/x")));
        // 숫자 안에 묻힌 코드는 상태 코드가 아니다
        assertEquals(ErrorClass.UNKNOWN, c.classify(new RuntimeException("processed 4290 tokens")));
        assertEquals(ErrorClass.UNKNOWN, c.classify(new RuntimeException("latency 5031ms")));
    }

    @Test
    void 잘못된_키는_AUTH_FAILED() {
        // Developer API가 잘못된 키에 돌려주는 본문 형태 (400 INVALID_ARGUMENT + reason API_KEY_INVALID)
        var e = new RuntimeException("400 Bad Request: API key not valid. Please pass a valid API key. "
                + "[reason: API_KEY_INVALID]");
        assertEquals(ErrorClass.AUTH_FAILED, c.classify(e));
        assertEquals(ErrorClass.AUTH_FAILED, c.classify(new RuntimeException("403 PERMISSION_DENIED")));
    }
}
