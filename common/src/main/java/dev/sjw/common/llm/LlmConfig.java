package dev.sjw.common.llm;

import com.google.genai.Client;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/** 레지스트리·활성 Translator 조립. BYOK(M2.5-S6)에서 요청 단위 생성으로 확장된다 (ADR-020). */
@Configuration
@EnableConfigurationProperties({LlmProperties.class, FakeLlmProperties.class})
public class LlmConfig {

    @Bean
    public ModelRegistry modelRegistry(LlmProperties props) {
        return new ModelRegistry(props);
    }

    /** 프로세스 단위 싱글턴 — quota 창·난수·지연이 여기 산다 (호출마다 Translator가 새로 만들어져도 유지). */
    @Bean
    public FakeProvider fakeProvider(FakeLlmProperties cfg,
                                     @Value("${sjw.llm.timeout-ms:60000}") int timeoutMs) {
        return new FakeProvider(cfg, java.time.Clock.systemUTC(), timeoutMs);
    }

    /**
     * 운영자 키 클라이언트 — 자동 구성의 Client(@ConditionalOnMissingBean)를 대체해 <b>하드 타임아웃</b>을 건다.
     * 타임아웃이 없으면 응답 없는 호출 하나가 job을 RUNNING에 붙잡는다 (26분 선례, §5.0 1-3).
     * 넘기면 예외 메시지의 "timeout"으로 {@code TIMEOUT}(일시 오류)에 분류되어 기존 재시도·서킷 경로를 탄다.
     * Developer API 모드 전용이다 — Vertex 설정은 원래 금지다(ADR-005 §4.3).
     */
    @Bean
    public Client googleGenAiClient(@Value("${spring.ai.google.genai.api-key}") String apiKey,
                                    @Value("${sjw.llm.timeout-ms:60000}") int timeoutMs) {
        return TranslatorFactory.genAiClient(apiKey, timeoutMs);
    }

    /** Anthropic 운영자 키는 provider: anthropic 모델을 쓸 때만 필요하다 — 비어 있어도 기동은 된다. */
    @Bean
    public TranslatorFactory translatorFactory(ChatClient.Builder builder, ModelRegistry registry,
                                               FakeProvider fake,
                                               @Value("${sjw.llm.timeout-ms:60000}") int timeoutMs,
                                               @Value("${ANTHROPIC_API_KEY:}") String anthropicApiKey) {
        return new TranslatorFactory(builder, registry, fake, timeoutMs, anthropicApiKey);
    }

    @Bean
    public Translator translator(TranslatorFactory factory, ModelRegistry registry) {
        return factory.forModel(registry.active().id());
    }
}
