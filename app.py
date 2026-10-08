"""Concurso de adivinanzas en vivo - FastAPI. Ejecutar: python app.py"""
import csv, io, json, os, secrets, socket, threading, time
from pathlib import Path
from typing import Optional
import segno, uvicorn
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
DATA = BASE / "data"; DATA.mkdir(exist_ok=True)
CFG = json.loads((BASE / "preguntas.json").read_text(encoding="utf-8"))
Q = CFG["preguntas"]; SEG = CFG.get("segundos", 30)
P1, PA = CFG.get("puntos_primero", 3), CFG.get("puntos_acierto", 1)
PORT = int(os.environ.get("PORT", 8000))
KF = DATA / "admin_key.txt"
KEY = os.environ.get("ADMIN_KEY") or (KF.read_text().strip() if KF.exists() else secrets.token_hex(3))
KF.write_text(KEY)
SF = DATA / "estado.json"
lock = threading.RLock()
G = json.loads(SF.read_text(encoding="utf-8")) if SF.exists() else {}
G.setdefault("phase", "lobby"); G.setdefault("qi", -1); G.setdefault("opened", 0)
G.setdefault("players", {}); G.setdefault("answers", {}); G.setdefault("v", 0)

def bump():
    G["v"] += 1
    tmp = SF.with_suffix(".tmp"); tmp.write_text(json.dumps(G, ensure_ascii=False)); tmp.replace(SF)

def deadline(): return G["opened"] + SEG
def tick():
    if G["phase"] == "question" and time.time() >= deadline():
        G["phase"] = "closed"; bump()

def points():
    """Puntos por respuesta: primero en acertar P1, demás aciertos PA."""
    pa, first = {}, {}
    for qk, lst in G["answers"].items():
        got = False
        for a in lst:
            if a["ok"]:
                pa[(qk, a["pid"])] = PA if got else P1; got = True
    return pa

def board():
    pa = points(); tot = {p: [0, 0] for p in G["players"]}
    for (qk, pid), v in pa.items():
        if pid in tot: tot[pid][0] += v; tot[pid][1] += 1
    rows = [{"pid": p, "name": G["players"][p]["name"], "pts": t[0], "ok": t[1]} for p, t in tot.items()]
    rows.sort(key=lambda r: (-r["pts"], -r["ok"], r["name"]))
    for i, r in enumerate(rows): r["pos"] = i + 1
    return rows

def state(pid=None, admin=False):
    with lock:
        tick(); ph, qi = G["phase"], G["qi"]
        q = Q[qi] if 0 <= qi < len(Q) else None
        lst = G["answers"].get(str(qi), []) if q else []
        b = board(); first = next((i for i, a in enumerate(lst) if a["ok"]), None)
        feed = [{"name": G["players"][a["pid"]]["name"], "ok": a["ok"], "t": a["t"], "rank": i + 1,
                 "win": i == first} for i, a in enumerate(lst) if a["pid"] in G["players"]]
        out = {"v": G["v"], "phase": ph, "qi": qi, "total": len(Q), "titulo": CFG["titulo"], "seg": SEG,
               "rem": max(0, int((deadline() - time.time()) * 1000)) if ph == "question" else 0,
               "n_players": len(G["players"]), "n_ans": len(lst), "feed": feed, "board": b[:10],
               "q": {"texto": q["texto"], "ops": q["opciones"]} if q and ph in ("question", "closed") else None,
               "correct": q["correcta"] if q and ph == "closed" else None,
               "correct_text": q["respuesta"] if q and ph == "closed" else None,
               "winner": feed[first]["name"] if first is not None else None}
        if pid is not None:
            me = G["players"].get(pid); out["me"] = None
            if me:
                mine = next((a for a in lst if a["pid"] == pid), None)
                pos = next((r for r in b if r["pid"] == pid), {})
                out["me"] = {"name": me["name"], "pts": pos.get("pts", 0), "pos": pos.get("pos"),
                             "answered": bool(mine), "ok": mine["ok"] if mine else None,
                             "rank": next((i + 1 for i, a in enumerate(lst) if a["pid"] == pid), None),
                             "t": mine["t"] if mine else None}
        if admin:
            out["admin"] = {"names": [p["name"] for p in G["players"].values()][-40:],
                            "hint": q["respuesta"] if q else None}
        return out

app = FastAPI(); app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
def need_key(k):
    if not k or not secrets.compare_digest(k, KEY): raise HTTPException(401, "Clave de administrador inválida")

class Join(BaseModel): nombre: str; apellido: str
class Ans(BaseModel): pid: str; option: int
class Act(BaseModel): keep_players: bool = True

