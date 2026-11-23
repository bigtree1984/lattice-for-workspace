# Lattice の Web アプリ（Cloud Run、M4）。アプリのコードだけを入れる（dev_data・テスト・測定は入れない）
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -m lattice && rm -rf app/__pycache__ app/*/__pycache__
USER lattice
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'
