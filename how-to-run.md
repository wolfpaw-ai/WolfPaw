# How to run Wolfpaw (single user)

Three paths. All three end with the React app reachable in a browser and the agent responding to chat.

You'll need an [Anthropic API key](https://console.anthropic.com/) in every path.

---

## 1. Localhost (testing)

**Prereqs:** Docker + Docker Compose.

```bash
git clone <repo-url> wolfpaw
cd wolfpaw
./install.sh                  # creates .env, asks for ANTHROPIC key
# edit .env → set WOLFPAW_ANTHROPIC_API_KEY
./install.sh                  # second run: builds + starts
```

Open `http://localhost:3000`. Enter any email at sign-in. The verify URL prints to the app's logs:

```bash
docker compose logs -f app | grep verify
```

Copy the URL, paste into your browser. Done.

Stop: `docker compose down`. Wipe everything: `docker compose down -v`.

---

## 2. AWS (easiest — single EC2 + Caddy)

**Prereqs:** AWS account, a domain you control, an SSH key in AWS.

1. **Launch an EC2 instance.** Amazon Linux 2023 (arm64), `t4g.small`, 20 GB gp3. Security group inbound: `22` (your IP), `80` + `443` (0.0.0.0/0). Attach an Elastic IP.

2. **Point DNS.** A record `ec2.wolfpaw.ai` → the Elastic IP.

