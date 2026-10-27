"""gunicorn settings for the VisionCast demo droplet (visioncast-web.service).

Installed to /srv/visioncast/deploy/gunicorn.conf.py. One worker process on a 1 GB
host: the per-process rate limits (webinars/ratelimit.py) then cover every request.
Loopback only; nginx is the only client. Access log format matches
docker/entrypoint.sh: path without query string, no client IP, referrer or user agent.
"""
bind = "127.0.0.1:8000"
workers = 1
threads = 2
timeout = 30
graceful_timeout = 30
# RuntimeDirectory=visioncast-web in the unit: tmpfs, private to the service.
worker_tmp_dir = "/run/visioncast-web"
max_requests = 2000
max_requests_jitter = 200
accesslog = "-"
access_log_format = '%(t)s "%(m)s %(U)s %(H)s" %(s)s %(B)s %(M)sms'
errorlog = "-"
