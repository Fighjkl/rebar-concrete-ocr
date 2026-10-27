package com.haitang.pdf2word.config;

import com.haitang.pdf2word.utils.PathUtils;
import lombok.RequiredArgsConstructor;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.ResourceHandlerRegistry;
import org.springframework.web.servlet.config.annotation.ViewControllerRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

@Configuration
@RequiredArgsConstructor
public class WebMvcConfig implements WebMvcConfigurer {

    private final FileProperties fileProperties;

    @Override
    public void addResourceHandlers(ResourceHandlerRegistry registry) {
        Path root = Paths.get(fileProperties.getRoot()).toAbsolutePath().normalize();
        PathUtils.setRoot(root);
        String docLocation = root.toUri().toString();
        if (!docLocation.endsWith("/")) {
            docLocation = docLocation + "/";
        }
        registry.addResourceHandler("/image/**")
                .addResourceLocations(docLocation)
                .setCachePeriod(3600);

        Path frontend = resolveFrontendDir(fileProperties.getFrontend());
        if (frontend != null) {
            String feLocation = frontend.toUri().toString();
            if (!feLocation.endsWith("/")) {
                feLocation = feLocation + "/";
            }
            // Hash router (#/history, #/upload, ...) means we only need to serve static assets and index.html.
            // RequestMapping handler has higher priority, so /api/** still wins over static handler.
            registry.addResourceHandler("/**")
                    .addResourceLocations(feLocation)
                    .setCachePeriod(0);
        }
    }

    @Override
    public void addViewControllers(ViewControllerRegistry registry) {
        // Forward root to the SPA entry. With Vue hash router this is enough; deeper paths go through the static handler.
        registry.addViewController("/").setViewName("forward:/index.html");
    }

    private Path resolveFrontendDir(String configured) {
        if (configured == null || configured.trim().isEmpty()) {
            return null;
        }
        Path configuredPath = Paths.get(configured);
        Path candidate = configuredPath.isAbsolute()
                ? configuredPath
                : Paths.get(System.getProperty("user.dir"), configured);
        candidate = candidate.toAbsolutePath().normalize();
        if (Files.exists(candidate.resolve("index.html"))) {
            return candidate;
        }
        Path moduleSibling = Paths.get(System.getProperty("user.dir"), "..", configured).toAbsolutePath().normalize();
        if (Files.exists(moduleSibling.resolve("index.html"))) {
            return moduleSibling;
        }
        return null;
    }
}