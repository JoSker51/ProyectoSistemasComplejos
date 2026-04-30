"""
Simulación: Red Ad-Hoc Mesh para Rescatistas en Edificio Colapsado
===================================================================
Red mesh pura: NO hay nodo central ni coordinador.
Cada rescatista es un nodo igual a los demás y:

  - Emite beacons: "Hola, soy el rescatista Nx"
  - Mantiene su propia tabla BATMAN con el estado de TODOS los demás
  - Detecta supervivientes cercanos de forma autónoma
  - Comparte lo que encontró a todos por OGM flooding
  - Detecta por sí solo si un compañero lleva mucho sin dar señal → ALERTA local
  - Cada nodo muestra en pantalla su visión individual de la red

La información NO pasa por un punto central.
Cada nodo conoce la situación porque los OGMs llegan a todos.

Controles:
    ESPACIO  → pausa / reanuda
    F        → accidente del Rescatista 2
    1/2/3/4  → ver la perspectiva de ese rescatista en el panel derecho
    R        → reiniciar
    Q        → salir
"""

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.animation as animation
from matplotlib.patches import FancyBboxPatch
from matplotlib.lines import Line2D
from dataclasses import dataclass, field
from typing import Optional
import math, random, collections

# ─── Edificio ──────────────────────────────────────────────────────────────
ANCHO  = 40.0
ALTO   = 30.0
PISO_H = 10.0

# ─── Red mesh / BATMAN ─────────────────────────────────────────────────────
RANGO_COMM     = 16.0   # metros: radio de comunicación directa
BEACON_CADA    = 3.0    # segundos entre beacons de identificación
BATMAN_CADA    = 4.0    # segundos entre OGMs propios
TIMEOUT_ALERTA = 12.0   # segundos sin señal de un compañero → alerta local
BATMAN_TTL     = 5      # saltos máximos de un OGM (cubre toda la red)

# ─── Supervivientes ────────────────────────────────────────────────────────
RANGO_DETECCION = 7.0   # metros: contacto directo con superviviente
MOVE_SPEED      = 0.30  # metros por paso

# ─── Simulación ────────────────────────────────────────────────────────────
DT            = 0.5
BATTERY_DRAIN = 0.035   # % por segundo

# ─── Colores ───────────────────────────────────────────────────────────────
C_NODES    = ["#378ADD", "#E8A838", "#1D9E75", "#9B59B6"]  # uno por rescatista
C_DEAD     = "#888780"
C_ALERT    = "#C0392B"
C_SURV_OK  = "#1D9E75"
C_SURV_NO  = "#E24B4A"
C_LINK     = "#B5D4F4"
C_OGM      = "#F0A500"
C_BEACON   = "#5DADE2"
C_BG       = "#F8F7F4"
C_WALL     = "#D3D1C7"
C_FLOOR    = "#E8E6E0"
C_FOUND_BY = ["#378ADD", "#E8A838", "#1D9E75", "#9B59B6"]


# ══════════════════════════════════════════════════════════════════
#  Estructuras de datos
# ══════════════════════════════════════════════════════════════════

@dataclass
class Packet:
    x: float; y: float
    tx: float; ty: float
    color: str
    label: str
    age: float = 0.0
    life: float = 1.6

    def progress(self) -> float:
        return min(self.age / self.life, 1.0)

    @property
    def pos(self):
        t = self.progress()
        return (self.x + (self.tx - self.x) * t,
                self.y + (self.ty - self.y) * t)


@dataclass
class OGM:
    """
    Originator Message de B.A.T.M.A.N.
    Cada rescatista genera el suyo propio y lo propaga a todos.
    Lleva el estado completo del nodo: posición, batería, supervivientes hallados.
    """
    origin_id:       int
    seq:             int
    ttl:             int
    path:            list        # nodos por los que pasó
    # Estado del nodo origen (propagado a la red)
    origin_x:        float
    origin_y:        float
    origin_battery:  float
    survivors_found: list        # ids de supervivientes que este nodo encontró
    alert_nodes:     list        # ids de compañeros que este nodo marcó en alerta


@dataclass
class Survivor:
    id: str
    x: float
    y: float
    piso: int
    found: bool = False
    found_by: Optional[int] = None
    found_at: float = 0.0


@dataclass
class PeerInfo:
    """
    Lo que un nodo sabe de un compañero, aprendido de sus OGMs.
    Cada nodo mantiene una tabla de estos registros.
    """
    node_id:         int
    last_x:          float = 0.0
    last_y:          float = 0.0
    last_battery:    float = 100.0
    last_seq:        int   = -1
    last_seen:       float = 0.0      # tiempo local en que llegó el último OGM
    via:             int   = -1       # siguiente salto para llegar a este par
    hops:            int   = 0
    survivors_found: list  = field(default_factory=list)
    in_alert:        bool  = False    # ¿algún compañero lo marcó en alerta?

    def seconds_ago(self, now: float) -> float:
        return now - self.last_seen

    def is_lost(self, now: float) -> bool:
        return self.seconds_ago(now) > TIMEOUT_ALERTA


