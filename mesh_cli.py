"""
╔══════════════════════════════════════════════════════════════════════╗
║  MESH CLI — Interfaz de control interactiva del nodo               ║
║  Sistema Operativo Descentralizado · Fase 2 · Dispositivos Reales  ║
╚══════════════════════════════════════════════════════════════════════╝

Uso (en otra terminal, mientras batman_node.py corre):
    python3 mesh_cli.py
    python3 mesh_cli.py --host 127.0.0.1

Comandos:
    status              Estado del nodo
    peers               Tabla de pares conocidos
    routes              Tabla de rutas B.A.T.M.A.N.
    mem                 Listar memoria distribuida
    mem <key>           Leer un valor
    memw <key> <valor>  Escribir en memoria
    task <tipo>         Enviar tarea ML
      tipos: linreg logreg svm dtree mlp sfusion astar
    tasks               Listar tareas
    fault               Log de fallos
    ping <ip>           Ping TCP a un nodo
    help                Ayuda
    exit                Salir
"""

import socket, json, struct, time, argparse
from typing import Optional

CTRL_PORT    = 5559
UNICAST_PORT = 5556
BUFFER       = 65535


def color(text, col):
    codes = {
        'reset': '\033[0m',   'bold':    '\033[1m',
        'green': '\033[92m',  'red':     '\033[91m',
        'yellow':'\033[93m',  'cyan':    '\033[96m',
        'blue':  '\033[94m',  'gray':    '\033[90m',
        'white': '\033[97m',  'magenta': '\033[95m',
    }
    return codes.get(col, '') + str(text) + codes['reset']


def encode(msg):
    raw = json.dumps(msg, separators=(',', ':')).encode()
    return struct.pack('>I', len(raw)) + raw


def decode_tcp(sock):
    try:
        h = b''
        while len(h) < 4:
            chunk = sock.recv(4 - len(h))
            if not chunk:
                return None
            h += chunk
        length = struct.unpack('>I', h)[0]
        d = b''
        while len(d) < length:
            chunk = sock.recv(min(length - len(d), BUFFER))
            if not chunk:
                return None
            d += chunk
        return json.loads(d.decode())
    except Exception:
        return None


def ctrl_call(cmd, host='127.0.0.1'):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(6)
        s.connect((host, CTRL_PORT))
        s.sendall(encode(cmd))
        resp = decode_tcp(s)
        s.close()
        return resp
    except ConnectionRefusedError:
        print(color("  Error: el nodo no está corriendo.", 'red'))
        return None
    except Exception as e:
        print(color(f"  Error: {e}", 'red'))
        return None


# ── Comandos ────────────────────────────────────────────────────────────

def cmd_status(args, host):
    r = ctrl_call({'cmd': 'status'}, host)
    if not r:
        return
    bat = r['battery']
    bat_col = 'green' if bat > 50 else ('yellow' if bat > 20 else 'red')
    failed  = r.get('failed', [])
    print()
    print(color("  ╔══ Estado del Nodo N" + str(r['node_id']) + " ══════════════════════╗", 'cyan'))
    print(color("  ║  Batería    : " + str(round(bat)) + "%", bat_col))
    print(color("  ║  Carga      : " + str(r['load']) + " tareas activas", 'white'))
    print(color("  ║  Reputación : " + str(r['reputation']), 'white'))
    print(color("  ║  Pares      : " + str(r['peers']) + " conocidos", 'white'))
    print(color("  ║  Rutas      : " + str(r['routes']), 'white'))
    print(color("  ║  Memoria    : " + str(r['mem_size']) + " entradas", 'white'))
    print(color("  ║  Tareas OK  : " + str(r['tasks_done']), 'green'))
    if failed:
        print(color("  ║  Caídos     : N" + ", N".join(map(str, failed)), 'red'))
    print(color("  ╚══════════════════════════════════════════╝", 'cyan'))
    print()


