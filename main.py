"""
Aspect .txt — serveur temps réel
---------------------------------
Serveur FastAPI + WebSocket pour la messagerie en temps réel :
- Connexion anonyme par prénom (pas d'email, pas de mot de passe)
- Un code admin optionnel donne le statut administrateur
- Les messages sont chiffrés au repos (AES via Fernet) dans la base SQLite
- Diffusion instantanée à tous les appareils connectés (WebSocket)
- Panneau de télémétrie : /telemetry (connexions live, débit de messages, uptime)

Lancer en local :
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8000

Le code admin est affiché dans le terminal au démarrage (ou fixe-le avec la
variable d'environnement ASPECT_ADMIN_CODE avant de lancer le serveur).
"""

import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

# En .exe (PyInstaller onefile), __file__ pointe vers un dossier temporaire
# qui est effacé à la fermeture — la base et la clé doivent vivre à côté de
# l'exécutable, pas dedans, sinon tout l'historique est perdu à chaque lancement.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "aspect.db"
KEY_PATH = BASE_DIR / "secret.key"

# ---------------------------------------------------------------------------
# Clé de chiffrement (générée une fois, réutilisée ensuite pour déchiffrer
# l'historique existant). Ne partage jamais ce fichier publiquement.
# ---------------------------------------------------------------------------
if KEY_PATH.exists():
    FERNET_KEY = KEY_PATH.read_bytes()
else:
    FERNET_KEY = Fernet.generate_key()
    KEY_PATH.write_bytes(FERNET_KEY)
fernet = Fernet(FERNET_KEY)

ADMIN_CODE = os.environ.get("ASPECT_ADMIN_CODE") or secrets.token_hex(4)
if not os.environ.get("ASPECT_ADMIN_CODE"):
    print(f"\n>>> Code admin généré pour cette session : {ADMIN_CODE}")
    print(">>> Donne-le uniquement à toi-même pour obtenir le statut admin.\n")

START_TIME = time.time()

