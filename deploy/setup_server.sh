#!/usr/bin/env bash
# GroupK 근태앱 서버 자동 설치 (Ubuntu 22.04 / 24.04)
#
# 사용법 (서버에서, 저장소를 /opt/groupk 에 받은 뒤):
#   sudo bash /opt/groupk/deploy/setup_server.sh hr.example.co.kr
#
# 수행 내용: 시간대(KST) · 스왑 · 방화벽 · Python 가상환경 · gunicorn 서비스 ·
#            Caddy(HTTPS 자동 인증서) · 일일 백업(30일 보관)
# 여러 번 실행해도 안전합니다(업데이트 후 재실행 가능).
set -euo pipefail

DOMAIN="${1:-}"
APP_DIR="/opt/groupk"
APP_USER="groupk"
DATA_DIR="$APP_DIR/instance"
BACKUP_DIR="/var/backups/groupk-hr"

if [[ $EUID -ne 0 ]]; then echo "sudo 로 실행하세요."; exit 1; fi
if [[ -z "$DOMAIN" ]]; then echo "사용법: sudo bash $0 <도메인>   예) hr.example.co.kr"; exit 1; fi
if [[ ! -f "$APP_DIR/hr/app.py" ]]; then echo "$APP_DIR 에 저장소가 없습니다. 먼저 git clone 하세요."; exit 1; fi

step(){ echo; echo "==== $* ===="; }

step "1/8 시간대 Asia/Seoul"
timedatectl set-timezone Asia/Seoul || ln -sf /usr/share/zoneinfo/Asia/Seoul /etc/localtime

step "2/8 패키지 설치"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip sqlite3 ufw curl gnupg debian-keyring debian-archive-keyring apt-transport-https
if ! apt-get install -y caddy 2>/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

step "3/8 스왑 1GB (메모리 1GB 이하 서버 안정화)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

step "4/8 실행 계정·가상환경"
id "$APP_USER" &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" gunicorn
mkdir -p "$DATA_DIR" "$BACKUP_DIR"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR" "$BACKUP_DIR"
chmod 750 "$DATA_DIR" "$BACKUP_DIR"

step "5/8 앱 서비스(systemd)"
cat > /etc/systemd/system/groupk-hr.service <<UNIT
[Unit]
Description=GroupK HR attendance app
After=network.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=HR_DATABASE=$DATA_DIR/hr.sqlite3
Environment=HR_SECURE_COOKIE=1
ExecStart=$APP_DIR/.venv/bin/gunicorn -w 2 -b 127.0.0.1:8000 --access-logfile - "hr.app:create_app()"
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable groupk-hr
systemctl restart groupk-hr

step "6/8 HTTPS(Caddy) — $DOMAIN"
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:8000
    header Strict-Transport-Security "max-age=31536000"
}
CADDY
systemctl enable caddy
systemctl reload caddy || systemctl restart caddy

step "7/8 방화벽(22·80·443)"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable

step "8/8 일일 백업 03:00, 30일 보관"
cat > /etc/cron.d/groupk-hr-backup <<CRON
0 3 * * * $APP_USER sqlite3 $DATA_DIR/hr.sqlite3 ".backup '$BACKUP_DIR/hr-\$(date +\%F).sqlite3'" && find $BACKUP_DIR -name 'hr-*.sqlite3' -mtime +30 -delete
CRON
chmod 644 /etc/cron.d/groupk-hr-backup

sleep 2
echo
if systemctl is-active --quiet groupk-hr; then echo "앱 서비스: 실행 중"; else echo "앱 서비스: 실패 → journalctl -u groupk-hr -n 50"; fi
if curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/login | grep -q 200; then echo "앱 응답: 정상"; fi
cat <<DONE

설치 완료. 다음 단계:
  1) 관리자 계정 만들기:
     sudo -u $APP_USER HR_DATABASE=$DATA_DIR/hr.sqlite3 $APP_DIR/.venv/bin/flask --app hr.app create-admin
  2) 브라우저에서 https://$DOMAIN 접속
     (인증서 발급에 1~2분 걸릴 수 있음. 실패 시: journalctl -u caddy -n 50)
DONE
