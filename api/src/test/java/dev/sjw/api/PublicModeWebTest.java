package dev.sjw.api;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import dev.sjw.api.error.LlmErrorHandler;
import dev.sjw.api.job.BatchController;
import dev.sjw.api.job.JobController;
import dev.sjw.api.tenant.TenantErrorHandler;
import dev.sjw.api.tenant.TenantGuard;
import dev.sjw.api.translation.TranslationController;
import dev.sjw.common.cache.CachedTranslation;
import dev.sjw.common.cache.TranslationCache;
import dev.sjw.common.failure.FailureClassifier;
import dev.sjw.common.job.BatchJobRepository;
import dev.sjw.common.job.BatchRow;
import dev.sjw.common.job.JobRow;
import dev.sjw.common.job.JobStatus;
import dev.sjw.common.job.TranslationJobRepository;
import dev.sjw.common.llm.FakeProvider;
import dev.sjw.common.llm.Translator;
import dev.sjw.common.llm.TranslatorFactory;
import dev.sjw.common.ner.NerUnavailableException;
import dev.sjw.common.quality.QualityGate;
import dev.sjw.common.tenant.Tenant;
import dev.sjw.common.tenant.TenantRepository;
import dev.sjw.common.translate.TranslationDtos.Meta;
import dev.sjw.common.translate.TranslationDtos.TranslationResponse;
import dev.sjw.common.translate.TranslationService;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

/**
 * 공개 모드 하드닝 (ADR-020 개정) — 컨트롤러 + 두 advice의 실제 조합을 DB·Redis·LLM 없이 검증한다.
 * advice 순서(테넌트 거절이 LLM 포괄 처리에 먹히지 않는가)와 SSE 요청의 JSON 거절까지 이 조합에서만 드러난다.
 */
class PublicModeWebTest {

    private static final String ALICE_KEY = "alice-key";
    private static final String BOB_KEY = "bob-key";
    private static final Tenant ALICE = new Tenant("alice", "alice", 50, false);
    private static final Tenant BOB = new Tenant("bob", "bob", 50, false);
    private static final Tenant OPERATOR = new Tenant("ops", "ops", 1000, true);
    private static final String BODY = "{\"text\":\"以金瑬爲承旨\",\"year\":1623}";

    private final TranslationService service = mock(TranslationService.class);
    private final TranslationCache cache = mock(TranslationCache.class);
    private final BatchJobRepository batches = mock(BatchJobRepository.class);
    private final TranslationJobRepository jobs = mock(TranslationJobRepository.class);
    private final TranslatorFactory factory = mock(TranslatorFactory.class);
    private MockMvc mvc;

    @BeforeEach
    void setUp() {
        TenantRepository tenants = mock(TenantRepository.class);
        when(tenants.findByApiKey(ALICE_KEY)).thenReturn(Optional.of(ALICE));
        when(tenants.findByApiKey(BOB_KEY)).thenReturn(Optional.of(BOB));
        when(tenants.findByApiKey("ops-key")).thenReturn(Optional.of(OPERATOR));
        StringRedisTemplate redis = mock(StringRedisTemplate.class);
        @SuppressWarnings("unchecked")
        ValueOperations<String, String> ops = mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(ops);
        when(ops.increment(anyString(), org.mockito.ArgumentMatchers.anyLong())).thenReturn(1L);

        TenantGuard guard = new TenantGuard(tenants, redis, true);
        when(cache.lookupL1(anyString())).thenReturn(Optional.empty());
        when(cache.lookupL2(anyString(), any())).thenReturn(Optional.empty());
        when(service.model()).thenReturn("gemini-x");
        when(factory.forModelWithKey(anyString(), anyString())).thenReturn(mock(Translator.class));
        when(service.prepare(anyString(), any())).thenReturn(
                new TranslationService.Prepared("prompt", List.of(), List.of(), Map.of()));

        mvc = MockMvcBuilders.standaloneSetup(
                        new TranslationController(service, factory, guard, cache,
                                mock(QualityGate.class), new SimpleMeterRegistry()),
                        new BatchController(batches, jobs, redis, guard, "unused.json"),
                        new JobController(jobs, redis, guard))
                .setControllerAdvice(new LlmErrorHandler(new FailureClassifier()), new TenantErrorHandler())
                .build();
    }

    @Test
    void 키_없는_요청은_401() throws Exception {
        mvc.perform(post("/api/v1/translations/sync").contentType(MediaType.APPLICATION_JSON).content(BODY))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.error").value("API_KEY_REQUIRED"));
    }

    @Test
    void BYOK_없어도_L1_캐시_히트는_응답한다() throws Exception {
        var hit = new CachedTranslation("김류를 승지로 삼았다", List.of(), List.of(), "gemini-x", "VERIFIED", "t");
        when(cache.lookupL1(anyString())).thenReturn(Optional.of(hit));
        when(cache.toResponse(any(), org.mockito.ArgumentMatchers.anyLong())).thenReturn(new TranslationResponse(
                hit.translatedText(), List.of(), List.of(), new Meta("gemini-x", "kb", "p", null, null, Map.of(), "L1_EXACT")));
        mvc.perform(post("/api/v1/translations/sync").header("X-Api-Key", ALICE_KEY)
                        .contentType(MediaType.APPLICATION_JSON).content(BODY))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.translatedText").value("김류를 승지로 삼았다"));
    }

