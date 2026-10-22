#!/bin/sh
set -eu

PROJECT_DIR=/opt/sdr-activity-monitor
SERVICE_USER=sdr-monitor
SOURCE_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
ask() {
    prompt=$1 default=${2-}
    if [ -n "$default" ]; then printf '%s [%s]: ' "$prompt" "$default"; else printf '%s: ' "$prompt"; fi
    IFS= read -r answer || die "input cancelled"
    printf '%s' "${answer:-$default}"
}
yesno() {
    prompt=$1 default=${2:-yes}
    if [ "$default" = yes ]; then marker=Y/n; else marker=y/N; fi
    printf '%s [%s]: ' "$prompt" "$marker"
    IFS= read -r answer || die "input cancelled"
    answer=${answer:-$default}
    case "$answer" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}

[ "$(id -u)" -eq 0 ] || die "run with sudo: sudo ./scripts/install.sh"
[ -f /etc/os-release ] || die "this installer supports Debian/Ubuntu-style Linux systems"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID_LIKE:-$ID}" in *debian*|*ubuntu*) ;; *) die "unsupported distribution: ${PRETTY_NAME:-$ID}" ;; esac

printf '%s\n' 'SDR Activity Monitor installer' '' \
  'This installs a receive-only experimental monitor under /opt/sdr-activity-monitor.' \
  'The wizard will ask for receiver, channel, alert, and dashboard information.' ''

enable_dashboard=no
public_dashboard=no
if yesno 'Install the local web dashboard?' yes; then
    enable_dashboard=yes
    if yesno 'Publish it through nginx with HTTPS?' no; then
        public_dashboard=yes
        dashboard_domain=$(ask 'Dashboard DNS name (must already resolve to this host)')
        [ -n "$dashboard_domain" ] || die "a dashboard DNS name is required"
        certbot_email=$(ask "Email for Let's Encrypt notices")
        [ -n "$certbot_email" ] || die "an email is required for unattended certificate setup"
    fi
fi

discord_webhook=''
if yesno 'Send alerts directly to a Discord webhook?' no; then
    discord_webhook=$(ask 'Discord webhook URL')
    case "$discord_webhook" in https://discord.com/api/webhooks/*|https://discordapp.com/api/webhooks/*) ;; *) die "the Discord webhook URL is not valid" ;; esac
fi

printf '%s\n' '' 'Installing operating-system packages...'
apt-get update
packages='rtl-sdr usbutils python3 python3-numpy python3-yaml'
if [ "$public_dashboard" = yes ]; then packages="$packages nginx certbot python3-certbot-nginx"; fi
# shellcheck disable=SC2086
DEBIAN_FRONTEND=noninteractive apt-get install -y $packages

id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home "$PROJECT_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
getent group plugdev >/dev/null 2>&1 || groupadd --system plugdev
usermod -a -G plugdev "$SERVICE_USER"

install -d -o root -g root -m 0755 "$PROJECT_DIR" "$PROJECT_DIR/app" "$PROJECT_DIR/config" "$PROJECT_DIR/scripts" "$PROJECT_DIR/web"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$PROJECT_DIR/data" "$PROJECT_DIR/logs"
for file in README.md ARCHITECTURE.md requirements.txt; do install -o root -g root -m 0644 "$SOURCE_DIR/$file" "$PROJECT_DIR/$file"; done
find "$SOURCE_DIR/app" -maxdepth 1 -type f -name '*.py' -exec install -o root -g root -m 0644 {} "$PROJECT_DIR/app/" \;
install -o root -g root -m 0644 "$SOURCE_DIR/web/index.html" "$PROJECT_DIR/web/index.html"
install -o root -g root -m 0755 "$SOURCE_DIR/scripts/configure.py" "$PROJECT_DIR/scripts/configure.py"
install -o root -g root -m 0644 "$SOURCE_DIR/config/rtl-sdr-blacklist.conf" /etc/modprobe.d/sdr-activity-monitor.conf

if [ -f "$PROJECT_DIR/config/config.yaml" ]; then
    if yesno 'A configuration already exists. Reconfigure it?' no; then
        python3 "$PROJECT_DIR/scripts/configure.py" --output "$PROJECT_DIR/config/config.yaml" --force
    else
        printf '%s\n' 'Keeping existing configuration.'
    fi
else
    python3 "$PROJECT_DIR/scripts/configure.py" --output "$PROJECT_DIR/config/config.yaml"
fi

install -o root -g root -m 0644 "$SOURCE_DIR/sdr-activity-monitor.service" /etc/systemd/system/sdr-activity-monitor.service
install -o root -g root -m 0644 "$SOURCE_DIR/sdr-dashboard-web.service" /etc/systemd/system/sdr-dashboard-web.service
install -o root -g root -m 0644 "$SOURCE_DIR/sdr-activity-monitor-calibration.service" /etc/systemd/system/sdr-activity-monitor-calibration.service
install -o root -g root -m 0644 "$SOURCE_DIR/sdr-activity-monitor-calibration.timer" /etc/systemd/system/sdr-activity-monitor-calibration.timer

if [ -n "$discord_webhook" ]; then
    umask 077
    printf 'SDR_MONITOR_DISCORD_WEBHOOK=%s\n' "$discord_webhook" > /etc/default/sdr-activity-monitor
    chown root:root /etc/default/sdr-activity-monitor
fi

python3 -m compileall -q "$PROJECT_DIR/app"
cd "$PROJECT_DIR"
python3 -m app.monitor --config config/config.yaml --check-config
systemctl daemon-reload
systemctl enable sdr-activity-monitor.service sdr-activity-monitor-calibration.timer
if [ "$enable_dashboard" = yes ]; then systemctl enable sdr-dashboard-web.service; else systemctl disable sdr-dashboard-web.service >/dev/null 2>&1 || true; fi

if [ "$public_dashboard" = yes ]; then
    nginx_site=/etc/nginx/sites-available/sdr-activity-monitor
    {
        printf 'server {\n'
        printf '    listen 80;\n'
        printf '    listen [::]:80;\n'
        printf '    server_name %s;\n' "$dashboard_domain"
        printf '    location / {\n'
        printf '        proxy_pass http://127.0.0.1:18090;\n'
        printf '        proxy_http_version 1.1;\n'
        # These dollar expressions belong to nginx and must remain literal.
        # shellcheck disable=SC2016
        printf '        proxy_set_header Host $host;\n'
        # shellcheck disable=SC2016
        printf '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
        # shellcheck disable=SC2016
        printf '        proxy_set_header X-Forwarded-Proto $scheme;\n'
        printf '    }\n'
        printf '}\n'
    } > "$nginx_site"
    ln -sf "$nginx_site" /etc/nginx/sites-enabled/sdr-activity-monitor
    nginx -t
    systemctl reload nginx
    certbot --nginx --non-interactive --agree-tos --redirect --email "$certbot_email" -d "$dashboard_domain"
fi

printf '%s\n' '' 'Installation complete.' \
  'Replug the RTL-SDR or reboot if the kernel DVB driver currently owns it.' \
  'Then run: sudo systemctl start sdr-activity-monitor.service'
if [ "$enable_dashboard" = yes ]; then printf '%s\n' 'Dashboard service: sudo systemctl start sdr-dashboard-web.service'; fi
printf '%s\n' 'Logs: sudo journalctl -u sdr-activity-monitor -f'
