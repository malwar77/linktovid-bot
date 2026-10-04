FROM python:3.11-slim

# ffmpeg for video merging + mp3 conversion
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && apt-get clean

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

# Hugging Face Spaces expects the app to listen on this port
ENV PORT=7860
# HF has open networking: no websocket bridge needed, direct MTProto TCP
ENV BRIDGE_MODE=0
ENV PYTHONUNBUFFERED=1

EXPOSE 7860
CMD ["python", "-c", "import os; print('BOTLEN:', len(os.environ.get('BOT_TOKEN','')), 'APID:', os.environ.get('TELEGRAM_API_ID','MISS'), 'PING:', len(os.environ.get('SELF_PING_URL','')))"]
