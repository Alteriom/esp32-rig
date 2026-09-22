#!/usr/bin/env bash
# Install the Nginx bootstrap or issue a Let's Encrypt certificate and enable
# TLS. First run with --host; after DNS and TCP 80/443 forwarding are working,
# rerun with --host, --issue, and --email.

set -euo pipefail

HOST=""
EMAIL=""
ISSUE=0
LAN=0
NETWORK="192.168.1.0/24"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --host) HOST="${2:-}"; shift 2 ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    --issue) ISSUE=1; shift ;;
    --lan) LAN=1; shift ;;
    --network) NETWORK="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$LAN" -eq 0 ] && { ! [[ "$HOST" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] || [[ "$HOST" != *.* ]]; }; then
  echo "--host must be a valid DNS hostname" >&2
  exit 2
fi
if [ "$LAN" -eq 1 ] && ! [[ "$NETWORK" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]]; then
  echo "--network must be an IPv4 CIDR" >&2
  exit 2
fi
if [ "$LAN" -eq 1 ] && [ "$ISSUE" -eq 1 ]; then
  echo "--issue cannot be combined with --lan" >&2
  exit 2
fi
if [ "$ISSUE" -eq 1 ] && [[ "$EMAIL" != *@*.* ]]; then
  echo "--email is required with --issue" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "run this installer with sudo" >&2
  exit 2
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
SITE=/etc/nginx/sites-available/alteriom-hil

apt-get update -qq
apt-get install -y -qq nginx certbot
install -d -m 0755 /var/www/alteriom-hil-acme

render() {
  local source="$1" target="$2"
  sed -e "s/__HIL_HOST__/$HOST/g" -e "s|__HIL_NETWORK__|$NETWORK|g" "$source" >"$target.tmp"
  install -o root -g root -m 0644 "$target.tmp" "$target"
  rm -f -- "$target.tmp"
}

if [ "$LAN" -eq 1 ]; then
  render "$HERE/nginx/alteriom-hil-lan.conf" "$SITE"
else
  render "$HERE/nginx/alteriom-hil-bootstrap.conf" "$SITE"
fi
ln -sfn "$SITE" /etc/nginx/sites-enabled/alteriom-hil
rm -f -- /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
  if [ "$LAN" -eq 1 ]; then
    ufw --force delete allow 80/tcp >/dev/null 2>&1 || true
    ufw --force delete allow 443/tcp >/dev/null 2>&1 || true
    ufw allow from "$NETWORK" to any port 80 proto tcp comment 'HIL dashboard from LAN'
  else
    ufw allow 80/tcp comment 'HIL ACME and HTTPS redirect'
    ufw allow 443/tcp comment 'HIL dashboard HTTPS'
  fi
fi

if [ "$LAN" -eq 1 ]; then
  echo "LAN-only dashboard ready at http://$(hostname -I | awk '{print $1}')/"
  echo "Only clients in $NETWORK are accepted; TLS remains deferred."
  exit 0
fi

if [ "$ISSUE" -eq 0 ]; then
  echo "Nginx ACME bootstrap installed for $HOST."
  echo "Forward public TCP 80 and 443 to this Pi, verify DNS, then rerun with --issue --email."
  exit 0
fi

certbot certonly --webroot -w /var/www/alteriom-hil-acme \
  -d "$HOST" --non-interactive --agree-tos --email "$EMAIL"
render "$HERE/nginx/alteriom-hil-tls.conf" "$SITE"
nginx -t
systemctl reload nginx
systemctl enable --now certbot.timer
certbot renew --dry-run
echo "HTTPS dashboard ready at https://$HOST/"