@dataclass
class Node:
    id: int
    x: float
    y: float
    piso: int
    alive: bool = True
    battery: float = 100.0

    # Topología local
    neighbors: list = field(default_factory=list)

    # Tabla BATMAN propia: peer_id → PeerInfo
    peer_table: dict = field(default_factory=dict)

    # Deduplicación OGM: origin_id → max_seq_visto
    seen_ogms: dict = field(default_factory=dict)

    # Temporizadores
    last_beacon: float = 0.0
    last_batman: float = 0.0

    # Supervivientes que este nodo encontró directamente
    survivors_found: list = field(default_factory=list)

    # Alertas que este nodo emitió sobre compañeros
    alerts_emitted: list = field(default_factory=list)

    # Estadísticas
    pkts_sent: int = 0
    pkts_recv: int = 0

    # Trayectoria
    history: list = field(default_factory=list)

    def color(self) -> str:
        return C_NODES[self.id - 1] if self.alive else C_DEAD

    def beacon_message(self) -> str:
        return f"Hola, soy el rescatista N{self.id}"

    def dist_to(self, other: "Node") -> float:
        dz = (self.piso - other.piso) * PISO_H
        return math.sqrt((self.x - other.x)**2 +
                         (self.y - other.y)**2 + dz**2)

    def dist_to_pos(self, x, y, piso) -> float:
        dz = (self.piso - piso) * PISO_H
        return math.sqrt((self.x - x)**2 + (self.y - y)**2 + dz**2)

    def knows_about(self, peer_id: int) -> bool:
        return peer_id in self.peer_table

    def situation_summary(self, now: float) -> str:
        """Resumen de lo que este nodo sabe de la red."""
        alive_peers = [pid for pid, p in self.peer_table.items()
                       if not p.is_lost(now)]
        lost_peers  = [pid for pid, p in self.peer_table.items()
                       if p.is_lost(now)]
        found_count = len(set(
            s for p in self.peer_table.values()
            for s in p.survivors_found
        ) | set(self.survivors_found))
        parts = [f"Compañeros visibles: {alive_peers}"]
        if lost_peers:
            parts.append(f"⚠ Sin señal: {lost_peers}")
        parts.append(f"Supervivientes hallados en red: {found_count}/3")
        return " | ".join(parts)


# ══════════════════════════════════════════════════════════════════
#  Motor de simulación
# ══════════════════════════════════════════════════════════════════