    @Test
    void 캐시_미스인데_BYOK_없으면_403이고_LLM도_NER도_안_부른다() throws Exception {
        mvc.perform(post("/api/v1/translations/sync").header("X-Api-Key", ALICE_KEY)
                        .contentType(MediaType.APPLICATION_JSON).content(BODY))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.error").value("BYOK_REQUIRED"));
        verify(service, never()).prepare(anyString(), any());
    }

    @Test
    void SSE_요청도_거절은_JSON으로_나간다() throws Exception {
        mvc.perform(post("/api/v1/translations/stream").header("X-Api-Key", ALICE_KEY)
                        .accept(MediaType.TEXT_EVENT_STREAM)
                        .contentType(MediaType.APPLICATION_JSON).content(BODY))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.error").value("BYOK_REQUIRED"));
    }

    @Test
    void operator_access_없는_테넌트는_비동기_job과_배치를_못_만든다() throws Exception {
        mvc.perform(post("/api/v1/translations").header("X-Api-Key", ALICE_KEY)
                        .contentType(MediaType.APPLICATION_JSON).content(BODY))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.error").value("OPERATOR_ACCESS_REQUIRED"));
        mvc.perform(post("/api/v1/batches").header("X-Api-Key", ALICE_KEY)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"offset\":0,\"limit\":1,\"budgetLimitCalls\":1}"))
                .andExpect(status().isForbidden());
        verify(jobs, never()).insertPending(any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void 남의_배치는_조회도_일시정지도_재개도_404() throws Exception {
        UUID id = UUID.randomUUID();
        when(batches.findById(id)).thenReturn(Optional.of(
                new BatchRow(id, "{}", 10, 0, 10, 0, 0, "RUNNING", ALICE.id())));
        mvc.perform(get("/api/v1/batches/" + id).header("X-Api-Key", BOB_KEY)).andExpect(status().isNotFound());
        mvc.perform(post("/api/v1/batches/" + id + "/pause").header("X-Api-Key", BOB_KEY))
                .andExpect(status().isNotFound());
        mvc.perform(post("/api/v1/batches/" + id + "/resume").header("X-Api-Key", BOB_KEY))
                .andExpect(status().isNotFound());
        verify(batches, never()).transition(any(), anyString(), anyString());

        mvc.perform(get("/api/v1/batches/" + id).header("X-Api-Key", ALICE_KEY)).andExpect(status().isOk());
    }

    @Test
    void 남의_job은_404() throws Exception {
        UUID id = UUID.randomUUID();
        when(jobs.findById(id)).thenReturn(Optional.of(new JobRow(id, null, "x", null, "h", JobStatus.PENDING,
                null, null, null, null, null, null, ALICE.id(), null, null, null)));
        mvc.perform(get("/api/v1/translations/" + id).header("X-Api-Key", BOB_KEY)).andExpect(status().isNotFound());
        mvc.perform(get("/api/v1/translations/" + id).header("X-Api-Key", ALICE_KEY)).andExpect(status().isOk());
    }

    @Test
    void 분당_429는_429와_Retry_After로_나간다() throws Exception {
        when(service.translate(any(TranslationService.Prepared.class), any())).thenThrow(new FakeProvider.ProviderError(
                "429 RESOURCE_EXHAUSTED. Quota exceeded for metric GenerateRequestsPerMinutePerProjectPerModel-FreeTier. "
                        + "Please retry in 12.3s."));
        mvc.perform(post("/api/v1/translations/sync").header("X-Api-Key", ALICE_KEY).header("X-Llm-Key", "user-key")
                        .contentType(MediaType.APPLICATION_JSON).content(BODY))
                .andExpect(status().isTooManyRequests())
                .andExpect(header().string("Retry-After", "13"))
                .andExpect(jsonPath("$.error").value("LLM_RATE_LIMITED"));
    }

    @Test
    void NER_장애는_503() throws Exception {
        when(service.prepare(anyString(), any())).thenThrow(new NerUnavailableException("NER 서버 호출 실패"));
        mvc.perform(post("/api/v1/translations/sync").header("X-Api-Key", ALICE_KEY).header("X-Llm-Key", "user-key")
                        .contentType(MediaType.APPLICATION_JSON).content(BODY))
                .andExpect(status().isServiceUnavailable())
                .andExpect(header().exists("Retry-After"))
                .andExpect(jsonPath("$.error").value("NER_UNAVAILABLE"));
    }

    @Test
    void 잘못된_BYOK_키는_400이고_응답에_키가_실리지_않는다() throws Exception {
        String badKey = "AIza" + "b".repeat(35);
        when(service.translate(any(TranslationService.Prepared.class), any())).thenThrow(new RuntimeException(
                "400 Bad Request: API key not valid (key=" + badKey + ") [reason: API_KEY_INVALID]"));
        var body = mvc.perform(post("/api/v1/translations/sync").header("X-Api-Key", ALICE_KEY)
                        .header("X-Llm-Key", badKey)
                        .contentType(MediaType.APPLICATION_JSON).content(BODY))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.error").value("LLM_KEY_REJECTED"))
                .andReturn().getResponse().getContentAsString();
        org.junit.jupiter.api.Assertions.assertFalse(body.contains(badKey), body);
    }

    @Test
    void 본문_검증_실패는_LLM_처리기에_먹히지_않고_400() throws Exception {
        mvc.perform(post("/api/v1/translations/sync").header("X-Api-Key", ALICE_KEY)
                        .contentType(MediaType.APPLICATION_JSON).content("{not json"))
                .andExpect(status().isBadRequest());
    }
}
