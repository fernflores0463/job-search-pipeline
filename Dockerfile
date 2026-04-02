FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY dashboard/ ./dashboard/
COPY db/ ./db/
COPY process_new_postings.py ./
COPY scrape_careers_page.py ./
COPY sample_data/ ./sample_data/
COPY config.example.json ./

EXPOSE 8080

CMD ["python3", "dashboard/server.py"]
