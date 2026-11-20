# Official release image, pinned to the multi-platform manifest; no runtime download.
FROM restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 AS restic

FROM python:3.12-slim-bookworm AS builder
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir uv==0.13.6 && uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --home-dir /data --no-create-home --shell /usr/sbin/nologin agent
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=restic /usr/bin/restic /usr/local/bin/restic
COPY docs/licenses/restic-BSD-2-Clause.txt /usr/share/doc/restic/LICENSE
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PA_HOME=/data \
    PA_CONTROL_ONLY=1
# Railway volumes are root-owned at mount time. deploy prepares only /data and drops to 10001
# before loading the application. Workloads execute on paired owned nodes, never here.
CMD ["python", "-m", "personal_agent.deploy"]
