package dev.sjw.common.llm;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import dev.sjw.common.failure.ErrorClass;
import dev.sjw.common.failure.FailureClassifier;
import java.math.BigDecimal;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * Anthropic 어댑터 — 네트워크 0. 실패 문장이 기존 분류기에서 Gemini와 같은 처리 경로로 떨어지는지가 핵심이다
 * (재시도·적응형 rate·DLQ가 코드 수정 없이 도는 근거).
 */
class ClaudeTranslatorTest {

    private final FailureClassifier classifier = new FailureClassifier();

    private ErrorClass classify(int code) {
        return classifier.classify(new IllegalStateException(ClaudeTranslator.messageFor(code, "x")));
    }

    @Test
    void 상태_코드가_Gemini와_같은_처리_경로로_분류된다() {
        assertEquals(ErrorClass.RATE_LIMITED, classify(429));
        assertEquals(ErrorClass.SERVER_ERROR, classify(529));   // overloaded — 일시 오류
        assertEquals(ErrorClass.SERVER_ERROR, classify(500));
        assertEquals(ErrorClass.AUTH_FAILED, classify(401));
        assertEquals(ErrorClass.AUTH_FAILED, classify(403));
        assertEquals(ErrorClass.MODEL_UNAVAILABLE, classify(404));
    }

    @Test
    void 거절과_타임아웃() {
        assertEquals(ErrorClass.CONTENT_FILTERED, classifier.classify(
                new IllegalStateException("anthropic refusal: blocked by safety classifier (cyber)")));
        assertEquals(ErrorClass.TIMEOUT, classifier.classify(
                new IllegalStateException("anthropic io timeout: AnthropicIoException")));
    }

    private static ModelSpec spec(String id, String provider) {
        return new ModelSpec(id, provider, "T1", null, null, BigDecimal.ZERO, BigDecimal.ZERO);
    }

    private static TranslatorFactory factory(String anthropicKey) {
        var registry = new ModelRegistry(new LlmProperties("claude-sonnet-5-5", null,
                List.of(spec("claude-sonnet-5-5", "anthropic"))));
        return new TranslatorFactory(null, registry, new FakeProvider(FakeLlmProperties.defaults()),
                TranslatorFactory.DEFAULT_TIMEOUT_MS, anthropicKey);
    }

    @Test
    void anthropic_provider는_ClaudeTranslator를_만든다() {
        var t = factory("test-key").forModel("claude-sonnet-5-5");
        assertInstanceOf(ClaudeTranslator.class, t);
        assertEquals("claude-sonnet-5-5", t.modelId());
        assertInstanceOf(ClaudeTranslator.class, factory(null).forModelWithKey("claude-sonnet-5-5", "byok-key"));
    }

    @Test
    void 운영자_키가_없으면_모델을_쓰는_순간_실패한다() {
        var e = assertThrows(IllegalStateException.class, () -> factory("").forModel("claude-sonnet-5-5"));
        assertTrue(e.getMessage().contains("ANTHROPIC_API_KEY"));
    }
}
