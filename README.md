# proyecto Sistemas Complejos
Juan Niño, Oscar Mateo Arrubla, Jose Santiago Gonzalez
---

## Archivos del proyecto

```
mesh_os/
├── batman_node.py   ← Nodo principal (corre en CADA dispositivo)
├── mesh_cli.py      ← CLI de control interactivo (terminal aparte)
└── setup_mesh.sh    ← Configura la red Wi-Fi Ad-Hoc en Linux
```

---

## Hardware necesario

| Componente | Opciones |
|---|---|
| **Dispositivos** | Raspberry Pi 3/4/5, laptop Linux, PC con Linux |
| **Wi-Fi** | Tarjeta que soporte modo **IBSS (Ad-Hoc)** |
| **OS** | Raspberry Pi OS, Ubuntu 20+, Debian 10+ |
| **Python** | 3.9 o superior (solo stdlib, sin pip) |
| **Mínimo** | 2 dispositivos (funciona con hasta ~20 nodos) |

> **Verificar soporte Ad-Hoc de tu tarjeta Wi-Fi:**
> ```bash
> iw list | grep "Supported interface modes" -A 10
> # Debe aparecer "IBSS" en la lista
> ```

---

## Paso 1 — Copiar los archivos

En **cada dispositivo**, crea una carpeta y copia los 3 archivos:

```bash
mkdir ~/mesh_os
cd ~/mesh_os
# Copia batman_node.py, mesh_cli.py y setup_mesh.sh aquí
```

---

## Paso 2 — Configurar la red Ad-Hoc

Ejecuta esto en **cada dispositivo**, cambiando la IP según el número de nodo:

### Nodo 1 (primer dispositivo):
```bash
sudo bash setup_mesh.sh wlan0 192.168.99.1 1
```

### Nodo 2 (segundo dispositivo):
```bash
sudo bash setup_mesh.sh wlan0 192.168.99.2 2
```

### Nodo 3 (tercer dispositivo):
```bash
sudo bash setup_mesh.sh wlan0 192.168.99.3 3
```

### Nodo 4 (cuarto dispositivo):
```bash
sudo bash setup_mesh.sh wlan0 192.168.99.4 4
```

> El script configura la red Ad-Hoc, asigna la IP estática y abre los puertos del firewall. Solo necesitas ejecutarlo una vez (o después de cada reinicio).

---

## Paso 3 — Verificar conectividad

Antes de iniciar el nodo, verifica que los dispositivos se ven entre sí:

```bash
# Desde el Nodo 1, hacer ping al Nodo 2:
ping 192.168.99.2

# Desde el Nodo 2, hacer ping al Nodo 1:
ping 192.168.99.1
```

Si el ping no responde:
```bash
# Ver si la interfaz está en modo Ad-Hoc:
iwconfig wlan0

# Verificar IP asignada:
ip addr show wlan0

# Re-ejecutar el setup:
sudo bash setup_mesh.sh wlan0 192.168.99.X X
```

---

## Paso 4 — Iniciar el nodo

En **cada dispositivo**, abre una terminal y ejecuta:

### Nodo 1:
```bash
cd ~/mesh_os
python3 batman_node.py --id 1 --interface wlan0 --bind 192.168.99.1
```

### Nodo 2:
```bash
cd ~/mesh_os
python3 batman_node.py --id 2 --interface wlan0 --bind 192.168.99.2
```

### Nodo 3:
```bash
cd ~/mesh_os
python3 batman_node.py --id 3 --interface wlan0 --bind 192.168.99.3
```

### Nodo 4:
```bash
cd ~/mesh_os
python3 batman_node.py --id 4 --interface wlan0 --bind 192.168.99.4
```

**Modo demo** (inyecta tareas ML automáticamente cada 20 segundos):
```bash
python3 batman_node.py --id 1 --interface wlan0 --bind 192.168.99.1 --demo
```

**Más logs para depuración:**
```bash
python3 batman_node.py --id 1 --interface wlan0 --log-level DEBUG
```

---

## Paso 5 — Usar el CLI de control

En una **segunda terminal** del mismo dispositivo:

```bash
cd ~/mesh_os
python3 mesh_cli.py
```

Verás el prompt:
```
  mesh>
```

### Comandos principales:

```
  mesh> status          # Estado del nodo: batería, carga, reputación
  mesh> peers           # Tabla de pares conocidos con TQ y hops
  mesh> routes          # Tabla de rutas B.A.T.M.A.N.
  mesh> mem             # Listar toda la memoria distribuida
  mesh> mem result.T1-ABC123    # Leer un resultado específico
  mesh> memw sensor.temp 22.5   # Escribir en memoria distribuida
  mesh> task mlp        # Enviar tarea MLP al mejor nodo disponible
  mesh> task linreg     # Enviar tarea de Regresión Lineal
  mesh> task sfusion    # Fusión de sensores
  mesh> task astar      # Planificación de rutas A*
  mesh> tasks           # Ver todas las tareas y sus estados
  mesh> fault           # Ver log de fallos y reconfiguraciones
  mesh> ping 192.168.99.2       # Ping TCP al Nodo 2
  mesh> help            # Ver todos los comandos
  mesh> exit            # Salir del CLI
```

