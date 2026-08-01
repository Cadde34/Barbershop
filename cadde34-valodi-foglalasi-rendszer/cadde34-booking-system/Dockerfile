FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
ENV PORT=8000

EXPOSE 8000
VOLUME ["/app/data"]

CMD ["python", "server.py"]
