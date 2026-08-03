FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-render.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-render.txt

COPY . .

# Shell form so ${PORT} supplied by Render is expanded at start-up.
CMD python -m streamlit run streamlit_app.py \
    --server.address=0.0.0.0 \
    --server.port=${PORT:-8501} \
    --server.headless=true
