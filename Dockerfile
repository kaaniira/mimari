# Python İmajı
FROM python:3.10-slim

# Çalışma dizini
WORKDIR /app

# Kütüphaneleri yükle
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kodları kopyala
COPY . .

# Cloud Run için Port ayarı
ENV PORT 8080

# Uygulamayı başlat (main:app)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
