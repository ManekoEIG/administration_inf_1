FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
# один воркер: метрики prometheus_client живут в памяти процесса
CMD ["gunicorn", "-w", "1", "--threads", "32", "-b", "0.0.0.0:8000", "app:app"]
