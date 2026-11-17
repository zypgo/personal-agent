FROM python:3.12-slim-bookworm AS builder
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir uv==0.13.6 && uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agent
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PA_HOME=/data \
    PA_CONTROL_ONLY=1
# Railway volumes are root-owned at mount time. deploy prepares only /data and drops to 10001
# before loading the application. Workloads execute on paired owned nodes, never here.
CMD ["python", "-m", "personal_agent.deploy"]
