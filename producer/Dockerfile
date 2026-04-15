FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY log_producer.py ./

CMD ["python", "log_producer.py"]
