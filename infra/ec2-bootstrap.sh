#!/bin/bash
# Bootstrap script — run once on a fresh EC2 Amazon Linux 2023 instance
# Usage: ssh ec2-user@<IP> 'bash -s' < infra/ec2-bootstrap.sh

set -e

REGION="us-west-2"
ACCOUNT_ID="057099254615"
ECR_URL="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
REPO="job-search-pipeline"

echo "==> Installing Docker"
sudo dnf install -y docker
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker ec2-user

echo "==> Installing Docker Compose plugin"
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

echo "==> Installing nginx and certbot"
sudo dnf install -y nginx python3-certbot-nginx

echo "==> Installing AWS CLI v2 (should already be present on AL2023)"
aws --version || sudo dnf install -y awscli

echo "==> Copying nginx config"
sudo cp /home/ec2-user/nginx.conf /etc/nginx/conf.d/job-search.conf
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl start nginx

echo "==> Obtaining TLS certificate"
sudo certbot --nginx -d jobs.fernflores.dev --non-interactive --agree-tos -m your-email@example.com

echo "==> Setting up certbot auto-renewal"
echo "0 3 * * * root certbot renew --quiet --post-hook 'systemctl reload nginx'" \
  | sudo tee /etc/cron.d/certbot-renew

echo "==> Logging into ECR"
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin "${ECR_URL}"

echo "==> Pulling and starting the app"
docker pull "${ECR_URL}/${REPO}:latest"

echo "==> Fetching DB password from SSM"
DB_PASSWORD=$(aws ssm get-parameter --region $REGION \
  --name /job-search/db-password-local --with-decryption \
  --query Parameter.Value --output text)

echo "==> Extracting schema.sql from image for first-boot init"
docker create --name tmp-schema "${ECR_URL}/${REPO}:latest" >/dev/null
docker cp tmp-schema:/app/db/schema.sql /home/ec2-user/schema.sql
docker rm tmp-schema >/dev/null

cat > /home/ec2-user/docker-compose.yml <<EOF
services:
  db:
    image: postgres:15
    environment:
      POSTGRES_DB: jobsearch
      POSTGRES_USER: jobsearch
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - /home/ec2-user/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U jobsearch"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  server:
    image: ${ECR_URL}/${REPO}:latest
    ports:
      - "8080:8080"
    environment:
      AWS_REGION: ${REGION}
      DB_HOST: db
      DB_NAME: jobsearch
      DB_USER: jobsearch
      DB_PASSWORD: ${DB_PASSWORD}
      DB_PORT: "5432"
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped
    volumes:
      - /home/ec2-user/config.json:/app/config.json:ro

volumes:
  pgdata:
EOF
chmod 600 /home/ec2-user/docker-compose.yml

echo "==> Installing nightly DB backup cron"
sudo install -m 0755 /home/ec2-user/backup-db.sh /usr/local/bin/backup-db.sh
sudo install -m 0755 /home/ec2-user/restore-db.sh /usr/local/bin/restore-db.sh
echo "0 3 * * * ec2-user /usr/local/bin/backup-db.sh >> /var/log/db-backup.log 2>&1" \
  | sudo tee /etc/cron.d/job-search-db-backup
sudo touch /var/log/db-backup.log
sudo chown ec2-user:ec2-user /var/log/db-backup.log

cd /home/ec2-user
docker compose up -d

echo "==> Bootstrap complete. App running on port 8080, proxied via nginx."
echo "    Postgres runs in the 'db' container; data persists in Docker volume 'pgdata'."
echo "    Nightly backup at 03:00 UTC -> s3://fernflores-job-search-resumes/db-backups/"
echo ""
echo "    Pre-bootstrap requirements (must be in /home/ec2-user/ before running this script):"
echo "      - config.json"
echo "      - nginx.conf"
echo "      - backup-db.sh"
echo "      - restore-db.sh"
echo ""
echo "    Required SSM parameter (create before bootstrap):"
echo "      /job-search/db-password-local (SecureString)"
