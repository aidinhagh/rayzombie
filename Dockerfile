FROM python:3.12-slim

WORKDIR /app

# deps first so they stay cached when only the code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# roster.db lives here; mount a Railway volume at /data and set
# DB_PATH=/data/roster.db so the group roster survives deploys
ENV DB_PATH=/data/roster.db

# the Web App is served from this same process; Railway injects $PORT
ENV PORT=8080
EXPOSE 8080

CMD ["python", "bot.py"]
