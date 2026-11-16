package dev.sjw.common.llm;

import com.google.genai.Client;
import com.google.genai.types.HttpOptions;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.google.genai.GoogleGenAiChatModel;
import org.springframework.ai.google.genai.GoogleGenAiChatOptions;

/**
 * 레지스트리 모델 id → Translator. 품질 게이트의 티어 승격(§5.4)과 BYOK(D10)가
 * 활성 싱글턴 밖의 클라이언트를 만드는 통로다. id는 레지스트리 검증을 거친다.
 *
 * <p>provider 축은 세 구현이다 (ADR-018 재검토 조건 이행): {@code google-genai}(실 API),
 * {@code anthropic}(실 API — {@link ClaudeTranslator}), {@code fake}(네트워크 0, quota 0 — {@link FakeProvider}).
 * 어느 쪽인지는 코드가 아니라 레지스트리 항목의 {@code provider}가 정한다 — "모델 교체 = 설정 변경"의 규칙 그대로.
 */
public class TranslatorFactory {

    public static final String PROVIDER_FAKE = "fake";
    public static final String PROVIDER_ANTHROPIC = "anthropic";
    public static final int DEFAULT_TIMEOUT_MS = 60_000;

    private final ChatClient.Builder builder;
    private final ModelRegistry registry;
    private final FakeProvider fake;
    private final int timeoutMs;
    private final String anthropicApiKey;
    /** 운영자 키 Anthropic 클라이언트 — 연결 풀을 공유하도록 프로세스당 하나. 첫 사용 시 만든다. */
    private com.anthropic.client.AnthropicClient anthropicOperator;

    public TranslatorFactory(ChatClient.Builder builder, ModelRegistry registry, FakeProvider fake) {
        this(builder, registry, fake, DEFAULT_TIMEOUT_MS);
    }

    public TranslatorFactory(ChatClient.Builder builder, ModelRegistry registry, FakeProvider fake,
                             int timeoutMs) {
        this(builder, registry, fake, timeoutMs, null);
    }

    public TranslatorFactory(ChatClient.Builder builder, ModelRegistry registry, FakeProvider fake,
                             int timeoutMs, String anthropicApiKey) {
        if (timeoutMs < 1) {
            throw new IllegalArgumentException("LLM 타임아웃은 1ms 이상: " + timeoutMs);
        }
        this.builder = builder;
        this.registry = registry;
        this.fake = fake;
        this.timeoutMs = timeoutMs;
        this.anthropicApiKey = anthropicApiKey;
    }

    static boolean isAnthropicSpec(ModelSpec spec) {
        return PROVIDER_ANTHROPIC.equals(spec.provider());
    }

    private synchronized com.anthropic.client.AnthropicClient anthropicOperator(String modelId) {
        if (anthropicOperator == null) {
            if (anthropicApiKey == null || anthropicApiKey.isBlank()) {
                throw new IllegalStateException("ANTHROPIC_API_KEY가 없어 " + modelId + "를 쓸 수 없다 (.env 확인)");
            }
            anthropicOperator = ClaudeTranslator.client(anthropicApiKey, timeoutMs);
        }
        return anthropicOperator;
    }

    /** 운영자·BYOK 클라이언트 공통 — 하드 타임아웃(ms)을 건 Developer API 클라이언트. */
    static Client genAiClient(String apiKey, int timeoutMs) {
        return Client.builder()
                .apiKey(apiKey)
                .httpOptions(HttpOptions.builder().timeout(timeoutMs).build())
                .build();
    }

    /** 운영자 키(자동 구성 ChatClient)로 — 티어 승격 등 내부 용도. */
    public Translator forModel(String modelId) {
        ModelSpec spec = registry.require(modelId);
        if (isFakeSpec(spec)) {
            return fake.translator(spec.id());
        }
        if (isAnthropicSpec(spec)) {
            return new ClaudeTranslator(anthropicOperator(spec.id()), spec.id());
        }
        return new GoogleGenAiTranslator(builder, spec.id());
    }

    /**
     * 이 모델의 결과를 <b>캐시에 적재해도 되는가</b>. fake 결과는 안 된다 — 캐시 키는
     * {@code {kb}:{prompt}:{ner}:{epoch}}이지 모델이 아니라서(ADR-009, 번역 LLM은 epoch 수동 손잡이),
     * 가짜 번역이 L1에 들어가면 같은 문장의 실 요청이 가짜를 받는다.
     */
    public boolean isFake(String modelId) {
        ModelSpec spec = modelId == null ? null : registry.all().stream()
                .filter(m -> m.id().equals(modelId)).findFirst().orElse(null);
        return spec != null && isFakeSpec(spec);
    }

    static boolean isFakeSpec(ModelSpec spec) {
        return PROVIDER_FAKE.equalsIgnoreCase(spec.provider());
    }

    /**
     * BYOK: 요청자가 가져온 LLM 키로 요청 단위 클라이언트를 만든다 (ADR-020).
     * 키는 이 스택 프레임 밖으로 나가지 않는다 — 저장·로깅 금지. 반환된 Translator를
     * 요청 처리 후 버리면 키도 함께 사라진다.
     */
    public Translator forModelWithKey(String modelId, String llmApiKey) {
        ModelSpec spec = registry.require(modelId);
        if (isFakeSpec(spec)) {
            return fake.translator(spec.id());   // 키가 필요 없다 — 저장·로깅 금지 규칙은 그대로 지켜진다 (안 쓰니까)
        }
        if (isAnthropicSpec(spec)) {
            // 요청 단위 클라이언트 — 반환된 Translator와 함께 버려지므로 키도 함께 사라진다
            return new ClaudeTranslator(ClaudeTranslator.client(llmApiKey, timeoutMs), spec.id());
        }
        var chatModel = GoogleGenAiChatModel.builder()
                .genAiClient(genAiClient(llmApiKey, timeoutMs))
                .options(GoogleGenAiChatOptions.builder().model(spec.id()).temperature(0.2).build())
                .build();
        return new GoogleGenAiTranslator(ChatClient.builder(chatModel), spec.id());
    }
}
