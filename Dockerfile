# Server by default; for the agent override the command, e.g.
#   docker run morphx-sim morphx-agent --server-url http://<server>:8000 --db /data/agent.sqlite3
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY morphx ./morphx
RUN pip install --no-cache-dir .

RUN useradd --uid 10001 morphx && mkdir /data && chown morphx /data
USER morphx
VOLUME /data
EXPOSE 8000

CMD ["morphx-server", "--host", "0.0.0.0", "--db", "/data/server.sqlite3"]
