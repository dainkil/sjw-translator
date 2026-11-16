package dev.sjw.api.docs;

import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;

/**
 * 루트를 번역 작업대(static/index.html)로 보낸다. Ingress가 `/`를 이 서비스로 넘기므로, 이게 없으면
 * 공개 주소의 첫 화면이 nginx 404다 — "URL 드릴게요"가 성립하지 않는다 (§5.0 Step 4).
 * API 문서(Swagger UI)는 작업대 상단의 링크로 연다.
 *
 * <p>정적 리소스 경로는 {@code TenantGuard}를 타지 않는다 — 문서 경로와 같은 이유다(OpenApiConfig).
 * 작업대는 키를 요청 헤더로만 실어 보낸다.
 */
@Controller
public class RootRedirectController {

    @GetMapping("/")
    public String root() {
        return "forward:/index.html";
    }
}