def cmd_peers(args, host):
    r = ctrl_call({'cmd': 'peers'}, host)
    if not r:
        return
    peers = r.get('peers', [])
    if not peers:
        print(color("  Sin pares conocidos aún. Espera unos segundos...", 'gray'))
        return
    print()
    hdr = ("  " + "ID".ljust(8) + "IP".ljust(18) + "Bat%".ljust(7)
           + "Carga".ljust(7) + "Rep".ljust(7) + "Hops".ljust(6)
           + "TQ".ljust(7) + "Estado")
    print(color(hdr, 'cyan'))
    print(color("  " + "─" * 70, 'gray'))
    for p in peers:
        bat_col = 'green' if p['battery'] > 50 else ('yellow' if p['battery'] > 20 else 'red')
        estado  = color('CAÍDO', 'red') if p['lost'] else color('OK', 'green')
        alert   = color(' ⚠', 'yellow') if p.get('in_alert') else ''
        nid_s   = color("N" + str(p['node_id']), 'bold')
        bat_s   = color(str(round(p['battery'])) + "%", bat_col)
        line    = ("  " + nid_s.ljust(8) + p['ip'].ljust(18)
                   + bat_s.ljust(7) + str(p['load']).ljust(7)
                   + str(p['reputation']).ljust(7) + str(p['hops']).ljust(6)
                   + str(p['tq']).ljust(7) + estado + alert)
        print(line)
    print()


def cmd_routes(args, host):
    r = ctrl_call({'cmd': 'routes'}, host)
    if not r:
        return
    routes = r.get('routes', [])
    if not routes:
        print(color("  Sin rutas calculadas aún.", 'gray'))
        return
    print()
    hdr = "  " + "Dest".ljust(8) + "Via ID".ljust(10) + "Via IP".ljust(18) + "Hops".ljust(6) + "TQ".ljust(8) + "Último OGM"
    print(color(hdr, 'cyan'))
    print(color("  " + "─" * 65, 'gray'))
    for rt in sorted(routes, key=lambda x: x['dest']):
        tq_col = 'green' if rt['tq'] > 0.7 else ('yellow' if rt['tq'] > 0.4 else 'red')
        ago    = round(time.time() - rt['last_seen'])
        dest_s = color("N" + str(rt['dest']), 'bold')
        via_s  = color("N" + str(rt['via_id']), 'blue')
        tq_s   = color(str(rt['tq']), tq_col)
        ago_s  = color(str(ago) + "s atrás", 'gray')
        line   = ("  " + dest_s.ljust(8) + via_s.ljust(10)
                  + rt['via_ip'].ljust(18) + str(rt['hops']).ljust(6)
                  + tq_s.ljust(8) + ago_s)
        print(line)
    print()


def cmd_mem(args, host):
    key = args[0] if args else None
    r   = ctrl_call({'cmd': 'mem_read', 'key': key}, host)
    if not r:
        return
    if key:
        val = r.get('value')
        if val is None:
            print(color("  Clave '" + key + "' no encontrada.", 'yellow'))
        else:
            print(color("  " + key, 'cyan') + color(" = " + str(val), 'white'))
        return
    entries = r.get('entries', [])
    if not entries:
        print(color("  Memoria distribuida vacía.", 'gray'))
        return
    print()
    hdr = "  " + "Clave".ljust(36) + "Valor".ljust(22) + "Ver".ljust(7) + "Autor".ljust(8) + "Réplicas"
    print(color(hdr, 'cyan'))
    print(color("  " + "─" * 80, 'gray'))
    for e in sorted(entries, key=lambda x: x['key']):
        val_str = str(e['value'])
        if len(val_str) > 20:
            val_str = val_str[:20] + "..."
        rep_ok  = len(e.get('replicas', [])) >= 2
        rep_col = 'green' if rep_ok else 'yellow'
        key_s   = color(e['key'][:34], 'white')
        ver_s   = color("v" + str(e['version']), 'blue')
        aut_s   = color("N" + str(e['author']), 'magenta')
        rep_s   = color("x" + str(len(e.get('replicas', []))), rep_col)
        line    = "  " + key_s.ljust(36) + val_str.ljust(22) + ver_s.ljust(7) + aut_s.ljust(8) + rep_s
        print(line)
    print(color("\n  Total: " + str(len(entries)) + " entradas", 'gray'))
    print()


def cmd_memw(args, host):
    if len(args) < 2:
        print(color("  Uso: memw <key> <value>", 'yellow'))
        return
    key   = args[0]
    value = ' '.join(args[1:])
    try:
        value = json.loads(value)
    except Exception:
        pass
    r = ctrl_call({'cmd': 'mem_write', 'key': key, 'value': value}, host)
    if r and r.get('ok'):
        print(color("  OK: " + key + " = " + str(value), 'green'))
    else:
        print(color("  Error al escribir.", 'red'))