class Simulation:

    def __init__(self):
        self.t         = 0.0
        self.paused    = False
        self.log_lines = []
        self.packets   = []
        self._ogm_seq  = 0
        self._init_world()

    def _init_world(self):
        # 4 rescatistas, todos iguales, sin jerarquía
        self.nodes = {
            1: Node(id=1, x=8,  y=25, piso=3),
            2: Node(id=2, x=28, y=25, piso=3),
            3: Node(id=3, x=18, y=15, piso=2),
            4: Node(id=4, x=6,  y=6,  piso=1),
        }
        self.survivors = {
            'S1': Survivor(id='S1', x=5,  y=4,  piso=1),
            'S2': Survivor(id='S2', x=24, y=27, piso=3),
            'S3': Survivor(id='S3', x=33, y=13, piso=2),
        }
        self.t        = 0.0
        self.packets  = []
        self.log_lines= []
        self._ogm_seq = 0

        # Inicializar last_batman escalonado para que no todos emitan a la vez
        for i, node in enumerate(self.nodes.values()):
            node.last_batman = -i * (BATMAN_CADA / len(self.nodes))
            node.last_beacon = -i * (BEACON_CADA / len(self.nodes))

        self._update_neighbors()
        self._log("Red mesh iniciada. 4 rescatistas activos, sin nodo central.", "info")
        self._log("Cada rescatista mantiene su propia tabla BATMAN.", "info")
        self._log("Busca supervivientes de forma autónoma por flooding OGM.", "info")

    # ── Topología ──────────────────────────────────────────────────────────

    def _update_neighbors(self):
        for node in self.nodes.values():
            node.neighbors = []
        for i, na in self.nodes.items():
            if not na.alive: continue
            for j, nb in self.nodes.items():
                if i >= j or not nb.alive: continue
                if na.dist_to(nb) <= RANGO_COMM:
                    na.neighbors.append(j)
                    nb.neighbors.append(i)

    # ── Paso principal ─────────────────────────────────────────────────────

    def step(self):
        if self.paused:
            return
        self.t += DT

        self._move_rescuers()
        self._update_neighbors()
        self._drain_battery()
        self._send_beacons()
        self._batman_cycle()
        self._detect_survivors()
        self._check_alerts()

        self.packets = [p for p in self.packets if p.age < p.life]
        for p in self.packets:
            p.age += DT

    # ── Movimiento ─────────────────────────────────────────────────────────

    def _move_rescuers(self):
        """
        Cada rescatista busca el superviviente más cercano que
        NINGÚN nodo de la red haya encontrado aún.
        Sabe cuáles ya fueron hallados gracias a los OGMs recibidos.
        """
        for nid, node in self.nodes.items():
            if not node.alive:
                continue

            # Supervivientes ya encontrados por toda la red (según OGMs recibidos)
            found_in_network = set(node.survivors_found)
            for peer in node.peer_table.values():
                found_in_network.update(peer.survivors_found)

            targets = [s for s in self.survivors.values()
                       if s.id not in found_in_network]

            if targets:
                target = min(targets,
                             key=lambda s: node.dist_to_pos(s.x, s.y, s.piso))
                dx = target.x - node.x
                dy = target.y - node.y
                d  = max(math.sqrt(dx*dx + dy*dy), 0.01)
                node.x += (dx / d) * MOVE_SPEED + random.gauss(0, 0.12)
                node.y += (dy / d) * MOVE_SPEED + random.gauss(0, 0.12)
            else:
                # Todos encontrados: patrullar
                node.x += random.gauss(0, 0.18)
                node.y += random.gauss(0, 0.18)

            node.x = max(1.0, min(ANCHO - 1.0, node.x))
            node.y = max(1.0, min(ALTO  - 1.0, node.y))
            node.history.append((node.x, node.y))
            if len(node.history) > 70:
                node.history.pop(0)

    # ── Batería ─────────────────────────────────────────────────────────────

    def _drain_battery(self):
        for node in self.nodes.values():
            if not node.alive:
                continue
            node.battery = max(0.0, node.battery - BATTERY_DRAIN * DT)
            if node.battery <= 0:
                node.alive = False
                self._log(f"N{node.id} sin batería — fuera de servicio.", "error")
                self._update_neighbors()

    # ── Beacons de identificación ───────────────────────────────────────────

    def _send_beacons(self):
        """
        Cada rescatista emite su mensaje de identificación
        "Hola, soy el rescatista Nx" a sus vecinos directos.
        """
        for nid, node in self.nodes.items():
            if not node.alive:
                continue
            if self.t - node.last_beacon < BEACON_CADA:
                continue
            node.last_beacon = self.t
            msg = node.beacon_message()

            for nb_id in node.neighbors:
                nb = self.nodes[nb_id]
                if not nb.alive:
                    continue
                node.pkts_sent += 1
                nb.pkts_recv   += 1

                # El vecino actualiza que recibió señal de este nodo
                if nid not in nb.peer_table:
                    nb.peer_table[nid] = PeerInfo(node_id=nid)
                nb.peer_table[nid].last_seen = self.t
                nb.peer_table[nid].last_x    = node.x
                nb.peer_table[nid].last_y    = node.y
                nb.peer_table[nid].via       = nid  # vecino directo

                # Si estaba en alerta y volvió a dar señal → cancelar alerta
                if nb.peer_table[nid].in_alert:
                    nb.peer_table[nid].in_alert = False
                    self._log(f"N{nb.id}: señal de N{nid} recuperada.", "ok")

                # Log ocasional del mensaje
                if random.random() < 0.15:
                    self._log(f'N{nid}→N{nb_id}: "{msg}"', "info")

                # Paquete visual
                self.packets.append(Packet(
                    x=node.x, y=node.y,
                    tx=nb.x,  ty=nb.y,
                    color=C_BEACON, label="BCN"
                ))

    # ── Protocolo BATMAN ───────────────────────────────────────────────────

    def _batman_cycle(self):
        """
        Cada rescatista genera su propio OGM con su estado actual
        (posición, batería, supervivientes encontrados).
        El OGM se propaga por toda la red por flooding con TTL.
        Al llegar a un nodo, ese nodo actualiza su tabla de pares.
        """
        for nid, node in self.nodes.items():
            if not node.alive:
                continue
            if self.t - node.last_batman < BATMAN_CADA:
                continue
            node.last_batman = self.t
            self._ogm_seq   += 1

            # Lista de compañeros que este nodo ha marcado en alerta
            alerts = [pid for pid, p in node.peer_table.items()
                      if p.in_alert]

            ogm = OGM(
                origin_id       = nid,
                seq             = self._ogm_seq,
                ttl             = BATMAN_TTL,
                path            = [nid],
                origin_x        = node.x,
                origin_y        = node.y,
                origin_battery  = node.battery,
                survivors_found = list(node.survivors_found),
                alert_nodes     = alerts,
            )
            self._flood_ogm(node, ogm)

    def _flood_ogm(self, sender: Node, ogm: OGM):
        """Envía el OGM a todos los vecinos activos del sender."""
        for nb_id in sender.neighbors:
            nb = self.nodes[nb_id]
            if not nb.alive:
                continue
            self._receive_ogm(nb, sender, ogm)

    def _receive_ogm(self, receiver: Node, from_node: Node, ogm: OGM):
        """
        Un nodo recibe un OGM.
        - Deduplicación: si ya vio este seq del mismo origen → descartar.
        - Actualiza su tabla de pares con el estado del nodo origen.
        - Reenvía si TTL > 0 (flooding hacia toda la red).
        """
        origin = ogm.origin_id

        # Deduplicación
        if receiver.seen_ogms.get(origin, -1) >= ogm.seq:
            return
        receiver.seen_ogms[origin] = ogm.seq
        receiver.pkts_recv += 1

        # Actualizar tabla de pares con lo que el OGM trae
        if origin not in receiver.peer_table:
            receiver.peer_table[origin] = PeerInfo(node_id=origin)

        peer = receiver.peer_table[origin]
        peer.last_seq        = ogm.seq
        peer.last_seen       = self.t
        peer.last_x          = ogm.origin_x
        peer.last_y          = ogm.origin_y
        peer.last_battery    = ogm.origin_battery
        peer.via             = from_node.id
        peer.hops            = len(ogm.path)
        peer.survivors_found = list(ogm.survivors_found)
        peer.in_alert        = False   # si llegó OGM, está vivo

        # Aprender también sobre los compañeros en alerta que reporta el origen
        for alert_id in ogm.alert_nodes:
            if alert_id in receiver.peer_table:
                receiver.peer_table[alert_id].in_alert = True

        # Aprender sobre supervivientes encontrados por la red
        for sid in ogm.survivors_found:
            if sid in self.survivors and not self.survivors[sid].found:
                self.survivors[sid].found    = True
                self.survivors[sid].found_by = origin
                self.survivors[sid].found_at = self.t
                self._log(
                    f"N{receiver.id} aprende via OGM: "
                    f"superviviente {sid} hallado por N{origin}.",
                    "ok"
                )

        # Paquete visual
        self.packets.append(Packet(
            x=from_node.x, y=from_node.y,
            tx=receiver.x,  ty=receiver.y,
            color=C_OGM, label="OGM"
        ))
        from_node.pkts_sent += 1

        # Reenviar con TTL decrementado
        if ogm.ttl > 1:
            new_ogm = OGM(
                origin_id       = ogm.origin_id,
                seq             = ogm.seq,
                ttl             = ogm.ttl - 1,
                path            = ogm.path + [receiver.id],
                origin_x        = ogm.origin_x,
                origin_y        = ogm.origin_y,
                origin_battery  = ogm.origin_battery,
                survivors_found = ogm.survivors_found,
                alert_nodes     = ogm.alert_nodes,
            )
            self._flood_ogm(receiver, new_ogm)

    # ── Detección de supervivientes ────────────────────────────────────────

    def _detect_survivors(self):
        """
        Cada rescatista detecta supervivientes por proximidad física.
        Al encontrar uno, lo anota localmente y en el próximo OGM
        lo propaga a toda la red.
        """
        for nid, node in self.nodes.items():
            if not node.alive:
                continue
            for sid, surv in self.survivors.items():
                if surv.found:
                    continue
                if node.dist_to_pos(surv.x, surv.y, surv.piso) <= RANGO_DETECCION:
                    surv.found    = True
                    surv.found_by = nid
                    surv.found_at = self.t
                    node.survivors_found.append(sid)
                    self._log(
                        f"N{nid}: ¡SUPERVIVIENTE {sid} ENCONTRADO! "
                        f"pos=({surv.x:.0f}m,{surv.y:.0f}m)  "
                        f"próximo OGM lo propagará a la red.",
                        "ok"
                    )
                    # Paquete visual inmediato a todos los vecinos
                    for nb_id in node.neighbors:
                        nb = self.nodes[nb_id]
                        self.packets.append(Packet(
                            x=node.x, y=node.y,
                            tx=nb.x,  ty=nb.y,
                            color=C_SURV_OK, label="FOUND"
                        ))

    # ── Alertas locales de señal perdida ───────────────────────────────────

    def _check_alerts(self):
        """
        Cada nodo verifica de forma AUTÓNOMA si algún compañero
        lleva demasiado tiempo sin dar señal.
        NO hay un coordinador central: cada rescatista toma esta decisión solo.
        """
        for nid, node in self.nodes.items():
            if not node.alive:
                continue
            for pid, peer in node.peer_table.items():
                elapsed = peer.seconds_ago(self.t)
                if elapsed > TIMEOUT_ALERTA and not peer.in_alert:
                    peer.in_alert = True
                    if pid not in node.alerts_emitted:
                        node.alerts_emitted.append(pid)
                    self._log(
                        f"⚠ N{nid} (autónomo): sin señal de N{pid} "
                        f"por {elapsed:.0f}s — ¿necesita ayuda?",
                        "error"
                    )

    # ── Fallos manuales ────────────────────────────────────────────────────

    def fail_node(self, node_id: int):
        node = self.nodes.get(node_id)
        if node is None or not node.alive:
            self._log(f"N{node_id} ya está inactivo.", "warn")
            return
        node.alive   = False
        node.battery = 0.0
        self._log(f"ACCIDENTE: N{node_id} dejó de responder.", "error")
        self._update_neighbors()

    # ── Log ────────────────────────────────────────────────────────────────

    def _log(self, msg: str, tipo: str = "info"):
        self.log_lines.append((self.t, msg, tipo))
        if len(self.log_lines) > 400:
            self.log_lines.pop(0)

    # ── Resumen global ─────────────────────────────────────────────────────

    def summary(self) -> dict:
        alive  = sum(1 for n in self.nodes.values() if n.alive)
        found  = sum(1 for s in self.survivors.values() if s.found)
        alerts = sum(
            1 for n in self.nodes.values()
            for p in n.peer_table.values()
            if p.in_alert
        )
        return {"t": self.t, "alive": alive, "found": found, "alerts": alerts}


