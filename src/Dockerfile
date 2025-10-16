FROM python:3.12-slim

WORKDIR /src

COPY --from=ghcr.io/astral-sh/uv:0.8.14 /uv /uvx /bin/

COPY . .

RUN uv sync --locked

ENV PYTHONPATH=/

CMD ["uv", "run", "run.py"]