def cmd_task(args, host):
    TIPOS = {
        'linreg': ('LinReg', {
            'X': [[1],[2],[3],[4],[5],[6]],
            'y': [1.2, 2.1, 2.9, 4.0, 5.1, 5.9],
            'lr': 0.01, 'epochs': 500}),
        'logreg': ('LogReg', {
            'X': [[0,0],[1,0],[0,1],[1,1],[0.5,0.5]],
            'y': [0, 0, 0, 1, 0],
            'lr': 0.1, 'epochs': 300}),
        'svm': ('SVM', {
            'X': [[1,2],[2,3],[3,3],[5,5],[6,5],[7,8]],
            'y': [-1,-1,-1,1,1,1],
            'lr': 0.001, 'C': 1.0, 'epochs': 200}),
        'dtree': ('DecTree', {
            'X': [[2,3],[5,4],[9,6],[4,7],[3,1],[6,2]],
            'y': [0, 0, 1, 0, 0, 1],
            'max_depth': 3}),
        'mlp': ('MLP', {
            'X': [[0,0],[0,1],[1,0],[1,1]],
            'y': [0, 1, 1, 0],
            'lr': 0.1, 'epochs': 1000, 'hidden': 8}),
        'sfusion': ('SensorFusion', {
            'readings': {
                'temp':     [22.1, 22.3, 22.0, 21.8, 22.5],
                'co2':      [410.0, 412.0, 409.0, 411.0],
                'pressure': [1013.0, 1012.5, 1013.2]}}),
        'astar': ('PathPlan', {
            'grid': [[0,0,0,0,0,0,0],
                     [0,1,1,1,0,1,0],
                     [0,0,0,1,0,1,0],
                     [0,1,0,0,0,0,0],
                     [0,1,1,1,1,0,0],
                     [0,0,0,0,1,0,0],
                     [0,0,0,0,0,0,0]],
            'start': [0,0], 'goal': [6,6]}),
    }
    tipo_key = args[0].lower() if args else ''
    if tipo_key not in TIPOS:
        print(color("  Tipos disponibles: " + ', '.join(TIPOS.keys()), 'yellow'))
        return
    nombre, payload = TIPOS[tipo_key]
    print(color("  Enviando tarea " + nombre + "...", 'cyan'))
    r = ctrl_call({'cmd': 'submit_task', 'task_type': nombre,
                   'payload': payload, 'priority': 2}, host)
    if not r:
        return
    print(color("  OK: " + r['task_id'] + " → N" + str(r['assigned_to']), 'green'))
    print(color("  Estado: " + r['state'], 'white'))


def cmd_tasks(args, host):
    r = ctrl_call({'cmd': 'tasks'}, host)
    if not r:
        return
    tasks = r.get('tasks', [])
    if not tasks:
        print(color("  Sin tareas registradas.", 'gray'))
        return
    print()
    hdr = "  " + "ID".ljust(22) + "Tipo".ljust(14) + "Nodo".ljust(8) + "Estado".ljust(12) + "Resultado"
    print(color(hdr, 'cyan'))
    print(color("  " + "─" * 72, 'gray'))
    STATE_COL = {
        'pending': 'gray', 'assigned': 'yellow',
        'running': 'cyan', 'done': 'green', 'failed': 'red'
    }
    for t in sorted(tasks, key=lambda x: x['created_at'], reverse=True)[:15]:
        sc  = STATE_COL.get(t['state'], 'white')
        res = str(t.get('result', ''))[:28] if t['state'] == 'done' else ''
        nid_s = color("N" + str(t['assigned_to']), 'blue')
        st_s  = color(t['state'], sc)
        print("  " + t['id'].ljust(22) + t['task_type'].ljust(14)
              + nid_s.ljust(8) + st_s.ljust(12) + color(res, 'gray'))
    print(color("\n  Total: " + str(len(tasks)), 'gray'))
    print()


