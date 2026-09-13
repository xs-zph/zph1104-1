FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# sentence-transformers / Chroma 的依赖优先使用预编译 wheel；保留基础编译工具兼容不同平台。
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
COPY requirements-ml.txt ./requirements-ml.txt
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install -r requirements-ml.txt

COPY app ./app
COPY data ./data
COPY mcp_servers ./mcp_servers
COPY prompts ./prompts
COPY scripts ./scripts
COPY static ./static
COPY templates ./templates
COPY run.py README.md .env.example ./

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data/chroma /app/logs \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["python", "run.py"]
