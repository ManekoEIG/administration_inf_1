import json
import logging
import os
import random
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, jsonify, request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

PORT = int(os.getenv("PORT", "8000"))
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "demo-service")

# ---------------------------------------------------------------- tracing
# Адрес берётся из OTEL_EXPORTER_OTLP_ENDPOINT (например http://jaeger:4317)
provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)


# ---------------------------------------------------------------- logging
class JsonFormatter(logging.Formatter):
    def format(self, record):
        ctx = trace.get_current_span().get_span_context()
        data = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "message": record.getMessage(),
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else "",
        }
        data.update(getattr(record, "fields", {}))
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger("demo")
log.setLevel(logging.INFO)
log.addHandler(handler)
log.propagate = False

# ---------------------------------------------------------------- metrics (RED)
REQUESTS = Counter("http_requests_total", "Всего HTTP-запросов", ["method", "endpoint", "status"])
ERRORS = Counter("http_errors_total", "Всего ответов 5xx", ["endpoint"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "Время ответа, сек",
    ["endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 2.5, 3, 5, 10),
)

app = Flask(__name__)
# /metrics и /health не трейсим, чтобы не засорять Jaeger
FlaskInstrumentor().instrument_app(app, excluded_urls="metrics,health")


def endpoint_label():
    return request.url_rule.rule if request.url_rule else "unmatched"


@app.before_request
def _start_timer():
    request._t0 = time.perf_counter()


@app.after_request
def _record(resp):
    if request.path == "/metrics":
        return resp
    ep = endpoint_label()
    REQUESTS.labels(request.method, ep, str(resp.status_code)).inc()
    LATENCY.labels(ep).observe(time.perf_counter() - getattr(request, "_t0", time.perf_counter()))
    if resp.status_code >= 500:
        ERRORS.labels(ep).inc()
    return resp


# ---------------------------------------------------------------- pages
PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Demo service</title>
<style>
body{font-family:system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px}
button{font-size:16px;padding:10px 18px;margin:6px 6px 6px 0;cursor:pointer}
pre{background:#f4f4f4;padding:12px;border-radius:6px;min-height:60px;white-space:pre-wrap}
</style></head><body>
<h1>Demo service</h1>
<button onclick="hit('/error')">Создать ошибку</button>
<button onclick="hit('/slow')">Создать задержку</button>
<button onclick="hit('/load')">Нагрузка</button>
<pre id="out">нажми кнопку…</pre>
<script>
async function hit(p){
  const out=document.getElementById('out'); out.textContent='… '+p;
  const t=performance.now();
  try{
    const r=await fetch(p,{method:'POST'});
    out.textContent=p+' → HTTP '+r.status+' за '+Math.round(performance.now()-t)+' мс\\n'+await r.text();
  }catch(e){out.textContent=String(e)}
}
</script></body></html>"""


@app.get("/")
def index():
    log.info("index page opened")
    return Response(PAGE, mimetype="text/html")


@app.get("/health")
def health():
    return "ok"


@app.get("/ping")
def ping():
    return jsonify(status="pong")


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------- buttons
@app.post("/error")
def make_error():
    span = trace.get_current_span()
    exc = RuntimeError("искусственная ошибка по кнопке")
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))
    log.error("simulated failure", extra={"fields": {"endpoint": "/error"}})
    return jsonify(error=str(exc)), 500


@app.post("/slow")
def make_slow():
    delay = random.uniform(1, 3)
    log.info("slow request started", extra={"fields": {"delay_s": round(delay, 2)}})
    with tracer.start_as_current_span("slow-dependency") as span:
        span.set_attribute("delay.seconds", delay)
        time.sleep(delay)
    log.info("slow request finished")
    return jsonify(slept=round(delay, 2))


def _self_call(_):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/ping", timeout=5).read()
    except Exception:
        pass


def _run_load(n):
    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(_self_call, range(n)))
    log.info("load finished", extra={"fields": {"requests": n}})


@app.post("/load")
def make_load():
    n = 600
    log.info("load started", extra={"fields": {"requests": n}})
    threading.Thread(target=_run_load, args=(n,), daemon=True).start()
    return jsonify(started=n), 202


# ---------------------------------------------------------------- alert receiver
@app.post("/alert-webhook")
def alert_webhook():
    payload = request.get_json(silent=True) or {}
    for a in payload.get("alerts", []):
        log.warning(
            "ALERT " + a.get("status", "?") + ": " + a.get("labels", {}).get("alertname", "?"),
            extra={"fields": {"summary": a.get("annotations", {}).get("summary", "")}},
        )
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
