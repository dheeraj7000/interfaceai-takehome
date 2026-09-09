FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir flask==3.0.3 gunicorn==22.0.0

COPY . .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "30", "app:app"]
