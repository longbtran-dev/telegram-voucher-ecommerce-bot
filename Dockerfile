FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jollibee_bot ./jollibee_bot
COPY create_jollibee_barcode/assets ./create_jollibee_barcode/assets
COPY logic_check_jollibee_code/worker ./logic_check_jollibee_code/worker
COPY tests ./tests

CMD ["python", "-m", "jollibee_bot"]
