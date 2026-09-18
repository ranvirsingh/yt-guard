FROM python:3.13.13-alpine
RUN pip install --no-cache-dir pyytlounge==3.4.0 aiohttp==3.14.3
WORKDIR /app
COPY yt_guard.py ui.html ./
USER 1000
ENV PYTHONUNBUFFERED=1 AUTH_FILE=/tmp/auth.json
CMD ["python", "yt_guard.py"]