# ══════════════════════════════════════════════════════════════════
#  Visualizador
# ══════════════════════════════════════════════════════════════════

class Visualizer:

    def __init__(self, sim: Simulation):
        self.sim          = sim
        self.selected_node = 1    # perspectiva del panel derecho (tecla 1-4)
        matplotlib.rcParams['toolbar'] = 'None'
        self.fig = plt.figure(figsize=(17, 9), facecolor=C_BG)
        self.fig.canvas.manager.set_window_title(
            "Red Ad-Hoc Mesh · Rescatistas autónomos sin nodo central")
        self._build_layout()
        self._connect_keys()
        self.ani = animation.FuncAnimation(
            self.fig, self._update, interval=100,
            blit=False, cache_frame_data=False)

    def _build_layout(self):
        gs = self.fig.add_gridspec(
            3, 3,
            left=0.03, right=0.98, top=0.94, bottom=0.04,
            hspace=0.40, wspace=0.28
        )
        self.ax_map   = self.fig.add_subplot(gs[:, 0:2])
        self.ax_table = self.fig.add_subplot(gs[0, 2])
        self.ax_bat   = self.fig.add_subplot(gs[1, 2])
        self.ax_log   = self.fig.add_subplot(gs[2, 2])

        self.fig.suptitle(
            "Red Ad-Hoc Mesh (B.A.T.M.A.N.) · 4 Rescatistas Autónomos · Sin Nodo Central",
            fontsize=12, color='#2C2C2A', y=0.98
        )
        self.fig.text(
            0.5, 0.005,
            "ESPACIO: pausa/reanuda   F: accidente N2   1/2/3/4: ver perspectiva   R: reiniciar   Q: salir",
            ha='center', fontsize=8, color='#888780'
        )

    # ── Plano del edificio ─────────────────────────────────────────────────

    def _draw_building(self, ax):
        ax.clear()
        ax.set_facecolor(C_BG)
        ax.set_xlim(-2, ANCHO + 4)
        ax.set_ylim(-3, ALTO + 5)
        ax.set_aspect('equal')
        ax.tick_params(labelsize=7)
        ax.set_xlabel("metros →", fontsize=7)
        ax.set_ylabel("metros ↑", fontsize=7)

        # Pisos
        for piso in range(3):
            y0 = piso * PISO_H
            ax.add_patch(FancyBboxPatch(
                (0, y0), ANCHO, PISO_H,
                boxstyle="square,pad=0",
                linewidth=1.2, edgecolor=C_WALL,
                facecolor=C_FLOOR, alpha=0.5, zorder=1))
            ax.text(-1.5, y0 + PISO_H/2, f"P{piso+1}",
                    va='center', ha='center', fontsize=8, color='#5F5E5A')

        # Paredes interiores
        for mx, mw, my, mh in [
            (12, 20, 0, PISO_H), (25, 0.3, 10, PISO_H),
            (8, 0.3, 20, PISO_H), (30, 0.3, 20, PISO_H),
            (15, 0.3, 0, PISO_H),
        ]:
            ax.add_patch(plt.Rectangle(
                (mx, my), mw, mh, color=C_WALL, zorder=2, alpha=0.7))

        # Escombros
        rng = np.random.default_rng(42)
        for _ in range(20):
            cx = rng.uniform(1, ANCHO-1); cy = rng.uniform(0.5, ALTO-0.5)
            rx = rng.uniform(0.4, 1.8);  ry = rng.uniform(0.3, 1.0)
            ax.add_patch(mpatches.Ellipse(
                (cx, cy), rx, ry, angle=rng.uniform(0, 180),
                color='#B4B2A9', alpha=0.4, zorder=3))

        # Rangos de comunicación
        for nid, node in self.sim.nodes.items():
            if not node.alive: continue
            ax.add_patch(plt.Circle(
                (node.x, node.y), RANGO_COMM,
                color=node.color(), fill=False,
                linestyle=':', linewidth=0.5, alpha=0.18, zorder=4))

        # Trayectorias
        for nid, node in self.sim.nodes.items():
            if len(node.history) < 2: continue
            col = node.color()
            hx  = [p[0] for p in node.history]
            hy  = [p[1] for p in node.history]
            ax.plot(hx, hy, color=col, linewidth=0.9,
                    linestyle='-', alpha=0.28, zorder=4)

        # Enlaces mesh activos
        drawn = set()
        for i, na in self.sim.nodes.items():
            if not na.alive: continue
            for j in na.neighbors:
                key = (min(i,j), max(i,j))
                if key in drawn: continue
                drawn.add(key)
                nb = self.sim.nodes[j]
                ax.plot([na.x, nb.x], [na.y, nb.y],
                        color=C_LINK, linewidth=1.2,
                        linestyle='--', alpha=0.5, zorder=5)

        # Paquetes animados
        for p in self.sim.packets:
            px, py = p.pos
            alpha  = max(0, 1 - p.progress())
            ax.plot(px, py, 'o', color=p.color,
                    markersize=5, alpha=alpha, zorder=9)

        # Supervivientes
        for sid, surv in self.sim.survivors.items():
            col = C_SURV_OK if surv.found else C_SURV_NO
            ax.plot(surv.x, surv.y, 'D', color=col, markersize=10,
                    markeredgecolor='white', markeredgewidth=0.9, zorder=10)
            lbl = f"{sid}"
            if surv.found:
                fc = C_NODES[surv.found_by - 1]
                lbl += f"\n✓ N{surv.found_by}"
            else:
                fc = col
            ax.text(surv.x, surv.y - 1.8, lbl,
                    ha='center', fontsize=7, color=fc,
                    fontweight='bold', zorder=11)

        # Nodos rescatistas
        for nid, node in self.sim.nodes.items():
            # Determinar si alguno de los pares de este nodo está en alerta
            node_in_alert = any(p.in_alert for p in node.peer_table.values())
            col = C_ALERT if node_in_alert else node.color()
            if not node.alive:
                col = C_DEAD

            ax.scatter(node.x, node.y, s=120, color=col, marker='o',
                       edgecolors='white', linewidths=1.3, zorder=12)

            icon  = '⚠' if node_in_alert and node.alive else \
                    ('✕' if not node.alive else '●')
            label = f"N{nid}\n{icon} {node.battery:.0f}%"
            ax.annotate(label, (node.x, node.y),
                        xytext=(0, 13), textcoords='offset points',
                        ha='center', fontsize=7.5, color=col,
                        fontweight='bold', zorder=13)

            # Resaltar el nodo seleccionado para el panel derecho
            if nid == self.selected_node and node.alive:
                ax.add_patch(plt.Circle(
                    (node.x, node.y), 2.2,
                    color=node.color(), fill=False,
                    linewidth=2, alpha=0.7, zorder=11))

            # Mensaje beacon flotante
            if node.alive and self.sim.t - node.last_beacon < 1.5:
                ax.annotate(f'"{node.beacon_message()}"',
                            (node.x, node.y),
                            xytext=(9, -20), textcoords='offset points',
                            fontsize=5.5, color='#555553', style='italic',
                            zorder=14,
                            bbox=dict(boxstyle='round,pad=0.2',
                                      fc='white', alpha=0.75, ec='none'))

        # Leyenda
        legend_items = [
            Line2D([0],[0], marker='o', color='w',
                   markerfacecolor=C_NODES[i], markersize=8,
                   label=f'Rescatista N{i+1}') for i in range(4)
        ] + [
            Line2D([0],[0], marker='o', color='w',
                   markerfacecolor=C_ALERT, markersize=8, label='⚠ Alerta compañero'),
            Line2D([0],[0], marker='D', color='w',
                   markerfacecolor=C_SURV_OK, markersize=8, label='Superviviente hallado'),
            Line2D([0],[0], marker='D', color='w',
                   markerfacecolor=C_SURV_NO, markersize=8, label='Superviviente sin hallar'),
            Line2D([0],[0], color=C_OGM,    linewidth=2, label='OGM BATMAN'),
            Line2D([0],[0], color=C_BEACON, linewidth=2, label='Beacon identificación'),
            Line2D([0],[0], color=C_LINK, linestyle='--', label='Enlace mesh'),
        ]
        ax.legend(handles=legend_items, loc='upper right',
                  fontsize=6, framealpha=0.88, edgecolor=C_WALL, ncol=2)

        # Título dinámico
        s      = self.sim.summary()
        estado = "PAUSADO" if self.sim.paused else f"T+ {s['t']:.1f}s"
        alert_txt = f"  ⚠ ALERTA" if s['alerts'] > 0 else ""
        ax.set_title(
            f"{estado}  ·  Rescatistas activos: {s['alive']}/4  ·  "
            f"Supervivientes: {s['found']}/3{alert_txt}",
            fontsize=9, pad=6,
            color=C_ALERT if s['alerts'] > 0 else '#2C2C2A'
        )

    # ── Perspectiva de un nodo (panel superior derecho) ────────────────────

    def _draw_node_perspective(self, ax):
        """
        Muestra lo que el nodo seleccionado sabe de la red.
        Esta es su visión individual, construida solo con los OGMs
        que recibió. Cada nodo puede tener una visión ligeramente distinta.
        """
        ax.clear()
        ax.set_facecolor(C_BG)
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)
        ax.axis('off')

        node = self.sim.nodes.get(self.selected_node)
        col  = node.color() if node and node.alive else C_DEAD
        ax.set_title(
            f"Perspectiva de N{self.selected_node} "
            f"({'activo' if node and node.alive else 'INACTIVO'})",
            fontsize=9, pad=4, color=col
        )

        if not node:
            return

        # Situación general que este nodo conoce
        summary = node.situation_summary(self.sim.t)
        ax.text(0.03, 0.96, summary, fontsize=6.5, color='#2C2C2A',
                transform=ax.transAxes, va='top', wrap=True,
                bbox=dict(boxstyle='round,pad=0.3', fc='white',
                          alpha=0.8, ec=C_WALL))

        # Cabecera tabla de pares
        y = 0.78
        ax.plot([0.02, 0.98], [y + 0.03, y + 0.03], color=C_WALL,
                linewidth=0.7, transform=ax.transAxes, clip_on=False)
        for txt, xp in [("Nodo", 0.02), ("Batería", 0.22),
                         ("Via", 0.40), ("Saltos", 0.55),
                         ("Estado", 0.70)]:
            ax.text(xp, y, txt, fontsize=7, fontweight='bold',
                    color='#2C2C2A', transform=ax.transAxes)

        # Filas: lo que sabe de cada compañero
        rows = sorted(node.peer_table.items())
        for i, (pid, peer) in enumerate(rows):
            yi  = y - 0.14 * (i + 1)
            ago = peer.seconds_ago(self.sim.t)

            if peer.in_alert or peer.is_lost(self.sim.t):
                row_col = C_ALERT
                estado  = f"⚠ {ago:.0f}s sin señal"
            else:
                row_col = C_NODES[pid - 1]
                estado  = f"OK ({ago:.0f}s)"

            bat_txt  = f"{peer.last_battery:.0f}%"
            via_txt  = f"N{peer.via}" if peer.via >= 0 else "?"
            hops_txt = str(peer.hops) if peer.hops else "1"

            ax.text(0.02, yi, f"N{pid}", fontsize=7.5, fontweight='bold',
                    color=row_col, transform=ax.transAxes)
            ax.text(0.22, yi, bat_txt, fontsize=7,
                    color='#444441', transform=ax.transAxes)
            ax.text(0.40, yi, via_txt, fontsize=7,
                    color='#444441', transform=ax.transAxes)
            ax.text(0.55, yi, hops_txt, fontsize=7,
                    color='#444441', transform=ax.transAxes)
            ax.text(0.70, yi, estado, fontsize=6.5,
                    color=row_col, transform=ax.transAxes)

            # Supervivientes que este par encontró
            if peer.survivors_found:
                ax.text(0.02, yi - 0.055,
                        f"  hallados: {', '.join(peer.survivors_found)}",
                        fontsize=6, color=C_SURV_OK, transform=ax.transAxes)

        if not rows:
            ax.text(0.5, 0.5, "Sin pares conocidos aún.\nEsperando OGMs...",
                    ha='center', va='center', fontsize=8,
                    color='#888780', transform=ax.transAxes)

        # Supervivientes propios de este nodo
        if node.survivors_found:
            ax.text(0.03, 0.06,
                    f"Encontrados por este nodo: {', '.join(node.survivors_found)}",
                    fontsize=7, color=C_SURV_OK, transform=ax.transAxes,
                    fontweight='bold')

    # ── Batería ────────────────────────────────────────────────────────────

    def _draw_battery(self, ax):
        ax.clear()
        ax.set_facecolor(C_BG)
        nids = list(self.sim.nodes.keys())
        bats = [self.sim.nodes[i].battery for i in nids]
        cols = []
        for i, b in zip(nids, bats):
            node = self.sim.nodes[i]
            if not node.alive:
                cols.append(C_DEAD)
            elif any(p.in_alert for p in node.peer_table.values()):
                cols.append(C_ALERT)
            elif b > 50:
                cols.append(node.color())
            elif b > 20:
                cols.append(C_OGM)
            else:
                cols.append(C_SURV_NO)
        bars = ax.bar([f"N{i}" for i in nids], bats,
                      color=cols, edgecolor='white', linewidth=0.8)
        for bar, val in zip(bars, bats):
            ax.text(bar.get_x() + bar.get_width()/2,
                    bar.get_height() + 1.5,
                    f"{val:.0f}%", ha='center', fontsize=7, color='#444441')
        ax.set_ylim(0, 110)
        ax.set_title("Batería por rescatista (%)", fontsize=9)
        ax.tick_params(labelsize=7)
        ax.grid(True, axis='y', alpha=0.2)
        ax.axhline(20, color=C_SURV_NO, linewidth=0.8,
                   linestyle='--', alpha=0.6, label='Crítico 20%')
        ax.legend(fontsize=6, framealpha=0.8)

    # ── Log ────────────────────────────────────────────────────────────────

    def _draw_log(self, ax):
        ax.clear()
        ax.set_facecolor(C_BG)
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)
        ax.axis('off')
        ax.set_title("Log de eventos (red completa)", fontsize=9, pad=4)
        lines = self.sim.log_lines[-12:]
        color_map = {"info": "#5F5E5A", "ok": "#0F6E56",
                     "warn": "#854F0B", "error": "#A32D2D"}
        n = len(lines)
        for i, (t, msg, tipo) in enumerate(lines):
            y   = 1 - (i + 1) / (n + 1)
            col = color_map.get(tipo, "#5F5E5A")
            ax.text(0.01, y, f"[{t:6.1f}s]", fontsize=6.2, color='#888780',
                    fontfamily='monospace', transform=ax.transAxes, va='center')
            ax.text(0.16, y, msg[:62], fontsize=6.2, color=col,
                    fontfamily='monospace', transform=ax.transAxes, va='center')

    # ── Update ─────────────────────────────────────────────────────────────

    def _update(self, frame):
        for _ in range(3):
            self.sim.step()
        self._draw_building(self.ax_map)
        self._draw_node_perspective(self.ax_table)
        self._draw_battery(self.ax_bat)
        self._draw_log(self.ax_log)
        self.fig.canvas.draw_idle()

    # ── Teclado ────────────────────────────────────────────────────────────

    def _connect_keys(self):
        self.fig.canvas.mpl_connect('key_press_event', self._on_key)

    def _on_key(self, event):
        k = (event.key or '').lower()
        if k == ' ':
            self.sim.paused = not self.sim.paused
            self.sim._log("Simulación " +
                          ("PAUSADA" if self.sim.paused else "REANUDADA") + ".", "warn")
        elif k == 'f':
            self.sim.fail_node(2)
        elif k in ('1', '2', '3', '4'):
            self.selected_node = int(k)
            self.sim._log(f"Mostrando perspectiva de N{k}.", "info")
        elif k == 'r':
            self.sim._init_world()
            self.selected_node = 1
        elif k == 'q':
            plt.close('all')

    def run(self):
        plt.show()


