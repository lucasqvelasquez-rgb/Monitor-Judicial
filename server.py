"""
Monitor Judicial - Servidor
============================
Corre en Railway 24/7. Consulta la Rama Judicial automáticamente,
detecta nuevas actuaciones y manda emails.
"""
import os, json, time, threading, smtplib, logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── Variables de entorno (se configuran en Railway) ────────────────────────────
EMAIL_DESTINO       = os.environ.get("EMAIL_DESTINO", "")
GMAIL_USUARIO       = os.environ.get("GMAIL_USUARIO", "")
GMAIL_APP_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")
INTERVALO_MINUTOS   = int(os.environ.get("INTERVALO_MINUTOS", "60"))
ADMIN_TOKEN         = os.environ.get("ADMIN_TOKEN", "mi_token_secreto")

# ── Base de datos simple en memoria + archivo ──────────────────────────────────
DB_FILE = "procesos.json"

def leer_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {"procesos": []}

def escribir_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

db = leer_db()
notifs_pendientes = []

# ── Consulta API Rama Judicial ─────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CO,es;q=0.9",
    "Referer": "https://consultaprocesos.ramajudicial.gov.co/",
    "Origin": "https://consultaprocesos.ramajudicial.gov.co",
}

def consultar_rama_judicial(radicado: str) -> dict:
    base = "https://consultaprocesos.ramajudicial.gov.co/api/v2"

    # Paso 1: buscar proceso por radicado
    url = f"{base}/Procesos/NumeroRadicacion/{radicado}/pagina/1"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()

    procesos = data.get("procesos", [])
    if not procesos:
        raise ValueError(f"Radicado {radicado} no encontrado en CPNU")

    proc = procesos[0]
    id_proceso = proc.get("idProceso")

    # Paso 2: obtener actuaciones
    url_acts = f"{base}/Proceso/{id_proceso}/actuaciones/pagina/1"
    r2 = requests.get(url_acts, headers=HEADERS, timeout=20)
    r2.raise_for_status()
    data_acts = r2.json()

    actuaciones = []
    for a in data_acts.get("actuaciones", []):
        actuaciones.append({
            "id":          str(a.get("idRegActuacion", f"a_{time.time()}")),
            "fecha":       a.get("fechaActuacion", ""),
            "tipo":        a.get("actuacion", ""),
            "descripcion": a.get("anotacion", "") or a.get("actuacion", ""),
            "anotacion":   a.get("codRegla") or None,
            "conDoc":      bool(a.get("conDocumentos", False)),
        })

    return {
        "idProceso":       id_proceso,
        "demandante":      proc.get("sujetosProcesales", ""),
        "demandado":       proc.get("despacho", ""),
        "despacho":        proc.get("despacho", ""),
        "tipo":            proc.get("tipoProceso", ""),
        "clase":           proc.get("claseProceso", ""),
        "subclase":        proc.get("subclaseProceso", ""),
        "estado":          proc.get("estadoProceso", "En trámite"),
        "ponente":         proc.get("ponente", ""),
        "fechaRadicacion": proc.get("fechaProceso", ""),
        "ultimaActuacion": proc.get("fechaUltimaActuacion", ""),
        "actuaciones":     actuaciones,
    }

