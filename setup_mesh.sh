#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════╗
# ║  setup_mesh.sh — Configuración de red Ad-Hoc para Linux            ║
# ║  Sistema Operativo Descentralizado · Fase 2 · Dispositivos Reales  ║
# ╠══════════════════════════════════════════════════════════════════════╣
# ║  Uso:                                                               ║
# ║    sudo bash setup_mesh.sh <INTERFAZ> <IP_DEL_NODO> <ID_NODO>     ║
# ║                                                                    ║
# ║  Ejemplos:                                                         ║
# ║    sudo bash setup_mesh.sh wlan0 192.168.99.1 1   # Nodo 1        ║
# ║    sudo bash setup_mesh.sh wlan0 192.168.99.2 2   # Nodo 2        ║
# ║    sudo bash setup_mesh.sh wlan0 192.168.99.3 3   # Nodo 3        ║
# ║    sudo bash setup_mesh.sh wlan0 192.168.99.4 4   # Nodo 4        ║
# ║                                                                    ║
# ║  Funciona en: Raspberry Pi (cualquier modelo), Ubuntu 20+,        ║
# ║               Debian 10+, cualquier Linux con iw/ip               ║
# ╚══════════════════════════════════════════════════════════════════════╝

set -e

# ── Colores ────────────────────────────────────────────────────────────
RED='\033[91m'; GREEN='\033[92m'; YELLOW='\033[93m'
CYAN='\033[96m'; GRAY='\033[90m'; RESET='\033[0m'; BOLD='\033[1m'

ok()   { echo -e "${GREEN}  ✓ $1${RESET}"; }
info() { echo -e "${CYAN}  → $1${RESET}"; }
warn() { echo -e "${YELLOW}  ⚠ $1${RESET}"; }
err()  { echo -e "${RED}  ✗ $1${RESET}"; exit 1; }

# ── Argumentos ─────────────────────────────────────────────────────────
IFACE="${1:-wlan0}"
NODE_IP="${2:-192.168.99.1}"
NODE_ID="${3:-1}"

# Parámetros fijos de la red mesh
SSID="MeshOS_AdHoc"
CHANNEL=6
SUBNET="192.168.99"
NETMASK="255.255.255.0"
BCAST="${SUBNET}.255"

echo -e ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║   Mesh OS · Configuración de Red Ad-Hoc             ║${RESET}"
echo -e "${BOLD}${CYAN}║   Interfaz: ${IFACE}   IP: ${NODE_IP}   Nodo: N${NODE_ID}   ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""

# ── Verificar root ──────────────────────────────────────────────────────
[ "$EUID" -ne 0 ] && err "Ejecuta con sudo: sudo bash setup_mesh.sh ..."

# ── Verificar herramientas ──────────────────────────────────────────────
for tool in iw ip iwconfig; do
    if ! command -v "$tool" &>/dev/null; then
        warn "$tool no encontrado. Instalando wireless-tools..."
        apt-get install -y wireless-tools iw 2>/dev/null || \
        yum install -y wireless-tools iw 2>/dev/null || \
        err "Instala manualmente: apt install wireless-tools iw"
    fi
done
ok "Herramientas de red disponibles"

# ── Detener servicios que interfieren ──────────────────────────────────
info "Deteniendo servicios que interfieren con el modo Ad-Hoc..."

systemctl stop NetworkManager    2>/dev/null && ok "NetworkManager detenido"    || true
systemctl stop wpa_supplicant    2>/dev/null && ok "wpa_supplicant detenido"    || true
systemctl stop dhcpcd            2>/dev/null && ok "dhcpcd detenido"            || true
systemctl stop avahi-daemon      2>/dev/null && ok "avahi-daemon detenido"      || true

# Matar procesos que puedan tener la interfaz tomada
pkill -f wpa_supplicant 2>/dev/null || true
pkill -f dhcpcd         2>/dev/null || true
sleep 1

# ── Bajar la interfaz y limpiar ────────────────────────────────────────
info "Configurando interfaz ${IFACE}..."
ip link set "$IFACE" down        2>/dev/null || true
iw dev "$IFACE" set type ibss    2>/dev/null || \
    iwconfig "$IFACE" mode ad-hoc 2>/dev/null || \
    err "No se pudo poner ${IFACE} en modo IBSS/Ad-Hoc. Verifica que la tarjeta lo soporte."

# ── Levantar interfaz y unirse a la celda IBSS ────────────────────────
ip link set "$IFACE" up

# Intentar con iw primero (más moderno)
if iw dev "$IFACE" ibss join "$SSID" "${CHANNEL}00" 2>/dev/null; then
    ok "Unido a celda IBSS via iw (canal ${CHANNEL})"
else
    # Fallback a iwconfig
    iwconfig "$IFACE" essid "$SSID" channel "$CHANNEL" mode ad-hoc || \
        err "No se pudo configurar la red Ad-Hoc en ${IFACE}"
    ok "Unido a celda IBSS via iwconfig (canal ${CHANNEL})"
fi

sleep 1

# ── Asignar IP estática ────────────────────────────────────────────────
info "Asignando IP ${NODE_IP}/24 a ${IFACE}..."
ip addr flush dev "$IFACE"
ip addr add "${NODE_IP}/24" broadcast "$BCAST" dev "$IFACE"
ip link set "$IFACE" up
ok "IP asignada: ${NODE_IP}/24  broadcast: ${BCAST}"

# ── Ruta por defecto dentro de la red mesh ────────────────────────────
ip route add "${SUBNET}.0/24" dev "$IFACE" 2>/dev/null || true
ok "Ruta de subred configurada"