---

## Flujo completo de ejemplo

```
[Nodo 1]                    [Nodo 2]                    [Nodo 3]
    │                           │                           │
    │←──── Beacon UDP ──────────│                           │
    │──── OGM (seq=1) ─────────→│──── OGM forward ─────────→│
    │                           │                           │
    │  (CLI: task mlp)          │                           │
    │                           │                           │
    │  Calcula scores:          │                           │
    │  N1=0.8, N2=1.2, N3=0.6  │                           │
    │                           │                           │
    │──── TASK TCP ────────────→│                           │
    │                           │  Ejecuta MLP              │
    │                           │  (XOR gate, 1000 épocas)  │
    │←──── TRES (resultado) ────│                           │
    │                           │                           │
    │  memory.write(result)     │                           │
    │                           │                           │
    │←──── MSYN (mem sync) ─────│←──── MSYN (mem sync) ────│
    │  (propagación del         │                           │
    │   resultado a todos       │                           │
    │   los nodos)              │                           │
```

---

## Arranque automático con systemd (opcional)

Para que el nodo inicie solo al encender la Raspberry Pi:

```bash
# El script setup_mesh.sh ya crea el servicio. Solo actívalo:
sudo systemctl enable mesh-node
sudo systemctl start mesh-node

# Ver logs del servicio:
sudo journalctl -u mesh-node -f

# Detener:
sudo systemctl stop mesh-node
```

---

## Escenario con laptops (sin Wi-Fi Ad-Hoc)

Si tu tarjeta Wi-Fi no soporta modo IBSS, puedes probar en red local (LAN/Wi-Fi normal):

```bash
# En cada dispositivo, solo iniciar el nodo con la IP de la red local:
python3 batman_node.py --id 1 --interface eth0 --bind 192.168.1.10

# El broadcast UDP funciona igual en LAN normal.
# El protocolo BATMAN opera exactamente igual.
```

---

## Puertos utilizados

| Puerto | Protocolo | Uso |
|--------|-----------|-----|
| 5555 | UDP broadcast | OGMs B.A.T.M.A.N. y Beacons |
| 5556 | TCP | Transferencia de tareas y resultados |
| 5557 | TCP | Sincronización de memoria distribuida |
| 5559 | TCP (loopback) | API de control del CLI |

---

## Salida esperada al iniciar

```
[10:32:01][INFO][Node[N1]] Nodo N1 activo — interfaz: wlan0
[10:32:01][INFO][Ctrl[N1]] CtrlAPI en 127.0.0.1:5559
[10:32:01][INFO][Node[N1]] Escuchando broadcast :5555
[10:32:01][INFO][Node[N1]] TCP tareas :5556
[10:32:03][INFO][Router[N1]] OGM de N2 (192.168.99.2) seq=1 TQ=1.00
[10:32:04][INFO][Router[N1]] OGM de N3 (192.168.99.3) seq=1 TQ=0.87
[10:32:05][INFO][Sched[N1]] Ejecutando MLP [T2-A3F9C1]
[10:32:08][INFO][Sched[N1]] OK T2-A3F9C1: {'mse': 0.00012, 'hidden': 4}
```

---

## Preguntas frecuentes

**¿Por qué no se ven los nodos?**
- Verifica que están en la misma red Ad-Hoc: `iwconfig wlan0` → debe mostrar el mismo ESSID `MeshOS_AdHoc`
- Verifica que el firewall no bloquea UDP 5555: `sudo ufw allow 5555`

**¿Por qué las tareas no se envían a otros nodos?**
- Los nodos necesitan ~8-12 segundos para descubrirse mutuamente (2-3 ciclos de OGM)
- Espera que `peers` muestre al menos un par antes de enviar tareas

**¿Se puede usar con más de 4 nodos?**
- Sí, el protocolo escala. Solo asigna IDs y IPs diferentes a cada nodo.

**¿Qué pasa si apago un nodo?**
- Después de 15 segundos sin señal, los demás detectan el fallo
- Las tareas asignadas a ese nodo se reasignan automáticamente
- La memoria se re-replica en los nodos restantes

**¿Dónde se guardan los datos de memoria?**
- En `./mesh_data/mem_nX.json` (donde X es el ID del nodo)
- Se restaura automáticamente al reiniciar el nodo
