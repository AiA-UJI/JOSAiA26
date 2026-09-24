import socket, urllib.request, json
HOSTS = [f"labrob{n:02d}.act.uji.es" for n in range(7, 19)]
PORT = 9000
for h in HOSTS:
    # 1) puerto master abierto?
    try:
        s = socket.create_connection((h, PORT), timeout=4); s.close(); openp = True
    except Exception:
        openp = False
    status = ""
    if openp:
        for ep in ("/api/status", "/status", "/"):
            try:
                with urllib.request.urlopen(f"http://{h}:{PORT}{ep}", timeout=5) as r:
                    body = r.read(400).decode("utf-8", "replace")
                    status = f"{ep} -> {body[:200]}"
                    break
            except Exception as e:
                status = f"{ep} ERR {type(e).__name__}"
    print(f"{h}: master_port_open={openp} {status}")