@app.get("/")
def home(): return FileResponse(BASE / "static" / "index.html")
@app.get("/admin")
def admin(): return FileResponse(BASE / "static" / "admin.html")

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("10.255.255.255", 1)); return s.getsockname()[0]
    except Exception: return "127.0.0.1"
    finally: s.close()

@app.get("/api/info")
def info(): return {"lan": f"http://{lan_ip()}:{PORT}"}

@app.get("/qr.svg")
def qr(data: str = Query(max_length=300)):
    b = io.BytesIO(); segno.make(data, error="m").save(b, kind="svg", scale=10, border=2, dark="#21295C", light="#ffffff")
    return Response(b.getvalue(), media_type="image/svg+xml")

@app.post("/api/join")
def join(j: Join):
    n, a = " ".join(j.nombre.split())[:30], " ".join(j.apellido.split())[:30]
    if len(n) < 2 or len(a) < 2: raise HTTPException(422, "Escribe tu nombre y apellido")
    k = f"{n} {a}".casefold()
    with lock:
        for pid, p in G["players"].items():
            if p["key"] == k: return {"pid": pid}
        pid = secrets.token_urlsafe(6); G["players"][pid] = {"name": f"{n} {a}", "key": k}; bump()
    return {"pid": pid}

@app.get("/api/state")
def pub(pid: str = ""): return state(pid)

@app.post("/api/answer")
def answer(a: Ans):
    with lock:
        tick()
        if G["phase"] != "question": raise HTTPException(409, "La adivinanza ya no está abierta")
        if a.pid not in G["players"]: raise HTTPException(404, "Participante no registrado")
        q = Q[G["qi"]]; lst = G["answers"].setdefault(str(G["qi"]), [])
        if any(x["pid"] == a.pid for x in lst): raise HTTPException(409, "Ya respondiste")
        if not 0 <= a.option < len(q["opciones"]): raise HTTPException(422, "Opción inválida")
        lst.append({"pid": a.pid, "opt": a.option, "ok": a.option == q["correcta"],
                    "t": round(time.time() - G["opened"], 3)}); bump()
    return {"ok": True}

@app.get("/api/admin/state")
def adm(x_admin_key: Optional[str] = Header(None)):
    need_key(x_admin_key); return state(None, True)

def start(i):
    G["qi"] = i; G["phase"] = "question"; G["opened"] = time.time(); G["answers"][str(i)] = []

@app.post("/api/admin/{action}")
def act(action: str, body: Act = Act(), x_admin_key: Optional[str] = Header(None)):
    need_key(x_admin_key)
    with lock:
        tick()
        if action == "next":
            i = G["qi"] + 1
            if i >= len(Q): G["phase"] = "finished"
            else: start(i)
        elif action == "close" and G["phase"] == "question": G["phase"] = "closed"
        elif action == "repeat" and G["qi"] >= 0: start(G["qi"])
        elif action == "finish": G["phase"] = "finished"
        elif action == "reset":
            G.update(phase="lobby", qi=-1, answers={})
            if not body.keep_players: G["players"] = {}
        else: raise HTTPException(400, "Acción no válida")
        bump()
    return {"ok": True}

@app.get("/api/admin/export.csv")
def export(kind: str = "respuestas", key: str = ""):
    need_key(key); o = io.StringIO(); w = csv.writer(o)
    with lock:
        if kind == "ranking":
            w.writerow(["posicion", "participante", "puntos", "aciertos"])
            for r in board(): w.writerow([r["pos"], r["name"], r["pts"], r["ok"]])
        else:
            pa = points(); w.writerow(["adivinanza", "pregunta", "participante", "opcion", "correcta", "orden", "tiempo_s", "puntos"])
            for qk, lst in sorted(G["answers"].items(), key=lambda x: int(x[0])):
                for i, a in enumerate(lst):
                    p = G["players"].get(a["pid"], {"name": "?"})["name"]
                    w.writerow([int(qk) + 1, Q[int(qk)]["texto"], p, "ABCD"[a["opt"]], "SI" if a["ok"] else "NO", i + 1, a["t"], pa.get((qk, a["pid"]), 0)])
    return Response("\ufeff" + o.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={kind}.csv"})

@app.get("/api/admin/export.json")
def export_json(key: str = ""):
    need_key(key)
    with lock: d = {"ranking": board(), "estado": G}
    return Response(json.dumps(d, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=resultados.json"})

if __name__ == "__main__":
    ip = lan_ip()
    print(f"\n=== {CFG['titulo']} ===\nParticipantes : http://{ip}:{PORT}/\nAdministrador : http://{ip}:{PORT}/admin?key={KEY}\n"
          f"(en este equipo: http://localhost:{PORT}/admin?key={KEY})\n")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
