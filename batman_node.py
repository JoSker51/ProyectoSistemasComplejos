"""
╔══════════════════════════════════════════════════════════════════════╗
║  BATMAN NODE — Nodo principal del Sistema Operativo Distribuido    ║
║  Fase 2 · Dispositivos Reales · Solo stdlib Python 3.9+           ║
╠══════════════════════════════════════════════════════════════════════╣
║  Uso:                                                               ║
║    python3 batman_node.py --id 1 --interface wlan0                 ║
║    python3 batman_node.py --id 2 --interface wlan0 --demo          ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import socket, threading, time, json, struct, random, math, heapq
import logging, argparse, os, signal, sys, uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import deque, defaultdict
from enum import Enum

# ══════════════════════════════════════════════════════════════════════
#  PUERTOS Y CONSTANTES
# ══════════════════════════════════════════════════════════════════════
BCAST_PORT    = 5555   # UDP broadcast: OGMs y beacons
UNICAST_PORT  = 5556   # TCP: transferencia de tareas y resultados
MEM_PORT      = 5557   # TCP: sincronización de memoria distribuida
CTRL_PORT     = 5559   # TCP loopback: API de control para el CLI

BATMAN_CADA   = 4.0    # segundos entre OGMs propios
BEACON_CADA   = 2.0    # segundos entre beacons de presencia
MEM_SYNC_CADA = 8.0    # segundos entre sincronizaciones de memoria
TIMEOUT_ALERT = 15.0   # segundos sin señal → nodo caído
BATMAN_TTL    = 6      # saltos máximos por OGM
MAX_LOAD      = 4      # tareas simultáneas máximas por nodo
BCAST_ADDR    = "255.255.255.255"
BUFFER        = 65535

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)


# ══════════════════════════════════════════════════════════════════════
#  SERIALIZACIÓN (JSON + framing de longitud para TCP)
# ══════════════════════════════════════════════════════════════════════
def encode(msg: dict) -> bytes:
    raw = json.dumps(msg, separators=(',', ':')).encode()
    return struct.pack('>I', len(raw)) + raw

def decode_tcp(sock) -> Optional[dict]:
    try:
        h = b''
        while len(h) < 4:
            c = sock.recv(4 - len(h))
            if not c: return None
            h += c
        length = struct.unpack('>I', h)[0]
        if length > 10_000_000: return None
        d = b''
        while len(d) < length:
            c = sock.recv(min(length - len(d), BUFFER))
            if not c: return None
            d += c
        return json.loads(d.decode())
    except Exception: return None

def encode_udp(msg: dict) -> bytes:
    return json.dumps(msg, separators=(',', ':')).encode()

def decode_udp(data: bytes) -> Optional[dict]:
    try:    return json.loads(data.decode())
    except: return None


# ══════════════════════════════════════════════════════════════════════
#  MÓDULO 1: BATMAN ROUTER
# ══════════════════════════════════════════════════════════════════════
@dataclass
class RouteEntry:
    dest: int; via_ip: str; via_id: int; hops: int
    tq: float; last_seen: float; seq: int

@dataclass
class PeerInfo:
    node_id: int; ip: str; last_seen: float
    battery: float = 100.0; load: int = 0; reputation: float = 1.0
    hops: int = 1; tq: float = 1.0
    survivors: list = field(default_factory=list)
    in_alert: bool = False

    def is_lost(self, now: float) -> bool:
        return (now - self.last_seen) > TIMEOUT_ALERT


class BatmanRouter:
    """
    Enrutador B.A.T.M.A.N. con Transmit Quality (TQ).
    Cada nodo emite sus propios OGMs y reenvía los ajenos.
    La tabla de rutas se actualiza eligiendo siempre la ruta con mayor TQ.
    Thread-safe.
    """
    def __init__(self, node_id: int):
        self.node_id = node_id
        self._lock   = threading.RLock()
        self.peers:     Dict[int, PeerInfo]   = {}
        self.routes:    Dict[int, RouteEntry] = {}
        self.seen_ogms: Dict[int, int]        = {}
        # sliding window de OGMs recibidos por (origin, via_ip)
        self._windows: Dict[Tuple[int,str], deque] = \
            defaultdict(lambda: deque(maxlen=16))
        self.log = logging.getLogger(f"Router[N{node_id}]")

    def receive_ogm(self, ogm: dict, from_ip: str, now: float) -> bool:
        """Procesa OGM. True = es nuevo, debe reenviarse."""
        origin = ogm['origin_id']; seq = ogm['seq']
        with self._lock:
            if self.seen_ogms.get(origin, -1) >= seq: return False
            self.seen_ogms[origin] = seq
            key = (origin, from_ip)
            self._windows[key].append(1)
            lq     = sum(self._windows[key]) / len(self._windows[key])
            acc_tq = ogm.get('tq', 1.0) * lq
            if origin not in self.peers:
                self.peers[origin] = PeerInfo(node_id=origin, ip=from_ip, last_seen=now)
            p = self.peers[origin]
            p.last_seen  = now;  p.ip         = from_ip
            p.battery    = ogm.get('battery',   100.0)
            p.load       = ogm.get('load',       0)
            p.reputation = ogm.get('reputation', 1.0)
            p.hops       = len(ogm.get('path',  []))
            p.tq         = acc_tq
            p.survivors  = ogm.get('survivors',  [])
            p.in_alert   = False
            path = ogm.get('path', [origin])
            if (origin not in self.routes or
                    acc_tq > self.routes[origin].tq or
                    seq    > self.routes[origin].seq):
                self.routes[origin] = RouteEntry(
                    dest=origin, via_ip=from_ip,
                    via_id=path[-1] if path else origin,
                    hops=len(path), tq=acc_tq,
                    last_seen=now, seq=seq)
            return True

    def link_quality(self, from_ip: str) -> float:
        with self._lock:
            vals = [v for (o,ip),dq in self._windows.items()
                    if ip == from_ip for v in dq]
            return sum(vals)/len(vals) if vals else 0.8

    def alive_peers(self, now: float) -> List[PeerInfo]:
        with self._lock:
            return [p for p in self.peers.values() if not p.is_lost(now)]

    def mark_alert(self, pid: int):
        with self._lock:
            if pid in self.peers: self.peers[pid].in_alert = True

    def mark_recovered(self, pid: int, now: float):
        with self._lock:
            if pid in self.peers: self.peers[pid].in_alert = False


# ══════════════════════════════════════════════════════════════════════
#  MÓDULO 2: MODELOS ML (implementados desde cero, sin dependencias)
# ══════════════════════════════════════════════════════════════════════
class TaskType(str, Enum):
    LINEAR_REGRESSION   = "LinReg"
    LOGISTIC_REGRESSION = "LogReg"
    SVM                 = "SVM"
    DECISION_TREE       = "DecTree"
    MLP                 = "MLP"
    SENSOR_FUSION       = "SensorFusion"
    PATH_PLANNING       = "PathPlan"

class TaskState(str, Enum):
    PENDING  = "pending";  ASSIGNED = "assigned"
    RUNNING  = "running";  DONE     = "done";  FAILED = "failed"

@dataclass
class Task:
    id:          str
    task_type:   str
    origin_id:   int
    priority:    int
    payload:     dict
    created_at:  float
    deadline:    float
    state:       str  = TaskState.PENDING
    assigned_to: int  = -1
    result:      dict = field(default_factory=dict)


def run_model(task_type: str, payload: dict) -> dict:
    """Ejecuta el modelo ML indicado. Solo usa stdlib."""

    # ── Regresión Lineal (gradiente descendente) ──────────────────
    if task_type == TaskType.LINEAR_REGRESSION:
        X  = payload.get('X', [[1],[2],[3],[4],[5]])
        y  = payload.get('y', [1, 2, 3, 4, 5])
        lr = payload.get('lr', 0.01)
        ep = payload.get('epochs', 500)
        n, m = len(X), len(X[0])
        w = [0.0] * m; b = 0.0
        for _ in range(ep):
            pr = [sum(X[i][j]*w[j] for j in range(m))+b for i in range(n)]
            er = [pr[i]-y[i] for i in range(n)]
            dw = [sum(er[i]*X[i][j] for i in range(n))/n for j in range(m)]
            w  = [w[j]-lr*dw[j] for j in range(m)]
            b -= lr * sum(er)/n
        pr  = [sum(X[i][j]*w[j] for j in range(m))+b for i in range(n)]
        mse = sum((pr[i]-y[i])**2 for i in range(n))/n
        return {'weights': [round(v,6) for v in w],
                'bias': round(b,6), 'mse': round(mse,8)}

    # ── Regresión Logística ────────────────────────────────────────
    elif task_type == TaskType.LOGISTIC_REGRESSION:
        def sig(x): return 1/(1+math.exp(-max(-500,min(500,x))))
        X  = payload.get('X', [])
        y  = payload.get('y', [])
        lr = payload.get('lr', 0.1)
        ep = payload.get('epochs', 300)
        n, m = len(X), len(X[0])
        w = [0.0]*m; b = 0.0
        for _ in range(ep):
            pr = [sig(sum(X[i][j]*w[j] for j in range(m))+b) for i in range(n)]
            er = [pr[i]-y[i] for i in range(n)]
            dw = [sum(er[i]*X[i][j] for i in range(n))/n for j in range(m)]
            w  = [w[j]-lr*dw[j] for j in range(m)]; b -= lr*sum(er)/n
        pr  = [sig(sum(X[i][j]*w[j] for j in range(m))+b) for i in range(n)]
        acc = sum(1 for i in range(n) if (pr[i]>0.5)==bool(y[i]))/n if n else 0
        return {'weights': [round(v,6) for v in w],
                'bias': round(b,6), 'accuracy': round(acc,4)}

    # ── SVM lineal (SGD, kernel lineal) ───────────────────────────
    elif task_type == TaskType.SVM:
        X  = payload.get('X', [])
        y  = payload.get('y', [])   # labels en {-1, 1}
        lr = payload.get('lr', 0.01)
        C  = payload.get('C',  1.0)
        ep = payload.get('epochs', 200)
        n, m = len(X), (len(X[0]) if X else 1)
        w = [0.0]*m; b = 0.0
        for _ in range(ep):
            for i in range(n):
                dot = sum(X[i][j]*w[j] for j in range(m))+b
                if y[i]*dot < 1:
                    w = [w[j]-lr*(w[j]-C*y[i]*X[i][j]) for j in range(m)]
                    b += lr*C*y[i]
                else:
                    w = [w[j]-lr*w[j] for j in range(m)]
        pr  = [1 if sum(X[i][j]*w[j] for j in range(m))+b>=0 else -1 for i in range(n)]
        acc = sum(1 for i in range(n) if pr[i]==y[i])/n if n else 0
        return {'weights': [round(v,6) for v in w],
                'bias': round(b,6), 'accuracy': round(acc,4)}

    # ── Árbol de Decisión (GINI, recursivo) ───────────────────────
    elif task_type == TaskType.DECISION_TREE:
        X  = payload.get('X', [])
        y  = payload.get('y', [])
        md = payload.get('max_depth', 4)
        def gini(lb):
            if not lb: return 0
            n = len(lb); c = {}
            for l in lb: c[l] = c.get(l,0)+1
            return 1 - sum((v/n)**2 for v in c.values())
        def best_split(X, y):
            best=None; bg=1.0
            for f in range(len(X[0])):
                for t in sorted(set(r[f] for r in X)):
                    l=[y[i] for i in range(len(X)) if X[i][f]<=t]
                    r=[y[i] for i in range(len(X)) if X[i][f]>t]
                    if not l or not r: continue
                    g=(len(l)*gini(l)+len(r)*gini(r))/len(y)
                    if g<bg: bg=g; best=(f,t)
            return best, bg
        def build(X, y, d):
            if not X or d==0 or len(set(y))==1:
                return {'leaf':True,'label':max(set(y),key=y.count) if y else 0}
            s, g = best_split(X, y)
            if s is None:
                return {'leaf':True,'label':max(set(y),key=y.count)}
            f, t = s
            lX=[X[i] for i in range(len(X)) if X[i][f]<=t]
            ly=[y[i] for i in range(len(X)) if X[i][f]<=t]
            rX=[X[i] for i in range(len(X)) if X[i][f]>t]
            ry=[y[i] for i in range(len(X)) if X[i][f]>t]
            return {'leaf':False,'feature':f,'threshold':t,'gini':round(g,4),
                    'left':build(lX,ly,d-1),'right':build(rX,ry,d-1)}
        return {'tree': build(X,y,md), 'root_gini': round(gini(y),4)}

    # ── MLP — Perceptrón Multicapa (1 capa oculta, ReLU + Sigmoid) ─
    elif task_type == TaskType.MLP:
        def relu(x): return max(0.0, x)
        def drelu(x): return 1.0 if x>0 else 0.0
        def sig(x): return 1/(1+math.exp(-max(-500,min(500,x))))
        X  = payload.get('X', [])
        y  = payload.get('y', [])
        hd = payload.get('hidden', 4)
        lr = payload.get('lr', 0.05)
        ep = payload.get('epochs', 500)
        n, idim = len(X), (len(X[0]) if X else 1)
        rng = random.Random(42)
        sc1 = math.sqrt(2/idim); sc2 = math.sqrt(2/hd)
        W1 = [[rng.gauss(0,sc1) for _ in range(idim)] for _ in range(hd)]
        b1 = [0.0]*hd
        W2 = [rng.gauss(0,sc2) for _ in range(hd)]; b2 = 0.0
        for _ in range(ep):
            for i in range(n):
                # Forward
                hv = [relu(sum(W1[k][j]*X[i][j] for j in range(idim))+b1[k])
                      for k in range(hd)]
                o  = sig(sum(W2[k]*hv[k] for k in range(hd))+b2)
                # Backward
                do = o - y[i]
                dW2= [do*hv[k] for k in range(hd)]; db2 = do
                dh = [W2[k]*do*drelu(hv[k]) for k in range(hd)]
                for k in range(hd):
                    for j in range(idim): W1[k][j] -= lr*dh[k]*X[i][j]
                    b1[k] -= lr*dh[k]
                for k in range(hd): W2[k] -= lr*dW2[k]
                b2 -= lr*db2
        preds = [sig(sum(W2[k]*relu(sum(W1[k][j]*X[i][j]
                 for j in range(idim))+b1[k]) for k in range(hd))+b2)
                 for i in range(n)]
        mse = sum((preds[i]-y[i])**2 for i in range(n))/n if n else 0
        return {'mse': round(mse,8), 'hidden': hd, 'epochs': ep}

    # ── Fusión de sensores (promedio ponderado por varianza) ───────
    elif task_type == TaskType.SENSOR_FUSION:
        readings = payload.get('readings', {})
        fused = {}
        for sid, vals in readings.items():
            if not vals: continue
            mean = sum(vals)/len(vals)
            var  = sum((v-mean)**2 for v in vals)/len(vals) if len(vals)>1 else 1.0
            fused[sid] = {'mean':round(mean,4),'var':round(var,6),
                          'weight':round(1/(var+1e-9),4)}
        tw  = sum(f['weight'] for f in fused.values())
        est = sum(f['mean']*f['weight'] for f in fused.values())/tw if tw else 0
        return {'fused_estimate': round(est,4), 'per_sensor': fused}

    # ── A* sobre grid ──────────────────────────────────────────────
    elif task_type == TaskType.PATH_PLANNING:
        grid  = payload.get('grid', [[0]*5]*5)
        start = tuple(payload.get('start', [0,0]))
        goal  = tuple(payload.get('goal',  [4,4]))
        rows, cols = len(grid), len(grid[0])
        def h(a,b): return abs(a[0]-b[0])+abs(a[1]-b[1])
        open_set = [(h(start,goal), 0, start, [start])]
        visited  = set()
        while open_set:
            _, cost, cur, path = heapq.heappop(open_set)
            if cur == goal:
                return {'path':[list(p) for p in path],
                        'cost':cost, 'length':len(path)}
            if cur in visited: continue
            visited.add(cur)
            for dr,dc in [(-1,0),(1,0),(0,-1),(0,1)]:
                nr,nc = cur[0]+dr, cur[1]+dc
                if 0<=nr<rows and 0<=nc<cols and grid[nr][nc]==0:
                    nxt = (nr,nc)
                    heapq.heappush(open_set,
                        (cost+1+h(nxt,goal), cost+1, nxt, path+[nxt]))
        return {'path':[], 'cost':-1, 'length':0}

    else:
        raise ValueError(f"Tipo desconocido: {task_type}")


# ══════════════════════════════════════════════════════════════════════
#  MÓDULO 2B: SCHEDULER DISTRIBUIDO
# ══════════════════════════════════════════════════════════════════════
class DistributedScheduler:
    """
    Scheduler sin maestro.
    score(n) = reputacion * (1 - carga/MAX) * (bateria/100)
    El nodo con mayor score recibe la tarea.
    """
    def __init__(self, node_id: int):
        self.node_id    = node_id
        self._lock      = threading.RLock()
        self.tasks:     Dict[str, Task] = {}
        self.my_tasks:  List[str]       = []
        self.reputation = 1.0
        self._done      = 0
        self._failed    = 0
        self.log = logging.getLogger(f"Sched[N{node_id}]")

    @property
    def load(self) -> int:
        with self._lock: return len(self.my_tasks)

    def score(self, battery: float) -> float:
        lf = max(0.0, 1.0 - self.load / MAX_LOAD)
        return self.reputation * lf * (battery / 100.0)

    def best_node(self, peers: List[PeerInfo], battery: float) -> int:
        cands = {self.node_id: self.score(battery)}
        for p in peers:
            s = p.reputation * max(0.0,1-p.load/MAX_LOAD) * (p.battery/100.0)
            cands[p.node_id] = s
        return max(cands, key=cands.get)

    def accept(self, task: Task):
        with self._lock:
            self.tasks[task.id] = task
            task.state       = TaskState.RUNNING
            task.assigned_to = self.node_id
            self.my_tasks.append(task.id)

    def execute(self, task: Task) -> dict:
        self.log.info(f"Ejecutando {task.task_type} [{task.id}]")
        try:
            result = run_model(task.task_type, task.payload)
            with self._lock:
                task.state  = TaskState.DONE
                task.result = result
                if task.id in self.my_tasks: self.my_tasks.remove(task.id)
                self._done += 1; self._update_rep()
            self.log.info(f"OK {task.id}: {str(result)[:80]}")
            return result
        except Exception as e:
            with self._lock:
                task.state = TaskState.FAILED
                if task.id in self.my_tasks: self.my_tasks.remove(task.id)
                self._failed += 1; self._update_rep()
            self.log.error(f"FALLO {task.id}: {e}")
            return {'error': str(e)}

    def _update_rep(self):
        t = self._done + self._failed
        if t:
            raw = self._done / t
            self.reputation = max(0.1, min(2.0,
                0.8*self.reputation + 0.2*(raw*2.0)))

    def to_dict(self, t: Task) -> dict:
        return {'id':t.id,'task_type':t.task_type,'origin_id':t.origin_id,
                'priority':t.priority,'payload':t.payload,
                'created_at':t.created_at,'deadline':t.deadline,
                'state':t.state,'assigned_to':t.assigned_to,'result':t.result}

    def from_dict(self, d: dict) -> Task:
        return Task(id=d['id'],task_type=d['task_type'],
                    origin_id=d['origin_id'],priority=d['priority'],
                    payload=d['payload'],created_at=d['created_at'],
                    deadline=d['deadline'],state=d.get('state',TaskState.PENDING),
                    assigned_to=d.get('assigned_to',-1),
                    result=d.get('result',{}))


# ══════════════════════════════════════════════════════════════════════
#  MÓDULO 3: MEMORIA DISTRIBUIDA (vector clocks + persistencia)
# ══════════════════════════════════════════════════════════════════════
@dataclass
class MemEntry:
    key: str; value: object; version: int
    vector_clk: dict; author: int; timestamp: float
    replicas: list = field(default_factory=list)

class DistributedMemory:
    """
    Clave-valor con vector clocks para consistencia causal (CRDT-like).
    Persiste en JSON para sobrevivir reinicios.
    Thread-safe.
    """
    def __init__(self, node_id: int, path: Optional[str] = None):
        self.node_id = node_id
        self._lock   = threading.RLock()
        self._store: Dict[str, MemEntry] = {}
        self._clock: Dict[str, int]      = {str(node_id): 0}
        self._wc     = 0
        self._path   = path
        self.log = logging.getLogger(f"Mem[N{node_id}]")
        if path and os.path.exists(path): self._load()

    def write(self, key: str, value: object) -> MemEntry:
        with self._lock:
            self._clock[str(self.node_id)] = \
                self._clock.get(str(self.node_id), 0) + 1
            self._wc += 1
            e = MemEntry(key=key, value=value, version=self._wc,
                         vector_clk=dict(self._clock), author=self.node_id,
                         timestamp=time.time(), replicas=[self.node_id])
            self._store[key] = e; self._save(); return e

    def read(self, key: str) -> Optional[object]:
        with self._lock:
            e = self._store.get(key); return e.value if e else None

    def merge(self, ed: dict) -> bool:
        with self._lock:
            e   = self._d2e(ed)
            loc = self._store.get(e.key)
            if loc is None:
                self._store[e.key] = e
                self._mclk(e.vector_clk); self._save(); return True
            ln = self._dom(loc.vector_clk, e.vector_clk)
            rn = self._dom(e.vector_clk, loc.vector_clk)
            if rn:
                self._store[e.key] = e
                self._mclk(e.vector_clk); self._save(); return True
            elif not ln and e.timestamp > loc.timestamp:
                self._store[e.key] = e
                self._mclk(e.vector_clk); self._save(); return True
            return False

    def digest(self) -> dict:
        with self._lock: return {k: e.version for k,e in self._store.items()}

    def newer_than(self, rd: dict) -> List[dict]:
        with self._lock:
            return [self._e2d(e) for k,e in self._store.items()
                    if k not in rd or e.version > rd[k]]

    def all_entries(self) -> List[dict]:
        with self._lock: return [self._e2d(e) for e in self._store.values()]

    def size(self) -> int:
        with self._lock: return len(self._store)

    def _dom(self, a, b) -> bool:
        ks = set(a)|set(b)
        return (all(a.get(k,0) >= b.get(k,0) for k in ks) and
                any(a.get(k,0) >  b.get(k,0) for k in ks))

    def _mclk(self, r):
        for k,v in r.items():
            self._clock[k] = max(self._clock.get(k,0), v)

    def _e2d(self, e) -> dict:
        return {'key':e.key,'value':e.value,'version':e.version,
                'vector_clk':e.vector_clk,'author':e.author,
                'timestamp':e.timestamp,'replicas':e.replicas}

    def _d2e(self, d) -> MemEntry:
        return MemEntry(key=d['key'],value=d['value'],version=d['version'],
                        vector_clk=d['vector_clk'],author=d['author'],
                        timestamp=d['timestamp'],replicas=d.get('replicas',[]))

    def _save(self):
        if not self._path: return
        try:
            tmp = self._path + ".tmp"
            with open(tmp,'w') as f:
                json.dump({'clock':self._clock,'wc':self._wc,
                           'store':{k:self._e2d(e) for k,e in self._store.items()}},f)
            os.replace(tmp, self._path)
        except Exception as ex:
            self.log.warning(f"Persist: {ex}")

    def _load(self):
        try:
            with open(self._path) as f: d = json.load(f)
            self._clock = d.get('clock', {str(self.node_id): 0})
            self._wc    = d.get('wc', 0)
            for k,ed in d.get('store',{}).items():
                self._store[k] = self._d2e(ed)
            self.log.info(f"Memoria restaurada: {len(self._store)} entradas")
        except Exception as ex:
            self.log.warning(f"Load: {ex}")


# ══════════════════════════════════════════════════════════════════════
#  MÓDULO 4: FAULT MANAGER
# ══════════════════════════════════════════════════════════════════════
class FaultManager:
    """
    Detección y reconfiguración ante fallos. Sin coordinador.
    Cada nodo monitorea a sus pares de forma independiente.
    """
    def __init__(self, node_id: int):
        self.node_id = node_id
        self._lock   = threading.RLock()
        self.failed: set = set()
        self._hist:  Dict[int, List[float]] = {}
        self._log:   deque = deque(maxlen=100)
        self.log = logging.getLogger(f"Fault[N{node_id}]")

    def check(self, peers: List[PeerInfo], now: float) -> List[int]:
        """Retorna IDs de nodos recién detectados como caídos."""
        newly = []
        with self._lock:
            for p in peers:
                if p.node_id in self.failed: continue
                if p.is_lost(now):
                    self.failed.add(p.node_id)
                    self._hist.setdefault(p.node_id,[]).append(now)
                    msg = f"Fallo N{p.node_id} (sin señal {now-p.last_seen:.0f}s)"
                    self._log.append((now, msg))
                    self.log.warning(msg)
                    newly.append(p.node_id)
        return newly

    def recover(self, nid: int, now: float):
        with self._lock:
            if nid in self.failed:
                self.failed.discard(nid)
                msg = f"Recuperación N{nid}"
                self._log.append((now, msg)); self.log.info(msg)

    def reassign(self, fid: int, sched: DistributedScheduler,
                 peers: List[PeerInfo], bat: float, now: float) -> int:
        count = 0
        with sched._lock:
            for t in sched.tasks.values():
                if (t.assigned_to == fid and
                        t.state in (TaskState.ASSIGNED, TaskState.RUNNING)):
                    t.state       = TaskState.PENDING
                    t.assigned_to = sched.best_node(peers, bat)
                    t.state       = TaskState.ASSIGNED
                    msg = f"Tarea {t.id}: N{fid}→N{t.assigned_to}"
                    self._log.append((now, msg))
                    self.log.info(msg); count += 1
        return count

    def repair_replicas(self, fid: int, mem: DistributedMemory, now: float):
        with mem._lock:
            for e in mem._store.values():
                if fid in e.replicas:
                    e.replicas.remove(fid)
                    self._log.append(
                        (now, f"Re-replicar '{e.key}' (pérdida en N{fid})"))

    def recent_log(self, n: int = 20) -> List[Tuple[float,str]]:
        with self._lock: return list(self._log)[-n:]


# ══════════════════════════════════════════════════════════════════════
#  CTRL API — Puerto 5559, solo loopback, para el CLI
# ══════════════════════════════════════════════════════════════════════
class CtrlAPI:
    def __init__(self, node: "MeshNode"):
        self.node = node
        self.log  = logging.getLogger(f"Ctrl[N{node.node_id}]")

    def start(self):
        threading.Thread(target=self._serve, daemon=True, name="ctrl-api").start()

    def _serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', CTRL_PORT))
        srv.listen(5); srv.settimeout(2.0)
        self.log.info(f"CtrlAPI en 127.0.0.1:{CTRL_PORT}")
        while self.node._running:
            try:
                conn,_ = srv.accept()
                threading.Thread(target=self._handle,
                                 args=(conn,), daemon=True).start()
            except socket.timeout: continue
            except Exception as e: self.log.error(f"Accept: {e}")
        srv.close()

    def _handle(self, conn):
        try:
            msg = decode_tcp(conn)
            if not msg: return
            resp = self._dispatch(msg.get('cmd',''), msg)
            conn.sendall(encode(resp))
        except Exception as e:
            try: conn.sendall(encode({'error':str(e)}))
            except: pass
        finally: conn.close()

    def _dispatch(self, cmd: str, msg: dict) -> dict:
        node = self.node; now = time.time()
        if cmd == 'status':
            return node.status()
        elif cmd == 'peers':
            with node.router._lock:
                return {'peers':[{
                    'node_id':p.node_id,'ip':p.ip,'battery':p.battery,
                    'load':p.load,'reputation':round(p.reputation,3),
                    'hops':p.hops,'tq':round(p.tq,3),'in_alert':p.in_alert,
                    'lost':p.is_lost(now),'last_seen':round(now-p.last_seen,1)
                } for p in node.router.peers.values()]}
        elif cmd == 'routes':
            with node.router._lock:
                return {'routes':[{
                    'dest':r.dest,'via_ip':r.via_ip,'via_id':r.via_id,
                    'hops':r.hops,'tq':round(r.tq,3),
                    'last_seen':r.last_seen,'seq':r.seq
                } for r in node.router.routes.values()]}
        elif cmd == 'mem_read':
            key = msg.get('key')
            if key: return {'value': node.memory.read(key)}
            return {'entries': node.memory.all_entries()}
        elif cmd == 'mem_write':
            key = msg.get('key'); val = msg.get('value')
            if not key: return {'ok': False}
            node.memory.write(key, val); return {'ok': True}
        elif cmd == 'submit_task':
            t = node.submit_task(msg.get('task_type','LinReg'),
                                 msg.get('payload',{}),
                                 msg.get('priority',2))
            return {'task_id':t.id,'assigned_to':t.assigned_to,'state':t.state}
        elif cmd == 'tasks':
            with node.scheduler._lock:
                return {'tasks':[node.scheduler.to_dict(t)
                                 for t in node.scheduler.tasks.values()]}
        elif cmd == 'fault_log':
            return {'log':node.fault_mgr.recent_log(20),
                    'failed':list(node.fault_mgr.failed)}
        return {'error': f'Cmd desconocido: {cmd}'}


# ══════════════════════════════════════════════════════════════════════
#  NODO PRINCIPAL — integra los 4 módulos + red
# ══════════════════════════════════════════════════════════════════════
class MeshNode:
    def __init__(self, node_id: int, interface: str,
                 bind_ip: str = "0.0.0.0",
                 data_dir: str = "./mesh_data"):
        self.node_id   = node_id
        self.interface = interface
        self.bind_ip   = bind_ip
        os.makedirs(data_dir, exist_ok=True)

        self.router    = BatmanRouter(node_id)
        self.scheduler = DistributedScheduler(node_id)
        self.memory    = DistributedMemory(node_id,
                         path=os.path.join(data_dir, f"mem_n{node_id}.json"))
        self.fault_mgr = FaultManager(node_id)
        self.ctrl_api  = CtrlAPI(self)

        self._running  = True
        self._battery  = self._read_bat()
        self._ogm_seq  = 0
        self.log = logging.getLogger(f"Node[N{node_id}]")

    # ── Arranque ─────────────────────────────────────────────────────
    def start(self):
        hilos = [
            ("beacon-tx",   self._beacon_tx),
            ("ogm-tx",      self._ogm_tx),
            ("bcast-rx",    self._bcast_rx),
            ("unicast-srv", self._unicast_srv),
            ("mem-sync",    self._mem_sync),
            ("task-worker", self._task_worker),
            ("fault-watch", self._fault_watch),
            ("battery-mon", self._bat_loop),
        ]
        for name, fn in hilos:
            threading.Thread(target=fn, name=name, daemon=True).start()
        self.ctrl_api.start()
        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        self.log.info(f"Nodo N{self.node_id} activo — interfaz: {self.interface}")

    def run_forever(self):
        self.start()
        while self._running: time.sleep(1)

    def _shutdown(self, *_):
        self.log.info("Apagando..."); self._running = False; sys.exit(0)

    # ── Batería ───────────────────────────────────────────────────────
    def _read_bat(self) -> float:
        for p in ["/sys/class/power_supply/BAT0/capacity",
                  "/sys/class/power_supply/BAT1/capacity",
                  "/sys/class/power_supply/battery/capacity"]:
            try:
                with open(p) as f: return float(f.read().strip())
            except: pass
        return 100.0

    def _bat_loop(self):
        while self._running:
            self._battery = self._read_bat(); time.sleep(30)

    # ── TX: Beacon UDP ────────────────────────────────────────────────
    def _beacon_tx(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        while self._running:
            msg = {'type':'BCN','node_id':self.node_id,
                   'battery':self._battery,'load':self.scheduler.load,
                   'rep':self.scheduler.reputation,'ts':time.time()}
            try: s.sendto(encode_udp(msg), (BCAST_ADDR, BCAST_PORT))
            except Exception as e: self.log.warning(f"Beacon: {e}")
            time.sleep(BEACON_CADA)
        s.close()

    # ── TX: OGM UDP ───────────────────────────────────────────────────
    def _ogm_tx(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        while self._running:
            self._ogm_seq += 1
            ogm = {'type':'OGM','origin_id':self.node_id,'seq':self._ogm_seq,
                   'ttl':BATMAN_TTL,'path':[self.node_id],
                   'battery':self._battery,'load':self.scheduler.load,
                   'reputation':self.scheduler.reputation,'tq':1.0,
                   'survivors':[],'alerts':list(self.fault_mgr.failed),
                   'mem_digest':self.memory.digest(),'ts':time.time()}
            try: s.sendto(encode_udp(ogm), (BCAST_ADDR, BCAST_PORT))
            except Exception as e: self.log.warning(f"OGM TX: {e}")
            time.sleep(BATMAN_CADA)
        s.close()

    # ── RX: Broadcast UDP ─────────────────────────────────────────────
    def _bcast_rx(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(('', BCAST_PORT)); s.settimeout(2.0)
        self.log.info(f"Escuchando broadcast :{BCAST_PORT}")
        while self._running:
            try:
                data, (from_ip, _) = s.recvfrom(BUFFER)
                msg = decode_udp(data)
                if not msg: continue
                sid = msg.get('node_id') or msg.get('origin_id')
                if sid == self.node_id: continue
                self._handle_bcast(msg, from_ip)
            except socket.timeout: continue
            except Exception as e: self.log.error(f"RX bcast: {e}")
        s.close()

    def _handle_bcast(self, msg: dict, from_ip: str):
        now = time.time(); mt = msg.get('type')
        if mt == 'BCN':
            nid = msg['node_id']
            with self.router._lock:
                if nid not in self.router.peers:
                    self.router.peers[nid] = PeerInfo(
                        node_id=nid, ip=from_ip, last_seen=now)
                p = self.router.peers[nid]
                p.last_seen  = now;  p.ip         = from_ip
                p.battery    = msg.get('battery',   100.0)
                p.load       = msg.get('load',       0)
                p.reputation = msg.get('rep',        1.0)
                if p.in_alert:
                    p.in_alert = False
                    self.fault_mgr.recover(nid, now)

        elif mt == 'OGM':
            is_new = self.router.receive_ogm(msg, from_ip, now)
            if is_new and msg.get('ttl', 0) > 1:
                fwd = dict(msg)
                fwd['ttl']  = msg['ttl'] - 1
                fwd['path'] = msg['path'] + [self.node_id]
                fwd['tq']   = msg.get('tq',1.0) * self.router.link_quality(from_ip)
                try:
                    sf = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sf.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sf.sendto(encode_udp(fwd), (BCAST_ADDR, BCAST_PORT))
                    sf.close()
                except: pass
            for aid in msg.get('alerts', []):
                self.router.mark_alert(aid)

    # ── Servidor TCP: tareas y resultados ─────────────────────────────
    def _unicast_srv(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind_ip, UNICAST_PORT))
        srv.listen(10); srv.settimeout(2.0)
        self.log.info(f"TCP tareas :{UNICAST_PORT}")
        while self._running:
            try:
                conn, addr = srv.accept()
                threading.Thread(target=self._handle_tcp,
                                 args=(conn,addr), daemon=True).start()
            except socket.timeout: continue
            except Exception as e: self.log.error(f"TCP accept: {e}")
        srv.close()

    def _handle_tcp(self, conn, addr):
        try:
            msg = decode_tcp(conn)
            if not msg: return
            mt = msg.get('type')
            if mt == 'TASK':
                t = self.scheduler.from_dict(msg['task'])
                self.scheduler.accept(t)
                conn.sendall(encode({'status':'accepted','task_id':t.id}))
                threading.Thread(target=self._exec_report,
                                 args=(t, addr[0]), daemon=True).start()
            elif mt == 'TRES':
                tid = msg['task_id']
                if tid in self.scheduler.tasks:
                    self.scheduler.tasks[tid].result = msg['result']
                    self.scheduler.tasks[tid].state  = TaskState.DONE
                conn.sendall(encode({'status':'ok'}))
            elif mt == 'PING':
                conn.sendall(encode({'type':'PONG','node_id':self.node_id,
                                     'battery':self._battery,
                                     'load':self.scheduler.load,
                                     'ts':time.time()}))
        except Exception as e: self.log.error(f"Handle TCP: {e}")
        finally: conn.close()

    def _exec_report(self, task: Task, origin_ip: str):
        result = self.scheduler.execute(task)
        self.memory.write(f"result.{task.id}", result)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10); s.connect((origin_ip, UNICAST_PORT))
            s.sendall(encode({'type':'TRES','task_id':task.id,
                              'result':result,'from_id':self.node_id}))
            s.close()
        except Exception as e:
            self.log.warning(f"No pude reportar {task.id}: {e}")

    # ── Sincronización de memoria ─────────────────────────────────────
    def _mem_sync(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind_ip, MEM_PORT))
        srv.listen(10); srv.settimeout(0.5)

        def accept_loop():
            while self._running:
                try:
                    conn,_ = srv.accept()
                    threading.Thread(target=self._handle_mem,
                                     args=(conn,), daemon=True).start()
                except socket.timeout: pass
                except: pass

        threading.Thread(target=accept_loop, daemon=True,
                         name="mem-acceptor").start()

        while self._running:
            time.sleep(MEM_SYNC_CADA)
            for peer in self.router.alive_peers(time.time()):
                try: self._push_mem(peer.ip)
                except Exception as e:
                    self.log.debug(f"Mem sync N{peer.node_id}: {e}")
        srv.close()

    def _push_mem(self, ip: str):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(8); s.connect((ip, MEM_PORT))
        s.sendall(encode({'type':'MREQ','node_id':self.node_id}))
        resp = decode_tcp(s)
        if resp:
            entries = self.memory.newer_than(resp.get('digest',{}))
            if entries:
                s.sendall(encode({'type':'MSYN','entries':entries,
                                  'from_id':self.node_id}))
                decode_tcp(s)
        s.close()

    def _handle_mem(self, conn):
        try:
            msg = decode_tcp(conn)
            if not msg: return
            if msg.get('type') == 'MREQ':
                conn.sendall(encode({'digest':self.memory.digest()}))
                msg2 = decode_tcp(conn)
                if msg2 and msg2.get('type') == 'MSYN':
                    merged = sum(1 for e in msg2.get('entries',[])
                                 if self.memory.merge(e))
                    conn.sendall(encode({'merged':merged}))
                    if merged:
                        self.log.debug(f"Mem: integradas {merged} entradas")
        except: pass
        finally: conn.close()

    # ── Worker de tareas propias ──────────────────────────────────────
    def _task_worker(self):
        while self._running:
            time.sleep(1)
            with self.scheduler._lock:
                pending = [t for t in self.scheduler.tasks.values()
                           if t.assigned_to == self.node_id
                           and t.state == TaskState.ASSIGNED]
            for task in pending:
                threading.Thread(target=self._exec_local,
                                 args=(task,), daemon=True).start()

    def _exec_local(self, task: Task):
        result = self.scheduler.execute(task)
        self.memory.write(f"result.{task.id}", result)

    # ── Vigilancia de fallos ──────────────────────────────────────────
    def _fault_watch(self):
        while self._running:
            time.sleep(5)
            now   = time.time()
            peers = list(self.router.peers.values())
            failed = self.fault_mgr.check(peers, now)
            for fid in failed:
                self.router.mark_alert(fid)
                alive = self.router.alive_peers(now)
                n = self.fault_mgr.reassign(
                    fid, self.scheduler, alive, self._battery, now)
                if n: self.log.warning(f"Reasignadas {n} tareas de N{fid}")
                self.fault_mgr.repair_replicas(fid, self.memory, now)

    # ── API pública ───────────────────────────────────────────────────
    def submit_task(self, task_type: str, payload: dict,
                    priority: int = 2, deadline: float = 60.0) -> Task:
        now  = time.time()
        task = Task(
            id=f"T{self.node_id}-{uuid.uuid4().hex[:6].upper()}",
            task_type=task_type, origin_id=self.node_id,
            priority=priority, payload=payload,
            created_at=now, deadline=now+deadline)
        with self.scheduler._lock:
            self.scheduler.tasks[task.id] = task
        alive = self.router.alive_peers(now)
        best  = self.scheduler.best_node(alive, self._battery)
        task.assigned_to = best
        if best == self.node_id:
            task.state = TaskState.ASSIGNED
            self.log.info(f"Tarea {task.id} ({task_type}) → LOCAL")
        else:
            peer = self.router.peers.get(best)
            if peer:
                task.state = TaskState.ASSIGNED
                threading.Thread(target=self._send_task,
                                 args=(task, peer.ip), daemon=True).start()
                self.log.info(
                    f"Tarea {task.id} ({task_type}) → N{best} ({peer.ip})")
            else:
                task.assigned_to = self.node_id
                task.state       = TaskState.ASSIGNED
        return task

    def _send_task(self, task: Task, ip: str):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10); s.connect((ip, UNICAST_PORT))
            s.sendall(encode({'type':'TASK',
                              'task':self.scheduler.to_dict(task)}))
            decode_tcp(s); s.close()
        except Exception as e:
            self.log.error(f"Envío tarea {task.id}: {e}")
            task.assigned_to = self.node_id
            task.state       = TaskState.ASSIGNED

    def status(self) -> dict:
        return {'node_id':self.node_id,'battery':self._battery,
                'load':self.scheduler.load,
                'reputation':round(self.scheduler.reputation,3),
                'peers':len(self.router.peers),
                'routes':len(self.router.routes),
                'mem_size':self.memory.size(),
                'failed':list(self.fault_mgr.failed),
                'tasks_done':self.scheduler._done}


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    p = argparse.ArgumentParser(description="Mesh OS · Nodo B.A.T.M.A.N.")
    p.add_argument('--id',        type=int, required=True,
                   help="ID único del nodo (entero positivo)")
    p.add_argument('--interface', default='wlan0',
                   help="Interfaz de red (wlan0, eth0, etc.)")
    p.add_argument('--bind',      default='0.0.0.0',
                   help="IP de bind para puertos TCP")
    p.add_argument('--data-dir',  default='./mesh_data',
                   help="Directorio de persistencia")
    p.add_argument('--demo',      action='store_true',
                   help="Inyectar tareas ML automáticamente cada 20s")
    p.add_argument('--log-level', default='INFO',
                   choices=['DEBUG','INFO','WARNING','ERROR'])
    args = p.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    node = MeshNode(args.id, args.interface, args.bind,
                    args.data_dir.replace('-','_'))

    if args.demo:
        DEMO_TASKS = [
            ('LinReg',   {'X':[[1],[2],[3],[4],[5]],'y':[2,4,5,4,5],
                          'lr':0.01,'epochs':500}),
            ('MLP',      {'X':[[0,0],[0,1],[1,0],[1,1]],'y':[0,1,1,0],
                          'lr':0.1,'epochs':1000,'hidden':4}),
            ('SVM',      {'X':[[1,2],[2,3],[5,5],[6,5]],'y':[-1,-1,1,1],
                          'lr':0.001,'C':1.0,'epochs':200}),
            ('SensorFusion', {'readings':{
                'temp':[22.1,22.3,22.0,21.8],
                'co2':[410,412,409,411],
                'pressure':[1013.0,1012.5,1013.2]}}),
            ('PathPlan', {'grid':[[0,0,0,0,0],[0,1,1,0,0],[0,0,0,1,0],
                                  [0,1,0,0,0],[0,0,0,0,0]],
                          'start':[0,0],'goal':[4,4]}),
        ]
        def demo_loop():
            time.sleep(10)
            i = 0
            while node._running:
                tt, pl = DEMO_TASKS[i % len(DEMO_TASKS)]
                t = node.submit_task(tt, pl, priority=2)
                node.memory.write(f"demo.task.{i}", t.id)
                i += 1; time.sleep(20)
        threading.Thread(target=demo_loop, daemon=True,
                         name="demo").start()

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║       Mesh OS · Nodo N{args.id} activo                            ║
╠══════════════════════════════════════════════════════════════╣
║  Interfaz : {args.interface:<48}║
║  Bind     : {args.bind:<48}║
╠══════════════════════════════════════════════════════════════╣
║  Puertos                                                     ║
║    UDP {BCAST_PORT}  → OGMs y Beacons (broadcast)              ║
║    TCP {UNICAST_PORT}  → Transferencia de tareas                ║
║    TCP {MEM_PORT}  → Sincronización de memoria              ║
║    TCP {CTRL_PORT}  → CLI de control (solo loopback)         ║
╠══════════════════════════════════════════════════════════════╣
║  En otra terminal:                                           ║
║    python3 mesh_cli.py                                       ║
║  Ctrl+C para apagar                                          ║
╚══════════════════════════════════════════════════════════════╝
""")
    node.run_forever()


if __name__ == "__main__":
    main()