# ---------------------------------------------------------------------------
# Base de données (SQLite, un seul fichier, zéro serveur externe requis)
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            token TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation TEXT NOT NULL,
            sender_name TEXT NOT NULL,
            ciphertext BLOB NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS banned (
            name TEXT PRIMARY KEY,
            banned_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_db()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Aspect .txt server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# token -> WebSocket (connexions actives, en mémoire)
CONNECTIONS: dict[str, WebSocket] = {}
# petit journal en mémoire pour la télémétrie (débit de messages)
MESSAGE_LOG: list[float] = []


@app.post("/api/join")
async def join(payload: dict):
    """Rejoindre avec juste un prénom. Aucun email, aucun mot de passe."""
    name = (payload.get("name") or "").strip()[:24]
    admin_code = (payload.get("admin_code") or "").strip()
    if not name:
        return JSONResponse({"error": "Nom manquant"}, status_code=400)

    token = secrets.token_hex(16)
    is_admin = 1 if admin_code and admin_code == ADMIN_CODE else 0

    conn = get_db()
    conn.execute(
        "INSERT INTO users (token, name, is_admin, created_at) VALUES (?, ?, ?, ?)",
        (token, name, is_admin, time.time()),
    )
    conn.commit()
    conn.close()

    return {"token": token, "name": name, "is_admin": bool(is_admin)}


def get_user(token: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    conn.close()
    return row


def is_banned(name: str) -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM banned WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row is not None


def require_admin(token: str):
    user = get_user(token)
    return user if user and user["is_admin"] else None


@app.get("/api/messages/{conversation}")
async def get_messages(conversation: str, token: str = Query(...)):
    user = get_user(token)
    if not user:
        return JSONResponse({"error": "Token invalide"}, status_code=401)
    conn = get_db()
    rows = conn.execute(
        "SELECT sender_name, ciphertext, created_at FROM messages WHERE conversation = ? ORDER BY id ASC",
        (conversation,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            text = fernet.decrypt(r["ciphertext"]).decode("utf-8")
        except Exception:
            text = "[message illisible]"
        out.append({"sender_name": r["sender_name"], "text": text, "created_at": r["created_at"]})
    return {"messages": out}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str = Query(None), name: str = Query(None)):
    # Deux modes de connexion : avec un token obtenu via /api/join, ou en
    # direct avec juste ?name=... (le serveur crée alors un compte anonyme
    # à la volée — pratique pour brancher rapidement le frontend fourni).
    user_name = name or "Anonyme"
    if token:
        user = get_user(token)
        if user:
            user_name = user["name"]
        else:
            token = None

    if is_banned(user_name):
        # Rejeté avant l'acceptation de la connexion : le client ne se
        # connecte jamais, pas d'accept()+close() qui laisserait une fenêtre.
        await websocket.close(code=4403)
        return

    await websocket.accept()

    if not token:
        token = secrets.token_hex(16)
        conn = get_db()
        conn.execute(
            "INSERT INTO users (token, name, is_admin, created_at) VALUES (?, ?, 0, ?)",
            (token, user_name, time.time()),
        )
        conn.commit()
        conn.close()

    CONNECTIONS[token] = websocket

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if data.get("type") != "message":
                continue

            text = (data.get("text") or "").strip()
            conversation = str(data.get("conversation") or "general")
            if not text:
                continue

            # chiffrement au repos avant stockage
            ciphertext = fernet.encrypt(text.encode("utf-8"))
            conn = get_db()
            conn.execute(
                "INSERT INTO messages (conversation, sender_name, ciphertext, created_at) VALUES (?, ?, ?, ?)",
                (conversation, user_name, ciphertext, time.time()),
            )
            conn.commit()
            conn.close()
            MESSAGE_LOG.append(time.time())

            # diffusion instantanée à tout le monde de connecté
            out = json.dumps({
                "type": "message",
                "conversation": conversation,
                "sender_name": user_name,
                "text": text,
                "created_at": time.time(),
            })
            dead = []
            for tok, ws in CONNECTIONS.items():
                try:
                    await ws.send_text(out)
                except Exception:
                    dead.append(tok)
            for tok in dead:
                CONNECTIONS.pop(tok, None)
    except WebSocketDisconnect:
        CONNECTIONS.pop(token, None)


@app.post("/api/admin/kick")
async def admin_kick(payload: dict):
    if not require_admin(payload.get("admin_token", "")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    target = (payload.get("name") or "").strip()
    kicked = 0
    for tok in list(CONNECTIONS.keys()):
        u = get_user(tok)
        if u and u["name"] == target:
            try:
                await CONNECTIONS[tok].close(code=4001)
            except Exception:
                pass
            CONNECTIONS.pop(tok, None)
            kicked += 1
    return {"kicked": kicked}


@app.post("/api/admin/ban")
async def admin_ban(payload: dict):
    if not require_admin(payload.get("admin_token", "")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    target = (payload.get("name") or "").strip()
    if not target:
        return JSONResponse({"error": "nom manquant"}, status_code=400)
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO banned (name, banned_at) VALUES (?, ?)", (target, time.time()))
    conn.commit()
    conn.close()
    for tok in list(CONNECTIONS.keys()):
        u = get_user(tok)
        if u and u["name"] == target:
            try:
                await CONNECTIONS[tok].close(code=4003)
            except Exception:
                pass
            CONNECTIONS.pop(tok, None)
    return {"banned": target}


@app.post("/api/admin/unban")
async def admin_unban(payload: dict):
    if not require_admin(payload.get("admin_token", "")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    target = (payload.get("name") or "").strip()
    conn = get_db()
    conn.execute("DELETE FROM banned WHERE name = ?", (target,))
    conn.commit()
    conn.close()
    return {"unbanned": target}


@app.get("/api/admin/banned")
async def admin_banned_list(admin_token: str = Query(...)):
    if not require_admin(admin_token):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    conn = get_db()
    rows = conn.execute("SELECT name, banned_at FROM banned ORDER BY banned_at DESC").fetchall()
    conn.close()
    return {"banned": [dict(r) for r in rows]}


@app.get("/api/admin/messages")
async def admin_messages(admin_token: str = Query(...), conversation: str = Query("general")):
    if not require_admin(admin_token):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    conn = get_db()
    rows = conn.execute(
        "SELECT id, sender_name, ciphertext, created_at FROM messages WHERE conversation = ? ORDER BY id DESC LIMIT 200",
        (conversation,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            text = fernet.decrypt(r["ciphertext"]).decode("utf-8")
        except Exception:
            text = "[message illisible]"
        out.append({"id": r["id"], "sender_name": r["sender_name"], "text": text, "created_at": r["created_at"]})
    return {"messages": out}


@app.post("/api/admin/delete_message")
async def admin_delete_message(payload: dict):
    if not require_admin(payload.get("admin_token", "")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    mid = payload.get("id")
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE id = ?", (mid,))
    conn.commit()
    conn.close()
    return {"deleted": mid}


@app.get("/api/telemetry")
async def telemetry(token: str = Query(...)):
    user = get_user(token)
    if not user or not user["is_admin"]:
        return JSONResponse({"error": "Réservé à l'admin"}, status_code=403)

    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    total_messages = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    names = []
    for tok in list(CONNECTIONS.keys()):
        row = get_user(tok)
        names.append(row["name"] if row else "?")
    conn.close()

    recent = [t for t in MESSAGE_LOG if time.time() - t < 60]
    return {
        "connected_count": len(CONNECTIONS),
        "connected_names": names,
        "total_users": total_users,
        "total_messages": total_messages,
        "messages_last_minute": len(recent),
        "uptime_seconds": round(time.time() - START_TIME),
    }


@app.get("/telemetry", response_class=HTMLResponse)
async def telemetry_page():
    # Tableau de bord admin : télémétrie live + outils de modération.
    return """
<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>Aspect .txt — télémétrie & modération</title>
<style>
  body { font-family: -apple-system, sans-serif; background:#0B0D12; color:#EAF2FF; margin:0; padding:32px; }
  h1 { font-size:20px; } h3 { font-size:15px; margin-top:34px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit,minmax(160px,1fr)); gap:14px; margin:20px 0; }
  .card { background:#131722; border:1px solid #232838; border-radius:12px; padding:16px; }
  .card b { display:block; font-size:26px; margin-top:6px; }
  .card span { font-size:12px; color:#8A93A6; }
  ul { padding-left:0; list-style:none; }
  li { display:flex; align-items:center; justify-content:space-between; padding:8px 12px; background:#131722; border:1px solid #232838; border-radius:8px; margin-bottom:6px; font-size:13px; }
  .msg-text { flex:1; margin:0 12px; color:#C8D2E0; }
  input, button { font-family:inherit; padding:8px 10px; border-radius:8px; border:1px solid #232838; background:#131722; color:#EAF2FF; }
  button { cursor:pointer; }
  button.danger { border-color:#5A2330; color:#FF8A9B; }
  button.warn { border-color:#5A4A23; color:#FFC98A; }
  .row { display:flex; gap:8px; margin-bottom:10px; }
</style></head>
<body>
  <h1>📡 Aspect .txt — télémétrie &amp; modération</h1>
  <div class="row">
    <input id="tok" placeholder="Token admin" style="width:280px">
    <button onclick="localStorage.setItem('aspect_admin_tok', document.getElementById('tok').value); loadAll()">Charger</button>
  </div>
  <div class="grid" id="grid"></div>

  <h3>Connectés maintenant</h3>
  <ul id="names"></ul>

  <h3>Bannir / débannir un prénom</h3>
  <div class="row">
    <input id="banName" placeholder="Prénom">
    <button class="danger" onclick="banUser()">Bannir</button>
    <button onclick="unbanUser()">Débannir</button>
  </div>
  <ul id="bannedList"></ul>

  <h3>Messages (conversation "general")</h3>
  <ul id="messages"></ul>

<script>
document.getElementById('tok').value = localStorage.getItem('aspect_admin_tok') || '';
function tok() { return document.getElementById('tok').value; }

async function loadAll() {
  if (!tok()) return;
  const res = await fetch('/api/telemetry?token=' + encodeURIComponent(tok()));
  if (!res.ok) { document.getElementById('grid').innerHTML = '<div class="card">Token invalide ou non-admin</div>'; return; }
  const d = await res.json();
  document.getElementById('grid').innerHTML = `
    <div class="card"><span>Connectés</span><b>${d.connected_count}</b></div>
    <div class="card"><span>Utilisateurs (total)</span><b>${d.total_users}</b></div>
    <div class="card"><span>Messages (total)</span><b>${d.total_messages}</b></div>
    <div class="card"><span>Messages / min</span><b>${d.messages_last_minute}</b></div>
    <div class="card"><span>Uptime (s)</span><b>${d.uptime_seconds}</b></div>`;
  document.getElementById('names').innerHTML = d.connected_names.map(n =>
    `<li><span>${n}</span><span><button class="warn" onclick="kickUser('${n}')">Kick</button> <button class="danger" onclick="banFromList('${n}')">Ban</button></span></li>`
  ).join('') || '<li>Personne n\\'est connecté</li>';

  const bres = await fetch('/api/admin/banned?admin_token=' + encodeURIComponent(tok()));
  const bd = await bres.json();
  document.getElementById('bannedList').innerHTML = (bd.banned || []).map(b =>
    `<li><span>${b.name}</span><button onclick="unbanNamed('${b.name}')">Débannir</button></li>`
  ).join('') || '<li>Aucun prénom banni</li>';

  const mres = await fetch('/api/admin/messages?admin_token=' + encodeURIComponent(tok()) + '&conversation=general');
  const md = await mres.json();
  document.getElementById('messages').innerHTML = (md.messages || []).map(m =>
    `<li><b>${m.sender_name}</b><span class="msg-text">${m.text}</span><button class="danger" onclick="deleteMessage(${m.id})">Supprimer</button></li>`
  ).join('') || '<li>Aucun message</li>';
}

async function kickUser(name) {
  await fetch('/api/admin/kick', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ admin_token: tok(), name }) });
  loadAll();
}
async function banFromList(name) {
  await fetch('/api/admin/ban', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ admin_token: tok(), name }) });
  loadAll();
}
async function banUser() {
  const name = document.getElementById('banName').value.trim();
  if (!name) return;
  await banFromList(name);
  document.getElementById('banName').value = '';
}
async function unbanNamed(name) {
  await fetch('/api/admin/unban', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ admin_token: tok(), name }) });
  loadAll();
}
async function unbanUser() {
  const name = document.getElementById('banName').value.trim();
  if (!name) return;
  await unbanNamed(name);
  document.getElementById('banName').value = '';
}
async function deleteMessage(id) {
  await fetch('/api/admin/delete_message', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ admin_token: tok(), id }) });
  loadAll();
}

setInterval(loadAll, 4000);
loadAll();
</script>
</body></html>
"""


@app.get("/")
async def root():
    return {"status": "ok", "service": "aspect-txt-server", "admin_code_hint": "voir le terminal au démarrage"}


if __name__ == "__main__":
    # Point d'entrée direct : indispensable pour un .exe PyInstaller fiable
    # (lancer via "python -m uvicorn" pose problème une fois figé en exécutable).
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"\n>>> Serveur Aspect .txt lancé sur http://0.0.0.0:{port}")
    print(">>> Laisse cette fenêtre ouverte tant que le serveur doit tourner.\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

