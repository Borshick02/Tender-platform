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
