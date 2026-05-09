# -*- coding: utf-8 -*-
"""
Утилиты для обхода антибот-защит в Playwright.
Скрывают navigator.webdriver, подделывают plugins/languages и прочие маркеры.
"""

STEALTH_JS = """
() => {
    // 1. Скрыть navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. Подделать chrome runtime
    if (!window.chrome) {
        window.chrome = {};
    }
    if (!window.chrome.runtime) {
        window.chrome.runtime = { PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' } };
    }

    // 3. Подделать plugins (пустой массив выдаёт headless)
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
        ],
    });

    // 4. Подделать languages
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'ru'] });

    // 5. Подделать permissions.query (Notification)
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);

    // 6. Скрыть automation-related свойства
    delete navigator.__proto__.webdriver;

    // 7. Подделать connection.rtt (headless часто = 0)
    if (navigator.connection) {
        Object.defineProperty(navigator.connection, 'rtt', { get: () => 50 });
    }

    // 8. Подделать WebGL vendor/renderer
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter.apply(this, arguments);
    };
}
"""


def apply_stealth(context):
    """Добавляет stealth-скрипт в Playwright BrowserContext."""
    context.add_init_script(STEALTH_JS)
    return context


# Типы ресурсов, которые не нужны для парсинга HTML карточек — они только замедляют.
_HEAVY_RESOURCE_TYPES = {"image", "media", "font"}

# Паттерны URL-ов аналитики/трекинга/рекламы: отрезаем, чтобы page.goto завершался быстрее.
_BLOCKED_URL_PATTERNS = (
    "google-analytics.com",
    "googletagmanager.com",
    "googletagservices.com",
    "googleadservices.com",
    "doubleclick.net",
    "googlesyndication.com",
    "adservice.google.",
    "facebook.net",
    "connect.facebook.net",
    "hotjar.com",
    "clarity.ms",
    "yandex.ru/metrika",
    "mc.yandex.ru",
    "tinypass.com",
    "cdn.segment.com",
    "mixpanel.com",
    "amplitude.com",
    "sentry.io",
    "cloudflareinsights.com",
    "hicdn.com",  # Alibaba/MiC трекинг
    "alicdn.com/tfs/",  # шрифты/иконки Alibaba
    "alibaba-inc.com/log",
    "acs.m.taobao.com",
    "go-mpulse.net",
    "logging.swiftype",
    ".ttf",
    ".woff",
    ".woff2",
)


def block_heavy_resources(context, *, block_images: bool = True, block_fonts: bool = True,
                          block_media: bool = True, block_analytics: bool = True) -> None:
    """
    Навешивает на BrowserContext маршрут, который аборта́ет тяжёлые ресурсы.
    Это сильно ускоряет page.goto(..., wait_until="domcontentloaded") —
    страница помечается как «загружена» сразу после HTML+CSS+JS без ожидания картинок.

    Работает и для HTTP, и для HTTPS, и для всех страниц внутри контекста.
    Функция безопасна для вызова до любых page.goto() и не ломает stealth.
    """
    blocked_types = set()
    if block_images:
        blocked_types.add("image")
    if block_media:
        blocked_types.add("media")
    if block_fonts:
        blocked_types.add("font")

    def _handler(route, request):
        try:
            rtype = request.resource_type
            url = request.url
            if rtype in blocked_types:
                return route.abort()
            if block_analytics:
                for pat in _BLOCKED_URL_PATTERNS:
                    if pat in url:
                        return route.abort()
            return route.continue_()
        except Exception:
            try:
                return route.continue_()
            except Exception:
                return None

    try:
        context.route("**/*", _handler)
    except Exception:
        pass
