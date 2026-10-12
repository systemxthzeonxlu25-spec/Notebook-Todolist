"""只信任直接连接来自本机的一层反向代理。"""
from werkzeug.middleware.proxy_fix import ProxyFix


class LoopbackProxyFix:
    def __init__(self, app):
        self.app = app
        self.proxy_app = ProxyFix(app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0)

    def __call__(self, environ, start_response):
        # 必须先检查原始连接地址，不能根据用户提供的转发头决定信任。
        if environ.get("REMOTE_ADDR") in {"127.0.0.1", "::1"}:
            return self.proxy_app(environ, start_response)
        return self.app(environ, start_response)
