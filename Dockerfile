# The pk CLI, e.g. for pk sandbox mcp in a workspace.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[cli]'

USER 10001
ENTRYPOINT ["pk"]
