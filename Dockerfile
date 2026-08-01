FROM python:3.12-slim

# Europe/Budapest időzóna támogatása a Railway konténerben.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN mkdir -p /data

ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV CADDE34_DB_PATH=/data/bookings.db

EXPOSE 8000
VOLUME ["/data"]

# A feltöltött alkalmazás jelenleg ebben a beágyazott mappában található.
WORKDIR /app/cadde34-valodi-foglalasi-rendszer/cadde34-booking-system

CMD ["python", "server.py"]
