# Mock LLM image. Build context is this directory (demo/mock_llm).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

EXPOSE 8080

# OpenAI-compatible endpoint at http://<host>:8080/v1/chat/completions
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
