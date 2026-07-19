FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY audiobook_relay.py .

EXPOSE 8321

CMD ["python", "audiobook_relay.py"]
