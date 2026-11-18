"""
Stockage des données du bot (remplace les fichiers .json qui disparaissent sur Render).

- Si la variable d'environnement MONGODB_URI existe : tout est sauvegardé dans MongoDB.
- Sinon : le bot utilise les fichiers .json locaux comme avant (pratique pour tester sur ton PC).

Migration automatique : au premier lancement, si MongoDB ne contient encore rien pour un
fichier (ex : niveaux.json), le bot y envoie le contenu du .json présent dans le dossier.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB", "discord_bot")

_collection = None
# Les écritures MongoDB partent dans un thread à part (une seule à la fois, dans l'ordre) :
# le bot ne se fige jamais pendant une sauvegarde.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mongo")

if MONGODB_URI:
    try:
        from pymongo import MongoClient

        _client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)
        _client.admin.command("ping")  # vérifie la connexion dès le démarrage
        _collection = _client[MONGODB_DB]["json_store"]
        print("[STOCKAGE] MongoDB connecté ✅")
    except Exception as e:
        # On s'arrête : sans base, les données seraient perdues au prochain redémarrage.
        raise SystemExit(f"[STOCKAGE] Connexion MongoDB impossible : {e}")
else:
    print("[STOCKAGE] MONGODB_URI absent : utilisation des fichiers .json locaux.")


def _lire_local(nom, defaut):
    if os.path.exists(nom):
        with open(nom, "r", encoding="utf-8") as f:
            return json.load(f)
    return defaut


def charger(nom, defaut=None):
    """Lit le contenu de `nom` (ex : "niveaux.json"). Retourne `defaut` si rien n'existe."""
    if defaut is None:
        defaut = {}
    if _collection is None:
        return _lire_local(nom, defaut)

    doc = _collection.find_one({"_id": nom})
    if doc is not None:
        return json.loads(doc["data"])

    # Première fois : on importe le fichier local s'il existe, puis on le range dans MongoDB.
    data = _lire_local(nom, defaut)
    if os.path.exists(nom):
        sauver(nom, data)
        print(f"[STOCKAGE] {nom} importé dans MongoDB.")
    return data


def _ecrire_mongo(nom, texte):
    try:
        _collection.replace_one({"_id": nom}, {"_id": nom, "data": texte}, upsert=True)
    except Exception as e:
        print(f"[STOCKAGE] Échec de sauvegarde de {nom} : {e}")


def sauver(nom, data):
    """Enregistre `data` (dict / list) sous le nom `nom`."""
    if _collection is None:
        with open(nom, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return
    # On fige le contenu en texte tout de suite (la suite peut modifier `data`), puis on envoie.
    # Stocké en texte JSON : aucune limite sur les clés (IDs numériques, etc.)
    texte = json.dumps(data, ensure_ascii=False)
    _executor.submit(_ecrire_mongo, nom, texte)
