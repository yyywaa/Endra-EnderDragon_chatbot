# Endra connector（coffeeroom 连接层，大脑在 alive-buddy）
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY connector ./connector
COPY scripts ./scripts
ENV LOG_LEVEL=INFO
CMD ["python", "-m", "connector.main"]
