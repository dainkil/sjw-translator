package dev.sjw.common.llm;

import com.anthropic.client.AnthropicClient;
import com.anthropic.client.okhttp.AnthropicOkHttpClient;
import com.anthropic.core.JsonValue;
import com.anthropic.core.http.StreamResponse;
import com.anthropic.errors.AnthropicIoException;
import com.anthropic.errors.AnthropicServiceException;
import com.anthropic.models.messages.Message;
import com.anthropic.models.messages.MessageCreateParams;
import com.anthropic.models.messages.RawMessageStreamEvent;
import com.anthropic.models.messages.StopReason;
import java.time.Duration;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;
import reactor.core.publisher.Flux;
import reactor.core.scheduler.Schedulers;

/**
 * Anthropic Messages API 어댑터 (공식 Java SDK). 레지스트리 항목의 {@code provider: anthropic}이 고른다 (ADR-018).
 *
 * <p>실패는 {@link dev.sjw.common.failure.FailureClassifier}가 읽는 문장으로 바꿔 던진다 — 분류기는
 * 예외 메시지의 상태 코드·키워드로 재시도/일시정지/격리를 가르므로, 여기서 Gemini와 같은 어휘로 맞추면
 * 재시도·적응형 rate·DLQ 경로가 코드 수정 없이 그대로 돈다. 키는 메시지에 실리지 않는다.
 *
 * <p>SDK 내장 재시도는 끈다(maxRetries 0) — 재시도는 워커가 분류 결과로 정한다 (spring.ai.retry 1회와 같은 정책).
 */
public class ClaudeTranslator implements Translator {

    /**
     * 확장 사고를 끄는 모델. Sonnet 5.5는 {@code disabled}를 400으로 거부하고 {@code between_tools}만 받는다
     * (다른 모델은 반대로 between_tools를 거부한다). KB A/B 개정 등록의 "사고 끔" 조건 (docs/benchmarks.md).
     */
    private static final Set<String> THINKING_OFF = Set.of("claude-sonnet-5-5");
    private static final long MAX_TOKENS = 16_000L;

    private final AnthropicClient client;
    private final String modelId;

    public ClaudeTranslator(AnthropicClient client, String modelId) {
        this.client = client;
        this.modelId = modelId;
    }

    /** 운영자·BYOK 공통 — 하드 타임아웃(ms)을 건 클라이언트. 키는 이 클라이언트 밖으로 나가지 않는다. */
    static AnthropicClient client(String apiKey, int timeoutMs) {
        return AnthropicOkHttpClient.builder()
                .apiKey(apiKey)
                .timeout(Duration.ofMillis(timeoutMs))
                .maxRetries(0)
                .build();
    }

    @Override
    public String modelId() {
        return modelId;
    }

    MessageCreateParams params(String prompt) {
        var b = MessageCreateParams.builder()
                .model(modelId)
                .maxTokens(MAX_TOKENS)
                .addUserMessage(prompt);
        if (THINKING_OFF.contains(modelId)) {
            b.putAdditionalBodyProperty("thinking", JsonValue.from(Map.of("type", "between_tools")));
        }
        return b.build();
    }

    @Override
    public LlmReply call(String prompt) {
        Message resp;
        try {
            resp = client.messages().create(params(prompt));
        } catch (RuntimeException e) {
            throw classifiable(e);
        }
        var usage = resp.usage();
        Integer in = Math.toIntExact(usage.inputTokens());
        Integer out = Math.toIntExact(usage.outputTokens());
        if (resp.stopReason().filter(StopReason.REFUSAL::equals).isPresent()) {
            // 정상 200이지만 본문이 없다 — CONTENT_FILTERED로 격리된다 (재시도해도 같은 답).
            String category = resp.stopDetails().map(d -> String.valueOf(d.category())).orElse("unknown");
            throw new IllegalStateException("anthropic refusal: blocked by safety classifier (" + category + ")");
        }
        String text = resp.content().stream()
                .flatMap(block -> block.text().stream())
                .map(t -> t.text())
                .collect(Collectors.joining());
        return new LlmReply(text, in, out);
    }

    @Override
    public Flux<String> stream(String prompt) {
        return Flux.<String>create(sink -> {
            try (StreamResponse<RawMessageStreamEvent> s = client.messages().createStreaming(params(prompt))) {
                s.stream()
                        .flatMap(event -> event.contentBlockDelta().stream())
                        .flatMap(delta -> delta.delta().text().stream())
                        .forEach(t -> sink.next(t.text()));
                sink.complete();
            } catch (RuntimeException e) {
                sink.error(classifiable(e));
            }
        }).subscribeOn(Schedulers.boundedElastic());
    }

    /** SDK 예외 → 분류기 어휘. 원인은 cause로 보존한다. */
    static RuntimeException classifiable(RuntimeException e) {
        if (e instanceof AnthropicServiceException se) {
            String type = se.errorType().map(String::valueOf).orElse("");
            return new IllegalStateException(messageFor(se.statusCode(), type), e);
        }
        if (e instanceof AnthropicIoException) {
            // 하드 타임아웃·연결 끊김 — TIMEOUT(일시 오류)로 재시도 경로를 탄다
            return new IllegalStateException("anthropic io timeout: " + e.getClass().getSimpleName(), e);
        }
        return e;
    }

    /** 상태 코드 → 분류기가 읽는 문장. 529(overloaded)는 Gemini의 503과 같은 일시 오류로 본다. */
    static String messageFor(int code, String type) {
        return switch (code) {
            case 429 -> "429 rate_limit (anthropic " + type + ")";
            case 529 -> "503 unavailable (anthropic 529 overloaded)";
            case 401 -> "unauthenticated (anthropic 401 " + type + ")";
            case 403 -> "permission_denied (anthropic 403 " + type + ")";
            case 404 -> "404 not_found (anthropic " + type + ")";
            default -> code >= 500
                    ? "500 internal (anthropic " + code + " " + type + ")"
                    : "anthropic " + code + " " + type;
        };
    }
}
