package dev.sjw.api.docs;

import io.swagger.v3.oas.models.Components;
import io.swagger.v3.oas.models.OpenAPI;
import io.swagger.v3.oas.models.info.Info;
import io.swagger.v3.oas.models.info.License;
import io.swagger.v3.oas.models.security.SecurityScheme;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * OpenAPI 문서 — 공개 엔드포인트의 진입점이다 (§5.0 Step 4). 인증 헤더가 두 축이라
 * 자동 생성 스펙만으로는 "왜 키가 두 개인가"가 드러나지 않아 여기서 설명을 붙인다.
 *
 * <p>문서 경로(`/v3/api-docs`, `/swagger-ui/**`)는 {@code TenantGuard}를 타지 않는다 —
 * guard가 컨트롤러 인자 해석 단계에서 동작하기 때문이다 (공개 모드에서 키 없이 200 실측).
 */
@Configuration
public class OpenApiConfig {

    @Bean
    public OpenAPI sjwOpenApi(@Value("${sjw.public-mode:false}") boolean publicMode) {
        String byok = publicMode
                ? "**공개 모드다.** 모든 요청에 `X-Api-Key`가 필요하고(없으면 401), "
                  + "캐시에 없는 문장은 `X-Llm-Key`(본인 Gemini 키)까지 필요하다(없으면 403 `BYOK_REQUIRED`). "
                  + "캐시에 있는 문장은 `X-Api-Key`만으로 응답한다."
                : "로컬 모드다. 키 없이 호출하면 `default` 테넌트로 처리된다.";

        return new OpenAPI()
                .info(new Info()
                        .title("승정원일기 번역 API")
                        .version("v1")
                        .description("""
                                조선 인조대 승정원일기 한문 → 현대 한국어 번역.

                                KB(인물 2,690명) 조회 → NER(SillokBERT ONNX INT8) → 프롬프트 조립 → LLM 호출.
                                KB 주입으로 인명 환각을 억제한다 (LLM 단독 대비 ETS 0.9038 → 0.9712).

                                %s

                                **왜 본인 키를 받나:** 운영자 무료 quota가 하루 20회라 공개하면 한 사람이
                                오전에 소진한다. 번역 비용은 요청자 키로 나간다 (ADR-020).
                                본인 키는 저장·로깅하지 않고 요청이 끝나면 버린다.
                                """.formatted(byok))
                        .license(new License().name("포트폴리오 프로젝트")))
                .components(new Components()
                        .addSecuritySchemes("ApiKey", new SecurityScheme()
                                .type(SecurityScheme.Type.APIKEY)
                                .in(SecurityScheme.In.HEADER)
                                .name("X-Api-Key")
                                .description("발급받은 테넌트 키. 일일 호출 상한이 이 키에 걸린다."))
                        .addSecuritySchemes("Byok", new SecurityScheme()
                                .type(SecurityScheme.Type.APIKEY)
                                .in(SecurityScheme.In.HEADER)
                                .name("X-Llm-Key")
                                .description("본인 Gemini API 키. 캐시 미스일 때만 쓰인다 — 저장하지 않는다.")));
    }
}
