"""低资源 VPS 配置：只接受本机 Caddy 的反向代理请求。"""

bind = "127.0.0.1:18765"
workers = 1
worker_class = "gthread"
threads = 2
timeout = 120
graceful_timeout = 30
keepalive = 3
max_requests = 400
max_requests_jitter = 50
preload_app = False
forwarded_allow_ips = "127.0.0.1"
secure_scheme_headers = {"X-FORWARDED-PROTO": "https"}
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190
# 不记录请求头、请求体或查询参数；应用也不应记录用户提供的 AI 密钥。
accesslog = None
errorlog = "-"
loglevel = "warning"
capture_output = False

