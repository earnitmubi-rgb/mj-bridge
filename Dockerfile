FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Hugging Face Spaces expects the app on port 7860
ENV PORT=7860

EXPOSE 7860

CMD ["python", "server.py"]
