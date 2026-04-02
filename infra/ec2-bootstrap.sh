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

cat > /home/ec2-user/docker-compose.yml <<EOF
services:
  server:
    image: ${ECR_URL}/${REPO}:latest
    ports:
      - "8080:8080"
    environment:
      AWS_REGION: ${REGION}
    restart: unless-stopped
    volumes:
      - /home/ec2-user/config.json:/app/config.json:ro
EOF

cd /home/ec2-user
docker compose up -d

echo "==> Bootstrap complete. App running on port 8080, proxied via nginx."
echo "    Remember to copy your config.json to /home/ec2-user/config.json"