# ── Email ──────────────────────────────────────────────────────────────────────
def enviar_email(asunto: str, cuerpo: str):
    if not GMAIL_USUARIO or not GMAIL_APP_PASSWORD or not EMAIL_DESTINO:
        log.warning("Email no configurado, saltando notificación.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = asunto
        msg["From"]    = GMAIL_USUARIO
        msg["To"]      = EMAIL_DESTINO
        msg.attach(MIMEText(cuerpo, "plain", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USUARIO, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USUARIO, EMAIL_DESTINO, msg.as_string())
        log.info(f"Email enviado: {asunto}")
    except Exception as e:
        log.error(f"Error enviando email: {e}")

# ── Motor de polling ───────────────────────────────────────────────────────────
def ciclo_monitoreo():
    # esperar 10s al arrancar para que el servidor esté listo
    time.sleep(10)
    while True:
        log.info(f"=== Ciclo monitoreo — {len(db['procesos'])} procesos ===")
        novedades = []

        for proc in db["procesos"]:
            if proc.get("fuente") != "rama":
                continue
            radicado = proc["numero"]
            try:
                info = consultar_rama_judicial(radicado)

                ids_prev    = {a["id"] for a in proc.get("actuaciones", [])}
                acts_nuevas = [a for a in info["actuaciones"] if a["id"] not in ids_prev]

                proc.update({
                    **info,
                    "status":    "ok",
                    "lastCheck": datetime.now().isoformat(),
                    "newCount":  len(acts_nuevas),
                    "errorMsg":  None,
                })

                if acts_nuevas:
                    alias = proc.get("alias", radicado)
                    novedades.append(f"• {alias}: {len(acts_nuevas)} nueva(s) — {acts_nuevas[0]['tipo']}")
                    notifs_pendientes.append({
                        "radicado": radicado,
                        "alias":    alias,
                        "nuevas":   len(acts_nuevas),
                        "tipo":     acts_nuevas[0]["tipo"],
                        "fecha":    datetime.now().isoformat(),
                    })
                    log.info(f"  ✓ {radicado}: {len(acts_nuevas)} nueva(s)")
                else:
                    log.info(f"  ✓ {radicado}: sin cambios")

            except Exception as e:
                proc["status"]   = "error"
                proc["errorMsg"] = str(e)
                log.error(f"  ✗ {radicado}: {e}")

        escribir_db(db)

        if novedades:
            cuerpo = (
                "Hola,\n\nSe detectaron nuevas actuaciones en tus procesos judiciales:\n\n"
                + "\n".join(novedades)
                + "\n\nIngresa al Monitor Judicial para ver el detalle.\n\n— Monitor Judicial"
            )
            enviar_email(
                f"⚖️ Monitor Judicial — {len(novedades)} proceso(s) con novedades",
                cuerpo,
            )

        log.info(f"Ciclo completado. Próxima consulta en {INTERVALO_MINUTOS} min.")
        time.sleep(INTERVALO_MINUTOS * 60)

# ── Middleware de autenticación ────────────────────────────────────────────────
def auth_requerida(f):
    from functools import wraps
    @wraps(f)
    def decorado(*args, **kwargs):
        token = request.headers.get("X-Token") or request.args.get("token")
        if token != ADMIN_TOKEN:
            return jsonify({"error": "No autorizado"}), 401
        return f(*args, **kwargs)
    return decorado

# ── Endpoints ──────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return jsonify({
        "app": "Monitor Judicial",
        "version": "2.0",
        "procesos": len(db["procesos"]),
        "timestamp": datetime.now().isoformat(),
    })

@app.route("/api/ping")
def ping():
    return jsonify({"ok": True})

@app.route("/api/procesos", methods=["GET"])
@auth_requerida
def listar():
    return jsonify(db["procesos"])

@app.route("/api/procesos", methods=["POST"])
@auth_requerida
def agregar():
    body = request.json or {}
    numero = body.get("numero", "").strip().replace(" ", "")
    if not numero:
        return jsonify({"error": "Número requerido"}), 400
    if any(p["numero"] == numero for p in db["procesos"]):
        return jsonify({"error": "Ya existe"}), 409

    nuevo = {
        "id":          f"p_{int(time.time()*1000)}",
        "numero":      numero,
        "alias":       body.get("alias", numero),
        "fuente":      body.get("fuente", "rama"),
        "samaiGuid":   body.get("samaiGuid", ""),
        "actuaciones": [],
        "newCount":    0,
        "lastCheck":   None,
        "status":      "pending",
        "demandante":  "", "demandado":  "", "despacho":  "",
        "tipo":        "", "clase":      "", "estado":    "",
        "ponente":     "", "errorMsg":   None,
    }
    db["procesos"].append(nuevo)

    # consultar inmediatamente si es Rama Judicial
    if nuevo["fuente"] == "rama":
        try:
            info = consultar_rama_judicial(numero)
            nuevo.update({**info, "status": "ok", "lastCheck": datetime.now().isoformat()})
        except Exception as e:
            nuevo["status"]   = "error"
            nuevo["errorMsg"] = str(e)

    escribir_db(db)
    return jsonify(nuevo), 201