3. **SSH in.** Install Docker (the AL2023 `docker` package doesn't bundle Compose v2 *or* buildx — grab both separately) + Caddy:

   ```bash
   sudo dnf install -y docker git
   sudo systemctl enable --now docker
   sudo usermod -aG docker ec2-user

   # Docker plugins (Compose v2 + buildx) — binary install
   sudo mkdir -p /usr/libexec/docker/cli-plugins
   sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-aarch64 \
     -o /usr/libexec/docker/cli-plugins/docker-compose
   sudo curl -SL https://github.com/docker/buildx/releases/download/v0.19.0/buildx-v0.19.0.linux-arm64 \
     -o /usr/libexec/docker/cli-plugins/docker-buildx
   sudo chmod +x /usr/libexec/docker/cli-plugins/docker-compose /usr/libexec/docker/cli-plugins/docker-buildx
   exit                                                # log back in for the docker group to take effect
   ```

   Caddy (binary install — AL2023 doesn't ship it). Quote the URL — the `&` is a shell metacharacter:

   ```bash
   curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=arm64" \
     | sudo tee /usr/local/bin/caddy > /dev/null
   sudo chmod +x /usr/local/bin/caddy
   caddy version                            # sanity check
   ```

4. **Bring up Wolfpaw.**

   ```bash
   git clone <repo-url> wolfpaw && cd wolfpaw
   ./install.sh
   # edit .env: set WOLFPAW_ANTHROPIC_API_KEY
   # set WOLFPAW_WEB_BASE_URL=https://ec2.wolfpaw.ai
   ./install.sh
   ```

5. **Run Caddy for TLS.** Create `/etc/caddy/Caddyfile`:

   ```caddy
   ec2.wolfpaw.ai {
       reverse_proxy localhost:3000
   }
   ```

   Run as a service:

   ```bash
   sudo caddy run --config /etc/caddy/Caddyfile &
   # for a real systemd unit, see https://caddyserver.com/docs/install
   ```

6. **Visit `https://ec2.wolfpaw.ai`.** Caddy gets a Let's Encrypt cert automatically.

---

## 3. AWS with Terraform

The OSS repo doesn't ship Terraform. Drop these two files into a new directory; they provision EC2 + Elastic IP + Route53. After `terraform apply`, SSH in and follow steps 3–6 from path 2.

**`main.tf`:**

```hcl
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" { region = var.region }

variable "region"           { default = "us-east-1" }
variable "domain"           {}                          # e.g. "ec2.wolfpaw.ai"
variable "zone_id"          {}                          # Route53 hosted zone id
variable "ssh_key_name"     {}                          # name of your EC2 key pair
variable "allowed_ssh_cidr" { default = "0.0.0.0/0" }   # lock this to your IP

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-arm64"]
  }
}

resource "aws_security_group" "wolfpaw" {
  name = "wolfpaw"
  ingress {
    from_port = 22, to_port = 22, protocol = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }
  ingress {
    from_port = 80, to_port = 80, protocol = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port = 443, to_port = 443, protocol = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port = 0, to_port = 0, protocol = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "wolfpaw" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = "t4g.small"
  key_name               = var.ssh_key_name
  vpc_security_group_ids = [aws_security_group.wolfpaw.id]
  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }
  tags = { Name = "wolfpaw" }
}

resource "aws_eip" "wolfpaw" {
  instance = aws_instance.wolfpaw.id
}

resource "aws_route53_record" "wolfpaw" {
  zone_id = var.zone_id
  name    = var.domain
  type    = "A"
  ttl     = 60
  records = [aws_eip.wolfpaw.public_ip]
}

output "ip"  { value = aws_eip.wolfpaw.public_ip }
output "ssh" { value = "ssh ec2-user@${aws_eip.wolfpaw.public_ip}" }
```

**`terraform.tfvars`:**

```hcl
domain       = "ec2.wolfpaw.ai"
zone_id      = "Z0123456ABCDEFGHIJ"
ssh_key_name = "my-key"
```

Then:

```bash
terraform init
terraform apply
ssh ec2-user@$(terraform output -raw ip)
# Now run steps 3–6 from path 2.
```

---

## 4. Raspberry Pi (local-first, no cloud infra)

All app code, Postgres, and file storage run on the Pi. The only outbound network calls are to Anthropic (model calls) and your communication channels (Telegram, Tavily, Voyage if you enable them).

**Prereqs:** Pi 4 (4 GB+) or Pi 5, 32 GB+ microSD (an SSD over USB 3 is better for DB write durability), **64-bit** Raspberry Pi OS (Lite is fine — no desktop needed).

1. **Install Docker:**

   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER && exit       # log back in
   ```

2. **Bring up Wolfpaw:**

   ```bash
   git clone <repo-url> wolfpaw && cd wolfpaw
   ./install.sh
   # edit .env: set WOLFPAW_ANTHROPIC_API_KEY
   # set WOLFPAW_WEB_BASE_URL=http://<pi-hostname>.local:3000  (or the Pi's IP)
   ./install.sh
   ```

3. **Access from another device on your LAN:** `http://<pi-hostname>.local:3000`.

**External access (optional, for Telegram or reaching the Pi from outside your home):**

- **Tailscale** is the simplest — install on the Pi, then reach it at `http://<pi-name>.tailnet.ts.net:3000`. For Telegram (which needs a public HTTPS webhook), enable Tailscale Funnel — no port-forwarding, no DDNS.
- **Port forwarding + DDNS + Caddy** is the traditional path. Same Caddyfile as section 2, plus a router port-forward and a dynamic-DNS provider.

**Pi-specific notes:**

- The Docker volumes (`wolfpaw_db` for Postgres, `wolfpaw_workspace` for files) live on the boot media by default. If that's a microSD card and you'll be writing a lot, move them to a USB SSD: edit `docker-compose.yml` to bind-mount `/mnt/ssd/wolfpaw_db` and `/mnt/ssd/wolfpaw_workspace` instead of named volumes.
- Memory budget: ~600 MB idle, ~1.5 GB peak (Sonnet call + matplotlib + Postgres + nginx). 4 GB is comfortable, 2 GB is tight, 1 GB will swap.
- Sandbox: stick with the default `subprocess` backend. E2B works but defeats the "no external compute" goal.

---

## After it's running (any path)

- **Telegram** (optional): create a bot via `@BotFather`, set `WOLFPAW_TELEGRAM_BOT_TOKEN` + `WOLFPAW_TELEGRAM_BOT_USERNAME` + `WOLFPAW_TELEGRAM_WEBHOOK_SECRET` in `.env`, `docker compose restart app`, then point the bot's webhook at `https://<your-domain>/channels/telegram/webhook`.
- **Voyage embeddings** (recommended for real retrieval): set `WOLFPAW_EMBEDDING_BACKEND=voyage` + `WOLFPAW_VOYAGE_API_KEY=...` in `.env`, restart.
- **Web search**: set `WOLFPAW_TAVILY_API_KEY=...`.
- **All other knobs:** [`.env.example`](.env.example).
- **Ops** (backup, update, troubleshooting): [`docs/self-host.md`](docs/self-host.md).