# ── Configurar firewall (UFW o iptables) ──────────────────────────────
info "Abriendo puertos del protocolo Mesh..."
PORTS=(5555 5556 5557 5558 5559)
PORT_NAMES=("OGM/Beacon UDP" "Unicast TCP" "Mem Sync TCP" "Task TCP" "CtrlAPI TCP")

if command -v ufw &>/dev/null; then
    ufw allow in on "$IFACE" 2>/dev/null || true
    for i in "${!PORTS[@]}"; do
        ufw allow "${PORTS[$i]}" 2>/dev/null || true
        ok "Puerto ${PORTS[$i]} abierto (${PORT_NAMES[$i]})"
    done
else
    # iptables directo
    for port in "${PORTS[@]}"; do
        iptables -I INPUT -i "$IFACE" -p udp --dport "$port" -j ACCEPT 2>/dev/null || true
        iptables -I INPUT -i "$IFACE" -p tcp --dport "$port" -j ACCEPT 2>/dev/null || true
    done
    ok "Puertos abiertos via iptables"
fi

# ── Habilitar broadcast UDP ────────────────────────────────────────────
# Asegura que el broadcast no sea bloqueado
iptables -I INPUT  -i "$IFACE" -m pkttype --pkt-type broadcast -j ACCEPT 2>/dev/null || true
iptables -I OUTPUT -o "$IFACE" -m pkttype --pkt-type broadcast -j ACCEPT 2>/dev/null || true

# ── Guardar configuración en archivo ──────────────────────────────────
CONFIG_DIR="/etc/mesh_os"
mkdir -p "$CONFIG_DIR"
cat > "${CONFIG_DIR}/node.conf" << EOF
# Mesh OS · Configuración del nodo N${NODE_ID}
# Generado: $(date)
NODE_ID=${NODE_ID}
NODE_IP=${NODE_IP}
IFACE=${IFACE}
SSID=${SSID}
CHANNEL=${CHANNEL}
SUBNET=${SUBNET}
EOF
ok "Configuración guardada en ${CONFIG_DIR}/node.conf"

# ── Crear script de inicio automático ────────────────────────────────
cat > "${CONFIG_DIR}/start_node.sh" << SCRIPT
#!/bin/bash
# Inicio automático del nodo N${NODE_ID}
cd "$(dirname "$(realpath "$0")")"
# Re-aplicar config de red si es necesario
bash "$(realpath "$0" | xargs dirname)/../../$(basename "$0")" "${IFACE}" "${NODE_IP}" "${NODE_ID}" 2>/dev/null || true
# Iniciar el nodo
python3 batman_node.py --id ${NODE_ID} --interface ${IFACE} --bind ${NODE_IP}
SCRIPT
chmod +x "${CONFIG_DIR}/start_node.sh"

# ── Crear servicio systemd (opcional) ────────────────────────────────
MESH_DIR=$(pwd)
cat > /etc/systemd/system/mesh-node.service << SERVICE
[Unit]
Description=Mesh OS Node N${NODE_ID}
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${MESH_DIR}
ExecStartPre=/bin/bash ${MESH_DIR}/setup_mesh.sh ${IFACE} ${NODE_IP} ${NODE_ID}
ExecStart=/usr/bin/python3 ${MESH_DIR}/batman_node.py --id ${NODE_ID} --interface ${IFACE} --bind ${NODE_IP}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload 2>/dev/null || true
ok "Servicio systemd creado: mesh-node.service"

# ── Verificación final ────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}  ── Verificación de la configuración ──${RESET}"
echo ""

IP_OK=$(ip addr show "$IFACE" | grep -c "$NODE_IP" || true)
if [ "$IP_OK" -gt 0 ]; then
    ok "Interfaz ${IFACE} configurada con ${NODE_IP}"
else
    warn "La IP no aparece asignada. Verifica manualmente con: ip addr show ${IFACE}"
fi

MODE=$(iwconfig "$IFACE" 2>/dev/null | grep -i "mode" | head -1 || echo "")
ok "Modo de red: ${MODE:-Ad-Hoc configurado}"

echo ""
echo -e "${BOLD}${GREEN}  ╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}  ║         ✓ Configuración completada                  ║${RESET}"
echo -e "${BOLD}${GREEN}  ╠══════════════════════════════════════════════════════╣${RESET}"
echo -e "${BOLD}${GREEN}  ║  Nodo:        N${NODE_ID}                                    ║${RESET}"
echo -e "${BOLD}${GREEN}  ║  IP:          ${NODE_IP}                           ║${RESET}"
echo -e "${BOLD}${GREEN}  ║  Red Ad-Hoc:  ${SSID}                   ║${RESET}"
echo -e "${BOLD}${GREEN}  ║  Canal:       ${CHANNEL}                                      ║${RESET}"
echo -e "${BOLD}${GREEN}  ╠══════════════════════════════════════════════════════╣${RESET}"
echo -e "${BOLD}${GREEN}  ║  Siguiente paso — iniciar el nodo:                  ║${RESET}"
echo -e "${BOLD}${GREEN}  ║                                                      ║${RESET}"
echo -e "${BOLD}${GREEN}  ║    python3 batman_node.py \\                          ║${RESET}"
echo -e "${BOLD}${GREEN}  ║      --id ${NODE_ID} --interface ${IFACE} --bind ${NODE_IP}  ║${RESET}"
echo -e "${BOLD}${GREEN}  ║                                                      ║${RESET}"
echo -e "${BOLD}${GREEN}  ║  O con systemd:                                      ║${RESET}"
echo -e "${BOLD}${GREEN}  ║    sudo systemctl start mesh-node                    ║${RESET}"
echo -e "${BOLD}${GREEN}  ╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
