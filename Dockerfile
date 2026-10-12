# HumanAPI x402 facilitator
FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra postgres
COPY humanapi_facilitator ./humanapi_facilitator
RUN uv sync --frozen --no-dev --extra postgres \
 && useradd --system --uid 10001 app && mkdir -p /data && chown -R app /app /data
USER app
ENV HF_HOST=0.0.0.0 HF_PORT=8402 HF_DATABASE_URL=sqlite:////data/facilitator.db
EXPOSE 8402
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', os.environ['HF_PORT']), timeout=4)" || exit 1
CMD ["sh", "-c", "HF_PORT=${PORT:-$HF_PORT} && export HF_PORT && exec .venv/bin/humanapi-facilitator"]
