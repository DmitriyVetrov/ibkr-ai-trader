# Trading runtime image.
#
# SCAFFOLD ONLY — built and used from Milestone 2 onwards. The runtime is an
# ordinary Python process: it does not depend on an interactive Claude Code
# session (specification section 1.1).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

COPY config/ ./config/
COPY schemas/ ./schemas/

# Never run as root.
RUN useradd --create-home --uid 10001 trader \
    && mkdir -p /app/data /app/trades /app/reports \
    && chown -R trader:trader /app
USER trader

ENTRYPOINT ["python", "-m", "trading_system.cli"]
CMD ["--help"]
