FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-render.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-render.txt

COPY . .

CMD ["python", "-m", "streamlit", "run", "product_viewer_app.py", "--server.address=0.0.0.0", "--server.headless=true"]
