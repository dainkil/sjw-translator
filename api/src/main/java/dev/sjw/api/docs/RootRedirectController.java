package dev.sjw.api.docs;

import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;

/**
 * 루트를 API 문서로 보낸다. Ingress가 `/`를 이 서비스로 넘기므로, 이게 없으면 공개 주소의
 * 첫 화면이 nginx 404다 — "URL 드릴게요"가 성립하지 않는다 (§5.0 Step 4).
 */
@Controller
public class RootRedirectController {

    @GetMapping("/")
    public String root() {
        return "redirect:/swagger-ui.html";
    }
}
