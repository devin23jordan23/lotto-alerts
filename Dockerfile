FROM python:3.13-slim
WORKDIR /app
COPY lotto /app/lotto
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data
RUN mkdir -p /app/data
CMD ["python", "-m", "lotto.main", "live"]