def cmd_fault(args, host):
    r = ctrl_call({'cmd': 'fault_log'}, host)
    if not r:
        return
    log    = r.get('log', [])
    failed = r.get('failed', [])
    print()
    if failed:
        print(color("  Nodos caídos: N" + ", N".join(map(str, failed)), 'red'))
    else:
        print(color("  Todos los nodos conocidos están activos.", 'green'))
    print()
    if not log:
        print(color("  Sin eventos de fallo registrados.", 'gray'))
        return
    print(color("  Log de fallos:", 'cyan'))
    print(color("  " + "─" * 65, 'gray'))
    for ts, msg in log:
        t_str = time.strftime('%H:%M:%S', time.localtime(ts))
        col   = 'red' if 'Fallo' in msg else ('green' if 'Recup' in msg else 'yellow')
        print("  " + color(t_str, 'gray') + "  " + color(msg, col))
    print()


def cmd_ping(args, host):
    if not args:
        print(color("  Uso: ping <ip>", 'yellow'))
        return
    ip = args[0]
    print(color("  Haciendo ping a " + ip + ":" + str(UNICAST_PORT) + "...", 'cyan'))
    try:
        t0 = time.time()
        s  = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((ip, UNICAST_PORT))
        raw = json.dumps({'type': 'PING'}, separators=(',',':')).encode()
        s.sendall(struct.pack('>I', len(raw)) + raw)
        resp = decode_tcp(s)
        rtt  = (time.time() - t0) * 1000
        s.close()
        if resp:
            print(color("  PONG de N" + str(resp['node_id'])
                        + "  RTT=" + str(round(rtt,1)) + "ms"
                        + "  bat=" + str(round(resp['battery'])) + "%"
                        + "  carga=" + str(resp['load']), 'green'))
        else:
            print(color("  Sin respuesta.", 'red'))
    except Exception as e:
        print(color("  Sin respuesta: " + str(e), 'red'))


def print_help():
    print(color("""
  ┌─────────────────────────────────────────────────────────────┐
  │              MESH OS · Comandos disponibles                 │
  ├─────────────────────────────────────────────────────────────┤
  │  status              Estado del nodo                        │
  │  peers               Tabla de pares conocidos               │
  │  routes              Tabla de rutas B.A.T.M.A.N.           │
  │  mem                 Listar memoria distribuida             │
  │  mem <key>           Leer un valor específico               │
  │  memw <key> <val>    Escribir en memoria distribuida        │
  │  task <tipo>         Enviar tarea ML al mejor nodo          │
  │    tipos: linreg logreg svm dtree mlp sfusion astar         │
  │  tasks               Listar tareas (últimas 15)             │
  │  fault               Log de fallos y reconfiguraciones      │
  │  ping <ip>           Ping TCP a un nodo                     │
  │  help                Esta ayuda                             │
  │  exit / quit         Salir                                  │
  └─────────────────────────────────────────────────────────────┘
""", 'cyan'))


def repl(host):
    print(color("""
  ╔══════════════════════════════════════════════════════╗
  ║      MESH OS · CLI de Control · Fase 2              ║
  ║      B.A.T.M.A.N. + Scheduler + Memoria + Fallos    ║
  ╚══════════════════════════════════════════════════════╝
""", 'cyan'))
    print(color("  Conectado al nodo en " + host + ":" + str(CTRL_PORT), 'gray'))
    print(color("  Escribe 'help' para ver los comandos.\n", 'gray'))

    CMDS = {
        'status': cmd_status,
        'peers':  cmd_peers,
        'routes': cmd_routes,
        'mem':    cmd_mem,
        'memw':   cmd_memw,
        'task':   cmd_task,
        'tasks':  cmd_tasks,
        'fault':  cmd_fault,
        'ping':   cmd_ping,
        'help':   lambda a, h: print_help(),
    }

    while True:
        try:
            raw = input(color("  mesh> ", 'green')).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        parts = raw.split()
        cmd   = parts[0].lower()
        args  = parts[1:]
        if cmd in ('exit', 'quit', 'q'):
            break
        elif cmd in CMDS:
            try:
                CMDS[cmd](args, host)
            except Exception as e:
                print(color("  Error: " + str(e), 'red'))
        else:
            print(color("  Comando desconocido: '" + cmd + "'. Escribe 'help'.", 'yellow'))

    print(color("  Saliendo.\n", 'gray'))


if __name__ == '__main__':
    import argparse as ap
    parser = ap.ArgumentParser(description="CLI de control del nodo Mesh OS")
    parser.add_argument('--host', default='127.0.0.1',
                        help="IP del nodo a controlar (default: 127.0.0.1)")
    args = parser.parse_args()
    repl(args.host)
