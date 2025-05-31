FROM python:3.11-slim

WORKDIR /app

# Install curl and awscli so the container can access EC2 metadata
RUN apt-get update && apt-get install -y curl awscli && apt-get clean

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python", "patients_pipeline.py"]