# ══════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 64)
    print("  Red Ad-Hoc Mesh · 4 Rescatistas Autónomos · Sin Nodo Central")
    print("=" * 64)
    print("  ESPACIO   → pausa / reanuda")
    print("  F         → simular accidente del Rescatista 2")
    print("  1/2/3/4   → ver la perspectiva individual de ese rescatista")
    print("  R         → reiniciar")
    print("  Q         → salir")
    print("=" * 64)
    print()
    print("  Cada rescatista emite:")
    print('    → "Hola, soy el rescatista Nx"  (beacon de identificación)')
    print()
    print("  La red funciona así:")
    print("    · No hay nodo central ni coordinador")
    print("    · Cada nodo mantiene su propia tabla BATMAN")
    print("    · Los OGMs propagan el estado por toda la red por flooding")
    print("    · Cada nodo detecta autónomamente si un compañero no da señal")
    print("    · Al encontrar un superviviente, el próximo OGM lo comunica a todos")
    print("=" * 64)

    sim = Simulation()
    viz = Visualizer(sim)
    viz.run()

    s = sim.summary()
    print(f"\nResumen final:")
    print(f"  Tiempo simulado        : {s['t']:.1f} s")
    print(f"  Rescatistas activos    : {s['alive']}/4")
    print(f"  Supervivientes hallados: {s['found']}/3")
    for sid, surv in sim.survivors.items():
        if surv.found:
            print(f"    {sid} → encontrado por N{surv.found_by} a T={surv.found_at:.1f}s")
