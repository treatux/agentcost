# AgentCost - AI Agent 用量看板
FROM python:3.11-slim

LABEL org.opencontainers.image.title="AgentCost"
LABEL org.opencontainers.image.description="AI Agent Token/成本监控看板"
LABEL org.opencontainers.image.version="1.3.0"

WORKDIR /app

# 复制代码
COPY parser.py server.py check.py config.json models_override.json ./
COPY static/ ./static/

# 数据目录（挂载卷持久化 DB/配置）
RUN mkdir -p /data && chmod 777 /data

ENV AGENTCOST_DATA_DIR=/data
ENV AGENTCOST_PORT=8666
ENV AGENTCOST_REFRESH_SECONDS=300

EXPOSE 8666

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8666/api/health', timeout=3)" || exit 1

CMD ["python3", "server.py"]
