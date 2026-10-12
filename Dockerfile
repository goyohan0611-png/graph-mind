# The MCP server alone, for directories that check a server starts and lists its tools (Glama).
# For real use install on the host instead (pip install graph-mind-memory; graph-mind-install):
# the capture service reads your AI apps' transcripts, which a container cannot see.
FROM python:3.12-slim
RUN pip install --no-cache-dir graph-mind-memory \
        --extra-index-url https://download.pytorch.org/whl/cpu
ENV PYTHONUTF8=1 GRAPH_MIND_HOME=/data
VOLUME /data
ENTRYPOINT ["graph-mind"]
