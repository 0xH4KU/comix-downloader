(async function() {
    try {
        window.__comixUrlTransformers = window.__comixUrlTransformers || [];

        function __comixLooksLikeApiClient(value) {
            return value
                && typeof value === 'object'
                && typeof value.get === 'function'
                && typeof value.post === 'function';
        }

        window.__comixGetApiClient = window.__comixGetApiClient || async function() {
            if (window.__comixApiClient) return window.__comixApiClient;

            const scripts = Array.from(document.querySelectorAll('script[src]'));
            const moduleUrls = [];
            for (const script of scripts) {
                const src = script.getAttribute('src') || '';
                if (src.includes('/assets/build/') && src.endsWith('.js')) {
                    moduleUrls.push(new URL(src, window.location.href).href);
                }
            }

            for (const moduleUrl of moduleUrls) {
                try {
                    const mod = await import(moduleUrl);
                    for (const exported of Object.values(mod)) {
                        if (__comixLooksLikeApiClient(exported)) {
                            window.__comixApiClient = exported;
                            return window.__comixApiClient;
                        }
                    }
                } catch (e) { continue; }
            }

            const mainScript = scripts.find((script) => {
                const src = script.getAttribute('src') || '';
                return src.includes('/assets/build/')
                    && src.includes('/dist/main-')
                    && src.endsWith('.js');
            });
            if (mainScript) {
                try {
                    const moduleUrl = new URL(mainScript.getAttribute('src'), window.location.href).href;
                    const resp = await fetch(moduleUrl);
                    const text = await resp.text();
                    const envMatch = text.match(/from"\.\/(env-[^"]+\.js)"/);
                    if (envMatch) {
                        const envUrl = new URL(envMatch[1], moduleUrl).href;
                        const mod = await import(envUrl);
                        for (const exported of Object.values(mod)) {
                            if (__comixLooksLikeApiClient(exported)) {
                                window.__comixApiClient = exported;
                                return window.__comixApiClient;
                            }
                        }
                    }
                } catch (e) { /* fall through to fetch fallback */ }
            }

            return null;
        };

        window.__comixJsonRequest = async function(method, url, body) {
            const u = new URL(url, window.location.origin);
            if (u.origin !== window.location.origin || !u.pathname.startsWith('/api/v1/')) {
                return { __handled: false };
            }

            const path = u.pathname.replace(/^\/api\/v1/, '') || '/';
            const params = {};
            u.searchParams.forEach((rawValue, key) => {
                const numeric = rawValue !== '' ? Number(rawValue) : NaN;
                const value = Number.isNaN(numeric) ? rawValue : numeric;
                if (Object.prototype.hasOwnProperty.call(params, key)) {
                    params[key] = Array.isArray(params[key])
                        ? [...params[key], value]
                        : [params[key], value];
                } else {
                    params[key] = value;
                }
            });

            const api = await window.__comixGetApiClient();
            if (!api) return { __handled: false };

            const config = Object.keys(params).length ? { params } : undefined;
            const upper = String(method || 'GET').toUpperCase();

            try {
                if (upper === 'GET') {
                    return { __handled: true, data: await api.get(path, config) };
                }
                if (upper === 'POST') {
                    return { __handled: true, data: await api.post(path, body, config) };
                }
                if (upper === 'PUT') {
                    return { __handled: true, data: await api.put(path, body, config) };
                }
                if (upper === 'PATCH') {
                    return { __handled: true, data: await api.patch(path, body, config) };
                }
                if (upper === 'DELETE') {
                    return { __handled: true, data: await api.delete(path, config) };
                }
            } catch (e) {
                const status = e && e.response && e.response.status;
                if (status) throw new Error(`HTTP ${status}`);
                throw e;
            }
            return { __handled: false };
        };
    } catch (e) {
        console.warn('[comix-dl] comix.to API client hook install failed:', e);
    }
})();
