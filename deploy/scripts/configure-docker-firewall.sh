#!/usr/bin/env bash
set -Eeuo pipefail

admin_cidr="${1:-${ADMIN_PUBLIC_CIDR:-}}"
client_cidrs="${2:-}"
http_mode="${3:-deny-http}"
external_interface="${EXTERNAL_INTERFACE:-$(ip -4 route show default | awk '{print $5; exit}')}"
if [ -z "${admin_cidr}" ]; then
  echo "usage: sudo $0 ADMIN_PUBLIC_IP/32 [CLIENT_CIDR[,CLIENT_CIDR...]] [allow-http]" >&2
  exit 2
fi
if [ -z "${external_interface}" ]; then
  echo "could not determine the external network interface" >&2
  exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

iptables -N INTDEMO-INGRESS 2>/dev/null || true
iptables -F INTDEMO-INGRESS
iptables -A INTDEMO-INGRESS -i "${external_interface}" \
  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
if [ "${http_mode}" = "allow-http" ]; then
  iptables -A INTDEMO-INGRESS -i "${external_interface}" -p tcp --dport 80 -j ACCEPT
else
  iptables -A INTDEMO-INGRESS -i "${external_interface}" -p tcp --dport 80 -j DROP
fi
iptables -A INTDEMO-INGRESS -i "${external_interface}" \
  -p tcp -s "${admin_cidr}" --dport 443 -j ACCEPT
iptables -A INTDEMO-INGRESS -i "${external_interface}" \
  -p udp -s "${admin_cidr}" --dport 443 -j ACCEPT
if [ -n "${client_cidrs}" ]; then
  IFS=',' read -r -a client_cidr_list <<< "${client_cidrs}"
  for client_cidr in "${client_cidr_list[@]}"; do
    client_cidr="${client_cidr//[[:space:]]/}"
    [ -n "${client_cidr}" ] || continue
    iptables -A INTDEMO-INGRESS -i "${external_interface}" \
      -p tcp -s "${client_cidr}" --dport 443 -j ACCEPT
    iptables -A INTDEMO-INGRESS -i "${external_interface}" \
      -p udp -s "${client_cidr}" --dport 443 -j ACCEPT
  done
fi
iptables -A INTDEMO-INGRESS -i "${external_interface}" -p tcp --dport 443 -j DROP
iptables -A INTDEMO-INGRESS -i "${external_interface}" -p udp --dport 443 -j DROP
iptables -A INTDEMO-INGRESS -j RETURN

iptables -C DOCKER-USER -j INTDEMO-INGRESS 2>/dev/null \
  || iptables -I DOCKER-USER 1 -j INTDEMO-INGRESS

if command -v netfilter-persistent >/dev/null 2>&1; then
  netfilter-persistent save
else
  echo "install iptables-persistent to preserve rules across reboot" >&2
fi
echo "external interface: ${external_interface}"
iptables -S INTDEMO-INGRESS
