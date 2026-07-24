FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for layer caching
COPY proxy/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# BLOCKER-2: Copy proxy source AND common/privacy shared module.
# Build context is now repo root (docker-compose: context: .)
COPY proxy/ ./proxy/
COPY common/ ./common/

WORKDIR /app/proxy

EXPOSE 8082

CMD ["python", "main.py"]