@app.route("/api/procesos/<proc_id>", methods=["DELETE"])
@auth_requerida
def eliminar(proc_id):
    db["procesos"] = [p for p in db["procesos"] if p["id"] != proc_id]
    escribir_db(db)
    return jsonify({"ok": True})

@app.route("/api/procesos/<proc_id>/consultar", methods=["POST"])
@auth_requerida
def consultar(proc_id):
    proc = next((p for p in db["procesos"] if p["id"] == proc_id), None)
    if not proc:
        return jsonify({"error": "No encontrado"}), 404
    if proc["fuente"] != "rama":
        return jsonify({"error": "SAMAI requiere consulta manual"}), 400
    try:
        info        = consultar_rama_judicial(proc["numero"])
        ids_prev    = {a["id"] for a in proc.get("actuaciones", [])}
        acts_nuevas = [a for a in info["actuaciones"] if a["id"] not in ids_prev]
        proc.update({**info, "status": "ok", "lastCheck": datetime.now().isoformat(), "newCount": len(acts_nuevas)})
        escribir_db(db)
        return jsonify(proc)
    except Exception as e:
        proc["status"] = "error"; proc["errorMsg"] = str(e)
        escribir_db(db)
        return jsonify({"error": str(e)}), 500

@app.route("/api/procesos/<proc_id>/actuacion", methods=["POST"])
@auth_requerida
def actuacion_manual(proc_id):
    proc = next((p for p in db["procesos"] if p["id"] == proc_id), None)
    if not proc:
        return jsonify({"error": "No encontrado"}), 404
    body = request.json or {}
    act  = {
        "id":          f"m_{int(time.time()*1000)}",
        "fecha":       body.get("fecha", datetime.now().date().isoformat()),
        "tipo":        body.get("tipo", ""),
        "descripcion": body.get("descripcion", ""),
        "anotacion":   body.get("anotacion"),
        "manual":      True,
    }
    proc.setdefault("actuaciones", []).insert(0, act)
    proc["newCount"]  = proc.get("newCount", 0) + 1
    proc["lastCheck"] = datetime.now().isoformat()
    escribir_db(db)
    return jsonify(act), 201

@app.route("/api/procesos/<proc_id>/visto", methods=["POST"])
@auth_requerida
def marcar_visto(proc_id):
    proc = next((p for p in db["procesos"] if p["id"] == proc_id), None)
    if proc:
        proc["newCount"] = 0
        escribir_db(db)
    return jsonify({"ok": True})

@app.route("/api/notificaciones", methods=["GET"])
@auth_requerida
def notificaciones():
    notifs = list(notifs_pendientes)
    notifs_pendientes.clear()
    return jsonify(notifs)

@app.route("/api/config", methods=["GET"])
@auth_requerida
def config_get():
    return jsonify({
        "email_destino":      EMAIL_DESTINO,
        "gmail_usuario":      GMAIL_USUARIO,
        "email_configurado":  bool(GMAIL_APP_PASSWORD and GMAIL_USUARIO),
        "intervalo_minutos":  INTERVALO_MINUTOS,
    })

@app.route("/api/config/test-email", methods=["POST"])
@auth_requerida
def test_email():
    try:
        enviar_email("✅ Monitor Judicial — Prueba", "Conexión exitosa.\n\n— Monitor Judicial")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Arranque ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    hilo = threading.Thread(target=ciclo_monitoreo, daemon=True)
    hilo.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
