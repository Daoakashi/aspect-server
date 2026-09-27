# Aspect .txt — serveur temps réel

Ce dossier contient le serveur Python (FastAPI) qui fait fonctionner la
messagerie en temps réel de l'appli **Aspect .txt**, le fichier `aspect-txt.html`
étant l'interface (déjà publiée en ligne, ou utilisable en local).

Testé dans cet environnement : connexion en temps réel entre deux clients,
chiffrement des messages au repos (AES via Fernet) et panneau de télémétrie —
tout fonctionne.

## 1. Lancer le serveur en local

```bash
cd aspect-server
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Au démarrage, le terminal affiche un **code admin** (sauf si tu fixes
`ASPECT_ADMIN_CODE` toi-même avant de lancer). Garde-le pour toi : c'est lui
qui te donne le statut admin.

## 2. Connecter l'appli (le fichier HTML) au serveur

1. Ouvre `aspect-txt.html` (ou le lien publié).
2. Va dans le panneau admin (icône ⚙️) → section **Serveur temps réel**.
3. Renseigne l'adresse WebSocket : en local `ws://localhost:8000/ws`,
   ou `wss://ton-adresse-publique/ws` une fois déployé (voir plus bas).
4. Mets ton prénom, clique **Connecter**.

Tant qu'aucun serveur n'est connecté, l'appli reste en **mode local** (messages
simulés sur l'appareil, comme avant) — rien n'est cassé si tu ne branches rien.

## 3. Rendre le serveur accessible à tout le monde (pas juste sur ton PC)

Ton ordinateur seul n'est pas accessible depuis internet par défaut. Pour que
tes camarades s'y connectent depuis chez eux, il faut héberger le serveur
quelque part. Deux options gratuites simples (aucune carte bancaire requise) :

- **Render.com** (Web Service, "Python" runtime) : pousse ce dossier sur
  GitHub, connecte le repo, commande de démarrage
  `uvicorn main:app --host 0.0.0.0 --port $PORT`.
- **Railway.app** : même principe, détection automatique de `requirements.txt`.

Une fois déployé, tu obtiens une adresse du type
`https://ton-app.onrender.com` → utilise `wss://ton-app.onrender.com/ws`
dans le panneau admin de l'appli.

## 4. Panneau de télémétrie (admin)

Ouvre `http://ton-serveur/telemetry` dans un navigateur, colle ton token
admin (récupéré via `/api/join` avec le bon code admin) : tu verras en direct
le nombre de connectés, les prénoms connectés, le nombre de messages, le
débit par minute et le temps de fonctionnement du serveur.

## 5. Ce que ce serveur fait vraiment, et ses limites honnêtes

- ✅ **Temps réel** : les messages arrivent instantanément (testé), pas de
  rafraîchissement manuel.
- ✅ **Anonymat par prénom** : aucune inscription, aucun email, aucun mot de
  passe — juste un prénom choisi librement.
- ✅ **Chiffrement au repos** : les messages sont chiffrés (AES/Fernet) dans
  la base de données. Si quelqu'un ouvre le fichier `aspect.db`, il ne voit
  que du texte chiffré.
- ⚠️ **Chiffrement en transit** : entre l'appli et le serveur, la sécurité du
  trajet dépend du protocole utilisé — `wss://` (chiffré, comme HTTPS) une
  fois déployé sur Render/Railway avec certificat automatique, ou `ws://`
  (non chiffré) en local sur ton réseau. Utilise toujours `wss://` en public.
- ⚠️ **Pas un vrai chiffrement de bout en bout** : le serveur (donc
  l'ordinateur qui l'héberge) peut techniquement déchiffrer les messages
  puisqu'il détient la clé (`secret.key`). Pour empêcher même l'admin de lire
  les messages, il faudrait un chiffrement fait côté client avec un échange
  de clés entre utilisateurs — une étape supplémentaire que je peux
  construire ensuite si tu veux vraiment ce niveau de confidentialité.
- ⚠️ **Modération admin minimale** : le compte admin actuel donne accès à la
  télémétrie, pas encore à des actions (bannir, supprimer un message) — dis-le
  moi si tu veux que j'ajoute ces outils.

## 6. Modération (admin)

Dans le panneau `/telemetry`, en plus des chiffres en direct, tu as maintenant :
- **Kick** un prénom connecté (déconnexion immédiate),
- **Bannir / débannir** un prénom (empêche toute reconnexion sous ce prénom
  tant qu'il est banni — limite honnête : rien n'empêche quelqu'un de revenir
  sous un *autre* prénom, propre au principe d'un système anonyme sans compte),
- **Voir et supprimer** les messages de la conversation "general" (le serveur
  les déchiffre pour l'affichage admin puisqu'il détient déjà la clé).

## 7. Version .exe (Windows) — l'erreur que tu as eue, corrigée

Le premier `.exe` plantait très probablement pour une raison très classique
avec PyInstaller + FastAPI/uvicorn : certains modules internes d'uvicorn sont
chargés dynamiquement, et PyInstaller ne les détecte pas tout seul → erreur
au lancement. J'ai corrigé deux choses :
1. `main.py` a maintenant un point d'entrée direct (`uvicorn.run(...)` dans
   le fichier lui-même) au lieu de dépendre de la commande `uvicorn main:app`.
2. `build_windows_exe.bat` force maintenant l'inclusion complète des paquets
   concernés (`--collect-all uvicorn/fastapi/starlette/...`).

**Relance `build_windows_exe.bat` avec cette version mise à jour.** Je ne
peux pas exécuter de vrai Windows ici pour te garantir à 100 % que ça
suffira — si l'erreur revient, la manière la plus utile de me la montrer :
ouvre une invite de commandes (`cmd`), va dans le dossier avec `cd`, tape
`aspect-server.exe` et appuie sur Entrée : la fenêtre ne se fermera plus
toute seule et tu pourras copier-coller le texte de l'erreur ici (au lieu
d'un double-clic, où la fenêtre d'erreur peut se fermer trop vite pour la lire).

