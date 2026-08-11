# Trading runtime image.
#
# The runtime is an ordinary Python process: it does not depend on an
# interactive Claude Code session (specification section 1.1).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

# The [ibkr] extra pulls in ib_async. Without it the runtime can only use the
# simulator, which would fail at connect time rather than at build time.
RUN pip install --no-cache-dir '.[ibkr]'

COPY config/ ./config/
COPY schemas/ ./schemas/

# Never run as root.
RUN useradd --create-home --uid 10001 trader \
    && mkdir -p /app/data /app/trades /app/reports \
    && chown -R trader:trader /app
USER trader

ENTRYPOINT ["python", "-m", "trading_system.cli"]
CMD ["--help"]
