import asyncio
import bisect
import hashlib
import io
import json
import os
import random
import re
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks



def charger_env():
    """En local : lit DISCORD_TOKEN.env / .env. Sur Render : les variables sont déjà là, on ne touche à rien."""
    for nom in ("DISCORD_TOKEN.env", ".env"):
        if not os.path.exists(nom):
            continue
        with open(nom, "r", encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if ligne and not ligne.startswith("#") and "=" in ligne:
                    cle, valeur = ligne.split("=", 1)
                    os.environ.setdefault(cle.strip(), valeur.strip().strip('"').strip("'"))


charger_env()  # doit passer AVANT "import storage" (qui lit MONGODB_URI)
import storage  # noqa: E402  (MongoDB sur Render, fichiers .json en local)

STATUT = ".gg/paek"


_web_demarre = False
etat_connexion = "connexion en cours..."   # affiché sur la page de statut


async def demarrer_serveur_web():
    """Mini serveur web : Render (offre Web Service) exige un port ouvert, et un site de ping
    (UptimeRobot...) peut l'appeler pour empêcher le bot de s'endormir. Inactif sans variable PORT."""
    global _web_demarre
    port = os.getenv("PORT")
    if not port or _web_demarre:
        return
    _web_demarre = True
    from aiohttp import web

    async def ok(_request):
        if client.is_ready():
            return web.Response(text=f"Bot connecté à Discord ✅ ({len(client.guilds)} serveur(s))")
        return web.Response(text=f"Bot NON connecté à Discord ⏳\nÉtat : {etat_connexion}")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    print(f"[WEB] Serveur de ping démarré sur le port {port}")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class MonBot(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        maj_stats.start()           # met à jour les compteurs
        verif_raid.start()          # expire le mode anti-raid
        sauvegarde_niveaux.start()  # sauvegarde les niveaux sur le disque
        boucle_dsstat.start()       # rapport quotidien du serveur
        verif_tiktok.start()        # alertes de paliers d'abonnés TikTok
        verif_videos.start()        # annonces de nouvelles vidéos (RSSHub, toutes les 5 min)
        if relay_notifyme is not None:
            relay_notifyme.verif_notify.start()  # /post : relais du salon Notify Me (toutes les 5 min)
        boucle_snapshots_antinuke.start()  # snapshots anti-nuke
        await demarrer_serveur_web()  # utile seulement sur Render (Web Service)
        await telecharger_mots()      # liste de mots interdits depuis GitHub (si MOTS_URL est défini)
        recharger_mots()              # fusionne liste de base + fichier + GitHub + MongoDB
        # Vues persistantes tickets (survivent au redémarrage)
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        self.add_view(DsstatView())  # boutons du rapport quotidien


client = MonBot(
    intents=intents,
    status=discord.Status.online,
    activity=discord.CustomActivity(name=STATUT),
)
tree = client.tree


@tree.error
async def erreur_commande(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Toute erreur de commande est écrite dans les logs ET affichée à l'utilisateur
    (au lieu de 'L'application ne répond pas')."""
    import traceback
    nom = getattr(interaction.command, "name", None) or (interaction.data or {}).get("name", "?")
    print(f"[COMMANDE] Erreur sur /{nom} : {error!r}")
    traceback.print_exception(type(error), error, error.__traceback__)

    # Commande inconnue en mémoire : on la réenregistre localement (aucune requête à Discord)
    if isinstance(error, app_commands.CommandNotFound) and interaction.guild and COMMANDES:
        for c in COMMANDES:
            tree.add_command(c, guild=interaction.guild, override=True)

    cause = getattr(error, "original", error)
    message = f"❌ Error in `/{nom}`: `{type(cause).__name__}: {cause}`"[:1900]
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception as e:
        print(f"[COMMANDE] Impossible d'envoyer le message d'erreur : {e}")


@client.event
async def on_interaction(interaction: discord.Interaction):
    nom = (interaction.data or {}).get("name") or (interaction.data or {}).get("custom_id", "?")
    print(f"[INTERACTION] {interaction.type.name} « {nom} » de {interaction.user} sur {getattr(interaction.guild, 'name', 'DM')}")

# ---------- SERVEUR PRINCIPAL ----------
# Tous les IDs ci-dessous (salons, rôles, catégorie...) n'existent que dans UN serveur : le serveur principal.
# Mets son ID dans la variable d'environnement GUILD_PRINCIPAL_ID (clic droit sur le serveur > Copier l'identifiant).
# Si elle est vide, le bot reconnaît le serveur principal au fait qu'il contient le salon CHANNEL_ID.
# Si le bot n'est dans AUCUN serveur principal, il passe tout seul sur le script relay_notifyme.py.
_gp = os.getenv("GUILD_PRINCIPAL_ID", "").strip()
GUILD_PRINCIPAL_ID = int(_gp) if _gp.isdigit() else 1397311784364281937  # ID du serveur principal (la variable d'environnement GUILD_PRINCIPAL_ID est prioritaire)
SCRIPT_RELAIS = "relay_notifyme.py"

CHANNEL_ID = 1420172051448201216  # salon réception mis à jour[cite: 2]
DUREE_TIMEOUT = timedelta(hours=1)
SPAM_MAX_MESSAGES = 5            # plus de 5 messages...
SPAM_FENETRE = 5                 # ...en moins de 5 secondes
DUREE_TIMEOUT_SPAM = timedelta(hours=1)
SPAM_ANALYSE = 30                # le bot relit les 30 dernières secondes pour tout effacer
TEXTWALL_MAX_CARACTERES = 1500   # un message plus long que ça = mur de texte
TEXTWALL_MAX_LIGNES = 15         # ...ou plus de 15 lignes
FICHIER_MOTS = "mots_interdits.txt"  # liste LDNOOBW fusionnée (un mot par ligne)
# Lien "raw" du fichier sur GitHub (ex: https://raw.githubusercontent.com/TON_PSEUDO/TON_REPO/main/mots_interdits.txt)
# À mettre dans les variables d'environnement (Render) sous le nom MOTS_URL. Optionnel.
URL_MOTS = os.getenv("MOTS_URL", "").strip()

# Mots en plus du fichier (insultes courantes FR/EN que tu veux garantir)
MOTS_SUPPLEMENTAIRES = [
    "connard", "connasse", "salope", "pute", "putain", "enculer", "enfoire",
    "batard", "merde", "fdp", "ntm", "ftg", "ptn", "petasse", "abruti",
    "fils de pute", "ferme ta gueule", "ta gueule", "nique ta mere",
    "nique ta race", "va te faire foutre", "va te faire enculer",
    "fuck", "fucking", "motherfucker", "shit", "bitch", "asshole",
    "bastard", "dickhead", "cunt", "slut", "whore", "stfu", "kys",
    "kill yourself", "fuck you", "fuck off", "retard", "pedo",
]

# Mots du fichier que tu veux autoriser quand même (en minuscules, sans accents)
MOTS_AUTORISES = set()


# ---------- DÉTECTION ----------
LEET = str.maketrans({"@": "a", "0": "o", "1": "i", "3": "e", "$": "s", "5": "s", "7": "t", "!": "i"})


def normaliser(texte):
    texte = unicodedata.normalize("NFKD", texte.lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return texte.translate(LEET)


def trouver_fichier_mots():
    """Cherche mots_interdits.txt à côté de Bot.py puis dans le dossier courant,
    sans tenir compte des majuscules (Linux/Render est sensible à la casse)."""
    dossiers = [os.path.dirname(os.path.abspath(__file__)), os.getcwd()]
    for dossier in dossiers:
        try:
            for nom in os.listdir(dossier):
                if nom.lower() == FICHIER_MOTS.lower():
                    return os.path.join(dossier, nom)
        except OSError:
            pass
    return None


def charger_mots(extra=None):
    mots = list(MOTS_SUPPLEMENTAIRES)
    chemin = trouver_fichier_mots()
    if chemin:
        with open(chemin, "r", encoding="utf-8") as f:
            lus = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        mots += lus
        print(f"[MOTS] {len(lus)} mots lus dans {chemin}")
    else:
        print(f"[ATTENTION] {FICHIER_MOTS} introuvable : seule la liste de base est utilisée.")
    if extra:
        mots += extra
    return mots


def construire_motif(mot):
    # chaque lettre peut être répétée ("fuuuck"), les espaces acceptent plusieurs espaces
    n = normaliser(mot).strip()
    morceaux = []
    for c in n:
        morceaux.append(r"\s+" if c.isspace() else re.escape(c) + "+")
    # Limites de mot pour le match normal
    return re.compile(r"(?<!\w)" + "".join(morceaux) + r"(?!\w)")


MOTIFS = []          # (motif_regex, mot_normalise)
MOTS_NORM = []       # liste des mots normalisés (pour substring dans le spam)


def appliquer_mots(mots):
    """(Re)construit les listes de détection en place."""
    vus = set()
    motifs, norms = [], []
    for m in mots:
        n = normaliser(m).strip()
        if n and n not in vus and n not in MOTS_AUTORISES:
            vus.add(n)
            motifs.append((construire_motif(m), n))
            norms.append(n)
    MOTIFS[:] = motifs
    MOTS_NORM[:] = norms


appliquer_mots(charger_mots())


FICHIER_MOTS_DB = "mots_interdits_db.json"  # stocké via storage (MongoDB sur Render)
_mots_github = []  # mots téléchargés depuis MOTS_URL (si défini)


def charger_mots_db():
    """Mots ajoutés via /importmots et /addmot (stockés dans MongoDB)."""
    try:
        data = storage.charger(FICHIER_MOTS_DB)
    except Exception as e:
        print(f"[ATTENTION] Lecture des mots MongoDB impossible : {e}")
        return []
    if isinstance(data, dict):
        data = data.get("mots", [])
    if not isinstance(data, list):
        return []
    return [str(m).strip() for m in data if str(m).strip()]


def sauver_mots_db(mots):
    storage.sauver(FICHIER_MOTS_DB, {"mots": mots})


def recharger_mots():
    """Reconstruit la détection : liste de base + fichier local + GitHub + MongoDB."""
    base = _mots_github + charger_mots_db()
    appliquer_mots(charger_mots(base))
    print(f"[MOTS] {len(MOTIFS)} mots actifs au total ({len(base)} depuis GitHub/MongoDB).")


async def telecharger_mots():
    """Récupère la liste depuis GitHub (MOTS_URL) si défini."""
    if not URL_MOTS:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(URL_MOTS, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    print(f"[ATTENTION] MOTS_URL a répondu {r.status} : liste GitHub non chargée.")
                    return
                texte = await r.text()
    except Exception as e:
        print(f"[ATTENTION] Impossible de télécharger MOTS_URL : {e}")
        return
    _mots_github[:] = [l.strip() for l in texte.splitlines() if l.strip() and not l.startswith("#")]
    print(f"[MOTS] {len(_mots_github)} mots téléchargés depuis GitHub.")


@tree.command(
    name="importmots",
    description="Import a banned words list (.txt, one word per line) into the bot storage",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(fichier="Text file with one banned word or phrase per line")
async def importmots(interaction: discord.Interaction, fichier: discord.Attachment):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    if fichier.size > 5 * 1024 * 1024:
        await interaction.response.send_message("File too large (5 MB max).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        brut = await fichier.read()
        texte = brut.decode("utf-8-sig")
    except Exception as e:
        await interaction.followup.send(f"Failed to read the file (UTF-8 text expected): {e}", ephemeral=True)
        return

    nouveaux = [l.strip() for l in texte.splitlines() if l.strip() and not l.startswith("#")]
    if not nouveaux:
        await interaction.followup.send("No words found in this file.", ephemeral=True)
        return

    existants = charger_mots_db()
    connus = {normaliser(m).strip() for m in existants}
    ajoutes = 0
    for m in nouveaux:
        n = normaliser(m).strip()
        if n and n not in connus:
            connus.add(n)
            existants.append(m)
            ajoutes += 1

    try:
        sauver_mots_db(existants)
    except Exception as e:
        await interaction.followup.send(f"Storage error: {e}", ephemeral=True)
        return

    recharger_mots()
    await interaction.followup.send(
        f"✅ **{ajoutes}** new words imported ({len(nouveaux) - ajoutes} already known).\n"
        f"The filter now uses **{len(MOTIFS)}** words.",
        ephemeral=True,
    )


@tree.command(name="addmot", description="Add a word or phrase to the banned words list")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(mot="The word or phrase to ban")
async def addmot(interaction: discord.Interaction, mot: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    mot = mot.strip()
    n = normaliser(mot).strip()
    if not n:
        await interaction.response.send_message("Invalid word.", ephemeral=True)
        return

    existants = charger_mots_db()
    if any(normaliser(m).strip() == n for m in existants):
        await interaction.response.send_message("This word is already in the list.", ephemeral=True)
        return

    existants.append(mot)
    try:
        sauver_mots_db(existants)
    except Exception as e:
        await interaction.response.send_message(f"Storage error: {e}", ephemeral=True)
        return
    recharger_mots()
    await interaction.response.send_message(f"✅ `{mot}` added to the banned words.", ephemeral=True)


@tree.command(name="removemot", description="Remove a word added with /addmot or /importmots")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(mot="The word or phrase to remove")
async def removemot(interaction: discord.Interaction, mot: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    n = normaliser(mot).strip()
    existants = charger_mots_db()
    restants = [m for m in existants if normaliser(m).strip() != n]
    if len(restants) == len(existants):
        await interaction.response.send_message(
            "This word is not in the stored list (words from the built-in list or the repo file can't be removed here).",
            ephemeral=True,
        )
        return

    try:
        sauver_mots_db(restants)
    except Exception as e:
        await interaction.response.send_message(f"Storage error: {e}", ephemeral=True)
        return
    recharger_mots()
    await interaction.response.send_message(f"✅ `{mot}` removed.", ephemeral=True)


# Les liens ne sont PAS analysés par le filtre de mots : leurs identifiants / signatures (chiffres et
# lettres au hasard, que normaliser() transforme en lettres) formaient par hasard des mots interdits
# ("tit", "ass"...) et faisaient sanctionner des liens de GIF normaux (~8 % des liens cdn.discordapp.com).
REGEX_URLS_COMPLETES = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def contient_mot_interdit(texte):
    """Retourne (True, mot) si un mot interdit est trouvé, sinon (False, None).
    Détecte aussi les mots cachés au milieu d'une longue chaîne de lettres
    (ex: qfshyfveshvykysqefusef → kys).
    """
    texte = normaliser(REGEX_URLS_COMPLETES.sub(" ", texte))

    # 1. Match classique (mot isolé)
    for motif, mot in MOTIFS:
        if motif.search(texte):
            return True, mot

    # 2. Mots cachés dans de longues chaînes sans espace (anti-obfuscation)
    #    On ne cherche que dans les tokens d'au moins 8 caractères
    #    pour limiter les faux positifs (ex: "class" ne match pas "ass")
    for token in re.findall(r"[a-z0-9]{8,}", texte):
        for mot in MOTS_NORM:
            if len(mot) >= 3 and mot in token:
                return True, mot

    return False, None


# ---------- ARRIVÉES / DÉPARTS ----------
bannis = set()
expulses = set()
leak_bans = set()  # (guild_id, user_id) bannis pour leak de fichier premium
sanction_cmd = set()  # (guild_id, user_id) sanctionnés via /ban /kick : pas de message auto


def ordinal(n):
    if 10 <= n % 100 <= 20:
        suffixe = "th"
    else:
        suffixe = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffixe}"


def est_serveur_principal(guild) -> bool:
    """True si ce serveur est celui où les IDs en dur (salons, rôles...) sont valables."""
    if guild is None:
        return False
    if GUILD_PRINCIPAL_ID:
        return guild.id == GUILD_PRINCIPAL_ID
    return guild.get_channel(CHANNEL_ID) is not None


_script_relais_a_lancer = None  # chemin de relay_notifyme.py si le bot doit changer de script


async def verifier_serveur_ou_relais() -> bool:
    """Appelé au démarrage. Retourne True si le bot N'EST PAS dans le serveur principal :
    il se déconnecte alors et principal() lance relay_notifyme.py à sa place."""
    global _script_relais_a_lancer
    if _script_relais_a_lancer:
        return True
    if any(g.unavailable for g in client.guilds):
        # Panne Discord : on ne bascule surtout pas en mode relais par erreur
        print("[SERVEUR] Un serveur est indisponible (panne Discord ?) : vérification repoussée.")
        return False
    if any(est_serveur_principal(g) for g in client.guilds):
        print("[SERVEUR] Serveur principal trouvé : le bot démarre normalement.")
        return False
    chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCRIPT_RELAIS)
    if not os.path.exists(chemin):
        print(f"[SERVEUR] Pas dans le serveur principal, mais {SCRIPT_RELAIS} est introuvable ({chemin}) : "
              f"le bot continue normalement.")
        return False
    print(f"[SERVEUR] Le bot n'est pas dans le serveur principal : passage sur {SCRIPT_RELAIS}.")
    _script_relais_a_lancer = chemin
    await client.close()  # principal() lance le script relais juste après
    return True


def lancer_script_relais(chemin):
    import subprocess
    import sys
    print(f"[SERVEUR] Lancement de {os.path.basename(chemin)}...", flush=True)
    if os.name == "nt":  # Windows : os.execv se comporte mal, on lance le script et on attend
        subprocess.call([sys.executable, chemin])
        raise SystemExit(0)
    os.execv(sys.executable, [sys.executable, chemin])  # remplace ce processus (Render reste content)


async def get_salon(guild):
    if not est_serveur_principal(guild):
        return None  # sinon fetch_channel irait chercher le salon du serveur principal depuis un autre serveur
    salon = guild.get_channel(CHANNEL_ID)
    if salon is None:
        try:
            salon = await guild.fetch_channel(CHANNEL_ID)
        except Exception as e:
            print(f"[ERREUR] Salon introuvable (mauvais ID ?) : {e}")
    return salon


async def envoyer(guild, texte):
    salon = await get_salon(guild)
    if salon is None:
        return
    try:
        await salon.send(texte)
    except discord.Forbidden:
        print("[ERREUR] Le bot n'a pas la permission d'écrire dans ce salon.")
    except Exception as e:
        print(f"[ERREUR] Envoi impossible : {e}")


COMMANDES = []  # commandes slash du bot, mémorisées au premier démarrage
nettoyage_fait = False


FICHIER_SYNC = "commandes_sync.json"   # mémorise ce qui a déjà été synchronisé (évite les 429 au redémarrage)
_etat_sync = {}
_hash_cmd = None


def empreinte_commandes():
    """Empreinte des commandes slash : si elle ne change pas, inutile de les renvoyer à Discord.
    En cas de problème de calcul, on renvoie une valeur unique : le bot resynchronise (comportement sûr)."""
    try:
        return _calculer_empreinte()
    except Exception as e:
        print(f"[ATTENTION] Empreinte des commandes impossible ({e}) : synchronisation complète.")
        return f"erreur-{time.time()}"


def _calculer_empreinte():
    items = []
    for c in sorted(COMMANDES, key=lambda c: c.name):
        params = []
        for p in getattr(c, "parameters", []):
            choix = [(ch.name, str(ch.value)) for ch in (getattr(p, "choices", None) or [])]
            params.append((p.name, p.description, bool(p.required), str(getattr(p, "type", "")), choix))
        items.append((c.name, c.description, str(getattr(c, "default_permissions", "")),
                      bool(getattr(c, "guild_only", False)), params))
    return hashlib.sha256(json.dumps(items, default=str, sort_keys=True).encode()).hexdigest()


def lire_etat_sync():
    try:
        etat = storage.charger(FICHIER_SYNC)
    except Exception:
        return {}
    return etat if isinstance(etat, dict) else {}


async def synchroniser(guild):
    """Enregistre les commandes slash uniquement sur ce serveur (pas de doublons).
    Ne contacte Discord que si les commandes ont changé ou si le serveur est nouveau :
    re-synchroniser à chaque démarrage / reconnexion provoque des erreurs 429."""
    global _hash_cmd
    try:
        for c in COMMANDES:
            tree.add_command(c, guild=guild, override=True)

        if _hash_cmd is None:
            _hash_cmd = empreinte_commandes()
        deja = (
            os.getenv("FORCE_SYNC") != "1"
            and _etat_sync.get("hash") == _hash_cmd
            and guild.id in _etat_sync.get("guilds", [])
        )
        if deja:
            print(f"[INFO] Commandes inchangées sur {guild.name} : synchronisation ignorée.")
            return

        commandes = await tree.sync(guild=guild)
        print(f"[INFO] {len(commandes)} commande(s) slash enregistrée(s) sur {guild.name}")
        _etat_sync["hash"] = _hash_cmd
        _etat_sync.setdefault("guilds", [])
        if guild.id not in _etat_sync["guilds"]:
            _etat_sync["guilds"].append(guild.id)
        try:
            storage.sauver(FICHIER_SYNC, _etat_sync)
        except Exception as e:
            print(f"[ATTENTION] État de synchronisation non sauvegardé : {e}")
    except discord.Forbidden:
        print(
            f"[ERREUR] Accès refusé sur {guild.name} : réinvite le bot avec la case "
            f"'applications.commands' cochée."
        )
    except Exception as e:
        print(f"[ERREUR] Synchronisation impossible sur {guild.name} : {e}")


@client.event
async def on_ready():
    global nettoyage_fait, honey_view_registered, regles_view_registered, _hash_cmd
    print(f"Connecté en tant que {client.user} | {len(MOTIFS)} mots interdits chargés")
    if await verifier_serveur_ou_relais():
        return  # pas dans le bon serveur : principal() lance relay_notifyme.py
    await client.change_presence(status=discord.Status.online, activity=discord.CustomActivity(name=STATUT))
    if not honey_view_registered:
        client.add_view(HoneypotCounterView())
        honey_view_registered = True
    if not regles_view_registered:
        client.add_view(RulesView())
        regles_view_registered = True
    if not nettoyage_fait:
        nettoyage_fait = True
        COMMANDES.extend(tree.get_commands())
        # Retire les commandes globales localement (elles créaient le doublon)
        tree.clear_commands(guild=None)
        _hash_cmd = empreinte_commandes()
        _etat_sync.update(lire_etat_sync())
        if os.getenv("FORCE_SYNC") == "1" or _etat_sync.get("hash") != _hash_cmd:
            # Les commandes ont changé (ou premier démarrage) : on repart de zéro, une seule fois.
            _etat_sync.clear()
            try:
                await tree.sync()
                print("[INFO] Anciennes commandes globales supprimées (anti-doublon)")
            except Exception as e:
                print(f"[ERREUR] Nettoyage des commandes globales impossible : {e}")
    for guild in client.guilds:
        await synchroniser(guild)
    # Snapshots anti-nuke pour les serveurs déjà activés
    for g in client.guilds:
        if antinuke_actif(g.id):
            try:
                prendre_snapshot_guild(g)
            except Exception as e:
                print(f"[ANTINUKE] Snapshot initial échoué sur {g.name}: {e}")

    print(f"[INFO] Prêt : {len(COMMANDES)} commande(s) en mémoire, {len(client.guilds)} serveur(s).")

    # Vocaux privés : nettoie les salons vides orphelins et réassocie les salons actifs
    await reconstruire_tous_vocaux()


@client.event
async def on_guild_join(guild):
    await synchroniser(guild)


# ---------- ANTI-RAID ----------
RAID_JOINS_MAX = 5              # 5+ joins...
RAID_FENETRE = 10               # ...en moins de 10 secondes
RAID_DUREE = timedelta(minutes=10)  # mode raid actif pendant 10 min
RAID_MUTE_NOUVEAUX = timedelta(hours=1)
RAID_ANNOUNCE_CHANNEL_ID = 1425505497049727119  # salon annonce anti-raid mis à jour[cite: 2]

# guild_id → deque de timestamps de join
_raid_joins = defaultdict(deque)
# guild_id → timestamp fin du mode raid (time.time())
_raid_mode_until = {}
# sauvegarde du niveau de vérif avant le raid
_raid_old_verification = {}


def raid_actif(guild_id: int) -> bool:
    until = _raid_mode_until.get(guild_id)
    if until is None:
        return False
    if time.time() >= until:
        _raid_mode_until.pop(guild_id, None)
        return False
    return True


async def activer_raid_mode(guild: discord.Guild, nb_joins: int):
    """Active le mode anti-raid : mute les nouveaux + monte la vérification."""
    _raid_mode_until[guild.id] = time.time() + RAID_DUREE.total_seconds()

    # Monte le niveau de vérification au maximum (si possible)
    try:
        _raid_old_verification[guild.id] = guild.verification_level
        if guild.verification_level != discord.VerificationLevel.highest:
            await guild.edit(
                verification_level=discord.VerificationLevel.highest,
                reason="Anti-raid: lockdown",
            )
    except discord.Forbidden:
        print("[RAID] Pas la permission de changer le niveau de vérification")
    except discord.HTTPException as e:
        print(f"[RAID] Erreur verification_level : {e}")

    # Pause les invitations existantes (supprime les invites non-vanity si possible)
    try:
        invites = await guild.invites()
        for inv in invites:
            try:
                await inv.delete(reason="Anti-raid: temporary invite lockdown")
            except discord.HTTPException:
                pass
    except discord.Forbidden:
        print("[RAID] Pas la permission de gérer les invitations")
    except discord.HTTPException as e:
        print(f"[RAID] Erreur invites : {e}")

    fin_ts = int(_raid_mode_until[guild.id])
    await envoyer_log(
        guild,
        "🚨 ANTI-RAID ACTIVATED",
        f"**Trigger:** {nb_joins}+ joins in {RAID_FENETRE}s\n"
        f"**Duration:** {int(RAID_DUREE.total_seconds() // 60)} minutes\n"
        f"**Ends at:** <t:{fin_ts}:f> (<t:{fin_ts}:R>)\n"
        f"**Actions:**\n"
        f"• New members will be muted ({int(RAID_MUTE_NOUVEAUX.total_seconds() // 60)} min)\n"
        f"• Verification level set to highest\n"
        f"• Existing invites deleted\n"
        f"Use `/raidunlock` to disable early.",
        discord.Color.red(),
    )

    # Annonce publique
    announce = guild.get_channel(RAID_ANNOUNCE_CHANNEL_ID)
    if announce is None and est_serveur_principal(guild):
        try:
            announce = await guild.fetch_channel(RAID_ANNOUNCE_CHANNEL_ID)
        except Exception:
            announce = None
    if announce is not None:
        try:
            await announce.send(
                f"Lots of people have joined the server so we are in raid mod till <t:{fin_ts}:f> "
                f"(<t:{fin_ts}:R>). If someone joins he'll be muted for 1 hours.\n"
                f"@everyone",
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
        except Exception as e:
            print(f"[RAID] Impossible d'envoyer l'annonce : {e}")


async def desactiver_raid_mode(guild: discord.Guild, manuel: bool = False):
    _raid_mode_until.pop(guild.id, None)
    _raid_joins[guild.id].clear()

    # Restaure le niveau de vérification
    old = _raid_old_verification.pop(guild.id, None)
    if old is not None:
        try:
            await guild.edit(
                verification_level=old,
                reason="Anti-raid: lockdown ended",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    await envoyer_log(
        guild,
        "✅ ANTI-RAID DISABLED",
        f"**Mode:** {'Manual (/raidunlock)' if manuel else 'Automatic (timer expired)'}",
        discord.Color.green(),
    )


@tasks.loop(seconds=30)
async def verif_raid():
    maintenant = time.time()
    for guild_id, until in list(_raid_mode_until.items()):
        if maintenant >= until:
            guild = client.get_guild(guild_id)
            if guild is not None:
                await desactiver_raid_mode(guild, manuel=False)
            else:
                _raid_mode_until.pop(guild_id, None)
                _raid_old_verification.pop(guild_id, None)


@verif_raid.before_loop
async def avant_verif_raid():
    await client.wait_until_ready()


@tree.command(
    name="raidunlock",
    description="Disable anti-raid mode early",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def raidunlock(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    if not raid_actif(interaction.guild.id):
        await interaction.response.send_message(
            "Anti-raid mode is not active.", ephemeral=True
        )
        return

    await desactiver_raid_mode(interaction.guild, manuel=True)
    await interaction.response.send_message(
        "✅ Anti-raid mode disabled.", ephemeral=True
    )


@tree.command(
    name="raidstatus",
    description="Check if anti-raid mode is active",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def raidstatus(interaction: discord.Interaction):
    gid = interaction.guild.id
    if raid_actif(gid):
        restant = int(_raid_mode_until[gid] - time.time())
        await interaction.response.send_message(
            f"🚨 Anti-raid is **active** — {restant // 60}m {restant % 60}s remaining.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "✅ Anti-raid is **not** active.",
            ephemeral=True,
        )


@client.event
async def on_member_join(member):
    guild = member.guild
    now = time.time()
    dsstat_evenement(guild.id, "joins", member)  # rapport quotidien

    # Rôle automatique à l'arrivée (retiré quand la personne clique sur "Accept rules")
    if not member.bot:
        await donner_role_arrivee(member)

    # Expire le mode raid si le timer est fini
    until = _raid_mode_until.get(guild.id)
    if until is not None and now >= until:
        await desactiver_raid_mode(guild, manuel=False)

    # Enregistre le join
    q = _raid_joins[guild.id]
    q.append(now)
    # Garde seulement la fenêtre récente
    while q and now - q[0] > RAID_FENETRE:
        q.popleft()

    # Déclenche le mode raid si seuil atteint
    if len(q) >= RAID_JOINS_MAX and not raid_actif(guild.id):
        await activer_raid_mode(guild, len(q))

    # Si mode raid actif → mute le nouveau
    if raid_actif(guild.id):
        try:
            await member.timeout(
                RAID_MUTE_NOUVEAUX,
                reason="Anti-raid: automatic mute for new joins",
            )
            await envoyer_log(
                guild,
                "🚨 New member muted (anti-raid)",
                f"**User:** {member.mention} (`{member.id}`)\n"
                f"**Reason:** Joined during anti-raid lockdown\n"
                f"**Duration:** {int(RAID_MUTE_NOUVEAUX.total_seconds() // 60)} minutes",
                discord.Color.orange(),
            )
        except discord.Forbidden:
            print(f"[RAID] Impossible de mute {member}")
        except discord.HTTPException as e:
            print(f"[RAID] Timeout impossible : {e}")
        return  # pas de message de bienvenue pendant un raid

    n = guild.member_count
    await envoyer(
        guild,
        f"Yoo supp {member.mention} ! You're the {ordinal(n)}, "
        f"welcome to {guild.name} 🔥",
    )


# ---------- RÔLE À L'ARRIVÉE, /embed ET BOUTON "ACCEPT RULES" ----------
ROLE_ARRIVEE_ID = 1420131845202509864   # donné automatiquement à l'arrivée
ROLE_REGLES_ID = 1397316935103942696    # donné quand la personne clique sur "Accept rules"
COULEUR_EMBED = discord.Color.from_rgb(155, 89, 182)  # violet

regles_view_registered = False


async def donner_role_arrivee(member: discord.Member):
    role = member.guild.get_role(ROLE_ARRIVEE_ID)
    if role is None:
        print(f"[ARRIVÉE] Rôle {ROLE_ARRIVEE_ID} introuvable sur {member.guild.name}.")
        return
    try:
        await member.add_roles(role, reason="Automatic role on join")
    except discord.Forbidden:
        print(
            f"[ARRIVÉE] Impossible de donner le rôle à {member} : le rôle du bot doit être "
            f"AU-DESSUS de « {role.name} » et avoir la permission Gérer les rôles."
        )
    except discord.HTTPException as e:
        print(f"[ARRIVÉE] Erreur en donnant le rôle à {member} : {e}")


class RulesView(discord.ui.View):
    """Bouton vert persistant : retire le rôle d'arrivée et donne le rôle 'règles acceptées'."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accept rules", style=discord.ButtonStyle.success, custom_id="accept_rules")
    async def accept_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            await interaction.followup.send("This only works inside the server.", ephemeral=True)
            return

        role_arrivee = guild.get_role(ROLE_ARRIVEE_ID)
        role_regles = guild.get_role(ROLE_REGLES_ID)
        if role_regles is None:
            await interaction.followup.send("The rules role no longer exists. Tell an admin.", ephemeral=True)
            return

        if role_regles in member.roles and (role_arrivee is None or role_arrivee not in member.roles):
            await interaction.followup.send("✅ You already accepted the rules.", ephemeral=True)
            return

        try:
            if role_arrivee is not None and role_arrivee in member.roles:
                await member.remove_roles(role_arrivee, reason="Accepted the rules")
            if role_regles not in member.roles:
                await member.add_roles(role_regles, reason="Accepted the rules")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I can't change your roles. An admin must put my role above these roles.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Discord error: {e}", ephemeral=True)
            return

        await interaction.followup.send("✅ Rules accepted, welcome!", ephemeral=True)


def _url_ok(texte: str) -> bool:
    return texte.startswith(("http://", "https://"))


SEPARATEUR = "━" * 20


def construire_embed(guild, titre, texte, footer=None, image=None, thumbnail=None):
    """Embed violet 'soigné' : grand titre + séparateur, auteur (serveur), vignette, pied de page, date."""
    description = f"# {titre}\n{SEPARATEUR}\n\n{texte}" if titre else texte
    embed = discord.Embed(description=description, color=COULEUR_EMBED, timestamp=discord.utils.utcnow())
    icone = guild.icon.url if guild and guild.icon else None
    if guild:
        embed.set_author(name=guild.name, icon_url=icone)
    miniature = thumbnail or icone
    if miniature:
        embed.set_thumbnail(url=miniature)
    if image:
        embed.set_image(url=image)
    pied = footer or (guild.name if guild else None)
    if pied:
        embed.set_footer(text=pied, icon_url=icone)
    return embed


class EmbedModal(discord.ui.Modal, title="Create an embed"):
    titre = discord.ui.TextInput(label="Title", required=False, max_length=256)
    description = discord.ui.TextInput(
        label="Description", style=discord.TextStyle.paragraph, required=True, max_length=4000
    )
    footer = discord.ui.TextInput(label="Footer (optional)", required=False, max_length=2048)
    image = discord.ui.TextInput(label="Big image URL (optional)", required=False, max_length=500)
    thumbnail = discord.ui.TextInput(label="Small image URL (optional)", required=False, max_length=500)

    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        image = self.image.value.strip()
        miniature = self.thumbnail.value.strip()
        for url in (image, miniature):
            if url and not _url_ok(url):
                await interaction.response.send_message(
                    f"❌ `{url}` is not a valid link (it must start with https://).", ephemeral=True
                )
                return

        embed = construire_embed(
            interaction.guild,
            self.titre.value.strip(),
            self.description.value,
            footer=self.footer.value.strip() or None,
            image=image or None,
            thumbnail=miniature or None,
        )

        try:
            await self.channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ I can't send messages in {self.channel.mention}.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Discord error: {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Embed sent in {self.channel.mention}.", ephemeral=True)


@tree.command(name="embed", description="Send a purple embed message (opens a form)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Where to send it (default: this channel)")
async def embed_cmd(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    await interaction.response.send_modal(EmbedModal(channel or interaction.channel))


@tree.command(name="button", description="Post the green 'Accept rules' button")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    channel="Where to post it (default: this channel)",
    message_id="Optional: ID of a message from me (e.g. a /embed) to attach the button to",
)
async def button_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None,
    message_id: str = None,
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    cible = channel or interaction.channel
    await interaction.response.defer(ephemeral=True)
    try:
        if message_id:
            try:
                msg = await cible.fetch_message(int(message_id.strip()))
            except (ValueError, discord.NotFound):
                await interaction.followup.send("❌ Message not found in that channel.", ephemeral=True)
                return
            if msg.author.id != client.user.id:
                await interaction.followup.send(
                    "❌ I can only add the button to my own messages (send it with /embed first).",
                    ephemeral=True,
                )
                return
            await msg.edit(view=RulesView())
        else:
            embed = construire_embed(
                interaction.guild,
                "📜 Rules",
                "Click the button below to accept the rules and get access to the server.",
            )
            await cible.send(embed=embed, view=RulesView())
    except discord.Forbidden:
        await interaction.followup.send(f"❌ I can't send messages in {cible.mention}.", ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Discord error: {e}", ephemeral=True)
        return
    await interaction.followup.send(f"✅ Button posted in {cible.mention}.", ephemeral=True)


# ---------- ALERTES ABONNÉS TIKTOK (paliers) ----------
# Les comptes suivis et leur message se règlent avec les commandes /tiktokadd, /tiktokremove, /tiktokcheck.
# Paliers : tous les 1000 jusqu'à 10k, tous les 5000 de 10k à 50k, tous les 10000 à partir de 50k.
TIKTOK_INTERVALLE_MIN = 15           # fréquence des vérifications
FICHIER_TIKTOK = "tiktok_paliers.json"          # derniers paliers annoncés
FICHIER_TIKTOK_COMPTES = "tiktok_comptes.json"  # comptes suivis + messages (MongoDB)
MESSAGE_TIKTOK_DEFAUT = "Yoo @everyone {user} has just reached {count} followers tysm all"
COMPTES_TIKTOK_DEFAUT = {
    "911_editzs": {"username": "911_editzs", "message": MESSAGE_TIKTOK_DEFAUT, "ping_id": None},
    "traxmilrz": {"username": "traxmilrz", "message": MESSAGE_TIKTOK_DEFAUT, "ping_id": None},
}


def calculer_palier(n: int) -> int:
    """Palier atteint pour n abonnés : 1000 par 1000 (<10k), 5000 par 5000 (10k-50k), 10000 par 10000 (50k+)."""
    if n < 10_000:
        pas = 1000
    elif n < 50_000:
        pas = 5000
    else:
        pas = 10_000
    return (n // pas) * pas


def formater_palier(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.3f}".rstrip("0").rstrip(".") + "M"
    return f"{n // 1000}k"


def charger_comptes_tiktok():
    try:
        data = storage.charger(FICHIER_TIKTOK_COMPTES)
    except Exception as e:
        print(f"[TIKTOK] Lecture des comptes impossible : {e}")
        data = None
    if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
        return data["accounts"]
    return {k: dict(v) for k, v in COMPTES_TIKTOK_DEFAUT.items()}


def sauver_comptes_tiktok(comptes):
    storage.sauver(FICHIER_TIKTOK_COMPTES, {"accounts": comptes})


def _charger_paliers():
    try:
        d = storage.charger(FICHIER_TIKTOK)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def texte_alerte(compte, palier):
    nom = f"<@{compte['ping_id']}>" if compte.get("ping_id") else f"**{compte['username']}**"
    modele = compte.get("message") or MESSAGE_TIKTOK_DEFAUT
    return modele.replace("{user}", nom).replace("{count}", f"**{formater_palier(palier)}**")


async def abonnes_via_livecounts(session, pseudo):
    """Récupère le nombre d'abonnés via l'API utilisée par livecounts.io (tokcounter).
    Plus fiable que le scrape direct de tiktok.com (souvent bloqué)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
        "Origin": "https://livecounts.io",
        "Referer": "https://livecounts.io/",
    }
    timeout = aiohttp.ClientTimeout(total=15)

    # 1) Recherche du userId à partir du username
    url_search = f"https://tiktok-api.tokcounter.com/user/search/{pseudo}"
    async with session.get(url_search, headers=headers, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"search HTTP {r.status}")
        data = await r.json()
    if not data.get("success"):
        raise RuntimeError(f"search failed: {data}")
    users = data.get("userData") or []
    if not users:
        raise RuntimeError("aucun résultat pour ce username")

    # Prend le premier résultat dont le username match (insensible à la casse), sinon le 1er
    choisi = None
    pseudo_l = pseudo.lower().lstrip("@")
    for u in users:
        # certains résultats ont "id" = handle, d'autres "username" = display name
        handle = str(u.get("id") or "").lower()
        if handle == pseudo_l:
            choisi = u
            break
    if choisi is None:
        choisi = users[0]

    user_id = choisi.get("userId") or choisi.get("id")
    if not user_id:
        raise RuntimeError("userId manquant dans la réponse search")

    # Si le search renvoie déjà les stats, on les utilise
    stats = choisi.get("stats") or {}
    if stats.get("followers") is not None:
        return int(stats["followers"])

    # 2) Sinon, appel stats
    url_stats = f"https://tiktok-api.tokcounter.com/user/stats/{user_id}"
    async with session.get(url_stats, headers=headers, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"stats HTTP {r.status}")
        data = await r.json()
    if not data.get("success"):
        raise RuntimeError(f"stats failed: {data}")
    if data.get("followerCount") is None:
        raise RuntimeError("followerCount manquant")
    return int(data["followerCount"])


async def abonnes_direct(session, pseudo):
    """Fallback : lit le nombre d'abonnés sur la page publique TikTok (souvent bloqué)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with session.get(f"https://www.tiktok.com/@{pseudo}", headers=headers,
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        html = await r.text()
    m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("page sans données (probablement bloquée)")
    data = json.loads(m.group(1))
    info = data["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]
    stats = info.get("statsV2") or info.get("stats")
    return int(stats["followerCount"])


async def recuperer_abonnes(pseudos):
    """Retourne {pseudo: nombre}. Essaie livecounts/tokcounter puis scrape TikTok en secours."""
    comptes = {}
    async with aiohttp.ClientSession() as session:
        for pseudo in pseudos:
            try:
                comptes[pseudo] = await abonnes_via_livecounts(session, pseudo)
                print(f"[TIKTOK] @{pseudo} : {comptes[pseudo]:,} (via livecounts/tokcounter)")
            except Exception as e1:
                print(f"[TIKTOK] livecounts échoué pour @{pseudo} : {e1} — essai scrape direct…")
                try:
                    comptes[pseudo] = await abonnes_direct(session, pseudo)
                    print(f"[TIKTOK] @{pseudo} : {comptes[pseudo]:,} (via scrape tiktok.com)")
                except Exception as e2:
                    print(f"[TIKTOK] Lecture impossible pour @{pseudo} : {e2}")
    return comptes


async def _envoyer_alerte_tiktok(compte, palier):
    canal = client.get_channel(RAID_ANNOUNCE_CHANNEL_ID)
    if canal is None:
        try:
            canal = await client.fetch_channel(RAID_ANNOUNCE_CHANNEL_ID)
        except Exception as e:
            print(f"[TIKTOK] Salon d'alerte introuvable : {e}")
            return False
    try:
        await canal.send(
            texte_alerte(compte, palier)[:2000],
            allowed_mentions=discord.AllowedMentions(everyone=True, users=True),
        )
        return True
    except discord.Forbidden:
        print("[TIKTOK] Pas la permission d'écrire / de mentionner @everyone dans le salon d'alerte.")
    except discord.HTTPException as e:
        print(f"[TIKTOK] Erreur d'envoi : {e}")
    return False


@tasks.loop(minutes=TIKTOK_INTERVALLE_MIN)
async def verif_tiktok():
    config = charger_comptes_tiktok()
    if not config:
        return
    lus = await recuperer_abonnes([c["username"] for c in config.values()])
    if not lus:
        return

    etat = _charger_paliers()
    modifie = False
    for cle, compte in config.items():
        nombre = lus.get(compte["username"])
        if nombre is None:
            continue
        palier = calculer_palier(nombre)
        precedent = etat.get(cle, {}).get("palier")
        if precedent is None:
            # Première observation : on mémorise sans alerter
            etat[cle] = {"palier": palier}
            modifie = True
            print(f"[TIKTOK] @{compte['username']} : {nombre} abonnés (palier de départ {palier}).")
        elif palier > precedent:
            if await _envoyer_alerte_tiktok(compte, palier):
                etat[cle] = {"palier": palier}  # le palier ne redescend jamais : pas de double alerte
                modifie = True
    if modifie:
        try:
            storage.sauver(FICHIER_TIKTOK, etat)
        except Exception as e:
            print(f"[TIKTOK] Sauvegarde impossible : {e}")


@verif_tiktok.before_loop
async def _avant_verif_tiktok():
    await client.wait_until_ready()


@tree.command(name="tiktokadd", description="Track a TikTok account and set the alert message")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    username="TikTok username (without @)",
    message="Alert text. Use {user} for the name/ping and {count} for the number. Optional.",
    ping="Discord member to ping instead of the TikTok name. Optional.",
)
async def tiktokadd(
    interaction: discord.Interaction,
    username: str,
    message: str = None,
    ping: discord.Member = None,
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    pseudo = username.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{2,24}", pseudo):
        await interaction.response.send_message(
            "❌ Invalid TikTok username (letters, numbers, `.` and `_` only).", ephemeral=True
        )
        return
    if message is not None and len(message) > 1500:
        await interaction.response.send_message("❌ Message too long (1500 characters max).", ephemeral=True)
        return

    comptes = charger_comptes_tiktok()
    cle = pseudo.lower()
    ancien = comptes.get(cle, {})
    compte = {
        "username": pseudo,
        "message": message if message is not None else ancien.get("message", MESSAGE_TIKTOK_DEFAUT),
        "ping_id": ping.id if ping is not None else ancien.get("ping_id"),
    }
    comptes[cle] = compte
    try:
        sauver_comptes_tiktok(comptes)
    except Exception as e:
        await interaction.response.send_message(f"❌ Storage error: {e}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"✅ **@{pseudo}** is tracked (every 1k below 10k, every 5k from 10k, every 10k from 50k).\n"
        f"Preview of the alert:\n>>> {texte_alerte(compte, 10000)}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="tiktokremove", description="Stop tracking a TikTok account")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(username="TikTok username (without @)")
async def tiktokremove(interaction: discord.Interaction, username: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    cle = username.strip().lstrip("@").lower()
    comptes = charger_comptes_tiktok()
    if cle not in comptes:
        await interaction.response.send_message("This account is not tracked.", ephemeral=True)
        return
    comptes.pop(cle)
    try:
        sauver_comptes_tiktok(comptes)
        etat = _charger_paliers()
        if etat.pop(cle, None) is not None:
            storage.sauver(FICHIER_TIKTOK, etat)
    except Exception as e:
        await interaction.response.send_message(f"❌ Storage error: {e}", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ **@{cle}** is no longer tracked.", ephemeral=True)


@tree.command(name="tiktokcheck", description="Show the tracked TikTok accounts and their current follower counts")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def tiktokcheck(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    config = charger_comptes_tiktok()
    if not config:
        await interaction.response.send_message("No TikTok account tracked. Use /tiktokadd.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    lus = await recuperer_abonnes([c["username"] for c in config.values()])
    etat = _charger_paliers()
    lignes = []
    for cle, compte in config.items():
        pseudo = compte["username"]
        ping = f", pings <@{compte['ping_id']}>" if compte.get("ping_id") else ""
        if pseudo in lus:
            palier = etat.get(cle, {}).get("palier")
            suivi = f", last alerted: {formater_palier(palier)}" if palier else ""
            lignes.append(f"**@{pseudo}** : {lus[pseudo]:,} followers{suivi}{ping}")
        else:
            lignes.append(f"**@{pseudo}** : ❌ could not be read (TikTok may be blocking the server){ping}")
    await interaction.followup.send(
        "\n".join(lignes), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


# ---------- ANNONCES DE NOUVELLES VIDÉOS (flux RSS / RSSHub) ----------
# Commandes : /follow (ajouter un lien RSS + salon d'annonce) et /stopfollow (arrêter).
# Toutes les 5 minutes le bot lit chaque flux suivi. Si une vidéo qu'il n'a jamais vue apparaît,
# il l'annonce avec son lien dans le salon choisi.
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, unquote

VIDEO_INTERVALLE_MIN = 5
VIDEO_MAX_ANNONCES = 3                  # max de vidéos annoncées d'un coup pour un flux (anti-flood)
VIDEO_AGE_MAX = timedelta(days=2)       # une vidéo "nouvelle" mais publiée il y a plus de 2 jours n'est pas annoncée
VIDEO_VUS_MAX = 200                     # nombre d'identifiants de vidéos mémorisés par flux
FICHIER_VIDEO_COMPTES = "video_comptes.json"   # flux suivis (MongoDB / fichier)
FICHIER_VIDEO_VUS = "video_vus.json"           # vidéos déjà vues / annoncées
MESSAGE_VIDEO_DEFAUT = "Yoo @everyone {user} just posted a new video!\n{link}"
VIDEO_VERROU = asyncio.Lock()


def _date_flux(texte):
    """Lit une date RSS (RFC 822) ou Atom (ISO 8601). Retourne un datetime UTC ou None."""
    if not texte:
        return None
    texte = texte.strip()
    d = None
    try:
        d = parsedate_to_datetime(texte)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(texte.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d is None:
        return None
    if d.tzinfo is None:
        from datetime import timezone
        d = d.replace(tzinfo=timezone.utc)
    return d


def parser_flux_videos(brut) -> list:
    """Lit un flux RSS 2.0 ou Atom. Retourne [{id, link, title, date}, ...] dans l'ordre du flux."""
    racine = ET.fromstring(brut)
    if racine.tag.split("}")[-1].lower() not in ("rss", "feed", "rdf"):
        raise ET.ParseError("not an RSS/Atom feed")
    videos = []
    for el in racine.iter():
        if not isinstance(el.tag, str) or el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        lien = titre = ident = date_txt = None
        for enfant in el:
            if not isinstance(enfant.tag, str):
                continue
            nom = enfant.tag.split("}")[-1]
            texte = (enfant.text or "").strip()
            if nom == "link":
                href = enfant.get("href")
                if href:
                    if lien is None or enfant.get("rel") in (None, "alternate"):
                        lien = href.strip()
                elif texte and lien is None:
                    lien = texte
            elif nom == "title":
                titre = texte
            elif nom in ("guid", "id"):
                ident = texte
            elif nom in ("pubDate", "published", "updated") and date_txt is None:
                date_txt = texte
        if not lien:
            continue
        videos.append({"id": ident or lien, "link": lien, "title": titre or "", "date": _date_flux(date_txt)})
    return videos


def videos_a_annoncer(videos: list, vus, maintenant):
    """Compare le flux aux vidéos déjà vues. Retourne (a_annoncer, a_ignorer), du plus ancien au plus récent.
    a_ignorer = nouvelles pour le bot mais trop vieilles ou en trop grand nombre : marquées vues sans annonce."""
    vus = set(vus)
    nouveaux = [v for v in videos if v["id"] not in vus]
    if all(v["date"] for v in nouveaux):
        nouveaux.sort(key=lambda v: v["date"])
    else:
        nouveaux.reverse()  # les flux listent la plus récente en premier
    a_annoncer, a_ignorer = [], []
    for v in nouveaux:
        if v["date"] and maintenant - v["date"] > VIDEO_AGE_MAX:
            a_ignorer.append(v)
        else:
            a_annoncer.append(v)
    if len(a_annoncer) > VIDEO_MAX_ANNONCES:
        a_ignorer += a_annoncer[:-VIDEO_MAX_ANNONCES]
        a_annoncer = a_annoncer[-VIDEO_MAX_ANNONCES:]
    return a_annoncer, a_ignorer


def texte_video(compte: dict, video: dict) -> str:
    modele = compte.get("message") or MESSAGE_VIDEO_DEFAUT
    titre = discord.utils.escape_mentions(video.get("title") or "")[:150]
    texte = (
        modele.replace("{user}", f"**{compte.get('name') or 'Someone'}**")
        .replace("{title}", titre)
        .replace("{link}", video["link"])
    )
    if video["link"] not in texte:
        texte += "\n" + video["link"]   # le lien est toujours présent
    return texte[:2000]


def cle_video(guild_id: int, url: str) -> str:
    """Clé courte et sûre (pas de point ni de slash : compatible MongoDB)."""
    return f"{guild_id}_{hashlib.sha1(url.strip().encode('utf-8')).hexdigest()[:12]}"


def nom_depuis_url(url: str) -> str:
    """Devine un nom d'affichage depuis le lien (ex: .../tiktok/user/@pseudo -> pseudo)."""
    p = urlparse(url)
    chemin = unquote(p.path)
    m = re.search(r"@([\w.\-]+)", chemin)
    if m:
        return m.group(1)
    morceaux = [s for s in chemin.split("/") if s]
    if morceaux:
        return morceaux[-1][:50]
    return (p.hostname or "feed")[:50]
# (fin des fonctions pures)


async def recuperer_flux_videos(session: aiohttp.ClientSession, url: str) -> list:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            # RSSHub explique souvent la raison dans la page d'erreur : on l'affiche
            try:
                corps = (await r.text())[:1500]
            except Exception:
                corps = ""
            corps = re.sub(r"<[^>]+>", " ", corps)
            corps = re.sub(r"\s+", " ", corps).strip()[:220]
            raise RuntimeError(f"the server answered HTTP {r.status}" + (f" — {corps}" if corps else ""))
        brut = await r.read()
    if len(brut) > 5 * 1024 * 1024:
        raise RuntimeError("feed too large")
    try:
        return parser_flux_videos(brut)
    except ET.ParseError as e:
        raise RuntimeError(f"this link is not a valid RSS feed ({e})")


def charger_comptes_video() -> dict:
    try:
        data = storage.charger(FICHIER_VIDEO_COMPTES)
    except Exception as e:
        print(f"[VIDEO] Lecture des flux impossible : {e}")
        return {}
    if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
        return data["accounts"]
    return {}


def charger_vus_video() -> dict:
    try:
        data = storage.charger(FICHIER_VIDEO_VUS)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def _envoyer_video(compte: dict, video: dict) -> bool:
    canal = client.get_channel(compte["channel_id"])
    if canal is None:
        try:
            canal = await client.fetch_channel(compte["channel_id"])
        except Exception as e:
            print(f"[VIDEO] Salon introuvable ({compte['channel_id']}) : {e}")
            return False
    try:
        await canal.send(
            texte_video(compte, video),
            allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=True),
        )
        return True
    except discord.Forbidden:
        print(f"[VIDEO] Pas la permission d'écrire dans #{getattr(canal, 'name', '?')}.")
    except discord.HTTPException as e:
        print(f"[VIDEO] Erreur d'envoi : {e}")
    return False


@tasks.loop(minutes=VIDEO_INTERVALLE_MIN)
async def verif_videos():
    try:
        async with VIDEO_VERROU:
            comptes = charger_comptes_video()
            if not comptes:
                return
            vus_tous = charger_vus_video()
            modifie = False
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for cle, compte in comptes.items():
                    try:
                        videos = await recuperer_flux_videos(session, compte["url"])
                    except Exception as e:
                        print(f"[VIDEO] {compte.get('name')} : lecture impossible ({e})")
                        continue
                    if not videos:
                        continue

                    vus = vus_tous.get(cle)
                    if vus is None:
                        # Première observation : on mémorise tout sans rien annoncer
                        vus_tous[cle] = [v["id"] for v in videos][:VIDEO_VUS_MAX]
                        modifie = True
                        print(f"[VIDEO] {compte['name']} : {len(videos)} vidéo(s) mémorisée(s), pas d'annonce.")
                        continue

                    a_annoncer, a_ignorer = videos_a_annoncer(videos, vus, discord.utils.utcnow())
                    nouveaux_ids = [v["id"] for v in a_ignorer]
                    for v in a_annoncer:
                        if await _envoyer_video(compte, v):
                            nouveaux_ids.append(v["id"])
                            print(f"[VIDEO] Annonce envoyée pour {compte['name']} : {v['link']}")
                    if nouveaux_ids:
                        vus_tous[cle] = (vus + nouveaux_ids)[-VIDEO_VUS_MAX:]
                        modifie = True
                    await asyncio.sleep(1)
            if modifie:
                storage.sauver(FICHIER_VIDEO_VUS, vus_tous)
    except Exception as e:  # la boucle ne doit jamais s'arrêter
        import traceback
        print(f"[VIDEO] Erreur dans la vérification : {e!r}")
        traceback.print_exc()


@verif_videos.before_loop
async def _avant_verif_videos():
    await client.wait_until_ready()


@tree.command(name="follow", description="Announce the new videos of an RSS feed in a channel (checked every 5 minutes)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    rss_link="The RSS link of the account (e.g. your RSSHub link)",
    channel="Channel where the announcement is sent",
    message="Announcement text. Use {user}, {title} and {link}. Optional.",
    name="Name shown in the announcement (default: guessed from the link). Optional.",
)
async def follow_cmd(
    interaction: discord.Interaction,
    rss_link: str,
    channel: discord.TextChannel,
    message: Optional[app_commands.Range[str, 1, 1500]] = None,
    name: Optional[app_commands.Range[str, 1, 50]] = None,
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    url = rss_link.strip().strip("<>")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        await interaction.response.send_message(
            "❌ Invalid link. It must start with `http://` or `https://`.", ephemeral=True
        )
        return
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages):
        await interaction.response.send_message(
            f"❌ I can't send messages in {channel.mention}. Give me **View Channel** and **Send Messages** there.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    # On vérifie que le lien est bien un flux lisible avant de l'enregistrer
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            videos = await recuperer_flux_videos(session, url)
    except Exception as e:
        await interaction.followup.send(
            f"❌ I couldn't read this feed: `{str(e)[:300]}`\nCheck the link and try again. Nothing was saved.",
            ephemeral=True,
        )
        return

    cle = cle_video(interaction.guild.id, url)
    async with VIDEO_VERROU:
        comptes = charger_comptes_video()
        ancien = comptes.get(cle, {})
        compte = {
            "guild_id": interaction.guild.id,
            "url": url,
            "name": name or ancien.get("name") or nom_depuis_url(url),
            "channel_id": channel.id,
            "message": message if message is not None else ancien.get("message"),
        }
        comptes[cle] = compte
        try:
            storage.sauver(FICHIER_VIDEO_COMPTES, {"accounts": comptes})
            vus = charger_vus_video()
            if cle not in vus and videos:
                # les vidéos déjà en ligne sont mémorisées : seules les prochaines seront annoncées
                vus[cle] = [v["id"] for v in videos][:VIDEO_VUS_MAX]
                storage.sauver(FICHIER_VIDEO_VUS, vus)
        except Exception as e:
            await interaction.followup.send(f"❌ Storage error: {e}", ephemeral=True)
            return

    if videos:
        etat = f"Feed OK: {len(videos)} existing video(s) noted (they won't be announced). Latest: <{videos[0]['link']}>"
    else:
        etat = "The feed works but is empty. The first video posted will be announced."
    apercu = texte_video(compte, {"link": "https://example.com/video", "title": "Video title"})
    await interaction.followup.send(
        f"✅ Now following **{compte['name']}** → {channel.mention} (checked every {VIDEO_INTERVALLE_MIN} min)\n"
        f"{etat}\nPreview of the announcement:\n>>> {apercu}"[:1990],
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="stopfollow", description="Stop following an RSS feed")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(feed="The feed to stop following (pick it in the list, or paste its link / name)")
async def stopfollow_cmd(interaction: discord.Interaction, feed: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    async with VIDEO_VERROU:
        comptes = charger_comptes_video()
        ici = {k: c for k, c in comptes.items() if c.get("guild_id") == interaction.guild.id}
        texte = feed.strip().strip("<>")
        cle = None
        if texte in ici:
            cle = texte
        else:
            trouves = [k for k, c in ici.items() if c.get("url") == texte]
            if not trouves:
                trouves = [k for k, c in ici.items() if (c.get("name") or "").lower() == texte.lower()]
            if len(trouves) == 1:
                cle = trouves[0]
            elif len(trouves) > 1:
                await interaction.response.send_message(
                    "Several feeds have this name. Please pick one from the list or paste its link.",
                    ephemeral=True,
                )
                return
        if cle is None:
            await interaction.response.send_message(
                "❌ This feed is not followed. Pick one from the list that appears when you type.", ephemeral=True
            )
            return
        nom = ici[cle].get("name")
        comptes.pop(cle)
        try:
            storage.sauver(FICHIER_VIDEO_COMPTES, {"accounts": comptes})
            vus = charger_vus_video()
            if vus.pop(cle, None) is not None:
                storage.sauver(FICHIER_VIDEO_VUS, vus)
        except Exception as e:
            await interaction.response.send_message(f"❌ Storage error: {e}", ephemeral=True)
            return
    await interaction.response.send_message(
        f"✅ No longer following **{nom}**.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


@stopfollow_cmd.autocomplete("feed")
async def stopfollow_autocomplete(interaction: discord.Interaction, current: str):
    saisie = current.lower()
    choix = []
    for cle, c in charger_comptes_video().items():
        if c.get("guild_id") != interaction.guild_id:
            continue
        libelle = f"{c.get('name')} — {c.get('url')}"[:100]
        if saisie in libelle.lower():
            choix.append(app_commands.Choice(name=libelle, value=cle))
    return choix[:25]



# ---------- RELAIS NOTIFYME (salon d'un autre serveur -> salon de ce serveur, aux couleurs du bot) ----------
# Principe : NotifyMe poste ses alertes TikTok dans un salon d'un serveur "relais" où le bot est aussi présent.
# Le bot lit ce message, reprend son contenu et le republie dans le salon choisi sur le serveur principal,
# dans un embed violet (COULEUR_EMBED) avec un bouton "Watch on TikTok". Commandes : /relay et /stoprelay.
FICHIER_RELAIS = "relais_notifyme.json"
RELAIS = None                        # {id du salon source (str): config}, chargé au premier usage
RELAIS_VUS = deque(maxlen=200)       # (salon source, lien) déjà relayés : évite les doublons
RE_LIEN_TIKTOK = re.compile(r"https?://(?:[\w-]+\.)?tiktok\.com/[^\s<>\"')\]]+", re.I)
RE_MENTIONS_RELAIS = re.compile(r"<@[!&]?\d+>|@everyone|@here")


def extraire_relais(message):
    """Lit un message (texte + embeds) et retourne (lien, titre, texte, image).
    Retourne None s'il n'y a aucun lien TikTok (le message n'est alors pas relayé)."""
    parties_recherche = [message.content or ""]
    parties_texte = [message.content or ""]
    titre = None
    image = None
    for e in message.embeds:
        parties_recherche += [e.url or "", e.title or "", e.description or ""]
        if e.description:
            parties_texte.append(e.description)
        for f in e.fields:
            parties_recherche.append(f"{f.name} {f.value}")
            parties_texte.append(f"**{f.name}** {f.value}")
        titre = titre or e.title
        if image is None:
            image = getattr(getattr(e, "image", None), "url", None) or getattr(getattr(e, "thumbnail", None), "url", None)
    m = RE_LIEN_TIKTOK.search("\n".join(parties_recherche))
    if not m:
        return None
    lien = m.group(0).rstrip(".,;!")
    texte = "\n".join(p for p in parties_texte if p)
    texte = RE_LIEN_TIKTOK.sub("", texte)              # le lien est dans le titre et le bouton
    texte = RE_MENTIONS_RELAIS.sub("", texte)          # pas de ping venant de l'autre serveur
    texte = re.sub(r"\(\s*\)|\[\s*\]", "", texte)   # parenthèses vides laissées par le lien retiré
    texte = re.sub(r"[ \t]+", " ", texte)
    texte = re.sub(r"\n{3,}", "\n\n", texte).strip()[:3800]
    return lien, (titre or "").strip()[:256], texte, image
# (fin des fonctions pures du relais)


def charger_relais() -> dict:
    global RELAIS
    if RELAIS is None:
        try:
            data = storage.charger(FICHIER_RELAIS)
        except Exception as e:
            print(f"[RELAIS] Lecture impossible : {e}")
            return {}
        RELAIS = data["routes"] if isinstance(data, dict) and isinstance(data.get("routes"), dict) else {}
    return RELAIS


def sauver_relais():
    storage.sauver(FICHIER_RELAIS, {"routes": RELAIS})


async def relais_notify(message: discord.Message):
    """Appelé pour chaque message de bot/webhook : si son salon est une source, on le republie."""
    try:
        if message.author.id == client.user.id:
            return
        route = charger_relais().get(str(message.channel.id))
        if not route:
            return
        extrait = extraire_relais(message)
        if not extrait:
            return
        lien, titre, texte, image = extrait
        cle = (message.channel.id, lien)
        if cle in RELAIS_VUS:
            return
        RELAIS_VUS.append(cle)

        canal = client.get_channel(route["dest_channel_id"])
        if canal is None:
            canal = await client.fetch_channel(route["dest_channel_id"])
        embed = discord.Embed(
            title=titre or "🎬 New TikTok video",
            url=lien,
            description=texte or None,
            color=COULEUR_EMBED,
            timestamp=discord.utils.utcnow(),
        )
        if image:
            embed.set_image(url=image)
        embed.set_footer(text=STATUT)
        vue = discord.ui.View()
        vue.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Watch on TikTok", url=lien))
        ping = f"<@&{route['ping_role_id']}>" if route.get("ping_role_id") else None
        await canal.send(
            content=ping,
            embed=embed,
            view=vue,
            allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False),
        )
        print(f"[RELAIS] Alerte relayée vers #{getattr(canal, 'name', '?')} : {lien}")
    except Exception as e:
        print(f"[RELAIS] Erreur : {e!r}")


@tree.command(name="relay", description="Repost the TikTok alerts of a channel from another server here, in the bot's colors")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    source_channel_id="ID of the channel where NotifyMe posts (in the other server, where I must also be)",
    destination="Channel of this server where the alert is reposted",
    ping="Role of this server to ping with the alert. Optional.",
)
async def relay_cmd(
    interaction: discord.Interaction,
    source_channel_id: str,
    destination: discord.TextChannel,
    ping: Optional[discord.Role] = None,
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    texte_id = source_channel_id.strip()
    if not re.fullmatch(r"\d{15,22}", texte_id):
        await interaction.response.send_message(
            "❌ Invalid ID. Enable Developer Mode in Discord, right-click the channel and choose **Copy Channel ID**.",
            ephemeral=True,
        )
        return
    source = client.get_channel(int(texte_id))
    if not isinstance(source, discord.TextChannel):
        await interaction.response.send_message(
            "❌ I can't see this channel. Make sure **I am in the other server** and can view that channel.",
            ephemeral=True,
        )
        return
    if source.id == destination.id:
        await interaction.response.send_message("❌ The source and the destination must be different channels.", ephemeral=True)
        return

    # Sécurité : il faut aussi être administrateur du serveur source (sinon on pourrait lire les salons des autres)
    guilde_src = source.guild
    membre = guilde_src.get_member(interaction.user.id)
    if membre is None:
        try:
            membre = await guilde_src.fetch_member(interaction.user.id)
        except discord.HTTPException:
            membre = None
    if membre is None or not (membre.guild_permissions.administrator or guilde_src.owner_id == membre.id):
        await interaction.response.send_message(
            "❌ You must also be an **administrator of the source server** to relay its messages.", ephemeral=True
        )
        return
    if not source.permissions_for(guilde_src.me).view_channel:
        await interaction.response.send_message(f"❌ I can't view **#{source.name}** in {guilde_src.name}.", ephemeral=True)
        return
    perms = destination.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        await interaction.response.send_message(
            f"❌ In {destination.mention} I need **View Channel**, **Send Messages** and **Embed Links**.", ephemeral=True
        )
        return

    routes = charger_relais()
    routes[str(source.id)] = {
        "source_channel_id": source.id,
        "source_guild_id": guilde_src.id,
        "dest_channel_id": destination.id,
        "dest_guild_id": interaction.guild.id,
        "ping_role_id": ping.id if ping else None,
    }
    try:
        sauver_relais()
    except Exception as e:
        await interaction.response.send_message(f"❌ Storage error: {e}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"✅ Every TikTok alert posted in **#{source.name}** ({guilde_src.name}) will be reposted in {destination.mention}"
        f"{' with ' + ping.mention if ping else ''}, in my colors.\n"
        "Use NotifyMe's *test notification* to try it. Only messages that contain a TikTok link are relayed.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="stoprelay", description="Stop reposting the alerts of a source channel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(relay="The relay to stop (pick it in the list, or paste the source channel ID)")
async def stoprelay_cmd(interaction: discord.Interaction, relay: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    routes = charger_relais()
    cle = relay.strip()
    route = routes.get(cle)
    if not route or route.get("dest_guild_id") != interaction.guild.id:
        await interaction.response.send_message(
            "❌ This relay doesn't exist. Pick one from the list that appears when you type.", ephemeral=True
        )
        return
    routes.pop(cle)
    try:
        sauver_relais()
    except Exception as e:
        await interaction.response.send_message(f"❌ Storage error: {e}", ephemeral=True)
        return
    await interaction.response.send_message("✅ Relay stopped.", ephemeral=True)


@stoprelay_cmd.autocomplete("relay")
async def stoprelay_autocomplete(interaction: discord.Interaction, current: str):
    saisie = current.lower()
    choix = []
    for cle, r in charger_relais().items():
        if r.get("dest_guild_id") != interaction.guild_id:
            continue
        src = client.get_channel(r["source_channel_id"])
        dst = client.get_channel(r["dest_channel_id"])
        nom_src = f"#{src.name} ({src.guild.name})" if src else f"channel {r['source_channel_id']}"
        nom_dst = f"#{dst.name}" if dst else f"channel {r['dest_channel_id']}"
        libelle = f"{nom_src} → {nom_dst}"[:100]
        if saisie in libelle.lower():
            choix.append(app_commands.Choice(name=libelle, value=cle))
    return choix[:25]


@client.event
async def on_member_ban(guild, user):
    if (guild.id, user.id) in honey_softban or (guild.id, user.id) in sanction_cmd:
        return  # ban du honeypot ou de /ban : pas de message auto
    bannis.add((guild.id, user.id))


@client.event
async def on_audit_log_entry_create(entry):
    dsstat_audit(entry)  # rapport quotidien : kicks / bans / timeouts
    if entry.action == discord.AuditLogAction.kick and entry.target:
        expulses.add((entry.guild.id, entry.target.id))

    # --- Anti-nuke ---
    try:
        if entry.action == discord.AuditLogAction.channel_delete:
            await antinuke_traiter_channel_delete(entry)
        elif entry.action == discord.AuditLogAction.role_update:
            await antinuke_traiter_role_update(entry)
        elif entry.action == discord.AuditLogAction.member_role_update:
            await antinuke_traiter_member_role_update(entry)
    except Exception as e:
        print(f"[ANTINUKE] Erreur traitement audit : {e}")


@client.event
async def on_member_remove(member):
    guild = member.guild
    cle = (guild.id, member.id)
    nom = member.name

    dsstat_evenement(guild.id, "removes", member)  # rapport quotidien

    if cle in honey_softban or cle in sanction_cmd:
        return  # honeypot ou /ban /kick : pas de message de départ auto

    await asyncio.sleep(2)

    if cle in leak_bans:
        leak_bans.discard(cle)
        texte = f"{member.mention} has been banned cuz he tried to leak premium asset"
    elif cle in bannis:
        bannis.discard(cle)
        texte = (
            f"{nom} has been banned from the server due to bad behavior. "
            f"We do not wish to see them back 🔨👋"
        )
    elif cle in expulses:
        expulses.discard(cle)
        texte = (
            f"{nom} has been kicked from the server due to misbehavior, "
            f"we hope to see them back soon 🥾👋"
        )
    else:
        texte = f"{nom} left the server, we hope to see them again soon 👋"

    texte += f"\nWe are now **{guild.member_count}** members."
    await envoyer(guild, texte)


# ---------- FILTRE D'INVITATIONS ----------
REGEX_INVITATION = re.compile(
    r"(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me|discord\.io|dsc\.gg|invite\.gg)/[a-z0-9-]+",
    re.IGNORECASE,
)


def contient_invitation_discord(texte):
    return bool(REGEX_INVITATION.search(texte))


# ---------- ANTI MUR DE TEXTE ----------
def est_mur_de_texte(contenus):
    """Vrai si un des textes est trop long ou a trop de lignes."""
    for texte in contenus:
        if not texte:
            continue
        if len(texte) > TEXTWALL_MAX_CARACTERES:
            return True
        if texte.count("\n") + 1 > TEXTWALL_MAX_LIGNES:
            return True
    return False


async def sanctionner_mur_de_texte(message, membre):
    """Même sanction que le spam : message supprimé + timeout 1 heure."""
    try:
        await message.delete()
    except discord.Forbidden:
        print("[ERREUR] Il me manque la permission 'Gérer les messages'.")
    except discord.NotFound:
        pass

    if membre.is_timed_out():
        return
    try:
        await membre.timeout(DUREE_TIMEOUT_SPAM, reason="Text wall (timeout 1 hour)")
        await message.channel.send(f"{membre.mention} has been muted for spamming")
        await envoyer_log(
            message.guild,
            "🔇 Mute — Text wall",
            f"**User:** {membre.mention} (`{membre.id}`)\n"
            f"**Channel:** {message.channel.mention}\n"
            f"**Reason:** Message too long ({len(message.content)} characters, "
            f"{message.content.count(chr(10)) + 1} lines)\n"
            f"**Duration:** 1 hour",
            discord.Color.orange(),
        )
    except discord.Forbidden:
        print(
            f"[ERREUR] Impossible de mute {membre} : permission 'Exclure temporairement "
            f"des membres' manquante, ou son rôle est au-dessus de celui du bot."
        )
    except discord.HTTPException as e:
        print(f"[ERREUR] Timeout impossible : {e}")


# ---------- ANTI-SPAM ----------
historique_messages = defaultdict(deque)  # (serveur, membre) -> (heure, message)
spam_jusqua = {}                          # (serveur, membre) -> heure de fin du nettoyage


def enregistrer_message(message):
    """Retourne la liste des messages à supprimer si c'est du spam, sinon None."""
    cle = (message.guild.id, message.author.id)
    maintenant = time.monotonic()

    # Messages qui arrivent juste après le déclenchement : on les supprime aussi
    if spam_jusqua.get(cle, 0) > maintenant:
        return [message]

    file = historique_messages[cle]
    file.append((maintenant, message))
    while file and maintenant - file[0][0] >= SPAM_FENETRE:
        file.popleft()

    if len(file) > SPAM_MAX_MESSAGES:
        a_supprimer = [m for _, m in file]
        file.clear()
        spam_jusqua[cle] = maintenant + SPAM_ANALYSE
        return a_supprimer
    return None


async def supprimer_messages(messages, membre):
    """Supprime les messages repérés, puis relit les salons concernés
    pour effacer tout ce que la personne a envoyé ces dernières secondes."""
    # 1. Les messages déjà repérés
    async def supprimer(m):
        try:
            await m.delete()
        except discord.HTTPException:
            pass

    await asyncio.gather(*(supprimer(m) for m in messages))

    # 2. Analyse de l'historique des salons où elle a écrit
    salons = {m.channel.id: m.channel for m in messages}
    limite = discord.utils.utcnow() - timedelta(seconds=SPAM_ANALYSE)

    def a_effacer(m):
        return m.author.id == membre.id and m.created_at >= limite

    for salon in salons.values():
        try:
            await salon.purge(limit=100, check=a_effacer, after=limite)
        except discord.Forbidden:
            print("[ERREUR] Il me manque 'Gérer les messages' ou 'Voir l'historique des messages'.")
        except discord.HTTPException as e:
            print(f"[ERREUR] Nettoyage impossible : {e}")


# ---------- STATISTIQUES (compteurs de rôles) ----------
FICHIER_STATS = "stats_config.json"


def charger_stats():
    return storage.charger(FICHIER_STATS)


def sauver_stats(data):
    storage.sauver(FICHIER_STATS, data)


def compter(role):
    return sum(1 for m in role.members if not m.bot)


async def mettre_a_jour_guild(guild, config):
    for entree in config["salons"]:
        salon = guild.get_channel(entree["salon_id"])
        role = guild.get_role(entree["role_id"])
        if salon is None or role is None:
            continue
        nom = f"{entree['texte']} : {compter(role)}"[:100]
        if salon.name != nom:
            try:
                await salon.edit(name=nom, reason="Mise à jour des statistiques")
            except discord.HTTPException as e:
                print(f"[ERREUR] Renommage impossible : {e}")


# Discord limite à 2 renommages par salon toutes les 10 minutes : 6 minutes est sûr
@tasks.loop(minutes=6)
async def maj_stats():
    for guild_id, config in charger_stats().items():
        guild = client.get_guild(int(guild_id))
        if guild:
            await mettre_a_jour_guild(guild, config)


@maj_stats.before_loop
async def avant_maj_stats():
    await client.wait_until_ready()


@tree.command(name="setupstat", description="Create the stats category with 3 role counters")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    role1="First role to count", texte1="Label for the first counter",
    role2="Second role to count", texte2="Label for the second counter",
    role3="Third role to count", texte3="Label for the third counter",
    categorie="Category name (optional)",
)
async def setupstat(
    interaction: discord.Interaction,
    role1: discord.Role, texte1: str,
    role2: discord.Role, texte2: str,
    role3: discord.Role, texte3: str,
    categorie: str = "📊 Server Stats",
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    data = charger_stats()

    # Si une installation existe déjà, on la remplace
    ancien = data.get(str(guild.id))
    if ancien:
        for entree in ancien["salons"]:
            salon = guild.get_channel(entree["salon_id"])
            if salon:
                try:
                    await salon.delete(reason="Remplacement des statistiques")
                except discord.HTTPException:
                    pass
        cat = guild.get_channel(ancien.get("categorie_id", 0))
        if cat:
            try:
                await cat.delete(reason="Remplacement des statistiques")
            except discord.HTTPException:
                pass

    # Salons visibles par tous mais personne ne peut s'y connecter (ce sont des compteurs)
    droits = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
    }

    try:
        cat = await guild.create_category(categorie, overwrites=droits, reason="/setupstat")
        salons = []
        for role, texte in ((role1, texte1), (role2, texte2), (role3, texte3)):
            nom = f"{texte} : {compter(role)}"[:100]
            salon = await guild.create_voice_channel(
                nom, category=cat, overwrites=droits, reason="/setupstat"
            )
            salons.append({"salon_id": salon.id, "role_id": role.id, "texte": texte})
    except discord.Forbidden:
        await interaction.followup.send(
            "Il me manque la permission **Gérer les salons** (et **Gérer les rôles**).",
            ephemeral=True,
        )
        return

    data[str(guild.id)] = {"categorie_id": cat.id, "salons": salons}
    sauver_stats(data)
    await interaction.followup.send(
        f"Catégorie **{categorie}** créée avec 3 compteurs. "
        f"Ils se mettent à jour toutes les 6 minutes.",
        ephemeral=True,
    )

@tree.command(
    name="checkmembers",
    description="Check that the bot sees the member count and the stats channels status",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    force_update="Si True, force immédiatement la mise à jour des salons stats",
)
async def checkmembers(interaction: discord.Interaction, force_update: bool = False):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    total_officiel = guild.member_count
    caches = list(guild.members)
    nb_cache = len(caches)
    bots = sum(1 for m in caches if m.bot)
    humains_cache = nb_cache - bots
    ecart = (total_officiel - nb_cache) if total_officiel is not None else None

    lignes = [
        f"**Serveur :** {guild.name}",
        f"**Total Discord (`guild.member_count`) :** **{total_officiel}**",
        (
            f"**Membres en cache (`len(guild.members)`) :** **{nb_cache}** "
            f"(humains ≈ {humains_cache}, bots ≈ {bots})"
        ),
    ]
    if ecart is not None and abs(ecart) > 2:
        lignes.append(
            f"⚠️ **Écart cache/total : {ecart:+d}** — le bot n'a peut-être pas encore "
            "chargé tous les membres en mémoire (normal sur les gros serveurs). "
            "Le total Discord reste la référence."
        )
    else:
        lignes.append("✅ Cache et total Discord sont alignés.")

    lignes.append("**Members intent :** oui (`intents.members = True`)")

    data = charger_stats()
    config = data.get(str(guild.id))
    if not config:
        lignes.append("\n**Salons stats :** ❌ pas configurés (`/setupstat`).")
    else:
        lignes.append("\n**Salons stats (auto toutes les 6 min) :**")
        for entree in config.get("salons", []):
            salon = guild.get_channel(entree["salon_id"])
            role = guild.get_role(entree["role_id"])
            if role is None:
                lignes.append(f"• rôle `{entree['role_id']}` introuvable")
                continue
            compte = compter(role)
            attendu = f"{entree['texte']} : {compte}"[:100]
            if salon is None:
                lignes.append(
                    f"• salon manquant pour **{entree['texte']}** "
                    f"(rôle {role.mention} → **{compte}**)"
                )
            else:
                ok = "✅" if salon.name == attendu else "⏳"
                lignes.append(
                    f"• {ok} {salon.mention}\n"
                    f"  actuel : `{salon.name}`\n"
                    f"  calculé : `{attendu}` (rôle {role.mention})"
                )

        if force_update:
            try:
                await mettre_a_jour_guild(guild, config)
                lignes.append(
                    "\n🔄 **Mise à jour forcée envoyée.** "
                    "Discord limite à 2 renommages / 10 min par salon."
                )
            except Exception as e:
                lignes.append(f"\n❌ Échec mise à jour forcée : `{e}`")
        else:
            lignes.append(
                "\n_Astuce : `force_update: True` pour pousser la mise à jour tout de suite "
                "(limite Discord : 2 renommages / 10 min)._"
            )

    lignes.append(
        "\n📌 Les compteurs `/setupstat` affichent le nombre de **membres ayant le rôle choisi**, "
        "pas automatiquement le total du serveur. "
        "Pour un compteur « total membres », utilise un rôle que tout le monde a "
        "(ex. rôle règles / membre)."
    )

    texte = "\n".join(lignes)
    if len(texte) <= 1900:
        await interaction.followup.send(texte, ephemeral=True)
    else:
        await interaction.followup.send(texte[:1900], ephemeral=True)
        if len(texte) > 1900:
            await interaction.followup.send(texte[1900:3800], ephemeral=True)



# ---------- HONEYPOT ----------
FICHIER_HONEY = "honeypot_config.json"


def charger_honey():
    return storage.charger(FICHIER_HONEY)


def sauver_honey(data):
    storage.sauver(FICHIER_HONEY, data)


HONEY = charger_honey()  # serveur -> {"salon_id": ..., "compteur": ..., "message_id": ...}


def texte_honey():
    # Le message reste fixe : le compteur est consultable via le bouton.
    return "# DO NOT WRITE IN THIS CHANNEL\n## 🍯 Honeypot active\n\nUse the button below to see how many bans the honeypot has triggered."

honey_softban = set()    # (serveur, membre) en cours de bannissement/débannissement
honey_view_registered = False


class HoneypotCounterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="View ban count",
        style=discord.ButtonStyle.secondary,
        emoji="🍯",
        custom_id="honeypot_ban_count",
    )
    async def ban_count(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = HONEY.get(str(interaction.guild.id), {})
        compteur = config.get("compteur", 0)
        await interaction.response.send_message(
            f"🍯 **Honeypot bans:** {compteur}",
            ephemeral=True,
        )


@tree.command(name="setuphoney", description="Create a honeypot channel: anyone who writes there is softbanned")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def setuphoney(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # Si un honeypot existe déjà, on supprime l'ancien salon
    ancien = HONEY.get(str(guild.id))
    compteur = ancien.get("compteur", 0) if ancien else 0  # on garde le compteur
    if ancien:
        vieux = guild.get_channel(ancien["salon_id"])
        if vieux:
            try:
                await vieux.delete(reason="Remplacement du honeypot")
            except discord.HTTPException:
                pass

    try:
        salon = await guild.create_text_channel(
            "honeypot",
            overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True)
            },
            reason="/setuphoney",
        )
        msg = await salon.send(texte_honey(), view=HoneypotCounterView())
    except discord.Forbidden:
        await interaction.followup.send(
            "Il me manque des permissions (**Gérer les salons**, **Envoyer des messages**).",
            ephemeral=True,
        )
        return

    HONEY[str(guild.id)] = {"salon_id": salon.id, "compteur": compteur, "message_id": msg.id}
    sauver_honey(HONEY)
    await interaction.followup.send(
        f"Honeypot créé : {salon.mention}. Tu peux le renommer et le déplacer comme tu veux.",
        ephemeral=True,
    )


async def incrementer_honey(guild):
    """+1 au compteur du honeypot sans modifier le message du salon."""
    config = HONEY.get(str(guild.id))
    if not config:
        return
    config["compteur"] = config.get("compteur", 0) + 1
    sauver_honey(HONEY)


async def traiter_honeypot(message, membre):
    guild = message.guild
    cle = (guild.id, membre.id)
    honey_softban.add(cle)
    try:
        await message.delete()
    except discord.HTTPException:
        pass
    try:
        # Bannir supprime les messages de la dernière heure, puis on débannit tout de suite
        await guild.ban(membre, reason="Honeypot", delete_message_seconds=3600)
        await guild.unban(membre, reason="Honeypot : débannissement immédiat")
        await incrementer_honey(guild)
        await envoyer(guild, f"{membre.mention} has been kicked cuz he got token grabbed")
        await envoyer_log(
            guild,
            "🍯 Honeypot triggered",
            f"**User:** {membre.mention} (`{membre.id}`)\n"
            f"**Reason:** Message in honeypot channel\n"
            f"**Action:** Ban + unban (softban)",
            discord.Color.red(),
        )
    except discord.Forbidden:
        print(
            f"[ERREUR] Impossible de bannir {membre} : permission 'Bannir des membres' "
            f"manquante, ou son rôle est au-dessus de celui du bot."
        )
    except discord.HTTPException as e:
        print(f"[ERREUR] Honeypot : {e}")
    await asyncio.sleep(15)
    honey_softban.discard(cle)


# ---------- MODÉRATION : /ban /kick /timeout ----------
UNITES_DUREE = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
TIMEOUT_MAX = timedelta(days=28)  # limite imposée par Discord


def parser_duree(texte):
    """'10m', '2h', '1d', '1h30m'... -> timedelta, ou None si invalide."""
    texte = texte.strip().lower().replace(" ", "")
    if not re.fullmatch(r"(?:\d+[smhdw])+", texte):
        return None
    total = sum(int(n) * UNITES_DUREE[u] for n, u in re.findall(r"(\d+)([smhdw])", texte))
    return timedelta(seconds=total)


def format_duree(td):
    reste = int(td.total_seconds())
    morceaux = []
    for nom, sec in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        valeur, reste = divmod(reste, sec)
        if valeur:
            morceaux.append(f"{valeur}{nom}")
    return " ".join(morceaux) or "0s"


def verifier_cible(interaction, cible):
    """Retourne un message d'erreur si la sanction est impossible, sinon None."""
    guild = interaction.guild
    if cible.id == interaction.user.id:
        return "You can't do that to yourself."
    if cible.id == client.user.id:
        return "I can't sanction myself."
    if cible.id == guild.owner_id:
        return "You can't sanction the server owner."
    membre = guild.get_member(cible.id)
    if membre is not None:
        if interaction.user.id != guild.owner_id and membre.top_role >= interaction.user.top_role:
            return "This member's highest role is equal to or above yours."
        if membre.top_role >= guild.me.top_role:
            return "This member's highest role is equal to or above mine, I can't sanction them."
    return None


async def liberer_sanction(cle):
    """Laisse le temps aux événements Discord d'arriver, puis nettoie les sets."""
    await asyncio.sleep(15)
    sanction_cmd.discard(cle)
    bannis.discard(cle)
    expulses.discard(cle)


@tree.command(name="ban", description="Ban a user from the server")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(user="The user to ban", reason="Why this user is banned")
async def ban_cmd(
    interaction: discord.Interaction,
    user: discord.User,
    reason: app_commands.Range[str, 1, 400],
):
    guild = interaction.guild
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("You need the **Ban Members** permission.", ephemeral=True)
        return
    if not guild.me.guild_permissions.ban_members:
        await interaction.response.send_message("I need the **Ban Members** permission.", ephemeral=True)
        return
    erreur = verifier_cible(interaction, user)
    if erreur:
        await interaction.response.send_message(erreur, ephemeral=True)
        return

    await interaction.response.defer()
    cle = (guild.id, user.id)
    sanction_cmd.add(cle)
    try:
        await guild.ban(user, reason=f"{interaction.user} : {reason}", delete_message_seconds=0)
    except discord.Forbidden:
        sanction_cmd.discard(cle)
        await interaction.edit_original_response(content="I can't ban this user (role hierarchy or missing permission).")
        return
    except discord.HTTPException as e:
        sanction_cmd.discard(cle)
        await interaction.edit_original_response(content=f"Error: {e}")
        return

    await interaction.edit_original_response(
        content=f"{interaction.user.mention} banned {user.mention} for the reason: *{discord.utils.escape_markdown(reason)}*",
        allowed_mentions=discord.AllowedMentions(users=[user]),
    )
    await envoyer_log(
        guild,
        "🔨 Member banned",
        f"**User:** {user.mention} (`{user.id}`)\n"
        f"**Moderator:** {interaction.user.mention} (`{interaction.user.id}`)\n"
        f"**Reason:** {reason}",
        discord.Color.red(),
    )
    asyncio.create_task(liberer_sanction(cle))


@tree.command(name="kick", description="Kick a member from the server")
@app_commands.guild_only()
@app_commands.default_permissions(kick_members=True)
@app_commands.describe(user="The member to kick", reason="Why this member is kicked")
async def kick_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: app_commands.Range[str, 1, 400],
):
    guild = interaction.guild
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message("You need the **Kick Members** permission.", ephemeral=True)
        return
    if not guild.me.guild_permissions.kick_members:
        await interaction.response.send_message("I need the **Kick Members** permission.", ephemeral=True)
        return
    erreur = verifier_cible(interaction, user)
    if erreur:
        await interaction.response.send_message(erreur, ephemeral=True)
        return

    await interaction.response.defer()
    cle = (guild.id, user.id)
    sanction_cmd.add(cle)
    try:
        await guild.kick(user, reason=f"{interaction.user} : {reason}")
    except discord.Forbidden:
        sanction_cmd.discard(cle)
        await interaction.edit_original_response(content="I can't kick this member (role hierarchy or missing permission).")
        return
    except discord.HTTPException as e:
        sanction_cmd.discard(cle)
        await interaction.edit_original_response(content=f"Error: {e}")
        return

    await interaction.edit_original_response(
        content=f"{interaction.user.mention} kicked {user.mention} for the reason: *{discord.utils.escape_markdown(reason)}*",
        allowed_mentions=discord.AllowedMentions(users=[user]),
    )
    await envoyer_log(
        guild,
        "🥾 Member kicked",
        f"**User:** {user.mention} (`{user.id}`)\n"
        f"**Moderator:** {interaction.user.mention} (`{interaction.user.id}`)\n"
        f"**Reason:** {reason}",
        discord.Color.orange(),
    )
    asyncio.create_task(liberer_sanction(cle))


@tree.command(name="timeout", description="Timeout a member for a given duration")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.describe(
    user="The member to timeout",
    duration="Duration: 30s, 10m, 2h, 1d, 1h30m... (max 28d)",
    reason="Why this member is timed out",
)
async def timeout_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str,
    reason: app_commands.Range[str, 1, 400],
):
    guild = interaction.guild
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You need the **Timeout Members** permission.", ephemeral=True)
        return
    if not guild.me.guild_permissions.moderate_members:
        await interaction.response.send_message("I need the **Timeout Members** permission.", ephemeral=True)
        return
    erreur = verifier_cible(interaction, user)
    if erreur:
        await interaction.response.send_message(erreur, ephemeral=True)
        return

    delai = parser_duree(duration)
    if delai is None or delai.total_seconds() <= 0:
        await interaction.response.send_message(
            "Invalid duration. Examples: `30s`, `10m`, `2h`, `1d`, `1h30m`.", ephemeral=True
        )
        return
    if delai > TIMEOUT_MAX:
        await interaction.response.send_message("The maximum timeout duration is **28 days**.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        await user.timeout(delai, reason=f"{interaction.user} : {reason}")
    except discord.Forbidden:
        await interaction.edit_original_response(
            content="I can't timeout this member (role hierarchy, missing permission, or they are an administrator)."
        )
        return
    except discord.HTTPException as e:
        await interaction.edit_original_response(content=f"Error: {e}")
        return

    texte_duree = format_duree(delai)
    await interaction.edit_original_response(
        content=(
            f"{interaction.user.mention} timed out {user.mention} for **{texte_duree}** "
            f"for the reason: *{discord.utils.escape_markdown(reason)}*"
        ),
        allowed_mentions=discord.AllowedMentions(users=[user]),
    )
    await envoyer_log(
        guild,
        "⏳ Member timed out",
        f"**User:** {user.mention} (`{user.id}`)\n"
        f"**Moderator:** {interaction.user.mention} (`{interaction.user.id}`)\n"
        f"**Duration:** {texte_duree}\n"
        f"**Reason:** {reason}",
        discord.Color.gold(),
    )


# ---------- MODÉRATION : /unban /untimeout /deletemessage ----------
@tree.command(name="unban", description="Unban a user")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(
    user="The banned user (pick from the list or paste their ID)",
    reason="Why they are unbanned (optional)",
)
async def unban_cmd(interaction: discord.Interaction, user: str, reason: Optional[str] = None):
    guild = interaction.guild
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("You need the **Ban Members** permission.", ephemeral=True)
        return
    if not guild.me.guild_permissions.ban_members:
        await interaction.response.send_message("I need the **Ban Members** permission.", ephemeral=True)
        return
    user_id = user.strip()
    if not user_id.isdigit():
        await interaction.response.send_message(
            "Pick a banned user from the list, or paste their numeric ID.", ephemeral=True
        )
        return
    reason = reason[:400] if reason else None

    await interaction.response.defer()
    try:
        entree = await guild.fetch_ban(discord.Object(id=int(user_id)))
    except discord.NotFound:
        await interaction.edit_original_response(content="This user is not banned.")
        return
    except discord.HTTPException as e:
        await interaction.edit_original_response(content=f"Error: {e}")
        return

    cible = entree.user
    try:
        await guild.unban(cible, reason=f"{interaction.user} : {reason or 'No reason provided'}")
    except discord.Forbidden:
        await interaction.edit_original_response(content="I can't unban this user (missing permission).")
        return
    except discord.HTTPException as e:
        await interaction.edit_original_response(content=f"Error: {e}")
        return

    texte = f"{interaction.user.mention} unbanned {cible.mention}"
    if reason:
        texte += f" for the reason: *{discord.utils.escape_markdown(reason)}*"
    await interaction.edit_original_response(
        content=texte, allowed_mentions=discord.AllowedMentions(users=[cible])
    )
    await envoyer_log(
        guild,
        "🔓 Member unbanned",
        f"**User:** {cible.mention} (`{cible.id}`)\n"
        f"**Moderator:** {interaction.user.mention} (`{interaction.user.id}`)\n"
        f"**Reason:** {reason or 'None given'}",
        discord.Color.green(),
    )


@unban_cmd.autocomplete("user")
async def unban_autocomplete(interaction: discord.Interaction, current: str):
    guild = interaction.guild
    if guild is None or not interaction.user.guild_permissions.ban_members:
        return []
    current = current.lower()
    choix = []
    try:
        async for entree in guild.bans(limit=1000):
            nom = f"{entree.user} ({entree.user.id})"
            if current in nom.lower():
                choix.append(app_commands.Choice(name=nom[:100], value=str(entree.user.id)))
                if len(choix) >= 25:
                    break
    except discord.HTTPException:
        return []
    return choix


@tree.command(name="untimeout", description="Remove a member's timeout")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.describe(user="The member to untimeout", reason="Why the timeout is removed (optional)")
async def untimeout_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: Optional[str] = None,
):
    guild = interaction.guild
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You need the **Timeout Members** permission.", ephemeral=True)
        return
    if not guild.me.guild_permissions.moderate_members:
        await interaction.response.send_message("I need the **Timeout Members** permission.", ephemeral=True)
        return
    erreur = verifier_cible(interaction, user)
    if erreur:
        await interaction.response.send_message(erreur, ephemeral=True)
        return
    if not user.is_timed_out():
        await interaction.response.send_message("This member is not timed out.", ephemeral=True)
        return
    reason = reason[:400] if reason else None

    await interaction.response.defer()
    try:
        await user.timeout(None, reason=f"{interaction.user} : {reason or 'No reason provided'}")
    except discord.Forbidden:
        await interaction.edit_original_response(
            content="I can't remove this timeout (role hierarchy or missing permission)."
        )
        return
    except discord.HTTPException as e:
        await interaction.edit_original_response(content=f"Error: {e}")
        return

    texte = f"{interaction.user.mention} removed the timeout of {user.mention}"
    if reason:
        texte += f" for the reason: *{discord.utils.escape_markdown(reason)}*"
    await interaction.edit_original_response(
        content=texte, allowed_mentions=discord.AllowedMentions(users=[user])
    )
    await envoyer_log(
        guild,
        "🔊 Timeout removed",
        f"**User:** {user.mention} (`{user.id}`)\n"
        f"**Moderator:** {interaction.user.mention} (`{interaction.user.id}`)\n"
        f"**Reason:** {reason or 'None given'}",
        discord.Color.green(),
    )


@tree.command(name="deletemessage", description="Delete a member's messages from the last 5 minutes")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    user="The member whose recent messages are deleted",
    channel="Only this channel (default: every channel)",
)
async def deletemessage_cmd(
    interaction: discord.Interaction,
    user: discord.User,
    channel: Optional[discord.TextChannel] = None,
):
    guild = interaction.guild
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("You need the **Manage Messages** permission.", ephemeral=True)
        return
    if not guild.me.guild_permissions.manage_messages:
        await interaction.response.send_message("I need the **Manage Messages** permission.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    depuis = discord.utils.utcnow() - timedelta(minutes=5)
    salons = [channel] if channel else guild.text_channels

    total = 0
    ignores = 0
    touches = []
    for salon in salons:
        perms = salon.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history and perms.manage_messages):
            ignores += 1
            continue
        try:
            supprimes = await salon.purge(
                limit=None,
                after=depuis,
                check=lambda m: m.author.id == user.id,
                bulk=True,
            )
        except discord.HTTPException as e:
            print(f"[ERREUR] /deletemessage dans #{salon.name} : {e}")
            ignores += 1
            continue
        if supprimes:
            total += len(supprimes)
            touches.append(salon.mention)

    if total == 0:
        texte = f"No message from {user.mention} in the last 5 minutes."
    else:
        texte = (
            f"🗑️ Deleted **{total}** message(s) from {user.mention} "
            f"(last 5 minutes) in {', '.join(touches)}."
        )
    if ignores:
        texte += f"\n⚠️ {ignores} channel(s) skipped (missing permissions)."
    await interaction.followup.send(texte[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    if total:
        await envoyer_log(
            guild,
            "🗑️ Messages deleted",
            f"**User:** {user.mention} (`{user.id}`)\n"
            f"**Moderator:** {interaction.user.mention} (`{interaction.user.id}`)\n"
            f"**Messages:** {total} (last 5 minutes)\n"
            f"**Channels:** {', '.join(touches)}",
            discord.Color.orange(),
        )


# ---------- NIVEAUX ----------
FICHIER_NIVEAUX = "niveaux.json"
FICHIER_IMPORT = "draftbot_levels.json"   # export du classement DraftBot
XP_MIN, XP_MAX = 15, 25                   # XP gagné par message
XP_COOLDOWN = 30                          # secondes entre deux gains d'XP (comme DraftBot)


def charger_niveaux():
    return storage.charger(FICHIER_NIVEAUX)


NIVEAUX = charger_niveaux()   # guild_id -> {user_id: xp_total}
_niveaux_modifies = False
_dernier_xp = {}              # (guild_id, user_id) -> timestamp du dernier gain

# Configuration du salon où les montées de niveau sont annoncées.
FICHIER_NIVEAUX_CONFIG = "niveaux_config.json"


def charger_config_niveaux():
    return storage.charger(FICHIER_NIVEAUX_CONFIG)


def sauver_config_niveaux(data):
    storage.sauver(FICHIER_NIVEAUX_CONFIG, data)


NIVEAUX_CONFIG = charger_config_niveaux()  # guild_id -> {"salon_id": int}


async def annoncer_niveau(guild, membre, niveau):
    """Annonce une montée de niveau dans le salon configuré."""
    config = NIVEAUX_CONFIG.get(str(guild.id))
    if not config:
        return

    salon = guild.get_channel(config.get("salon_id"))
    if salon is None:
        return

    try:
        embed = discord.Embed(
            title="🎉 Level Up!",
            description=f"{membre.mention} just reached **level {niveau}**! 🔥",
            color=discord.Color.purple(),
        )
        embed.set_thumbnail(url=membre.display_avatar.url)
        await salon.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=[membre]),
        )
    except discord.Forbidden:
        print(f"[NIVEAUX] Impossible d'envoyer l'annonce dans #{salon.name}.")
    except discord.HTTPException as e:
        print(f"[NIVEAUX] Erreur lors de l'annonce : {e}")


def sauver_niveaux():
    global _niveaux_modifies
    if not _niveaux_modifies:
        return
    _niveaux_modifies = False
    storage.sauver(FICHIER_NIVEAUX, NIVEAUX)


@tasks.loop(seconds=30)
async def sauvegarde_niveaux():
    sauver_niveaux()


# Même courbe que DraftBot/MEE6 : passer du niveau n au niveau n+1 coûte 5n² + 50n + 100 XP
def xp_pour_niveau(n):
    return 5 * n * n + 50 * n + 100


def xp_cumule(n):
    """XP total nécessaire pour atteindre le niveau n."""
    return 5 * (n - 1) * n * (2 * n - 1) // 6 + 25 * n * (n - 1) + 100 * n


def niveau_depuis_xp(xp):
    n = 0
    while xp >= xp_cumule(n + 1):
        n += 1
    return n


async def gagner_xp(message, membre):
    global _niveaux_modifies
    cle = (message.guild.id, membre.id)
    maintenant = time.time()
    if maintenant - _dernier_xp.get(cle, 0) < XP_COOLDOWN:
        return
    _dernier_xp[cle] = maintenant

    donnees = NIVEAUX.setdefault(str(message.guild.id), {})
    avant = donnees.get(str(membre.id), 0)
    apres = avant + random.randint(XP_MIN, XP_MAX)
    donnees[str(membre.id)] = apres
    _niveaux_modifies = True

    niveau_avant = niveau_depuis_xp(avant)
    niveau = niveau_depuis_xp(apres)
    if niveau > niveau_avant:
        await annoncer_niveau(message.guild, membre, niveau)


@tree.command(
    name="setuplevels",
    description="Create the channel used for level-up announcements",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    salon="Name of the channel to create/use for level-up announcements",
)
async def setuplevels(interaction: discord.Interaction, salon: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    nom = salon.strip()
    if not nom:
        await interaction.response.send_message("Le nom du salon ne peut pas être vide.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # Si un salon portant ce nom existe déjà, on l'utilise.
    channel = discord.utils.get(guild.text_channels, name=nom)
    if channel is None:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=False,
                    read_message_history=True,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    embed_links=True,
                    read_message_history=True,
                ),
            }
            channel = await guild.create_text_channel(
                name=nom[:100],
                overwrites=overwrites,
                reason="/setuplevels",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Il me manque la permission **Gérer les salons** pour créer le salon.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"Impossible de créer le salon : {e}", ephemeral=True)
            return

    NIVEAUX_CONFIG[str(guild.id)] = {"salon_id": channel.id}
    sauver_config_niveaux(NIVEAUX_CONFIG)

    await interaction.followup.send(
        f"✅ Salon des niveaux configuré : {channel.mention}\n"
        f"Les membres ne peuvent pas écrire dedans et le bot y annoncera chaque montée de niveau.",
        ephemeral=True,
    )


@tree.command(name="rank", description="Show your level (or another member's)")
@app_commands.guild_only()
@app_commands.describe(user="The member to look up (default: you)")
async def rank_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    user = user or interaction.user
    donnees = NIVEAUX.get(str(interaction.guild.id), {})
    xp = donnees.get(str(user.id), 0)
    niveau = niveau_depuis_xp(xp)
    actuel = xp - xp_cumule(niveau)
    besoin = xp_pour_niveau(niveau)
    position = 1 + sum(
        1 for uid, v in donnees.items()
        if v > xp and interaction.guild.get_member(int(uid)) is not None
    )
    pleins = int(10 * actuel / besoin)
    barre = "▰" * pleins + "▱" * (10 - pleins)

    embed = discord.Embed(title=user.display_name, color=discord.Color.blurple())
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Level", value=str(niveau), inline=True)
    embed.add_field(name="Rank", value=f"#{position}", inline=True)
    embed.add_field(name="Total XP", value=formater(xp), inline=True)
    embed.add_field(
        name="Progress",
        value=f"{barre}\n{formater(actuel)} / {formater(besoin)} XP",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="myrank", description="Show your own level")
@app_commands.guild_only()
async def myrank_cmd(interaction: discord.Interaction):
    user = interaction.user
    donnees = NIVEAUX.get(str(interaction.guild.id), {})
    xp = donnees.get(str(user.id), 0)
    niveau = niveau_depuis_xp(xp)
    actuel = xp - xp_cumule(niveau)
    besoin = xp_pour_niveau(niveau)
    position = 1 + sum(
        1 for uid, v in donnees.items()
        if v > xp and interaction.guild.get_member(int(uid)) is not None
    )
    pleins = int(10 * actuel / besoin)
    barre = "▰" * pleins + "▱" * (10 - pleins)

    embed = discord.Embed(title=user.display_name, color=discord.Color.blurple())
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Level", value=str(niveau), inline=True)
    embed.add_field(name="Rank", value=f"#{position}", inline=True)
    embed.add_field(name="Total XP", value=formater(xp), inline=True)
    embed.add_field(
        name="Progress",
        value=f"{barre}\n{formater(actuel)} / {formater(besoin)} XP",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="leaderboard", description="Show the top 10 of the level leaderboard")
@app_commands.guild_only()
async def leaderboard_cmd(interaction: discord.Interaction):
    donnees = NIVEAUX.get(str(interaction.guild.id), {})
    classes = sorted(donnees.items(), key=lambda kv: kv[1], reverse=True)
    top = [(uid, xp) for uid, xp in classes if interaction.guild.get_member(int(uid)) is not None][:10]
    if not top:
        await interaction.response.send_message("Nobody has any XP yet.", ephemeral=True)
        return
    lignes = [
        f"**{i}.** <@{uid}> — level **{niveau_depuis_xp(xp)}** • {formater(xp)} XP"
        for i, (uid, xp) in enumerate(top, start=1)
    ]
    embed = discord.Embed(
        title=f"🏆 {interaction.guild.name} — Leaderboard",
        description="\n".join(lignes),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="setxp", description="Set a member's level (fix an import)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="The member", level="The level to give them")
async def setxp_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    level: app_commands.Range[int, 0, 200],
):
    global _niveaux_modifies
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    donnees = NIVEAUX.setdefault(str(interaction.guild.id), {})
    ancien_xp = donnees.get(str(user.id), 0)
    ancien_niveau = niveau_depuis_xp(ancien_xp)
    donnees[str(user.id)] = xp_cumule(level)
    _niveaux_modifies = True
    sauver_niveaux()

    if level > ancien_niveau:
        await annoncer_niveau(interaction.guild, user, level)

    await interaction.response.send_message(
        f"{user.mention} is now **level {level}**.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="importlevels", description="Import the levels exported from DraftBot")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def importlevels_cmd(interaction: discord.Interaction):
    global _niveaux_modifies
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # Read the import through the same storage backend as the bot (MongoDB on Render).
    try:
        lignes = storage.charger(FICHIER_IMPORT)
    except Exception as e:
        await interaction.followup.send(
            f"Storage error while loading `{FICHIER_IMPORT}`: {e}", ephemeral=True
        )
        return


    if not lignes:
        await interaction.followup.send(
            f"No data named `{FICHIER_IMPORT}` was found in the bot storage. "
            "The import file must first be saved to MongoDB using the storage system.",
            ephemeral=True,
        )
        return

    if not isinstance(lignes, list):
        await interaction.followup.send(
            f"Invalid `{FICHIER_IMPORT}` format: expected a JSON list of entries with `id` and `xp`.",
            ephemeral=True,
        )
        return


    donnees = NIVEAUX.setdefault(str(guild.id), {})
    absents = importes = invalides = 0
    for ligne in lignes:
        if not isinstance(ligne, dict) or "id" not in ligne or "xp" not in ligne:
            invalides += 1
            continue
        try:
            uid = str(int(ligne["id"]))
            xp = int(ligne["xp"])
            if xp < 0:
                raise ValueError("negative XP")
        except (TypeError, ValueError, OverflowError):
            invalides += 1
            continue

        donnees[uid] = max(int(donnees.get(uid, 0)), xp)
        importes += 1
        if guild.get_member(int(uid)) is None:
            absents += 1

    if importes == 0:
        await interaction.followup.send("No valid entries were found in the stored import.", ephemeral=True)
        return


    _niveaux_modifies = True
    sauver_niveaux()
    presents = importes - absents
    texte = f"✅ **{importes}** levels imported. **{presents}** members are on this server."
    if absents:
        texte += f"\n{absents} imported users are not currently on this server."
    if invalides:
        texte += f"\n⚠️ **{invalides}** invalid entries skipped."
    await interaction.followup.send(texte, ephemeral=True)



# ---------- TELECHARGEMENT VIDEO (/dl) ----------
# Commande slash : /dl link:<url>
# Sites : TikTok, Streamable (via yt-dlp) — YouTube retiré
# Gros fichiers : litterbox.catbox.moe (lien auto-supprime apres 24 h)
# Prerequis serveur : pip install yt-dlp

DL_MAX_OCTETS = 200 * 1024 * 1024  # 200 Mo max telechargement
DL_TIMEOUT_SEC = 240
REGEX_URL_VIDEO = re.compile(
    r"https?://(?:www\.)?"
    r"(?:"
    r"tiktok\.com/[^\s]+|vm\.tiktok\.com/[^\s]+|vt\.tiktok\.com/[^\s]+"
    r"|streamable\.com/[^\s]+"
    r")",
    re.IGNORECASE,
)


def extraire_url_video(texte: str) -> str | None:
    if not texte:
        return None
    m = REGEX_URL_VIDEO.search(texte)
    return m.group(0).rstrip(")>.,]") if m else None


async def telecharger_video(url: str, dossier: str) -> tuple[str | None, str | None]:
    """Télécharge une vidéo TikTok / Streamable avec yt-dlp. Retourne (chemin, erreur)."""
    os.makedirs(dossier, exist_ok=True)
    sortie = os.path.join(dossier, "%(id)s.%(ext)s")

    proxy = (
        os.getenv("YTDLP_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or ""
    ).strip()

    opts = {
        "outtmpl": sortie,
        "format": "mp4/bestvideo*+bestaudio/best/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": DL_MAX_OCTETS,
        "nocheckcertificate": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if proxy:
        opts["proxy"] = proxy

    def _run():
        try:
            import yt_dlp
        except ImportError:
            return None, (
                "yt-dlp is not installed. Add `yt-dlp[default]>=2025.1.0` to requirements.txt "
                "then redeploy with clear build cache."
            )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    return None, "Could not extract the video."
                chemin = ydl.prepare_filename(info)
                if not os.path.exists(chemin):
                    base, _ = os.path.splitext(chemin)
                    for ext in (".mp4", ".webm", ".mkv", ".mov"):
                        if os.path.exists(base + ext):
                            chemin = base + ext
                            break
                if not os.path.exists(chemin):
                    return None, "Downloaded file not found."
                taille = os.path.getsize(chemin)
                if taille > DL_MAX_OCTETS:
                    try:
                        os.remove(chemin)
                    except OSError:
                        pass
                    return None, (
                        f"Video too large ({taille // (1024 * 1024)} MB). "
                        f"Bot limit = {DL_MAX_OCTETS // (1024 * 1024)} MB."
                    )
                if taille == 0:
                    try:
                        os.remove(chemin)
                    except OSError:
                        pass
                    return None, "Empty file."
                return chemin, None
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            if "unsupported url" in low or "no video" in low:
                return None, "Unsupported link or no video found."
            if "private" in low:
                return None, "Private / inaccessible video."
            if any(s in low for s in ("youtube", "youtu.be")):
                return None, "YouTube is not supported. Use a **TikTok** or **Streamable** link."
            return None, f"yt-dlp error: {msg[:280]}"

    return await asyncio.to_thread(_run)


async def upload_temporaire(chemin: str) -> tuple[str | None, str | None]:
    """Upload temporaire avec plusieurs hebergeurs en secours.
    1) litterbox 24h (jusqu'a ~1 Go)
    2) 0x0.st (retention selon taille)
    3) gofile.io (gros fichiers)
    Retourne (url, erreur).
    """

    def _multipart(fields: dict, file_field: str, filename: str, data: bytes, content_type="application/octet-stream"):
        import uuid
        boundary = "----WebKitFormBoundary" + uuid.uuid4().hex[:16]
        chunks = []
        for name, value in fields.items():
            chunks.append(
                ("--" + boundary + "\r\n"
                 + 'Content-Disposition: form-data; name="' + name + '"\r\n\r\n'
                 + str(value) + "\r\n").encode("utf-8")
            )
        chunks.append(
            ("--" + boundary + "\r\n"
             + 'Content-Disposition: form-data; name="' + file_field
             + '"; filename="' + filename + '"\r\n'
             + "Content-Type: " + content_type + "\r\n\r\n").encode("utf-8")
        )
        chunks.append(data)
        chunks.append(("\r\n--" + boundary + "--\r\n").encode("utf-8"))
        body = b"".join(chunks)
        return body, boundary

    def _post(url, body, boundary, timeout=180):
        import urllib.request
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "User-Agent": "Mozilla/5.0 (compatible; DiscordBot/1.0)",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace").strip()

    def _litterbox(data, filename):
        body, boundary = _multipart(
            {"reqtype": "fileupload", "time": "24h"},
            "fileToUpload",
            filename,
            data,
        )
        texte = _post(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            body,
            boundary,
        )
        if texte.startswith("https://"):
            return texte, None
        return None, "litterbox: " + texte[:180]

    def _zero_x_zero(data, filename):
        body, boundary = _multipart({}, "file", filename, data)
        texte = _post("https://0x0.st", body, boundary)
        if texte.startswith("https://"):
            return texte, None
        return None, "0x0.st: " + texte[:180]

    def _gofile(data, filename):
        import urllib.request
        import json
        with urllib.request.urlopen("https://api.gofile.io/servers", timeout=30) as r:
            info = json.loads(r.read().decode())
        servers = info.get("data", {}).get("servers") or []
        if not servers:
            return None, "gofile: aucun serveur"
        server = servers[0]["name"]
        body, boundary = _multipart({}, "file", filename, data)
        texte = _post(f"https://{server}.gofile.io/uploadFile", body, boundary, timeout=240)
        try:
            payload = json.loads(texte)
        except Exception:
            return None, "gofile: reponse invalide"
        if payload.get("status") != "ok":
            return None, "gofile: " + str(payload)[:180]
        link = payload.get("data", {}).get("downloadPage") or payload.get("data", {}).get("directLink")
        if not link:
            return None, "gofile: pas de lien"
        return link, None

    def _run():
        filename = os.path.basename(chemin)
        with open(chemin, "rb") as f:
            data = f.read()
        erreurs = []
        for nom, fn in (
            ("litterbox", _litterbox),
            ("0x0.st", _zero_x_zero),
            ("gofile", _gofile),
        ):
            try:
                url, err = fn(data, filename)
                if url:
                    return url, None
                erreurs.append(err or nom)
            except Exception as e:
                erreurs.append(f"{nom}: {e}")
        return None, "Tous les hebergeurs ont echoue — " + " | ".join(erreurs)[:300]

    return await asyncio.to_thread(_run)


def extraire_url_depuis_message(message: discord.Message) -> str | None:
    """Cherche une URL vidéo dans le contenu, les embeds ou les pièces jointes vidéo."""
    textes = [message.content or ""]
    for emb in getattr(message, "embeds", []) or []:
        if emb.url:
            textes.append(emb.url)
        if emb.description:
            textes.append(emb.description)
        if emb.title:
            textes.append(emb.title)
        if getattr(emb, "author", None) and getattr(emb.author, "url", None):
            textes.append(emb.author.url)
    for t in textes:
        url = extraire_url_video(t)
        if url:
            return url
    # Pièce jointe vidéo déjà sur Discord
    for att in getattr(message, "attachments", []) or []:
        ct = (att.content_type or "").lower()
        fn = (att.filename or "").lower()
        if ct.startswith("video/") or fn.endswith((".mp4", ".webm", ".mov", ".mkv")):
            return att.url
    return None


def _url_video_valide(url: str) -> bool:
    if not url:
        return False
    if REGEX_URL_VIDEO.search(url):
        return True
    return any(s in url.lower() for s in ("tiktok", "streamable"))


async def _pipeline_dl(url: str, *, guild, author, statut_msg, dossier_id, channel, reference_msg=None):
    """Logique commune de téléchargement (slash, menu contextuel ou !dl en réponse)."""
    dossier = os.path.join("/tmp", "dl_" + str(dossier_id))
    chemin = None
    try:
        chemin, err = await telecharger_video(url, dossier)
        if err or not chemin:
            await statut_msg.edit(content="Failed: " + (err or "download impossible"))
            return

        taille = os.path.getsize(chemin)
        taille_mo = taille / (1024 * 1024)
        limite_discord = getattr(guild, "filesize_limit", None) or (25 * 1024 * 1024)

        if taille <= limite_discord:
            await statut_msg.edit(content="Sending to Discord ({:.1f} MB)...".format(taille_mo))
            try:
                kwargs = {
                    "content": "Video requested by " + author.mention,
                    "file": discord.File(chemin, filename=os.path.basename(chemin)),
                    "mention_author": False,
                }
                if reference_msg is not None:
                    await reference_msg.reply(**kwargs)
                else:
                    await channel.send(
                        content=kwargs["content"],
                        file=kwargs["file"],
                    )
                try:
                    await statut_msg.delete()
                except discord.HTTPException:
                    pass
                return
            except discord.HTTPException:
                await statut_msg.edit(
                    content=(
                        "Discord upload refused. Temporary 24h upload ({:.1f} MB)...".format(
                            taille_mo
                        )
                    )
                )
        else:
            await statut_msg.edit(
                content=(
                    "File too large for Discord ({:.1f} MB). "
                    "Temporary 24h upload...".format(taille_mo)
                )
            )

        lien, err_up = await upload_temporaire(chemin)
        if err_up or not lien:
            await statut_msg.edit(content="Upload failed: " + (err_up or "unknown"))
            return

        await statut_msg.edit(
            content=(
                "Video requested by "
                + author.mention
                + "\n**Temporary link:** "
                + lien
                + "\nSize: {:.1f} MB".format(taille_mo)
            )
        )
    except Exception as e:
        try:
            await statut_msg.edit(content="Error: " + str(e))
        except Exception:
            pass
    finally:
        try:
            if chemin and os.path.exists(chemin):
                os.remove(chemin)
            if os.path.isdir(dossier):
                for nom in os.listdir(dossier):
                    try:
                        os.remove(os.path.join(dossier, nom))
                    except OSError:
                        pass
                try:
                    os.rmdir(dossier)
                except OSError:
                    pass
        except OSError:
            pass


async def executer_dl(interaction: discord.Interaction, url: str):
    """Télécharge via une interaction (slash ou menu contextuel)."""
    await interaction.response.defer()
    statut_msg = await interaction.followup.send("Download in progress...")
    await _pipeline_dl(
        url,
        guild=interaction.guild,
        author=interaction.user,
        statut_msg=statut_msg,
        dossier_id=interaction.id,
        channel=interaction.channel,
        reference_msg=None,
    )


async def executer_dl_message(message: discord.Message, url: str):
    """Télécharge en répondant à un message (commande !dl)."""
    statut_msg = await message.reply("Download in progress...", mention_author=False)
    await _pipeline_dl(
        url,
        guild=message.guild,
        author=message.author,
        statut_msg=statut_msg,
        dossier_id=message.id,
        channel=message.channel,
        reference_msg=message,
    )


async def traiter_dl_message(message: discord.Message):
    """!dl en réponse à un message, ou !dl <url>."""
    contenu = (message.content or "").strip()
    url = None

    parties = contenu.split(None, 1)
    if len(parties) == 2:
        url = extraire_url_video(parties[1]) or parties[1].strip()

    if url is None and message.reference:
        ref = message.reference
        cible = ref.resolved
        if cible is None and ref.message_id:
            try:
                cible = await message.channel.fetch_message(ref.message_id)
            except (discord.NotFound, discord.HTTPException):
                cible = None
        if cible is not None:
            # Fichier vidéo déjà uploadé sur Discord
            for att in getattr(cible, "attachments", []) or []:
                ct = (att.content_type or "").lower()
                fn = (att.filename or "").lower()
                if ct.startswith("video/") or fn.endswith((".mp4", ".webm", ".mov", ".mkv")):
                    try:
                        await message.reply(
                            file=await att.to_file(),
                            mention_author=False,
                        )
                        return
                    except Exception:
                        await message.reply(
                            "File already on Discord: " + att.url,
                            mention_author=False,
                        )
                        return
            url = extraire_url_depuis_message(cible)

    if not url:
        await message.reply(
            "Reply to a message with a **TikTok / Streamable** link using `!dl`, "
            "or send `!dl <url>`.\n"
            "You can also use `/dl` or right-click → **Apps** → **Download video**.",
            mention_author=False,
        )
        return

    if not _url_video_valide(url):
        await message.reply(
            "Unrecognized link. Supported: **TikTok** and **Streamable**.",
            mention_author=False,
        )
        return

    await executer_dl_message(message, url)


@tree.command(
    name="dl",
    description="Download a TikTok or Streamable video",
)
@app_commands.guild_only()
@app_commands.describe(
    link="TikTok or Streamable URL (optional if you use Apps → Download video)",
)
async def dl_cmd(interaction: discord.Interaction, link: Optional[str] = None):
    """Slash /dl — link is optional. To download from a message, right-click it → Apps → Download video."""
    url = None
    if link and link.strip():
        url = extraire_url_video(link) or link.strip()

    if not url:
        await interaction.response.send_message(
            "Provide a **TikTok / Streamable** link, or right-click a message "
            "→ **Apps** → **Download video**.",
            ephemeral=True,
        )
        return

    if not _url_video_valide(url):
        await interaction.response.send_message(
            "Unrecognized link. Supported: **TikTok** and **Streamable**.",
            ephemeral=True,
        )
        return

    await executer_dl(interaction, url)


@tree.context_menu(name="Download video")
@app_commands.guild_only()
async def dl_context_menu(interaction: discord.Interaction, message: discord.Message):
    """Right-click a message → Apps → Download video (works like replying with !dl)."""
    # Pièce jointe vidéo déjà sur Discord : renvoyer le fichier directement
    for att in getattr(message, "attachments", []) or []:
        ct = (att.content_type or "").lower()
        fn = (att.filename or "").lower()
        if ct.startswith("video/") or fn.endswith((".mp4", ".webm", ".mov", ".mkv")):
            try:
                await interaction.response.defer()
                await interaction.followup.send(
                    content="Video requested by " + interaction.user.mention,
                    file=await att.to_file(),
                )
                return
            except Exception:
                await interaction.response.send_message(
                    "File already on Discord: " + att.url,
                    ephemeral=True,
                )
                return

    url = extraire_url_depuis_message(message)
    if not url:
        await interaction.response.send_message(
            "No **TikTok / Streamable** link found in that message.",
            ephemeral=True,
        )
        return

    if not _url_video_valide(url):
        await interaction.response.send_message(
            "Unrecognized link. Supported: **TikTok** and **Streamable**.",
            ephemeral=True,
        )
        return

    await executer_dl(interaction, url)





# ---------- RECONNAISSANCE MUSICALE (/songfinder) ----------
# Léger en RAM : on n'utilise PAS de modèle local.
# 1) extrait ~12 s d'audio compressé (ffmpeg)  2) envoie à Shazam via shazamio
# Prérequis : pip install shazamio   (+ ffmpeg sur la machine si possible)
MUSIC_MAX_OCTETS = 200 * 1024 * 1024  # 200 Mo max (le fichier est téléchargé sur le disque, pas en RAM)
MUSIC_SANS_FFMPEG_OCTETS = 8 * 1024 * 1024  # sans ffmpeg, on ne peut traiter que les petits fichiers
MUSIC_SAMPLE_SEC = 12                # seules les 12 premières secondes sont analysées
MUSIC_TIMEOUT_SEC = 45

# File d'attente (illimitée) : un seul morceau analysé à la fois, les autres attendent leur tour (ordre d'arrivée)
MUSIC_VERROU = asyncio.Lock()
MUSIC_ATTENTE = []                   # tickets : le premier est en cours, les suivants attendent


async def _music_extraire_echantillon(src: str, dest: str) -> tuple[bool, str]:
    """Réduit le fichier à un court extrait mono basse qualité pour limiter RAM/CPU/réseau.
    Retourne (ok, message_erreur). Si ffmpeg est absent, copie simplement la source."""
    def _run():
        import shutil
        import subprocess
        ffmpeg = shutil.which("ffmpeg")
        petit = os.path.getsize(src) <= MUSIC_SANS_FFMPEG_OCTETS
        if not ffmpeg:
            if not petit:
                return False, (
                    "ffmpeg is not installed on the server, so only files up to "
                    f"{MUSIC_SANS_FFMPEG_OCTETS // (1024 * 1024)} MB can be processed."
                )
            # Pas de ffmpeg : on utilise le fichier tel quel (moins optimal mais fonctionnel)
            shutil.copy2(src, dest)
            return True, ""
        try:
            # -t durée, mono, 16 kHz, 64 kb/s → extrait tout petit
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", src,
                "-t", str(MUSIC_SAMPLE_SEC),
                "-vn",
                "-ac", "1",
                "-ar", "16000",
                "-b:a", "64k",
                dest,
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
            if proc.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) == 0:
                if not petit:
                    return False, "ffmpeg could not read any audio from this file."
                # Fallback : fichier original (petits fichiers seulement)
                shutil.copy2(src, dest)
            return True, ""
        except Exception as e:
            if not petit:
                return False, str(e)[:200]
            try:
                shutil.copy2(src, dest)
                return True, ""
            except Exception:
                return False, str(e)[:200]

    return await asyncio.to_thread(_run)


async def _music_reconnaitre(chemin: str) -> tuple[dict | None, str | None]:
    """Reconnaît le morceau via Shazam (réseau uniquement, pas de modèle local)."""
    try:
        from shazamio import Shazam
    except ImportError:
        return None, (
            "shazamio n'est pas installé. Ajoute `shazamio` dans requirements.txt "
            "puis redeploie (`pip install shazamio`)."
        )
    try:
        shazam = Shazam()
        # API shazamio : recognize (versions récentes) ou recognize_song (anciennes)
        if hasattr(shazam, "recognize"):
            data = await asyncio.wait_for(shazam.recognize(chemin), timeout=MUSIC_TIMEOUT_SEC)
        else:
            data = await asyncio.wait_for(shazam.recognize_song(chemin), timeout=MUSIC_TIMEOUT_SEC)
        return data, None
    except asyncio.TimeoutError:
        return None, "Délai dépassé (Shazam trop lent)."
    except Exception as e:
        msg = str(e)[:300]
        if "404" in msg or "not found" in msg.lower():
            return None, "not_found"
        return None, msg


def _music_parse_resultat(data: dict) -> dict | None:
    """Extrait titre / artiste / album / cover / urls depuis la réponse Shazam."""
    if not data or not isinstance(data, dict):
        return None
    track = data.get("track") or data
    if not isinstance(track, dict):
        return None
    title = track.get("title") or track.get("heading", {}).get("title")
    artist = (
        track.get("subtitle")
        or track.get("heading", {}).get("subtitle")
        or track.get("artist")
    )
    if not title and not artist:
        return None

    album = None
    sections = track.get("sections") or []
    for sec in sections:
        if sec.get("type") == "SONG" or sec.get("tabname") == "Song":
            for meta in sec.get("metadata") or []:
                if str(meta.get("title", "")).lower() in ("album", "albums"):
                    album = meta.get("text")
                    break
        if album:
            break
    if not album:
        album = (track.get("sections") or [{}])[0].get("metadata", [{}])[0].get("text") if track.get("sections") else None

    cover = None
    images = track.get("images") or {}
    if isinstance(images, dict):
        cover = images.get("coverarthq") or images.get("coverart") or images.get("background")

    urls = track.get("share", {}) if isinstance(track.get("share"), dict) else {}
    share_url = urls.get("href") or track.get("url")
    apple = None
    hub = track.get("hub") or {}
    for opt in hub.get("options") or []:
        if str(opt.get("caption", "")).lower().find("apple") >= 0 or opt.get("providername") == "applemusic":
            actions = opt.get("actions") or hub.get("actions") or []
            for a in actions:
                if a.get("uri") or a.get("url"):
                    apple = a.get("uri") or a.get("url")
                    break
        if apple:
            break
    if not apple:
        for a in (hub.get("actions") or []):
            u = a.get("uri") or a.get("url")
            if u and "apple" in str(u).lower():
                apple = u
                break

    return {
        "title": title or "Unknown",
        "artist": artist or "Unknown",
        "album": album,
        "cover": cover,
        "url": share_url,
        "apple": apple,
    }


MUSIC_EXTENSIONS_OK = (
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".wma",
    ".mp4", ".webm", ".mov", ".mkv", ".avi",
)


def _attachment_audio_video(att: discord.Attachment) -> bool:
    nom = (att.filename or "").lower()
    ct = (att.content_type or "").lower()
    type_ok = ct.startswith("audio/") or ct.startswith("video/")
    ext_ok = any(nom.endswith(ext) for ext in MUSIC_EXTENSIONS_OK)
    return type_ok or ext_ok


def _songfinder_embed(info: dict, requester_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="🎵 Track found",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Title", value=info["title"], inline=True)
    embed.add_field(name="Artist", value=info["artist"], inline=True)
    if info.get("album"):
        embed.add_field(name="Album", value=info["album"], inline=True)
    if info.get("cover"):
        embed.set_thumbnail(url=info["cover"])
    liens = []
    if info.get("url"):
        liens.append(f"[Shazam]({info['url']})")
    if info.get("apple"):
        liens.append(f"[Apple Music]({info['apple']})")
    if liens:
        embed.add_field(name="Links", value=" • ".join(liens), inline=False)
    embed.set_footer(text=f"Requested by {requester_name}")
    return embed


async def _music_telecharger(attachment: discord.Attachment, dest: str):
    """Télécharge la pièce jointe par morceaux de 1 Mo directement sur le disque (RAM quasi nulle)."""
    timeout = aiohttp.ClientTimeout(total=600, sock_connect=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(attachment.url) as rep_http:
            if rep_http.status != 200:
                raise RuntimeError(f"download failed (HTTP {rep_http.status})")
            total = 0
            with open(dest, "wb") as f:
                async for morceau in rep_http.content.iter_chunked(1024 * 1024):
                    total += len(morceau)
                    if total > MUSIC_MAX_OCTETS:
                        raise RuntimeError(
                            f"file larger than {MUSIC_MAX_OCTETS // (1024 * 1024)} MB"
                        )
                    f.write(morceau)


async def _songfinder_traiter(attachment: discord.Attachment, dossier_id, requester_name: str):
    """Télécharge la pièce jointe, reconnaît le morceau, retourne (embed|None, erreur|None)."""
    nom = (attachment.filename or "audio").lower()

    dossier = os.path.join("/tmp", f"music_{dossier_id}")
    src = os.path.join(dossier, "input" + (os.path.splitext(nom)[1] or ".bin"))
    sample = os.path.join(dossier, "sample.mp3")
    try:
        os.makedirs(dossier, exist_ok=True)
        await _music_telecharger(attachment, src)

        ok, err = await _music_extraire_echantillon(src, sample)
        if not ok:
            return None, f"❌ Could not process the file: {err}"

        data, err = await _music_reconnaitre(sample if os.path.exists(sample) else src)
        if err == "not_found" or (data is None and not err):
            return None, "🔍 No match found. Try a clearer / longer excerpt (with less noise)."
        if err and data is None:
            return None, f"❌ Recognition failed: {err}"

        info = _music_parse_resultat(data or {})
        if not info:
            return None, "🔍 No match found. Try another excerpt of the track."

        return _songfinder_embed(info, requester_name), None
    except Exception as e:
        return None, f"❌ Error: {str(e)[:300]}"
    finally:
        try:
            if os.path.isdir(dossier):
                for nom_f in os.listdir(dossier):
                    try:
                        os.remove(os.path.join(dossier, nom_f))
                    except OSError:
                        pass
                try:
                    os.rmdir(dossier)
                except OSError:
                    pass
        except OSError:
            pass


def _music_notifier(maj_statut, texte):
    """Met à jour le message de statut sans bloquer (et sans jamais faire planter la file)."""
    if maj_statut is None:
        return

    async def _run():
        try:
            await maj_statut(texte)
        except Exception:
            pass

    asyncio.create_task(_run())


async def _songfinder_depuis_attachment(attachment: discord.Attachment, dossier_id, requester_name: str, maj_statut=None):
    """Met la demande en file d'attente : UN SEUL son analysé à la fois, dans l'ordre d'arrivée.
    maj_statut(texte) est appelé (async) pour informer la personne de sa position.
    Retourne (embed|None, erreur|None)."""
    if attachment.size and attachment.size > MUSIC_MAX_OCTETS:
        return None, (
            f"❌ File too large (**{attachment.size // (1024 * 1024)} MB**). "
            f"Maximum is **{MUSIC_MAX_OCTETS // (1024 * 1024)} MB**."
        )
    ticket = {"maj": maj_statut}
    MUSIC_ATTENTE.append(ticket)   # pas d'await entre l'ajout et le verrou : l'ordre est respecté
    try:
        position = MUSIC_ATTENTE.index(ticket)
        if position > 0:
            _music_notifier(
                maj_statut,
                f"⏳ Another song is being analyzed. You are **#{position}** in the queue...",
            )
        async with MUSIC_VERROU:
            _music_notifier(maj_statut, "🔍 Searching for the track...")
            return await _songfinder_traiter(attachment, dossier_id, requester_name)
    finally:
        MUSIC_ATTENTE.remove(ticket)
        # ceux qui attendent encore (hors le prochain, qui démarre) voient leur position avancer
        for i, t in enumerate(MUSIC_ATTENTE):
            if i >= 1:
                _music_notifier(t["maj"], f"⏳ You are **#{i}** in the queue...")


@tree.command(
    name="songfinder",
    description="Recognize a song from an audio/video file (Shazam, low RAM)",
)
@app_commands.guild_only()
@app_commands.describe(fichier="Audio or video file containing the song (max 200 MB)")
async def songfinder_cmd(interaction: discord.Interaction, fichier: discord.Attachment):
    """Recognize a song from a file. Designed to stay light on RAM on Render."""
    if not _attachment_audio_video(fichier):
        await interaction.response.send_message(
            "❌ Please send an **audio** or **video** file "
            "(mp3, wav, ogg, m4a, mp4, webm…).",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    async def maj(texte):
        await interaction.edit_original_response(content=texte)

    embed, err = await _songfinder_depuis_attachment(
        fichier, interaction.id, interaction.user.display_name, maj
    )
    if err:
        await interaction.edit_original_response(content=err, embed=None)
        return
    await interaction.edit_original_response(content=None, embed=embed)


async def traiter_songfinder_message(message: discord.Message):
    """!songfinder en réponse à un message avec un fichier audio/vidéo (comme !dl)."""
    attachment = None

    # 1) Pièce jointe sur le message !songfinder lui-même
    for att in message.attachments:
        if _attachment_audio_video(att):
            attachment = att
            break

    # 2) Sinon, message auquel on répond
    if attachment is None and message.reference:
        ref = message.reference
        cible = ref.resolved
        if cible is None and ref.message_id:
            try:
                cible = await message.channel.fetch_message(ref.message_id)
            except (discord.NotFound, discord.HTTPException):
                cible = None
        if cible is not None:
            for att in getattr(cible, "attachments", []) or []:
                if _attachment_audio_video(att):
                    attachment = att
                    break

    if attachment is None:
        await message.reply(
            "Reply to a message with an **audio/video** file using `!songfinder`, "
            "attach a file with `!songfinder`, or use `/songfinder`.\n"
            f"Max size: **{MUSIC_MAX_OCTETS // (1024 * 1024)} MB**.",
            mention_author=False,
        )
        return

    statut = await message.reply("🔍 Searching for the track...", mention_author=False)

    async def maj(texte):
        await statut.edit(content=texte)

    embed, err = await _songfinder_depuis_attachment(
        attachment, message.id, message.author.display_name, maj
    )
    try:
        if err:
            await statut.edit(content=err)
        else:
            await statut.edit(content=None, embed=embed)
    except discord.HTTPException:
        if err:
            await message.channel.send(err)
        elif embed:
            await message.channel.send(embed=embed)

# ---------- FILTRE ----------
@client.event
async def on_message(message):
    # Relais NotifyMe : on lit les messages des bots dans les salons "source" avant de les ignorer
    if message.guild is not None and message.author.bot:
        await relais_notify(message)

    if message.author.bot or message.guild is None:
        return

    membre = message.author
    if not isinstance(membre, discord.Member):
        return

    # Ne rien faire du tout si la personne est propriétaire, administrateur,
    # ou si son rôle est égal ou supérieur à celui du bot
    est_protege = (
        membre.id == message.guild.owner_id
        or membre.guild_permissions.administrator
        or membre.top_role >= message.guild.me.top_role
    )

    # Honeypot : tout message dans le salon piège est sanctionné
    config_honey = HONEY.get(str(message.guild.id))
    if config_honey and message.channel.id == config_honey["salon_id"]:
        if not est_protege:
            await traiter_honeypot(message, membre)
        return

    # Les messages transférés stockent le contenu et les fichiers du message
    # d'origine dans message_snapshots, pas forcément dans message.content/attachments.
    # On récupère donc tout le contenu à analyser avant les filtres.
    contenus_a_analyser = [message.content or ""]
    snapshots = getattr(message, "message_snapshots", None) or []
    for snapshot in snapshots:
        contenus_a_analyser.append(getattr(snapshot, "content", "") or "")

    # Fichiers bloqués (anti-reupload de contenu payant), y compris dans un
    # message transféré.
    if not est_protege:
        if await traiter_fichier_bloque(message, membre):
            return

    # Mots interdits : on analyse le texte du message ET celui des snapshots
    # des messages transférés.
    trouve = False
    mot_detecte = None
    for contenu in contenus_a_analyser:
        trouve, mot_detecte = contient_mot_interdit(contenu)
        if trouve:
            break
    if not est_protege and trouve:
        try:
            await message.delete()
        except discord.Forbidden:
            print("[ERREUR] Il me manque la permission 'Gérer les messages'.")
        except discord.NotFound:
            pass

        raison = f"Forbidden word detected: {mot_detecte}"
        try:
            await membre.timeout(DUREE_TIMEOUT, reason=raison)
            await message.channel.send(
                f"{membre.mention} has been muted for hateful conduct"
            )
            await envoyer_log(
                message.guild,
                "🔇 Mute — Hateful conduct",
                f"**User:** {membre.mention} (`{membre.id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Reason:** `{mot_detecte}`\n"
                f"**Duration:** 1 hour\n"
                f"**Message:** ||{message.content[:200]}||",
                discord.Color.orange(),
            )
        except discord.Forbidden:
            print(
                f"[ERREUR] Impossible de mute {membre} : permission 'Exclure temporairement "
                f"des membres' manquante, ou son rôle est au-dessus de celui du bot."
            )
        except discord.HTTPException as e:
            print(f"[ERREUR] Timeout impossible : {e}")
        return

    # Anti mur de texte
    if not est_protege and est_mur_de_texte(contenus_a_analyser):
        await sanctionner_mur_de_texte(message, membre)
        return

    # Anti-spam
    if not est_protege:
        a_supprimer = enregistrer_message(message)
        if a_supprimer:
            await supprimer_messages(a_supprimer, membre)
            deja_mute = membre.is_timed_out()
            if not deja_mute:
                try:
                    await membre.timeout(DUREE_TIMEOUT_SPAM, reason="Spam (timeout 1 hour)")
                    await message.channel.send(
                        f"{membre.mention} has been muted for spamming"
                    )
                    await envoyer_log(
                        message.guild,
                        "🔇 Mute — Spam",
                        f"**User:** {membre.mention} (`{membre.id}`)\n"
                        f"**Channel:** {message.channel.mention}\n"
                        f"**Reason:** Too many messages in a short time\n"
                        f"**Duration:** 1 hour",
                        discord.Color.orange(),
                    )
                except discord.Forbidden:
                    print(
                        f"[ERREUR] Impossible de mute {membre} : permission 'Exclure temporairement "
                        f"des membres' manquante, ou son rôle est au-dessus de celui du bot."
                    )
                except discord.HTTPException as e:
                    print(f"[ERREUR] Timeout impossible : {e}")
            return

    # Rapport quotidien : on compte le message (il a passé tous les filtres)
    dsstat_message(message.guild.id, membre.id)

    # Niveaux : le message est passé par tous les filtres, il rapporte de l'XP
    await gagner_xp(message, membre)

    # /fun : réponse personnalisée du bot quand un membre ciblé écrit
    await fun_repondre(message, membre)

    contenu_cmd = (message.content or "").strip()
    if contenu_cmd == "!ping":
        await message.channel.send("Pong !")
        return

    # !dl — reply to a message with a link, or !dl <url>
    if contenu_cmd == "!dl" or contenu_cmd.startswith("!dl "):
        await traiter_dl_message(message)
        return

    # !songfinder — reply to a message with an audio/video file (or attach one)
    if (
        contenu_cmd in ("!songfinder", "!song finder")
        or contenu_cmd.startswith("!songfinder ")
        or contenu_cmd.startswith("!song finder ")
    ):
        await traiter_songfinder_message(message)
        return



# ---------- FICHIERS BLOQUÉS (anti-reupload) ----------
FICHIER_BLOCKED = "blocked_files.json"
DUREE_TIMEOUT_FICHIER = timedelta(hours=1)


def charger_blocked():
    return storage.charger(FICHIER_BLOCKED)


def sauver_blocked(data):
    storage.sauver(FICHIER_BLOCKED, data)


async def lire_piece_jointe(attachment: discord.Attachment) -> bytes:
    """Télécharge une pièce jointe."""
    return await attachment.read()


async def hash_attachment(attachment: discord.Attachment) -> str:
    """Télécharge l'attachment et retourne son SHA-256."""
    data = await lire_piece_jointe(attachment)
    return hashlib.sha256(data).hexdigest()


@tree.command(
    name="blockfile",
    description="Block a file so it can no longer be posted on the server",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    fichier="The file to block (exact content will be blocked)",
)
async def blockfile(interaction: discord.Interaction, fichier: discord.Attachment):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        file_hash = await hash_attachment(fichier)
    except Exception as e:
        await interaction.followup.send(f"Failed to read the file: {e}", ephemeral=True)
        return

    data = charger_blocked()
    cle = str(interaction.guild.id)
    data.setdefault(cle, {})

    if file_hash in data[cle]:
        await interaction.followup.send(
            f"This file is already blocked.\nHash: `{file_hash}`",
            ephemeral=True,
        )
        return

    data[cle][file_hash] = {
        "filename": fichier.filename,
        "size": fichier.size,
        "added_by": interaction.user.id,
        "added_at": discord.utils.utcnow().isoformat(),
    }
    sauver_blocked(data)

    await interaction.followup.send(
        f"✅ File **{fichier.filename}** is now blocked.\n"
        f"Hash: `{file_hash}`\n"
        f"Anyone who posts this exact file will be **permanently banned**.",
        ephemeral=True,
    )
    await envoyer_log(
        interaction.guild,
        "🚫 File blocked",
        f"**File:** {fichier.filename}\n"
        f"**Hash:** `{file_hash}`\n"
        f"**By:** {interaction.user.mention}",
        discord.Color.dark_red(),
    )


@tree.command(
    name="unblockfile",
    description="Unblock a previously blocked file (by hash)",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    file_hash="The SHA-256 hash of the file to unblock",
)
async def unblockfile(interaction: discord.Interaction, file_hash: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    file_hash = file_hash.strip().lower()
    data = charger_blocked()
    cle = str(interaction.guild.id)
    entries = data.get(cle, {})

    if file_hash not in entries:
        await interaction.response.send_message(
            "This hash is not in the blocked list.",
            ephemeral=True,
        )
        return

    info = entries.pop(file_hash)
    sauver_blocked(data)

    await interaction.response.send_message(
        f"✅ Unblocked **{info.get('filename', file_hash)}**.\nHash: `{file_hash}`",
        ephemeral=True,
    )
    await envoyer_log(
        interaction.guild,
        "✅ File unblocked",
        f"**File:** {info.get('filename', '?')}\n"
        f"**Hash:** `{file_hash}`\n"
        f"**By:** {interaction.user.mention}",
        discord.Color.green(),
    )


@tree.command(
    name="listblocked",
    description="List all blocked files on this server",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def listblocked(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    data = charger_blocked()
    entries = data.get(str(interaction.guild.id), {})

    if not entries:
        await interaction.response.send_message("No blocked files.", ephemeral=True)
        return

    lines = []
    for h, info in list(entries.items())[:25]:
        lines.append(f"• **{info.get('filename', '?')}** — `{h[:16]}…`")

    embed = discord.Embed(
        title=f"Blocked files ({len(entries)})",
        description="\n".join(lines),
        color=discord.Color.dark_red(),
    )
    if len(entries) > 25:
        embed.set_footer(text=f"Showing 25 / {len(entries)}")

    await interaction.response.send_message(embed=embed, ephemeral=True)


async def traiter_fichier_bloque(message, membre):
    """Vérifie les pièces jointes du message et de ses messages transférés."""
    attachments = list(message.attachments)

    # Un message transféré expose les pièces jointes du message d'origine
    # via MessageSnapshot.attachments. Discord.py 2.5+ fournit message_snapshots.
    snapshots = getattr(message, "message_snapshots", None) or []
    for snapshot in snapshots:
        attachments.extend(getattr(snapshot, "attachments", []) or [])

    if not attachments:
        return False

    data = charger_blocked()
    entries = data.get(str(message.guild.id), {})
    if not entries:
        return False

    # Évite de hasher deux fois la même pièce jointe si Discord l'expose
    # à la fois sur le message et dans le snapshot.
    vus = set()
    for att in attachments:
        identifiant = getattr(att, "id", None) or getattr(att, "url", None)
        if identifiant in vus:
            continue
        vus.add(identifiant)
        try:
            # On ne limite plus la taille ici : un fichier bloqué peut aussi être
            # volumineux ou avoir été transféré depuis un autre message.
            file_hash = await hash_attachment(att)
        except Exception as e:
            print(f"[BLOCKFILE] Erreur hash : {e}")
            continue

        if file_hash in entries:
            info = entries[file_hash]
            try:
                await message.delete()
            except discord.Forbidden:
                print("[BLOCKFILE] Permission 'Gérer les messages' manquante.")
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                print(f"[BLOCKFILE] Suppression impossible : {e}")

            try:
                await message.channel.send(
                    f"{membre.mention} has been banned cuz he tried to leak premium asset"
                )
            except discord.HTTPException:
                pass

            leak_bans.add((message.guild.id, membre.id))
            try:
                await message.guild.ban(
                    membre,
                    reason=f"Leak premium asset: {info.get('filename', file_hash)}",
                    delete_message_seconds=0,
                )
            except discord.Forbidden:
                print(f"[BLOCKFILE] Impossible de bannir {membre}")
                leak_bans.discard((message.guild.id, membre.id))
            except discord.HTTPException as e:
                print(f"[BLOCKFILE] Ban impossible : {e}")
                leak_bans.discard((message.guild.id, membre.id))

            await envoyer_log(
                message.guild,
                "🚫 Blocked file posted — BAN",
                f"**User:** {membre.mention} (`{membre.id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**File:** {att.filename}\n"
                f"**Blocked as:** {info.get('filename', '?')}\n"
                f"**Hash:** `{file_hash}`\n"
                f"**Reason:** Tried to leak premium asset\n"
                f"**Action:** Permanent ban",
                discord.Color.dark_red(),
            )
            return True

    return False


# ---------- LOGS ----------
FICHIER_LOGS = "logs_config.json"


def charger_logs():
    return storage.charger(FICHIER_LOGS)


def sauver_logs(data):
    storage.sauver(FICHIER_LOGS, data)


async def envoyer_log(guild, title, description, color=discord.Color.dark_grey()):
    """Envoie un embed de log dans le salon configuré avec /setuplogs."""
    if guild is None:
        return
    data = charger_logs()
    channel_id = data.get(str(guild.id))
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[LOGS] Impossible d'envoyer le log : {e}")


@tree.command(
    name="setuplogs",
    description="Create or set the channel where the bot will send its logs",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    salon="Name of the log channel (created if it does not exist)",
)
async def setuplogs(interaction: discord.Interaction, salon: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    channel = discord.utils.get(guild.text_channels, name=salon)
    if channel is None:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    embed_links=True,
                ),
            }
            channel = await guild.create_text_channel(
                name=salon[:100],
                overwrites=overwrites,
                reason="/setuplogs",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I need the **Manage Channels** permission.",
                ephemeral=True,
            )
            return
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

    data = charger_logs()
    data[str(guild.id)] = channel.id
    sauver_logs(data)

    await interaction.followup.send(
        f"Log channel set to {channel.mention}.\n"
        f"All bot actions will be logged there.",
        ephemeral=True,
    )
    await envoyer_log(
        guild,
        "📋 Logs enabled",
        f"Configured by {interaction.user.mention}",
        discord.Color.green(),
    )


# ---------- RAPPORT QUOTIDIEN (/setupdsstat) ----------
FICHIER_DSSTAT = "dsstat.json"
DSSTAT_FUSEAU = "Europe/Paris"   # la "journée" va de minuit à minuit dans ce fuseau
SEUIL_TRES_ACTIF = 50            # 🔥 à partir de 50 messages dans la journée
SEUIL_ACTIF = 10                 # 🟢 à partir de 10 messages (🟡 en dessous)
DSSTAT_PAGES_MAX = 5             # nombre maximum de pages (liste des membres / des évènements)
HISTORIQUE_MAX = 400             # nombre de jours conservés (le graphique va jusqu'à 365 jours)
JOURS_GRAPHIQUE = (7, 28, 60, 365)
LISTES_EVENEMENTS = ("joins", "removes", "kicks", "bans", "honeypot", "timeouts")

try:
    from zoneinfo import ZoneInfo
    FUSEAU = ZoneInfo(DSSTAT_FUSEAU)
except Exception:
    # Sous Windows il faut parfois `pip install tzdata`. En attendant : heure locale du PC.
    FUSEAU = datetime.now().astimezone().tzinfo
    print(f"[ATTENTION] Fuseau {DSSTAT_FUSEAU} introuvable (pip install tzdata) : heure locale utilisée.")


def normaliser_cfg(cfg):
    for cle in LISTES_EVENEMENTS:
        if not isinstance(cfg.get(cle), list):
            cfg[cle] = []   # ancien format (simples compteurs) : on repart de zéro
    cfg.setdefault("messages", {})
    cfg.setdefault("historique", {})
    return cfg


def charger_dsstat():
    return {gid: normaliser_cfg(cfg) for gid, cfg in storage.charger(FICHIER_DSSTAT).items()}


DSSTAT = charger_dsstat()   # guild_id -> journée en cours + historique des jours passés
_dsstat_modifie = False


def sauver_dsstat():
    global _dsstat_modifie
    if not _dsstat_modifie:
        return
    _dsstat_modifie = False
    storage.sauver(FICHIER_DSSTAT, DSSTAT)


def aujourdhui():
    return datetime.now(FUSEAU).date().isoformat()


def dsstat_vide(salon_id, jour):
    cfg = {"salon_id": salon_id, "date": jour}
    return normaliser_cfg(cfg)


def dsstat_message(guild_id, user_id):
    global _dsstat_modifie
    cfg = DSSTAT.get(str(guild_id))
    if cfg is None:
        return
    messages = cfg["messages"]
    messages[str(user_id)] = messages.get(str(user_id), 0) + 1
    _dsstat_modifie = True


def dsstat_evenement(guild_id, cle, cible, **extra):
    """Enregistre un évènement (arrivée, départ, ban...) avec le nom de la personne."""
    global _dsstat_modifie
    cfg = DSSTAT.get(str(guild_id))
    if cfg is None or getattr(cible, "bot", False):
        return
    nom = getattr(cible, "display_name", None) or getattr(cible, "name", None) or "Unknown user"
    evenement = {"id": str(cible.id), "name": nom, "t": datetime.now(FUSEAU).strftime("%H:%M")}
    evenement.update({k: v for k, v in extra.items() if v})
    cfg[cle].append(evenement)
    _dsstat_modifie = True


def duree_courte(td):
    minutes = max(1, round(td.total_seconds() / 60))
    jours, reste = divmod(minutes, 1440)
    heures, minutes = divmod(reste, 60)
    morceaux = [f"{jours}d" if jours else "", f"{heures}h" if heures else "", f"{minutes}m" if minutes else ""]
    return " ".join(m for m in morceaux if m)


def dsstat_audit(entry):
    """Enregistre kicks / bans / timeouts d'après le journal d'audit (toutes sources confondues)."""
    guild = entry.guild
    cible = entry.target
    if guild is None or cible is None:
        return
    par = getattr(entry.user, "display_name", None) or "Unknown"
    raison = entry.reason or ""
    # Les commandes /ban /kick /timeout écrivent "Modérateur : raison" : on retrouve le vrai modérateur
    if entry.user is not None and client.user is not None and entry.user.id == client.user.id:
        m = re.match(r"^(.+?) : (.*)$", raison, re.S)
        if m:
            par, raison = m.group(1), m.group(2)

    if entry.action == discord.AuditLogAction.kick:
        dsstat_evenement(guild.id, "kicks", cible, by=par, reason=raison)
    elif entry.action == discord.AuditLogAction.ban:
        if raison.startswith("Honeypot"):
            dsstat_evenement(guild.id, "honeypot", cible)
        else:
            dsstat_evenement(guild.id, "bans", cible, by=par, reason=raison)
    elif entry.action == discord.AuditLogAction.member_update:
        fin = getattr(entry.after, "timed_out_until", None)
        if fin is not None:
            dsstat_evenement(
                guild.id, "timeouts", cible,
                by=par, reason=raison, dur=duree_courte(fin - entry.created_at),
            )


def liste_partis(rec):
    """Départs 'volontaires' : tous les départs sauf ceux causés par un kick / ban / honeypot."""
    sanctionnes = {e["id"] for cle in ("kicks", "bans", "honeypot") for e in rec.get(cle, [])}
    return [e for e in rec.get("removes", []) if e["id"] not in sanctionnes]


def donnees_jour(cfg, jour_iso):
    """Les données d'un jour : la journée en cours, ou une journée archivée."""
    if cfg is None:
        return None
    if jour_iso == cfg.get("date"):
        return cfg
    return cfg.get("historique", {}).get(jour_iso)


def archiver_jour(cfg, total_membres):
    rec = {cle: cfg.get(cle, []) for cle in LISTES_EVENEMENTS}
    rec["messages_total"] = sum(cfg.get("messages", {}).values())
    rec["writers"] = len(cfg.get("messages", {}))
    rec["total"] = total_membres   # nombre de membres en fin de journée (sert au graphique du total)
    return rec


def libelle_jour(jour_iso):
    return datetime.strptime(jour_iso, "%Y-%m-%d").strftime("%A %d %B %Y")


def decouper_lignes(lignes, taille=3800):
    pages, courant = [], ""
    for ligne in lignes:
        if len(courant) + len(ligne) + 1 > taille:
            pages.append(courant)
            courant = ""
        courant += ligne + "\n"
    if courant:
        pages.append(courant)
    return pages


def compteurs_du_jour(rec):
    return {
        "joins": len(rec.get("joins", [])),
        "left": len(liste_partis(rec)),
        "bans": len(rec.get("bans", [])),
        "kicks": len(rec.get("kicks", [])),
        "timeouts": len(rec.get("timeouts", [])),
        "honeypot": len(rec.get("honeypot", [])),
    }


def construire_rapport(guild, cfg, apercu=False):
    """Retourne la liste des embeds du rapport : le 1er est le résumé, les suivants la liste des membres."""
    c = compteurs_du_jour(cfg)
    classement = sorted(cfg.get("messages", {}).items(), key=lambda kv: kv[1], reverse=True)
    total = sum(n for _, n in classement)
    humains = sum(1 for m in guild.members if not m.bot)
    muets = max(0, humains - len(classement))
    tres_actifs = sum(1 for _, n in classement if n >= SEUIL_TRES_ACTIF)
    actifs = sum(1 for _, n in classement if SEUIL_ACTIF <= n < SEUIL_TRES_ACTIF)
    peu_actifs = len(classement) - tres_actifs - actifs

    titre = f"📊 Daily report — {libelle_jour(cfg['date'])}"
    if apercu:
        titre += " (so far)"
    resume = discord.Embed(
        title=titre,
        description=(
            f"🔥 Very active (≥{SEUIL_TRES_ACTIF} msgs): **{tres_actifs}**  •  "
            f"🟢 Active ({SEUIL_ACTIF}–{SEUIL_TRES_ACTIF - 1}): **{actifs}**  •  "
            f"🟡 Low (1–{SEUIL_ACTIF - 1}): **{peu_actifs}**\n"
            f"*Click a button below to see the names, or open the graph.*"
        ),
        color=discord.Color.blurple(),
    )
    resume.add_field(name="📥 Joined", value=str(c["joins"]))
    resume.add_field(name="📤 Left", value=str(c["left"]))
    resume.add_field(name="🔨 Banned", value=str(c["bans"]))
    resume.add_field(name="🥾 Kicked", value=str(c["kicks"]))
    resume.add_field(name="⏳ Timed out", value=str(c["timeouts"]))
    resume.add_field(name="🍯 Honeypot", value=str(c["honeypot"]))
    resume.add_field(name="💬 Messages", value=str(total))
    resume.add_field(name="👥 Members who wrote", value=f"{len(classement)} / {humains}")
    resume.add_field(name="😴 Silent members", value=str(muets))
    # La date au début du footer sert aux boutons pour retrouver de quelle journée il s'agit
    resume.set_footer(text=f"{cfg['date']} • midnight to midnight ({DSSTAT_FUSEAU})")

    embeds = [resume]
    if not classement:
        embeds.append(discord.Embed(
            title="💬 Activity",
            description="Nobody wrote any message that day.",
            color=discord.Color.blurple(),
        ))
        return embeds

    lignes = []
    for rang, (uid, n) in enumerate(classement, start=1):
        puce = "🔥" if n >= SEUIL_TRES_ACTIF else "🟢" if n >= SEUIL_ACTIF else "🟡"
        lignes.append(f"`{rang:>2}.` {puce} <@{uid}> — **{n}** message{'s' if n > 1 else ''}")

    pages = decouper_lignes(lignes)
    if len(pages) > DSSTAT_PAGES_MAX:
        reste = sum(p.count("\n") for p in pages[DSSTAT_PAGES_MAX:])
        pages = pages[:DSSTAT_PAGES_MAX]
        pages[-1] += f"… and **{reste}** more member(s) with fewer messages."
    for i, page in enumerate(pages):
        embeds.append(discord.Embed(
            title="💬 Activity (most → least active)" if i == 0 else "💬 Activity (continued)",
            description=page,
            color=discord.Color.blurple(),
        ))
    return embeds


# ----- Graphique : arrivées / départs sur 7, 28, 60 ou 365 jours -----
def serie_jours(cfg, jours, fin=None):
    """[(date, arrivés, partis)] pour les `jours` derniers jours, jusqu'à `fin` (aujourd'hui par défaut)."""
    fin = fin or datetime.now(FUSEAU).date()
    serie = []
    for i in range(jours - 1, -1, -1):
        d = fin - timedelta(days=i)
        rec = donnees_jour(cfg, d.isoformat())
        serie.append((
            d,
            len(rec.get("joins", [])) if rec else 0,
            len(liste_partis(rec)) if rec else 0,
        ))
    return serie


def serie_total(guild, cfg, jours, fin=None):
    """[(date, nombre de membres, estimé?)] jour par jour. jours=0 -> depuis la création du serveur.

    Les jours enregistrés par le bot sont exacts. Avant, Discord ne donne pas l'historique : on
    l'estime d'après les dates d'arrivée des membres actuels (ceux déjà partis ne sont pas comptés)."""
    fin = fin or datetime.now(FUSEAU).date()
    humains = [m for m in guild.members if not m.bot]
    arrivees = sorted(m.joined_at.astimezone(FUSEAU).date() for m in humains if m.joined_at)
    creation = guild.created_at.astimezone(FUSEAU).date()
    debut = creation if jours == 0 else max(creation, fin - timedelta(days=jours - 1))

    enregistres = {
        iso: rec["total"] for iso, rec in cfg.get("historique", {}).items() if rec.get("total") is not None
    }
    premier = min(enregistres) if enregistres else None
    avant = [iso for iso in enregistres if iso < debut.isoformat()]
    dernier = enregistres[max(avant)] if avant else None

    serie = []
    d = debut
    while d <= fin:
        iso = d.isoformat()
        if d == fin:
            total, estime = len(humains), False
        elif iso in enregistres:
            total, estime = enregistres[iso], False
            dernier = total
        elif premier is not None and iso > premier and dernier is not None:
            total, estime = dernier, False   # jour sans enregistrement (bot éteint) : dernière valeur connue
        else:
            total, estime = bisect.bisect_right(arrivees, d), True
        serie.append((d, total, estime))
        d += timedelta(days=1)
    return serie


def tracer_graphique(serie, jours):
    """Dessine le graphique (PNG en mémoire). Demande matplotlib : pip install matplotlib"""
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator

    unite = "per day"
    if len(serie) > 90:   # sur une longue période, on regroupe par semaine pour rester lisible
        groupes, fin = [], len(serie)
        while fin > 0:
            debut = max(0, fin - 7)
            morceau = serie[debut:fin]
            groupes.append((morceau[-1][0], sum(x[1] for x in morceau), sum(x[2] for x in morceau)))
            fin = debut
        serie = list(reversed(groupes))
        unite = "per week"

    n = len(serie)
    etiquettes = [d.strftime("%d %b") for d, _, _ in serie]
    arrives = [x[1] for x in serie]
    partis = [x[2] for x in serie]
    fond, texte, grille = "#2b2d31", "#dbdee1", "#40444b"
    vert, rouge = "#57f287", "#ed4245"

    fig = Figure(figsize=(10, 4.6), dpi=130, facecolor=fond)
    ax = fig.subplots()
    ax.set_facecolor(fond)
    x = list(range(n))
    lib_arrives = f"Joined ({sum(arrives)})"
    lib_partis = f"Left ({sum(partis)})"

    if n <= 31:
        largeur = 0.4
        ax.bar([i - largeur / 2 for i in x], arrives, largeur, color=vert, label=lib_arrives)
        ax.bar([i + largeur / 2 for i in x], partis, largeur, color=rouge, label=lib_partis)
        if n <= 14:   # peu de barres : on écrit la valeur au-dessus
            for i in x:
                if arrives[i]:
                    ax.text(i - largeur / 2, arrives[i], str(arrives[i]), ha="center", va="bottom", color=texte, fontsize=9)
                if partis[i]:
                    ax.text(i + largeur / 2, partis[i], str(partis[i]), ha="center", va="bottom", color=texte, fontsize=9)
    else:
        ax.plot(x, arrives, color=vert, linewidth=2, label=lib_arrives)
        ax.fill_between(x, arrives, color=vert, alpha=0.15)
        ax.plot(x, partis, color=rouge, linewidth=2, label=lib_partis)
        ax.fill_between(x, partis, color=rouge, alpha=0.15)

    pas = max(1, n // 10)
    ax.set_xticks(x[::pas])
    ax.set_xticklabels(etiquettes[::pas], color=texte, fontsize=9)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_ylim(bottom=0)
    ax.tick_params(axis="y", colors=texte)
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="y", color=grille, linewidth=0.8)
    ax.set_axisbelow(True)
    for bord in ax.spines.values():
        bord.set_visible(False)
    ax.set_title(f"Joined vs left — last {jours} days ({unite})", color=texte, fontsize=13, pad=12)
    ax.set_ylabel(f"People {unite}", color=texte)
    ax.legend(facecolor=fond, edgecolor=grille, labelcolor=texte, loc="upper left")
    fig.tight_layout()

    tampon = io.BytesIO()
    fig.savefig(tampon, format="png", facecolor=fond)
    tampon.seek(0)
    return tampon


def tracer_graphique_total(serie, jours):
    """Courbe du nombre total de membres (PNG en mémoire). Demande matplotlib."""
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator

    n = len(serie)
    x = list(range(n))
    dates = [d for d, _, _ in serie]
    totaux = [t for _, t, _ in serie]
    estimes = [e for _, _, e in serie]
    fond, texte, grille = "#2b2d31", "#dbdee1", "#40444b"
    couleur, couleur_est = "#5865f2", "#949cf7"

    fig = Figure(figsize=(10, 4.6), dpi=130, facecolor=fond)
    ax = fig.subplots()
    ax.set_facecolor(fond)

    k = next((i for i, e in enumerate(estimes) if not e), n)   # 1er jour enregistré
    if k > 0:
        ax.plot(x[:min(k + 1, n)], totaux[:min(k + 1, n)], color=couleur_est, linewidth=2,
                linestyle="--", label="Estimated (join dates of current members)")
    if k < n:
        ax.plot(x[k:], totaux[k:], color=couleur, linewidth=2.4, label="Recorded daily")
    ax.fill_between(x, totaux, color=couleur, alpha=0.15)
    ax.annotate(str(totaux[-1]), (x[-1], totaux[-1]), textcoords="offset points", xytext=(0, 9),
                ha="right", color=texte, fontsize=11, fontweight="bold")

    if jours == 0:
        ax.set_ylim(bottom=0)
    else:
        bas, haut = min(totaux), max(totaux)
        marge = max(2, round((haut - bas) * 0.15))
        ax.set_ylim(bas - marge, haut + marge)
    ax.set_xlim(-0.5, n - 0.5)

    pas = max(1, n // 10)
    format_date = "%b %Y" if n > 300 else "%d %b"
    ax.set_xticks(x[::pas])
    ax.set_xticklabels([d.strftime(format_date) for d in dates[::pas]], color=texte, fontsize=9)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.tick_params(axis="y", colors=texte)
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="y", color=grille, linewidth=0.8)
    ax.set_axisbelow(True)
    for bord in ax.spines.values():
        bord.set_visible(False)
    titre = "Total members — since the beginning" if jours == 0 else f"Total members — last {jours} days"
    ax.set_title(titre, color=texte, fontsize=13, pad=12)
    ax.set_ylabel("Members", color=texte)
    ax.legend(facecolor=fond, edgecolor=grille, labelcolor=texte, loc="upper left")
    fig.tight_layout()

    tampon = io.BytesIO()
    fig.savefig(tampon, format="png", facecolor=fond)
    tampon.seek(0)
    return tampon


async def creer_graphique(guild, mode="flux", jours=7, fin=None):
    """Retourne (embed, fichier). mode : 'flux' (arrivés / partis) ou 'total' (nombre de membres)."""
    cfg = DSSTAT.get(str(guild.id))
    aujourd = datetime.now(FUSEAU).date()
    en_cours = fin is None or fin == aujourd
    nom_fichier = f"dsstat_{mode}_{jours}d.png"

    if mode == "total":
        serie = serie_total(guild, cfg, jours, fin)
        tampon = await asyncio.to_thread(tracer_graphique_total, serie, jours)
        actuel = serie[-1][1]
        if jours == 0:
            bilan = f"since {serie[0][0].strftime('%d %b %Y')}"
            titre = "👥 Total members — since the beginning"
        else:
            bilan = f"**{actuel - serie[0][1]:+d}** over the period"
            titre = f"👥 Total members — last {jours} days"
        embed = discord.Embed(
            title=titre,
            description=f"👥 **{actuel}** members  •  {bilan}",
            color=discord.Color.blurple(),
        )
        if any(e for _, _, e in serie):
            embed.set_footer(text="Dashed = estimated from the join dates of current members (people who "
                                  "already left aren't counted). Solid = recorded daily by the bot.")
    else:
        serie = serie_jours(cfg, jours, fin)
        tampon = await asyncio.to_thread(tracer_graphique, serie, jours)
        arrives = sum(x[1] for x in serie)
        partis = sum(x[2] for x in serie)
        embed = discord.Embed(
            title=f"📈 Joined vs left — last {jours} days",
            description=f"📥 **{arrives}** joined  •  📤 **{partis}** left  •  Net: **{arrives - partis:+d}**",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=(
            "Today is included (day in progress). " if en_cours else ""
        ) + "Days before /setupdsstat count as 0.")

    embed.set_image(url=f"attachment://{nom_fichier}")
    return embed, discord.File(tampon, filename=nom_fichier)


class GraphView(discord.ui.View):
    """Choix du graphique (arrivés/partis ou total) et de la période. Visible uniquement par celui qui a cliqué."""

    def __init__(self, mode="flux", jours=7):
        super().__init__(timeout=900)
        for cle, libelle, defaut in (("flux", "📊 Joined / left", 7), ("total", "👥 Total members", 0)):
            bouton = discord.ui.Button(
                label=libelle,
                style=discord.ButtonStyle.success if cle == mode else discord.ButtonStyle.secondary,
                row=0,
            )
            bouton.callback = self._callback(cle, defaut)
            self.add_item(bouton)
        periodes = JOURS_GRAPHIQUE + ((0,) if mode == "total" else ())
        for n in periodes:
            bouton = discord.ui.Button(
                label="Since the start" if n == 0 else f"{n} days",
                style=discord.ButtonStyle.primary if n == jours else discord.ButtonStyle.secondary,
                row=1,
            )
            bouton.callback = self._callback(mode, n)
            self.add_item(bouton)

    def _callback(self, mode, jours):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                embed, fichier = await creer_graphique(interaction.guild, mode, jours)
            except ImportError:
                await interaction.followup.send(MESSAGE_MATPLOTLIB, ephemeral=True)
                return
            await interaction.edit_original_response(embed=embed, attachments=[fichier], view=GraphView(mode, jours))
        return callback


MESSAGE_MATPLOTLIB = "The graph needs **matplotlib**: run `pip install matplotlib` on the machine running the bot, then restart it."


async def envoyer_graphique(interaction, jours, mode="flux"):
    await interaction.response.defer(ephemeral=True)
    if DSSTAT.get(str(interaction.guild.id)) is None:
        await interaction.followup.send("Run `/setupdsstat` first.", ephemeral=True)
        return
    try:
        embed, fichier = await creer_graphique(interaction.guild, mode, jours)
    except ImportError:
        await interaction.followup.send(MESSAGE_MATPLOTLIB, ephemeral=True)
        return
    await interaction.followup.send(embed=embed, file=fichier, view=GraphView(mode, jours), ephemeral=True)


# ----- Boutons du rapport : liste des noms -----
TITRES_LISTES = {
    "joins": "📥 Joined",
    "left": "📤 Left",
    "bans": "🔨 Banned",
    "kicks": "🥾 Kicked",
    "timeouts": "⏳ Timed out",
    "honeypot": "🍯 Honeypot",
}


def date_du_rapport(message):
    """Retrouve la date du rapport dans le footer de son 1er embed."""
    for embed in (message.embeds if message else []):
        if embed.footer and embed.footer.text:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", embed.footer.text)
            if m:
                return m.group(1)
    return None


def ligne_evenement(cle, e):
    nom = discord.utils.escape_markdown(e.get("name", "Unknown"))
    ligne = f"• **{nom}** (`{e['id']}`) • {e.get('t', '??:??')}"
    if cle == "timeouts" and e.get("dur"):
        ligne += f" • for **{e['dur']}**"
    if e.get("by"):
        ligne += f" • by {discord.utils.escape_markdown(e['by'])}"
    if e.get("reason"):
        ligne += f"\n   ↳ *{discord.utils.escape_markdown(e['reason'][:200])}*"
    return ligne


async def afficher_liste(interaction, cle):
    jour = date_du_rapport(interaction.message)
    rec = donnees_jour(DSSTAT.get(str(interaction.guild.id)), jour) if jour else None
    if rec is None:
        await interaction.response.send_message("The data of this day is no longer available.", ephemeral=True)
        return

    evenements = liste_partis(rec) if cle == "left" else rec.get(cle, [])
    titre = f"{TITRES_LISTES[cle]} — {libelle_jour(jour)} ({len(evenements)})"
    if not evenements:
        await interaction.response.send_message(f"{TITRES_LISTES[cle]}: nobody on {libelle_jour(jour)}.", ephemeral=True)
        return

    pages = decouper_lignes([ligne_evenement(cle, e) for e in evenements])
    a_envoyer = pages[:DSSTAT_PAGES_MAX]
    for i, page in enumerate(a_envoyer):
        embed = discord.Embed(
            title=titre if i == 0 else f"{TITRES_LISTES[cle]} (continued)",
            description=page,
            color=discord.Color.blurple(),
        )
        if i == 0:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)
    if len(pages) > DSSTAT_PAGES_MAX:
        await interaction.followup.send("…the list is too long to be shown in full.", ephemeral=True)


class DsstatView(discord.ui.View):
    """Boutons sous le rapport quotidien. Persistants : ils marchent aussi après un redémarrage du bot."""

    def __init__(self, compteurs=None):
        super().__init__(timeout=None)
        if compteurs:   # ex. "Joined" -> "Joined (7)"
            for enfant in self.children:
                cle = (enfant.custom_id or "").replace("dsstat_", "")
                if cle in compteurs:
                    enfant.label = f"{enfant.label} ({compteurs[cle]})"

    @discord.ui.button(label="Joined", emoji="📥", style=discord.ButtonStyle.secondary, custom_id="dsstat_joins", row=0)
    async def bouton_joins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await afficher_liste(interaction, "joins")

    @discord.ui.button(label="Left", emoji="📤", style=discord.ButtonStyle.secondary, custom_id="dsstat_left", row=0)
    async def bouton_left(self, interaction: discord.Interaction, button: discord.ui.Button):
        await afficher_liste(interaction, "left")

    @discord.ui.button(label="Banned", emoji="🔨", style=discord.ButtonStyle.secondary, custom_id="dsstat_bans", row=0)
    async def bouton_bans(self, interaction: discord.Interaction, button: discord.ui.Button):
        await afficher_liste(interaction, "bans")

    @discord.ui.button(label="Kicked", emoji="🥾", style=discord.ButtonStyle.secondary, custom_id="dsstat_kicks", row=0)
    async def bouton_kicks(self, interaction: discord.Interaction, button: discord.ui.Button):
        await afficher_liste(interaction, "kicks")

    @discord.ui.button(label="Timed out", emoji="⏳", style=discord.ButtonStyle.secondary, custom_id="dsstat_timeouts", row=0)
    async def bouton_timeouts(self, interaction: discord.Interaction, button: discord.ui.Button):
        await afficher_liste(interaction, "timeouts")

    @discord.ui.button(label="Honeypot", emoji="🍯", style=discord.ButtonStyle.secondary, custom_id="dsstat_honeypot", row=1)
    async def bouton_honeypot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await afficher_liste(interaction, "honeypot")

    @discord.ui.button(label="Graph", emoji="📈", style=discord.ButtonStyle.primary, custom_id="dsstat_graph", row=1)
    async def bouton_graph(self, interaction: discord.Interaction, button: discord.ui.Button):
        await envoyer_graphique(interaction, 7, "flux")

    @discord.ui.button(label="Total", emoji="👥", style=discord.ButtonStyle.primary, custom_id="dsstat_total", row=1)
    async def bouton_total(self, interaction: discord.Interaction, button: discord.ui.Button):
        await envoyer_graphique(interaction, 0, "total")


async def envoyer_rapport(guild, cfg, apercu=False):
    salon = guild.get_channel(cfg.get("salon_id"))
    if salon is None:
        print(f"[DSSTAT] Salon introuvable sur {guild.name} : rapport ignoré.")
        return False
    embeds = construire_rapport(guild, cfg, apercu)
    resume, pages = embeds[0], embeds[1:]

    # Les deux graphiques : arrivés/partis (7 jours) et nombre total de membres depuis le début du serveur
    graphes, fichiers = [], []
    fin = datetime.strptime(cfg["date"], "%Y-%m-%d").date()
    try:
        for mode, jours in (("flux", 7), ("total", 0)):
            embed, fichier = await creer_graphique(guild, mode, jours, fin)
            graphes.append(embed)
            fichiers.append(fichier)
    except ImportError:
        print("[DSSTAT] matplotlib manquant : rapport envoyé sans graphiques (pip install matplotlib).")
        graphes, fichiers = [], []
    except Exception as e:
        print(f"[DSSTAT] Graphiques impossibles : {e}")
        graphes, fichiers = [], []

    try:
        options = {
            "embeds": [resume] + graphes,
            "view": DsstatView(compteurs_du_jour(cfg)),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if fichiers:
            options["files"] = fichiers
        await salon.send(**options)
        # La liste d'activité : une page par message (limite Discord : 6000 caractères par message)
        for page in pages:
            await salon.send(embeds=[page], allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        print(f"[DSSTAT] Envoi du rapport impossible sur {guild.name} : {e}")
        return False
    return True


@tasks.loop(seconds=30)
async def boucle_dsstat():
    """Dès que la date change : envoie le rapport de la veille, l'archive, puis repart de zéro."""
    global _dsstat_modifie
    jour = aujourdhui()
    for gid, cfg in list(DSSTAT.items()):
        if cfg.get("date") == jour:
            continue
        guild = client.get_guild(int(gid))
        if guild is None:
            continue
        await envoyer_rapport(guild, cfg)
        historique = cfg.get("historique", {})
        humains = sum(1 for m in guild.members if not m.bot)
        historique[cfg["date"]] = archiver_jour(cfg, humains)
        for vieux in sorted(historique)[:-HISTORIQUE_MAX]:
            del historique[vieux]
        nouveau = dsstat_vide(cfg.get("salon_id"), jour)
        nouveau["historique"] = historique
        DSSTAT[gid] = nouveau
        _dsstat_modifie = True
    sauver_dsstat()


@boucle_dsstat.before_loop
async def avant_boucle_dsstat():
    await client.wait_until_ready()


@tree.command(name="setupdsstat", description="Create the channel where the daily server report is posted")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(salon="Name of the channel (created if it does not exist, default: daily-stats)")
async def setupdsstat(interaction: discord.Interaction, salon: str = "daily-stats"):
    global _dsstat_modifie
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    channel = discord.utils.get(guild.text_channels, name=salon)
    if channel is None:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    embed_links=True,
                    attach_files=True,
                ),
            }
            channel = await guild.create_text_channel(
                name=salon[:100],
                overwrites=overwrites,
                reason="/setupdsstat",
            )
        except discord.Forbidden:
            await interaction.followup.send("I need the **Manage Channels** permission.", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

    existant = DSSTAT.get(str(guild.id))
    if existant:
        existant["salon_id"] = channel.id   # on garde les compteurs et l'historique
    else:
        DSSTAT[str(guild.id)] = dsstat_vide(channel.id, aujourdhui())
    _dsstat_modifie = True
    sauver_dsstat()

    try:
        await channel.send(
            "📊 **Daily stats enabled.** A report is posted here every day just after midnight "
            f"({DSSTAT_FUSEAU}): joins, leaves, bans, kicks, timeouts and member activity. "
            "Use the buttons under each report to see the names and the graph."
        )
    except discord.HTTPException:
        pass
    await interaction.followup.send(
        f"Daily report channel set to {channel.mention}.\n"
        f"The first report will be posted after midnight ({DSSTAT_FUSEAU}). "
        f"Use `/dsstatnow` to preview today's stats and `/dsstatgraph` for the graph.",
        ephemeral=True,
    )


@tree.command(name="dsstatnow", description="Post today's stats so far in the stats channel (preview)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def dsstatnow(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    cfg = DSSTAT.get(str(interaction.guild.id))
    if not cfg:
        await interaction.response.send_message("Run `/setupdsstat` first.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if await envoyer_rapport(interaction.guild, cfg, apercu=True):
        await interaction.followup.send(f"Preview sent in <#{cfg['salon_id']}>.", ephemeral=True)
    else:
        await interaction.followup.send("Couldn't send the preview (channel missing or no permission).", ephemeral=True)


@tree.command(name="dsstatgraph", description="Show the graph of members who joined / left, or the total number of members")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(graph="Which graph", days="Period shown on the graph")
@app_commands.choices(
    graph=[
        app_commands.Choice(name="Joined vs left", value="flux"),
        app_commands.Choice(name="Total members", value="total"),
    ],
    days=[app_commands.Choice(name=f"{n} days", value=n) for n in JOURS_GRAPHIQUE]
    + [app_commands.Choice(name="Since the beginning", value=0)],
)
async def dsstatgraph(interaction: discord.Interaction, graph: str = "flux", days: int = 7):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    if graph == "flux" and days == 0:
        days = 365   # "depuis le début" n'a de sens que pour le total
    await envoyer_graphique(interaction, days, graph)


# ---------- ANTI-NUKE (protection contre destruction du serveur) ----------
# Détecte :
#   - suppressions massives de salons
#   - attribution massive de la permission Administrateur
# Actions :
#   - bannit l'auteur principal
#   - retire les rôles admin aux personnes qui les ont reçus récemment
#   - restaure au mieux les salons supprimés (snapshot)
#
# Nécessite : View Audit Log, Manage Channels, Manage Roles, Ban Members
# Le rôle du bot DOIT être au-dessus des rôles qu'il doit sanctionner.

FICHIER_ANTINUKE = "antinuke_config.json"
FICHIER_SNAPSHOTS = "antinuke_snapshots.json"

# Seuils par défaut (par acteur)
ANTINUKE_CHANNEL_DELETE_MAX = 3          # 3+ suppressions...
ANTINUKE_CHANNEL_DELETE_WINDOW = 20      # ...en moins de 20 secondes
ANTINUKE_ADMIN_GRANT_MAX = 2             # 2+ grants d'Administrator...
ANTINUKE_ADMIN_GRANT_WINDOW = 60         # ...en moins de 60 secondes
ANTINUKE_RESTORE_LIMIT = 40              # max de salons restaurés d'un coup

# Permissions considérées comme dangereuses (en plus d'Administrator)
PERMS_DANGEREUSES = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "ban_members",
    "kick_members",
    "manage_webhooks",
)


def charger_antinuke():
    try:
        data = storage.charger(FICHIER_ANTINUKE)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def sauver_antinuke(data):
    storage.sauver(FICHIER_ANTINUKE, data)


def charger_snapshots():
    try:
        data = storage.charger(FICHIER_SNAPSHOTS)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def sauver_snapshots(data):
    storage.sauver(FICHIER_SNAPSHOTS, data)


ANTINUKE = charger_antinuke()          # guild_id -> config
SNAPSHOTS = charger_snapshots()        # guild_id -> {channel_id: info}

# Compteurs en mémoire : (guild_id, user_id) -> deque[timestamps]
_antinuke_channel_deletes = defaultdict(deque)
_antinuke_admin_grants = defaultdict(deque)   # grants d'admin (role ou member)
# Qui a reçu un admin récemment : guild_id -> list[{user_id, role_id, by, ts}]
_antinuke_recent_admin_receivers = defaultdict(list)
# Protections anti-doublon de sanction
_antinuke_punished = set()             # (guild_id, user_id) récemment sanctionnés


def antinuke_actif(guild_id: int) -> bool:
    cfg = ANTINUKE.get(str(guild_id), {})
    return bool(cfg.get("enabled", False))


def antinuke_whitelist(guild: discord.Guild, user_id: int) -> bool:
    """Owner + liste blanche configurée + le bot lui-même."""
    if user_id == guild.owner_id:
        return True
    if client.user and user_id == client.user.id:
        return True
    cfg = ANTINUKE.get(str(guild.id), {})
    return user_id in set(cfg.get("whitelist", []))


def _serialiser_overwrites(channel: discord.abc.GuildChannel) -> list:
    out = []
    for target, ow in channel.overwrites.items():
        try:
            allow, deny = ow.pair()
            out.append({
                "id": target.id,
                "type": "role" if isinstance(target, discord.Role) else "member",
                "allow": allow.value,
                "deny": deny.value,
            })
        except Exception:
            continue
    return out


def snapshot_channel(channel: discord.abc.GuildChannel) -> dict:
    """Capture l'essentiel d'un salon pour pouvoir le recréer."""
    info = {
        "id": channel.id,
        "name": channel.name,
        "type": int(channel.type),
        "position": channel.position,
        "category_id": channel.category_id,
        "overwrites": _serialiser_overwrites(channel),
    }
    if isinstance(channel, discord.TextChannel):
        info["topic"] = channel.topic
        info["nsfw"] = channel.nsfw
        info["slowmode_delay"] = channel.slowmode_delay
    elif isinstance(channel, discord.VoiceChannel):
        info["bitrate"] = channel.bitrate
        info["user_limit"] = channel.user_limit
        info["nsfw"] = getattr(channel, "nsfw", False)
    elif isinstance(channel, discord.CategoryChannel):
        pass
    elif isinstance(channel, discord.ForumChannel):
        info["topic"] = getattr(channel, "topic", None)
        info["nsfw"] = getattr(channel, "nsfw", False)
    return info


def prendre_snapshot_guild(guild: discord.Guild):
    """Met à jour le snapshot de tous les salons du serveur."""
    data = {}
    for ch in guild.channels:
        try:
            data[str(ch.id)] = snapshot_channel(ch)
        except Exception as e:
            print(f"[ANTINUKE] Snapshot impossible pour {ch}: {e}")
    SNAPSHOTS[str(guild.id)] = data
    try:
        sauver_snapshots(SNAPSHOTS)
    except Exception as e:
        print(f"[ANTINUKE] Sauvegarde snapshot impossible : {e}")


async def restaurer_salon(guild: discord.Guild, info: dict):
    """Recrée un salon à partir d'un snapshot. Retourne le nouveau channel ou None."""
    try:
        overwrites = {}
        for ow in info.get("overwrites", []):
            target = None
            if ow.get("type") == "role":
                target = guild.get_role(ow["id"])
            else:
                target = guild.get_member(ow["id"])
            if target is None:
                continue
            overwrites[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(ow.get("allow", 0)),
                discord.Permissions(ow.get("deny", 0)),
            )

        category = guild.get_channel(info["category_id"]) if info.get("category_id") else None
        kwargs = {
            "name": info.get("name", "restored-channel")[:100],
            "overwrites": overwrites,
            "reason": "Anti-nuke: channel restore",
        }
        if category and isinstance(category, discord.CategoryChannel):
            kwargs["category"] = category

        t = info.get("type", 0)
        if t == int(discord.ChannelType.category):
            ch = await guild.create_category(**kwargs)
        elif t == int(discord.ChannelType.voice):
            kwargs["bitrate"] = info.get("bitrate", 64000)
            kwargs["user_limit"] = info.get("user_limit", 0)
            ch = await guild.create_voice_channel(**kwargs)
        elif t == int(discord.ChannelType.stage_voice):
            ch = await guild.create_stage_channel(**kwargs)
        elif t == int(discord.ChannelType.forum):
            ch = await guild.create_forum_channel(**kwargs)
        else:
            # text / news / etc.
            kwargs["topic"] = info.get("topic")
            kwargs["nsfw"] = info.get("nsfw", False)
            kwargs["slowmode_delay"] = info.get("slowmode_delay", 0)
            ch = await guild.create_text_channel(**kwargs)

        # Position (best-effort)
        try:
            await ch.edit(position=info.get("position", 0))
        except Exception:
            pass
        return ch
    except discord.Forbidden:
        print(f"[ANTINUKE] Pas la permission de recréer le salon {info.get('name')}")
    except Exception as e:
        print(f"[ANTINUKE] Erreur restauration salon {info.get('name')}: {e}")
    return None


def _nettoyer_deque(q: deque, fenetre: float):
    maintenant = time.time()
    while q and maintenant - q[0] > fenetre:
        q.popleft()


async def antinuke_sanctionner(guild: discord.Guild, acteur: discord.Member | discord.User, raison: str, channels_a_restaurer: list | None = None):
    """Bannit l'acteur, retire les admins récemment donnés, restaure les salons."""
    cle = (guild.id, acteur.id)
    if cle in _antinuke_punished:
        return
    _antinuke_punished.add(cle)
    # Nettoyage après 2 min
    asyncio.get_event_loop().call_later(120, lambda: _antinuke_punished.discard(cle))

    membre = guild.get_member(acteur.id)

    # 1. Retirer tous les rôles (sauf @everyone) si possible, puis ban
    if membre is not None and not antinuke_whitelist(guild, membre.id):
        try:
            roles_a_retirer = [r for r in membre.roles if r != guild.default_role and r < guild.me.top_role]
            if roles_a_retirer:
                await membre.remove_roles(*roles_a_retirer, reason=f"Anti-nuke: {raison}")
        except Exception as e:
            print(f"[ANTINUKE] Impossible de retirer les rôles de {acteur}: {e}")

        try:
            await guild.ban(
                acteur,
                reason=f"Anti-nuke: {raison}",
                delete_message_seconds=0,
            )
            sanction_cmd.add(cle)  # évite le message auto de départ
        except discord.Forbidden:
            print(f"[ANTINUKE] Impossible de bannir {acteur} (hiérarchie / permission)")
        except Exception as e:
            print(f"[ANTINUKE] Erreur ban {acteur}: {e}")

    # 2. Retirer Administrator aux personnes qui l'ont reçu récemment (via cet acteur ou dans la fenêtre)
    maintenant = time.time()
    recent = _antinuke_recent_admin_receivers.get(guild.id, [])
    restants = []
    for rec in recent:
        if maintenant - rec["ts"] > 300:  # 5 min
            continue
        # On retire si c'est le même acteur ou si on est en mode "destruction globale"
        if rec.get("by") == acteur.id or True:
            membre_cible = guild.get_member(rec["user_id"])
            role = guild.get_role(rec["role_id"])
            if membre_cible and role and role in membre_cible.roles:
                try:
                    if role.permissions.administrator or any(
                        getattr(role.permissions, p, False) for p in PERMS_DANGEREUSES
                    ):
                        await membre_cible.remove_roles(role, reason="Anti-nuke: revoke dangerous role")
                        print(f"[ANTINUKE] Rôle {role.name} retiré à {membre_cible}")
                except Exception as e:
                    print(f"[ANTINUKE] Impossible de retirer le rôle à {membre_cible}: {e}")
        else:
            restants.append(rec)
    _antinuke_recent_admin_receivers[guild.id] = restants

    # 3. Restaurer les salons
    restaures = []
    if channels_a_restaurer:
        for info in channels_a_restaurer[:ANTINUKE_RESTORE_LIMIT]:
            ch = await restaurer_salon(guild, info)
            if ch:
                restaures.append(ch.name)

    # 4. Log
    desc = (
        f"**Actor:** {acteur.mention} (`{acteur.id}`)\n"
        f"**Reason:** {raison}\n"
        f"**Action:** Roles stripped + ban\n"
        f"**Channels restored:** {len(restaures)}"
    )
    if restaures:
        desc += "\n• " + ", ".join(f"`#{n}`" for n in restaures[:15])
        if len(restaures) > 15:
            desc += f" … (+{len(restaures) - 15})"
    await envoyer_log(guild, "🛡️ ANTI-NUKE TRIGGERED", desc, discord.Color.dark_red())

    # Annonce dans le salon d'annonce anti-raid si dispo
    announce = guild.get_channel(RAID_ANNOUNCE_CHANNEL_ID)
    if announce is None and est_serveur_principal(guild):
        try:
            announce = await guild.fetch_channel(RAID_ANNOUNCE_CHANNEL_ID)
        except Exception:
            announce = None
    if announce is not None:
        try:
            await announce.send(
                f"🛡️ **Anti-nuke activated** — {acteur.mention} has been banned for destructive actions.\n"
                f"Channels restored: **{len(restaures)}**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            pass


async def antinuke_traiter_channel_delete(entry: discord.AuditLogEntry):
    """Appelé quand un salon est supprimé."""
    guild = entry.guild
    if guild is None or not antinuke_actif(guild.id):
        return
    acteur = entry.user
    if acteur is None or antinuke_whitelist(guild, acteur.id):
        return

    # Enregistre la suppression
    cle = (guild.id, acteur.id)
    q = _antinuke_channel_deletes[cle]
    maintenant = time.time()
    q.append(maintenant)
    _nettoyer_deque(q, ANTINUKE_CHANNEL_DELETE_WINDOW)

    # Snapshot du salon qui vient d'être supprimé (si on l'avait)
    snap = SNAPSHOTS.get(str(guild.id), {}).get(str(entry.target.id) if entry.target else "")
    channels_a_restaurer = []
    if snap:
        channels_a_restaurer.append(snap)

    if len(q) >= ANTINUKE_CHANNEL_DELETE_MAX:
        # Récupère tous les snapshots des salons récemment supprimés par cet acteur
        # (on se contente du snapshot global actuel + celui-ci)
        # Pour être plus précis on pourrait stocker une file des derniers deletes.
        tous = list(SNAPSHOTS.get(str(guild.id), {}).values())
        # On ne restaure que ceux qui n'existent plus
        existants = {c.id for c in guild.channels}
        a_restorer = [s for s in tous if s.get("id") not in existants]
        if snap and snap not in a_restorer:
            a_restorer.append(snap)
        await antinuke_sanctionner(
            guild,
            acteur,
            f"Mass channel deletion ({len(q)} in {ANTINUKE_CHANNEL_DELETE_WINDOW}s)",
            channels_a_restaurer=a_restorer,
        )
        q.clear()


async def antinuke_traiter_role_update(entry: discord.AuditLogEntry):
    """Détecte l'ajout de permissions dangereuses sur un rôle."""
    guild = entry.guild
    if guild is None or not antinuke_actif(guild.id):
        return
    acteur = entry.user
    if acteur is None or antinuke_whitelist(guild, acteur.id):
        return

    # entry.changes contient les changements
    dangereux = False
    for change in (entry.changes or []):
        attr = getattr(change, "key", None) or getattr(change, "attribute", None)
        if attr in PERMS_DANGEREUSES:
            avant = getattr(change, "before", None)
            apres = getattr(change, "after", None)
            if apres is True and avant is not True:
                dangereux = True
                break
        # Parfois Discord envoie permissions comme un entier
        if attr == "permissions":
            try:
                avant_p = discord.Permissions(getattr(change, "before", 0) or 0)
                apres_p = discord.Permissions(getattr(change, "after", 0) or 0)
                for p in PERMS_DANGEREUSES:
                    if getattr(apres_p, p, False) and not getattr(avant_p, p, False):
                        dangereux = True
                        break
            except Exception:
                pass

    if not dangereux:
        return

    cle = (guild.id, acteur.id)
    q = _antinuke_admin_grants[cle]
    q.append(time.time())
    _nettoyer_deque(q, ANTINUKE_ADMIN_GRANT_WINDOW)

    # On enregistre aussi le rôle modifié pour pouvoir le "nettoyer" plus tard
    role = entry.target
    if isinstance(role, discord.Role):
        # On note les membres qui ont ce rôle (ils reçoivent potentiellement l'admin)
        for m in role.members:
            _antinuke_recent_admin_receivers[guild.id].append({
                "user_id": m.id,
                "role_id": role.id,
                "by": acteur.id,
                "ts": time.time(),
            })

    if len(q) >= ANTINUKE_ADMIN_GRANT_MAX:
        await antinuke_sanctionner(
            guild,
            acteur,
            f"Mass dangerous permission grant ({len(q)} in {ANTINUKE_ADMIN_GRANT_WINDOW}s)",
        )
        q.clear()


async def antinuke_traiter_member_role_update(entry: discord.AuditLogEntry):
    """Quelqu'un a reçu / perdu un rôle. On regarde si le rôle a Administrator."""
    guild = entry.guild
    if guild is None or not antinuke_actif(guild.id):
        return
    acteur = entry.user
    if acteur is None or antinuke_whitelist(guild, acteur.id):
        return

    # changes: role add / remove
    for change in (entry.changes or []):
        # discord.py : change.key peut être "$add" ou "roles"
        key = getattr(change, "key", None)
        after = getattr(change, "after", None)
        if key in ("$add", "roles") and after:
            roles_ajoutes = after if isinstance(after, list) else [after]
            for r in roles_ajoutes:
                role = r if isinstance(r, discord.Role) else guild.get_role(getattr(r, "id", r))
                if role is None:
                    continue
                if role.permissions.administrator or any(
                    getattr(role.permissions, p, False) for p in PERMS_DANGEREUSES
                ):
                    cible = entry.target
                    if cible:
                        _antinuke_recent_admin_receivers[guild.id].append({
                            "user_id": cible.id,
                            "role_id": role.id,
                            "by": acteur.id,
                            "ts": time.time(),
                        })
                    cle = (guild.id, acteur.id)
                    q = _antinuke_admin_grants[cle]
                    q.append(time.time())
                    _nettoyer_deque(q, ANTINUKE_ADMIN_GRANT_WINDOW)
                    if len(q) >= ANTINUKE_ADMIN_GRANT_MAX:
                        await antinuke_sanctionner(
                            guild,
                            acteur,
                            f"Mass admin role assignment ({len(q)} in {ANTINUKE_ADMIN_GRANT_WINDOW}s)",
                        )
                        q.clear()


@tasks.loop(minutes=10)
async def boucle_snapshots_antinuke():
    """Met à jour régulièrement les snapshots des salons."""
    for guild in client.guilds:
        if antinuke_actif(guild.id):
            try:
                prendre_snapshot_guild(guild)
            except Exception as e:
                print(f"[ANTINUKE] Snapshot périodique échoué sur {guild.name}: {e}")


@boucle_snapshots_antinuke.before_loop
async def _avant_snapshots_antinuke():
    await client.wait_until_ready()


@tree.command(
    name="setupantinuke",
    description="Enable / configure anti-nuke protection (mass channel delete + admin grants)",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    enabled="Turn anti-nuke on or off",
)
async def setupantinuke(interaction: discord.Interaction, enabled: bool = True):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    # Seul le owner (ou un admin whitelisté) devrait pouvoir activer, mais on reste souple
    guild = interaction.guild
    ANTINUKE[str(guild.id)] = {
        "enabled": enabled,
        "whitelist": ANTINUKE.get(str(guild.id), {}).get("whitelist", []),
        "configured_by": interaction.user.id,
        "configured_at": discord.utils.utcnow().isoformat(),
    }
    sauver_antinuke(ANTINUKE)

    if enabled:
        prendre_snapshot_guild(guild)
        await interaction.response.send_message(
            "✅ **Anti-nuke enabled.**\n\n"
            f"• Channel deletes: **{ANTINUKE_CHANNEL_DELETE_MAX}+** in {ANTINUKE_CHANNEL_DELETE_WINDOW}s → ban + restore\n"
            f"• Admin / dangerous permission grants: **{ANTINUKE_ADMIN_GRANT_MAX}+** in {ANTINUKE_ADMIN_GRANT_WINDOW}s → ban + revoke\n\n"
            "⚠️ Put the bot role **above** every role it may need to strip or ban.\n"
            "Use `/antinuke_whitelist` to protect trusted admins.",
            ephemeral=True,
        )
        await envoyer_log(
            guild,
            "🛡️ Anti-nuke enabled",
            f"Configured by {interaction.user.mention}",
            discord.Color.green(),
        )
    else:
        await interaction.response.send_message("❌ Anti-nuke disabled.", ephemeral=True)


@tree.command(
    name="antinuke_whitelist",
    description="Add or remove a trusted user from the anti-nuke whitelist",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    user="User to add or remove",
    action="add or remove",
)
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
])
async def antinuke_whitelist_cmd(
    interaction: discord.Interaction,
    user: discord.User,
    action: str,
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    if interaction.user.id != interaction.guild.owner_id and not antinuke_whitelist(interaction.guild, interaction.user.id):
        # Seul le owner ou quelqu'un déjà whitelisté peut modifier
        await interaction.response.send_message(
            "Only the server owner (or an already whitelisted user) can edit the whitelist.",
            ephemeral=True,
        )
        return

    cfg = ANTINUKE.setdefault(str(interaction.guild.id), {"enabled": False, "whitelist": []})
    wl = list(cfg.get("whitelist", []))
    if action == "add":
        if user.id not in wl:
            wl.append(user.id)
        msg = f"✅ {user.mention} added to the anti-nuke whitelist."
    else:
        wl = [uid for uid in wl if uid != user.id]
        msg = f"✅ {user.mention} removed from the anti-nuke whitelist."
    cfg["whitelist"] = wl
    ANTINUKE[str(interaction.guild.id)] = cfg
    sauver_antinuke(ANTINUKE)
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(
    name="antinuke_status",
    description="Show anti-nuke status and current whitelist",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def antinuke_status(interaction: discord.Interaction):
    cfg = ANTINUKE.get(str(interaction.guild.id), {})
    enabled = cfg.get("enabled", False)
    wl = cfg.get("whitelist", [])
    snaps = len(SNAPSHOTS.get(str(interaction.guild.id), {}))
    texte = (
        f"**Anti-nuke:** {'🟢 ON' if enabled else '🔴 OFF'}\n"
        f"**Channel delete threshold:** {ANTINUKE_CHANNEL_DELETE_MAX} / {ANTINUKE_CHANNEL_DELETE_WINDOW}s\n"
        f"**Admin grant threshold:** {ANTINUKE_ADMIN_GRANT_MAX} / {ANTINUKE_ADMIN_GRANT_WINDOW}s\n"
        f"**Channels in snapshot:** {snaps}\n"
        f"**Whitelist:** {', '.join(f'<@{uid}>' for uid in wl) if wl else '_empty (owner always protected)_'}"
    )
    await interaction.response.send_message(texte, ephemeral=True)


@tree.command(
    name="antinuke_snapshot",
    description="Force an immediate channel snapshot (for restore)",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def antinuke_snapshot(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    prendre_snapshot_guild(interaction.guild)
    n = len(SNAPSHOTS.get(str(interaction.guild.id), {}))
    await interaction.followup.send(f"✅ Snapshot taken: **{n}** channels stored.", ephemeral=True)





@client.event
async def on_guild_channel_create(channel):
    if not isinstance(channel, discord.abc.GuildChannel):
        return
    guild = channel.guild
    if not antinuke_actif(guild.id):
        return
    try:
        SNAPSHOTS.setdefault(str(guild.id), {})[str(channel.id)] = snapshot_channel(channel)
        sauver_snapshots(SNAPSHOTS)
    except Exception as e:
        print(f"[ANTINUKE] Snapshot create échoué : {e}")


@client.event
async def on_guild_channel_update(before, after):
    if not isinstance(after, discord.abc.GuildChannel):
        return
    guild = after.guild
    if not antinuke_actif(guild.id):
        return
    try:
        SNAPSHOTS.setdefault(str(guild.id), {})[str(after.id)] = snapshot_channel(after)
        sauver_snapshots(SNAPSHOTS)
    except Exception as e:
        print(f"[ANTINUKE] Snapshot update échoué : {e}")


@client.event
async def on_guild_channel_delete(channel):
    # Snapshot volontairement conservé pour pouvoir restaurer
    pass


# ---------- TICKETS ----------
FICHIER_TICKETS = "tickets_config.json"
TICKET_CATEGORY_ID = 1492234570093826209  # catégorie des tickets mise à jour[cite: 2]
TRANSCRIPT_CHANNEL_ID = 1565108521941336104  # salon de transcript mis à jour[cite: 2]


def charger_tickets():
    return storage.charger(FICHIER_TICKETS)


def sauver_tickets(data):
    storage.sauver(FICHIER_TICKETS, data)


class TicketModal(discord.ui.Modal, title="Open a ticket"):
    subject = discord.ui.TextInput(
        label="Subject",
        placeholder="What is your ticket about?",
        required=True,
        max_length=100,
    )
    details = discord.ui.TextInput(
        label="Details",
        placeholder="Optional details...",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user
        data = charger_tickets()
        cle = str(guild.id)

        ouverts = data.get(cle, {}).get("open", {})
        if str(user.id) in ouverts:
            channel_id = ouverts[str(user.id)]
            ch = guild.get_channel(channel_id)
            if ch is not None:
                await interaction.response.send_message(
                    f"You already have an open ticket: {ch.mention}",
                    ephemeral=True,
                )
                return
            else:
                del ouverts[str(user.id)]

        category = guild.get_channel(TICKET_CATEGORY_ID)
        if category is None or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Ticket category not found. Please contact an administrator.",
                ephemeral=True,
            )
            return

        safe_name = re.sub(r"[^a-z0-9\-]", "", user.name.lower())[:20] or "user"
        channel_name = f"ticket-{safe_name}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                embed_links=True,
            ),
        }
        # Leader + Staff peuvent voir et répondre (IDs mis à jour)[cite: 2]
        for role_id in (1397320661243793480, 1410411003291631707):
            role = guild.get_role(role_id)
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                )

        try:
            ticket_channel = await category.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                reason=f"Ticket by {user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to create channels in the ticket category.",
                ephemeral=True,
            )
            return
        except Exception as e:
            await interaction.response.send_message(f"Error creating ticket: {e}", ephemeral=True)
            return

        if cle not in data:
            data[cle] = {"open": {}, "claimed": {}, "meta": {}}
        data[cle].setdefault("open", {})[str(user.id)] = ticket_channel.id
        data[cle].setdefault("meta", {})[str(ticket_channel.id)] = {
            "user_id": user.id,
            "subject": str(self.subject),
            "details": str(self.details) if self.details.value else "",
        }
        sauver_tickets(data)

        embed = discord.Embed(
            title="🎫 Ticket opened",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Subject", value=str(self.subject), inline=False)
        if self.details.value:
            embed.add_field(name="Details", value=str(self.details), inline=False)
        embed.add_field(name="Opened by", value=user.mention, inline=True)
        embed.set_footer(text=f"User ID: {user.id}")

        view = TicketControlView()
        await ticket_channel.send(
            content=(
                f"{user.mention} Welcome! A staff member will be with you shortly.\n"
                f"<@&1397320661243793480> <@&1410411003291631707>"
            ),
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
        )

        await interaction.response.send_message(
            f"Your ticket has been created: {ticket_channel.mention}",
            ephemeral=True,
        )
        await envoyer_log(
            guild,
            "🎫 Ticket opened",
            f"**User:** {user.mention} (`{user.id}`)\n"
            f"**Channel:** {ticket_channel.mention}\n"
            f"**Subject:** {self.subject}",
            discord.Color.blurple(),
        )


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open a ticket",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_open_btn",
        emoji="🎫",
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal())


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Claim ticket",
        style=discord.ButtonStyle.success,
        custom_id="ticket_claim_btn",
        emoji="✋",
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only administrators can claim tickets.", ephemeral=True
            )
            return

        data = charger_tickets()
        cle = str(interaction.guild.id)
        channel_id = str(interaction.channel.id)
        claimed = data.get(cle, {}).get("claimed", {})

        if channel_id in claimed:
            staff_id = claimed[channel_id]
            await interaction.response.send_message(
                f"This ticket is already claimed by <@{staff_id}>.",
                ephemeral=True,
            )
            return

        data.setdefault(cle, {}).setdefault("claimed", {})[channel_id] = interaction.user.id
        sauver_tickets(data)

        await interaction.response.send_message(
            f"✅ This ticket is now handled by {interaction.user.mention}."
        )
        await envoyer_log(
            interaction.guild,
            "✋ Ticket claimed",
            f"**Staff:** {interaction.user.mention} (`{interaction.user.id}`)\n"
            f"**Channel:** {interaction.channel.mention}",
            discord.Color.green(),
        )

    @discord.ui.button(
        label="Transcript",
        style=discord.ButtonStyle.secondary,
        custom_id="ticket_transcript_btn",
        emoji="📄",
    )
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only administrators can generate transcripts.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        data = charger_tickets()
        cle = str(interaction.guild.id)
        meta = data.get(cle, {}).get("meta", {}).get(str(interaction.channel.id), {})
        subject = meta.get("subject", "No subject")

        messages = []
        async for msg in interaction.channel.history(limit=500, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author = f"{msg.author} ({msg.author.id})"
            content = msg.content or ""
            if msg.embeds:
                content += " [embed]"
            if msg.attachments:
                content += " " + " ".join(a.url for a in msg.attachments)
            messages.append(f"[{ts}] {author}: {content}")

        transcript_text = (
            f"Ticket Transcript\n"
            f"Subject: {subject}\n"
            f"Channel: #{interaction.channel.name} ({interaction.channel.id})\n"
            f"Generated by: {interaction.user} ({interaction.user.id})\n"
            f"{'=' * 50}\n\n"
            + "\n".join(messages)
        )

        filename = f"transcript-{interaction.channel.id}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(transcript_text)

        admin_channel = interaction.guild.get_channel(TRANSCRIPT_CHANNEL_ID)
        if admin_channel is None:
            await interaction.followup.send("Transcript channel not found.", ephemeral=True)
            try:
                os.remove(filename)
            except OSError:
                pass
            return

        embed = discord.Embed(
            title="📄 Ticket Transcript",
            description=f"**Subject:** {subject}\n**Channel:** {interaction.channel.mention}",
            color=discord.Color.dark_grey(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Saved by {interaction.user}")

        try:
            await admin_channel.send(
                embed=embed,
                file=discord.File(filename, filename=filename),
            )
            await interaction.followup.send("Transcript sent to the admin channel.", ephemeral=True)
            await envoyer_log(
                interaction.guild,
                "📄 Transcript saved",
                f"**Staff:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                f"**Channel:** {interaction.channel.mention}\n"
                f"**Subject:** {subject}",
                discord.Color.dark_grey(),
            )
        except Exception as e:
            await interaction.followup.send(f"Failed to send transcript: {e}", ephemeral=True)
        finally:
            try:
                os.remove(filename)
            except OSError:
                pass

    @discord.ui.button(
        label="Close ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_close_btn",
        emoji="🔒",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = charger_tickets()
        cle = str(interaction.guild.id)
        meta = data.get(cle, {}).get("meta", {}).get(str(interaction.channel.id), {})
        owner_id = meta.get("user_id")

        is_admin = interaction.user.guild_permissions.administrator
        is_owner = owner_id is not None and interaction.user.id == owner_id

        if not (is_admin or is_owner):
            await interaction.response.send_message(
                "Only the ticket owner or an administrator can close this ticket.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "🔒 This ticket will be closed in 5 seconds..."
        )

        channel_name = interaction.channel.name
        channel_id = interaction.channel.id
        subject = meta.get("subject", "No subject")

        await asyncio.sleep(5)

        data = charger_tickets()
        cle = str(interaction.guild.id)
        if cle in data:
            open_map = data[cle].get("open", {})
            to_del = [uid for uid, cid in open_map.items() if cid == channel_id]
            for uid in to_del:
                del open_map[uid]
            data[cle].get("claimed", {}).pop(str(channel_id), None)
            data[cle].get("meta", {}).pop(str(channel_id), None)
            sauver_tickets(data)

        await envoyer_log(
            interaction.guild,
            "🔒 Ticket closed",
            f"**Closed by:** {interaction.user.mention} (`{interaction.user.id}`)\n"
            f"**Channel:** #{channel_name} (`{channel_id}`)\n"
            f"**Subject:** {subject}",
            discord.Color.red(),
        )

        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException as e:
            print(f"[TICKET] Impossible de supprimer le salon : {e}")


@tree.command(
    name="setupticket",
    description="Create the ticket panel in a channel",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    salon="Name of the channel where the ticket panel will be posted",
)
async def setupticket(interaction: discord.Interaction, salon: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    channel = discord.utils.get(guild.text_channels, name=salon)
    if channel is None:
        try:
            channel = await guild.create_text_channel(
                name=salon[:100],
                reason="/setupticket",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I need the **Manage Channels** permission.",
                ephemeral=True,
            )
            return
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

    embed = discord.Embed(
        title="🎫 Support Tickets",
        description=(
            "Here you can create a ticket to ask the staff for information.\n\n"
            "Click the button below to open a ticket."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="One open ticket per person maximum")

    view = TicketPanelView()
    await channel.send(embed=embed, view=view)

    await interaction.followup.send(
        f"Ticket panel created in {channel.mention}.",
        ephemeral=True,
    )


# ---------- FORMATAGE ----------
def formater(n):
    # nombre exact, avec un espace pour les milliers (ex : 9 969)
    return f"{n:,}".replace(",", " ")


# ============================================================
# VOCAUX PRIVÉS AVEC SALLE D'ATTENTE
# ============================================================

VOCAL_CONFIG_FILE = "vocaux_config.json"


def charger_config_vocaux():
    try:
        return storage.charger(VOCAL_CONFIG_FILE)
    except Exception as e:
        print(f"[VOCAL] Erreur chargement config : {e}")
        return {}


def sauvegarder_config_vocaux():
    try:
        storage.sauver(VOCAL_CONFIG_FILE, VOCAL_CONFIG)
    except Exception as e:
        print(f"[VOCAL] Erreur sauvegarde config : {e}")


VOCAL_CONFIG = charger_config_vocaux()
SALONS_VOCAUX = {}
DEMANDES_VOCAL = {}


def _est_salle_attente(channel: discord.abc.GuildChannel) -> bool:
    """True si le salon suit la convention de nom des salles d'attente."""
    return isinstance(channel, discord.VoiceChannel) and channel.name.lower().startswith("waiting room of ")


def _est_salon_prive_nom(channel: discord.abc.GuildChannel) -> bool:
    """True si le salon suit la convention de nom des vocaux privés (...'s vc)."""
    return isinstance(channel, discord.VoiceChannel) and channel.name.endswith("'s vc")


def _owner_depuis_overwrites(channel: discord.VoiceChannel):
    """Retrouve le propriétaire via les permissions du salon (membre avec accès dédié)."""
    bot_id = client.user.id if client.user else None
    candidats = []
    for target, ow in channel.overwrites.items():
        if not isinstance(target, discord.Member):
            continue
        if bot_id is not None and target.id == bot_id:
            continue
        if ow.view_channel is True and ow.connect is True:
            candidats.append(target.id)
    if len(candidats) == 1:
        return candidats[0]
    if candidats:
        return candidats[0]
    return None


def _trouver_paire_vocale(category: discord.CategoryChannel, channel: discord.VoiceChannel):
    """À partir d'un salon (privé ou attente), retrouve (room, waiting, owner_id) dans la catégorie."""
    if _est_salle_attente(channel):
        suffix = channel.name[len("waiting room of "):].strip()
        room = None
        for ch in category.voice_channels:
            if ch.id == channel.id:
                continue
            if ch.name == f"{suffix}'s vc":
                room = ch
                break
        waiting = channel
    elif _est_salon_prive_nom(channel):
        base = channel.name[:-len("'s vc")].strip()
        waiting = None
        for ch in category.voice_channels:
            if ch.id == channel.id:
                continue
            if ch.name == f"waiting room of {base}":
                waiting = ch
                break
        room = channel
    else:
        return None

    if room is None:
        return None

    owner_id = _owner_depuis_overwrites(room)
    if owner_id is None and room.members:
        for m in room.members:
            if not m.bot:
                owner_id = m.id
                break
    return room, waiting, owner_id


def _enregistrer_paire_vocale(guild_id: int, room: discord.VoiceChannel, waiting, owner_id: int):
    SALONS_VOCAUX[room.id] = {
        "owner_id": owner_id,
        "waiting_id": waiting.id if waiting else None,
        "guild_id": guild_id,
        "waiting": False,
    }
    if waiting is not None:
        SALONS_VOCAUX[waiting.id] = {
            "owner_id": owner_id,
            "room_id": room.id,
            "waiting": True,
            "guild_id": guild_id,
        }


async def reconstruire_vocaux_guild(guild: discord.Guild):
    """Au démarrage : réassocie les vocaux encore occupés et supprime les vocaux privés vides
    (et leurs salles d'attente) laissés orphelins après un redémarrage / mise à jour."""
    config = VOCAL_CONFIG.get(str(guild.id))
    if not config:
        return

    category = guild.get_channel(config.get("category_id"))
    if category is None or not isinstance(category, discord.CategoryChannel):
        print(f"[VOCAL] Catégorie introuvable sur {guild.name} — reconstruction ignorée.")
        return

    trigger_id = config.get("trigger_id")
    vus = set()
    supprimes = 0
    restaures = 0

    for channel in list(category.voice_channels):
        if channel.id == trigger_id or channel.id in vus:
            continue
        if not (_est_salle_attente(channel) or _est_salon_prive_nom(channel)):
            continue

        paire = _trouver_paire_vocale(category, channel)
        if paire is None:
            if _est_salle_attente(channel) and len(channel.members) == 0:
                try:
                    await channel.delete(reason="Orphan waiting room cleanup on startup")
                    supprimes += 1
                except Exception as e:
                    print(f"[VOCAL] Suppression orphelin impossible ({channel.name}): {e}")
            continue

        room, waiting, owner_id = paire
        vus.add(room.id)
        if waiting is not None:
            vus.add(waiting.id)

        if len(room.members) == 0:
            for ch in (room, waiting):
                if ch is None:
                    continue
                try:
                    await ch.delete(reason="Empty private VC cleanup on startup")
                    supprimes += 1
                except Exception as e:
                    print(f"[VOCAL] Suppression vide impossible ({ch.name}): {e}")
            continue

        if owner_id is None:
            print(f"[VOCAL] Impossible de retrouver le propriétaire de {room.name}, ignoré.")
            continue
        _enregistrer_paire_vocale(guild.id, room, waiting, owner_id)
        restaures += 1

    if supprimes or restaures:
        print(
            f"[VOCAL] {guild.name}: {restaures} salon(s) privé(s) restauré(s), "
            f"{supprimes} salon(s) vide(s)/orphelin(s) supprimé(s)."
        )


async def reconstruire_tous_vocaux():
    for guild in client.guilds:
        try:
            await reconstruire_vocaux_guild(guild)
        except Exception as e:
            print(f"[VOCAL] Reconstruction échouée sur {getattr(guild, 'name', '?')}: {e}")



class DemandeVocalView(discord.ui.View):
    def __init__(self, guild_id, owner_id, user_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.user_id = user_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Seul le propriétaire du salon peut accepter cette personne.", ephemeral=True
            )
            return

        demande = DEMANDES_VOCAL.get(self.user_id)
        if not demande:
            await interaction.response.edit_message(content="❌ Cette demande n'existe plus.", view=None)
            return

        guild = client.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.edit_message(content="❌ Serveur introuvable.", view=None)
            return

        room = guild.get_channel(demande["room_id"])
        member = guild.get_member(self.user_id)
        if room is None or member is None:
            DEMANDES_VOCAL.pop(self.user_id, None)
            await interaction.response.edit_message(content="❌ Le salon ou l'utilisateur est introuvable.", view=None)
            return

        try:
            # Tant que cette permission n'est pas ajoutée, l'utilisateur
            # ne voit pas et ne peut pas rejoindre le salon privé.
            await room.set_permissions(
                member,
                view_channel=True,
                connect=True,
                speak=True,
                stream=True,
                send_messages=True,
                read_message_history=True,
            )
            await member.move_to(room)
            DEMANDES_VOCAL.pop(self.user_id, None)
            await interaction.response.edit_message(
                content=f"✅ **{member.display_name}** has been accepted into your private room.",
                view=None,
            )
        except discord.Forbidden:
            await interaction.response.edit_message(
                content="❌ Je n'ai pas les permissions nécessaires pour modifier les permissions ou déplacer cet utilisateur.",
                view=None,
            )
        except Exception as e:
            print(f"[VOCAL] Erreur acceptation : {e}")
            await interaction.response.edit_message(content="❌ Une erreur est survenue.", view=None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Seul le propriétaire du salon peut refuser cette personne.", ephemeral=True
            )
            return

        demande = DEMANDES_VOCAL.pop(self.user_id, None)
        if not demande:
            await interaction.response.edit_message(content="❌ Cette demande n'existe plus.", view=None)
            return

        guild = client.get_guild(self.guild_id)
        if guild:
            member = guild.get_member(self.user_id)
            waiting = guild.get_channel(demande["waiting_id"])
            if member and waiting:
                try:
                    if member.voice:
                        await member.move_to(waiting)
                except Exception:
                    pass

        await interaction.response.edit_message(content="❌ Demande refusée.", view=None)


async def creer_salon_prive(guild, owner):
    config = VOCAL_CONFIG.get(str(guild.id))
    if not config:
        return None

    # La catégorie est créée UNIQUEMENT par /setupvoc.
    # Si elle n'existe plus, on ne recrée pas une nouvelle catégorie à chaque vocal.
    category = guild.get_channel(config.get("category_id"))
    if category is None or not isinstance(category, discord.CategoryChannel):
        print(f"[VOCAL] Catégorie configurée introuvable sur {guild.name}. Relance /setupvoc.")
        return None

    # SALON PRIVÉ : invisible et inaccessible par défaut.
    overwrites_room = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
        owner: discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, stream=True, move_members=True,
            send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, stream=True,
            move_members=True, manage_channels=True,
            send_messages=True, read_message_history=True
        ),
    }

    room = await guild.create_voice_channel(
        name=f"{owner.display_name}'s vc",
        category=category,
        overwrites=overwrites_room,
    )

    # SALLE D'ATTENTE : visible et rejoignable par les membres.
    # Le salon privé, lui, reste totalement invisible jusqu'à l'acceptation.
    overwrites_waiting = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True, speak=False),
        owner: discord.PermissionOverwrite(view_channel=True, connect=True, speak=False, move_members=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, move_members=True, manage_channels=True
        ),
    }

    waiting = await guild.create_voice_channel(
        name=f"waiting room of {owner.display_name}",
        category=category,
        overwrites=overwrites_waiting,
    )

    SALONS_VOCAUX[room.id] = {
        "owner_id": owner.id,
        "waiting_id": waiting.id,
        "guild_id": guild.id,
        "waiting": False,
    }
    SALONS_VOCAUX[waiting.id] = {
        "owner_id": owner.id,
        "room_id": room.id,
        "waiting": True,
        "guild_id": guild.id,
    }

    return room, waiting


@tree.command(
    name="kickvoc",
    description="Kick someone from your private voice channel"
)
@app_commands.guild_only()
@app_commands.describe(user="The member to kick from your private voice channel")
async def kickvoc(interaction: discord.Interaction, user: discord.Member):
    guild = interaction.guild
    owner = interaction.user

    # Trouver le salon privé appartenant à l'utilisateur qui lance la commande.
    room = None
    for room_id, data in SALONS_VOCAUX.items():
        if data.get("guild_id") == guild.id and not data.get("waiting") and data.get("owner_id") == owner.id:
            room = guild.get_channel(room_id)
            break

    if room is None:
        await interaction.response.send_message(
            "❌ You do not have an active private voice channel.", ephemeral=True
        )
        return

    if user not in room.members:
        await interaction.response.send_message(
            f"❌ **{user.display_name}** is not in your private voice channel.", ephemeral=True
        )
        return

    if user.id == owner.id:
        await interaction.response.send_message(
            "❌ You cannot kick yourself from your own channel.", ephemeral=True
        )
        return

    try:
        # Retirer aussi l'autorisation d'accès pour qu'il ne puisse pas
        # revenir immédiatement dans le salon privé.
        await room.set_permissions(user, overwrite=None)
        await user.move_to(None, reason=f"Expulsé du vocal par {owner}")
        await interaction.response.send_message(
            f"✅ **{user.display_name}** has been kicked from your private voice channel.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I do not have permission to kick this member.", ephemeral=True
        )
    except Exception as e:
        print(f"[VOCAL] Erreur kick vocal : {e}")
        await interaction.response.send_message(
            "❌ An error occurred while kicking this member.", ephemeral=True
        )


@tree.command(
    name="setupvoc",
    description="Configure the private voice channel system"
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    trigger="The voice channel members join to create their private room"
)
async def setupvoc(interaction: discord.Interaction, trigger: discord.VoiceChannel):
    guild = interaction.guild

    # /setupvoc crée la catégorie une seule fois.
    # Si le système est déjà configuré et que la catégorie existe, on la réutilise.
    old_config = VOCAL_CONFIG.get(str(guild.id), {})
    category = guild.get_channel(old_config.get("category_id")) if old_config.get("category_id") else None

    if category is None or not isinstance(category, discord.CategoryChannel):
        try:
            category = await guild.create_category("🔊・VOCAUX PRIVÉS")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I do not have permission to create a category.", ephemeral=True
            )
            return
        except Exception as e:
            print(f"[VOCAL] Erreur création catégorie : {e}")
            await interaction.response.send_message("❌ Could not create the category.", ephemeral=True)
            return

    VOCAL_CONFIG[str(guild.id)] = {
        "trigger_id": trigger.id,
        "category_id": category.id,
    }
    sauvegarder_config_vocaux()

    await interaction.response.send_message(
        f"✅ **Private voice system configured!**\n\n"
        f"🎙️ Trigger channel: {trigger.mention}\n"
        f"📁 Category: **{category.name}**\n\n"
        f"When someone joins {trigger.mention}, a private room and a waiting room will be created automatically.",
        ephemeral=True,
    )


async def _vocal_nettoyage(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Supprime le salon privé ET la waiting room uniquement quand le salon est vide,
    quel que soit le dernier à quitter (le départ du propriétaire seul ne supprime rien).
    Fonctionne aussi pour les salons reconnus après un redémarrage."""
    if before.channel is None or before.channel == after.channel:
        return
    if not isinstance(before.channel, discord.VoiceChannel):
        return

    guild = member.guild
    data = SALONS_VOCAUX.get(before.channel.id)
    room = None
    waiting = None
    room_id = None
    waiting_id = None

    if data:
        if data.get("waiting"):
            room_id = data.get("room_id")
            waiting_id = before.channel.id
        else:
            room_id = before.channel.id
            waiting_id = data.get("waiting_id")
        room = guild.get_channel(room_id) if room_id else None
        waiting = guild.get_channel(waiting_id) if waiting_id else None
    else:
        config = VOCAL_CONFIG.get(str(guild.id))
        if not config:
            return
        category = before.channel.category
        if category is None or category.id != config.get("category_id"):
            return
        if not (_est_salle_attente(before.channel) or _est_salon_prive_nom(before.channel)):
            return
        paire = _trouver_paire_vocale(category, before.channel)
        if paire is None:
            return
        room, waiting, owner_id = paire
        if room is None:
            return
        room_id = room.id
        waiting_id = waiting.id if waiting else None
        if owner_id is not None:
            _enregistrer_paire_vocale(guild.id, room, waiting, owner_id)

    if room_id is None:
        return

    await asyncio.sleep(2)

    room = guild.get_channel(room_id)
    waiting = guild.get_channel(waiting_id) if waiting_id else None

    if room is not None and len(room.members) > 0:
        return

    SALONS_VOCAUX.pop(room_id, None)
    if waiting_id:
        SALONS_VOCAUX.pop(waiting_id, None)

    for user_id, demande in list(DEMANDES_VOCAL.items()):
        if demande.get("room_id") == room_id or demande.get("waiting_id") == waiting_id:
            DEMANDES_VOCAL.pop(user_id, None)

    if waiting:
        for waiting_member in list(waiting.members):
            try:
                await waiting_member.move_to(None)
            except Exception:
                pass

    if room:
        try:
            await room.delete(reason="Temporary voice channel finished")
        except Exception as e:
            print(f"[VOCAL] Erreur suppression salon : {e}")

    if waiting:
        try:
            await waiting.delete(reason="Temporary waiting room finished")
        except Exception as e:
            print(f"[VOCAL] Erreur suppression attente : {e}")

    print(f"[VOCAL] Salons de {member.display_name} supprimés (vocal + waiting room).")


@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    await _vocal_entrees(member, before, after)
    await _vocal_nettoyage(member, before, after)


async def _vocal_entrees(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    config = VOCAL_CONFIG.get(str(guild.id))
    if not config:
        return

    trigger_id = config.get("trigger_id")

    # 1. Le membre rejoint le vocal déclencheur.
    if after.channel and after.channel.id == trigger_id:
        for room_id, data in list(SALONS_VOCAUX.items()):
            if data.get("owner_id") == member.id and not data.get("waiting"):
                room = guild.get_channel(room_id)
                if room:
                    try:
                        await member.move_to(room)
                    except Exception as e:
                        print(f"[VOCAL] Erreur déplacement propriétaire : {e}")
                    return

        try:
            result = await creer_salon_prive(guild, member)
            if not result:
                return
            room, _waiting = result
            await member.move_to(room)
        except discord.Forbidden:
            print(f"[VOCAL] Permissions insuffisantes pour créer le salon sur {guild.name}")
        except Exception as e:
            print(f"[VOCAL] Erreur création salon : {e}")
        return

    # 2. Un membre rejoint une salle d'attente (connue en mémoire OU orpheline après redémarrage).
    if after.channel and isinstance(after.channel, discord.VoiceChannel):
        data = SALONS_VOCAUX.get(after.channel.id)
        room = None
        waiting = after.channel
        owner_id = None

        if data and data.get("waiting"):
            owner_id = data.get("owner_id")
            room_id = data.get("room_id")
            room = guild.get_channel(room_id) if room_id else None
        elif _est_salle_attente(after.channel):
            category = after.channel.category
            if category is not None:
                paire = _trouver_paire_vocale(category, after.channel)
                if paire is not None:
                    room, waiting, owner_id = paire
                    if room is not None and owner_id is not None:
                        _enregistrer_paire_vocale(guild.id, room, waiting, owner_id)

        if owner_id is None or room is None:
            return

        if member.id == owner_id or member.id in DEMANDES_VOCAL:
            return

        owner = guild.get_member(owner_id)
        if owner is None:
            return

        DEMANDES_VOCAL[member.id] = {
            "guild_id": guild.id,
            "owner_id": owner_id,
            "room_id": room.id,
            "waiting_id": waiting.id,
        }

        try:
            await room.send(
                f"🔔 **Access request to your room**\n\n"
                f"👤 **{member.display_name}** wants to join your private room.\n\n"
                f"📍 They are currently in **{after.channel.name}**.\n\n"
                f"Choose an action:",
                view=DemandeVocalView(guild.id, owner_id, member.id),
            )
        except discord.Forbidden:
            print(f"[VOCAL] Impossible d'envoyer la demande dans le chat de {room.name}.")
        except Exception as e:
            print(f"[VOCAL] Erreur envoi demande dans le chat vocal : {e}")
        return



# ---------- FUN (/fun, /stopfun) ----------
FUN_CIBLES = defaultdict(dict)     # guild_id -> {user_id: message personnalisé ou None} (en mémoire)
FUN_DERNIER = {}                   # (guild_id, user_id) -> timestamp de la dernière réponse
FUN_DERNIER_MODELE = {}            # (guild_id, user_id) -> index du dernier modèle (évite les répétitions)
FUN_COOLDOWN = 3                   # secondes minimum entre deux réponses pour un même membre

# {name} = pseudo du membre, {quote} = début de son message
FUN_MESSAGES = [
    "Oh look, **{name}** just spoke. Everybody act normal. 👀",
    "**{name}** said something. Bold move. 🫡",
    "Breaking news: **{name}** has entered the chat... again. 📰",
    "Ladies and gentlemen, **{name}** has something to say! 🎤",
    "Another message from **{name}**? The people demand more! 🍿",
    "**{name}** is typing... and the server will never be the same. ✨",
    "Noted, **{name}**. I'll pass that on to absolutely nobody. 📝",
    "I was about to say something, but then **{name}** spoke. Iconic. 😌",
    "The council has reviewed **{name}**'s message. Verdict: certified moment. ⚖️",
    "Somebody check on **{name}**, they're on a roll today. 🔥",
]
FUN_MESSAGES_CITATION = [
    "Did **{name}** just say \"{quote}\"? Legendary. 🏆",
    "**{name}** really typed \"{quote}\" and hit send. Respect. 🫡",
    "\"{quote}\" - **{name}**, 2026. Remember this day. 🗿",
    "Let the record show that **{name}** said: \"{quote}\" 📜",
]


async def fun_repondre(message: discord.Message, membre: discord.Member):
    """Si le membre est ciblé par /fun, le bot répond avec un message personnalisé."""
    cibles = FUN_CIBLES.get(message.guild.id)
    if not cibles or membre.id not in cibles:
        return

    cle = (message.guild.id, membre.id)
    maintenant = time.time()
    if maintenant - FUN_DERNIER.get(cle, 0) < FUN_COOLDOWN:
        return
    FUN_DERNIER[cle] = maintenant

    # Début du message (nettoyé : pas de mentions ni de mise en forme)
    extrait = " ".join((message.content or "").split())
    extrait = discord.utils.escape_markdown(discord.utils.escape_mentions(extrait))
    extrait = extrait.replace('"', "'")
    if len(extrait) > 60:
        extrait = extrait[:57] + "..."

    perso = cibles.get(membre.id)   # message choisi avec /fun (peut contenir plusieurs choix séparés par |)
    if perso:
        modeles = [m.strip() for m in perso.split("|") if m.strip()]
    else:
        modeles = list(FUN_MESSAGES)
        if extrait:
            modeles += FUN_MESSAGES_CITATION
    # on évite de répéter deux fois de suite le même modèle
    dernier = FUN_DERNIER_MODELE.get(cle)
    choix = [i for i in range(len(modeles)) if modeles[i] != dernier] or list(range(len(modeles)))
    modele = modeles[random.choice(choix)]
    FUN_DERNIER_MODELE[cle] = modele

    # replace (et non format) : le texte du staff peut contenir des { }
    texte = modele.replace("{name}", discord.utils.escape_markdown(membre.display_name))
    texte = texte.replace("{quote}", extrait)[:2000]
    try:
        await message.reply(texte, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        print(f"[FUN] Impossible d'envoyer la réponse : {e}")


@tree.command(name="fun", description="Choose what the bot says every time a member writes")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    user="The member the bot will react to",
    message="What the bot says. Use {name} for their name, {quote} for their message, | to give several choices",
)
async def fun_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    message: Optional[app_commands.Range[str, 1, 300]] = None,
):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("You need the **Manage Messages** permission.", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("You can't target a bot.", ephemeral=True)
        return
    deja = user.id in FUN_CIBLES[interaction.guild.id]
    FUN_CIBLES[interaction.guild.id][user.id] = message
    apercu = (
        f"I'll say: {discord.utils.escape_markdown(message)}" if message
        else "I'll use random messages."
    )
    debut = "updated" if deja else "enabled"
    await interaction.response.send_message(
        f"🎉 Fun mode {debut} for {user.mention}. {apercu}\nUse `/stopfun` to stop.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="stopfun", description="Stop the fun mode (for one member, or for everyone)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(user="The member to stop (leave empty to stop everyone)")
async def stopfun_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("You need the **Manage Messages** permission.", ephemeral=True)
        return
    cibles = FUN_CIBLES[interaction.guild.id]
    if user is not None:
        if user.id not in cibles:
            await interaction.response.send_message(f"Fun mode isn't active for {user.mention}.", ephemeral=True)
            return
        cibles.pop(user.id, None)
        await interaction.response.send_message(f"🛑 Fun mode stopped for {user.mention}.", ephemeral=True)
        return
    if not cibles:
        await interaction.response.send_message("Fun mode isn't active for anyone.", ephemeral=True)
        return
    nb = len(cibles)
    cibles.clear()
    await interaction.response.send_message(f"🛑 Fun mode stopped for everyone ({nb} member(s)).", ephemeral=True)


# ---------- HELP ----------
@tree.command(
    name="help",
    description="Show the commands available to every member",
)
@app_commands.guild_only()
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Member commands",
        description="Commands anyone can use on this server.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📊 Levels",
        value=(
            "`/rank [user]` — Show your level (or another member's)\n"
            "`/myrank` — Show your own level\n"
            "`/leaderboard` — Top 10 levels on the server"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎬 Media",
        value=(
            "`/dl [link]` — Download a TikTok / Streamable video\n"
            "`!dl` — Reply to a message with a link to download it\n"
            "`/songfinder fichier:` — Recognize a song from an audio/video file\n"
            "`!songfinder` — Reply to a message with an audio/video file to identify it"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔊 Private voice",
        value="`/kickvoc user:` — Kick someone from **your** private voice channel",
        inline=False,
    )
    embed.add_field(
        name="ℹ️ Help",
        value="`/help` — Show this message",
        inline=False,
    )
    embed.set_footer(text="Admins: use /adminhelp for the full command list")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(
    name="adminhelp",
    description="Show all admin / staff commands (administrators only)",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def adminhelp_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🛠️ Admin & staff commands",
        description="Full list of moderation and configuration commands.",
        color=discord.Color.dark_gold(),
    )
    embed.add_field(
        name="🔨 Moderation",
        value=(
            "`/ban` `/kick` `/timeout` — Sanction a member\n"
            "`/unban` `/untimeout` — Remove a sanction\n"
            "`/deletemessage` — Delete a member's messages from the last 5 minutes"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛡️ Anti-raid & anti-nuke",
        value=(
            "`/raidstatus` `/raidunlock` — Anti-raid status / unlock\n"
            "`/setupantinuke` — Enable anti-nuke\n"
            "`/antinuke_status` `/antinuke_whitelist` `/antinuke_snapshot` — Anti-nuke tools"
        ),
        inline=False,
    )
    embed.add_field(
        name="🚫 Filters & files",
        value=(
            "`/addmot` `/removemot` `/importmots` — Banned words list\n"
            "`/blockfile` `/unblockfile` `/listblocked` — Block premium file reuploads\n"
            "`/setuphoney` — Create the honeypot channel"
        ),
        inline=False,
    )
    embed.add_field(
        name="📈 Levels & stats",
        value=(
            "`/setuplevels` — Level-up announcement channel\n"
            "`/setxp` `/importlevels` — Set / import levels\n"
            "`/setupstat` `/checkmembers` — Role counters\n"
            "`/setupdsstat` `/dsstatnow` `/dsstatgraph` — Daily server report"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎫 Tickets & logs",
        value=(
            "`/setupticket` — Ticket panel\n"
            "`/setuplogs` — Bot log channel\n"
            "`/embed` `/button` — Custom embed & Accept rules button"
        ),
        inline=False,
    )
    embed.add_field(
        name="📱 TikTok & voice",
        value=(
            "`/tiktokadd` `/tiktokremove` `/tiktokcheck` — Follower milestone alerts\n"
            "`/setupvoc` — Private voice system"
        ),
        inline=False,
    )
    embed.add_field(
        name="📹 Video announcements",
        value=(
            "`/follow rss_link: channel:` — Announce the new videos of an RSS feed (RSSHub...) in a channel, checked every 5 min\n"
            "`/stopfollow feed:` — Stop following a feed\n"
            "`/look channel: name:` — (in the NotifyMe server) Save a Notify Me channel under a name\n"
            "`/post name: channel: [message:] [now:]` — Every 5 min, repost the new message of that Notify Me channel here with your message\n"
            "`/stoplook name:` `/stoppost name:` — Stop one"
        ),
        inline=False,
    )
    embed.add_field(
        name="📡 NotifyMe relay",
        value=(
            "`/relay source_channel_id: destination:` — Repost the TikTok alerts NotifyMe posts in another server here, in my colors\n"
            "`/stoprelay relay:` — Stop a relay"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎉 Fun",
        value=(
            "`/fun user: [message:]` — The bot answers every message of this member with the text you choose\n"
            "`/stopfun [user]` — Stop it (for one member, or everyone)"
        ),
        inline=False,
    )
    embed.add_field(
        name="👤 Also available to members",
        value="`/rank` `/myrank` `/leaderboard` `/dl` `/songfinder` `!dl` `!songfinder` `/kickvoc` `/help`",
        inline=False,
    )
    embed.set_footer(text="All command descriptions are in English in the Discord UI")
    await interaction.response.send_message(embed=embed, ephemeral=True)



# ---------- /post : relais Notify Me -> serveur principal (script relay_notifyme.py) ----------
# /post et /stoppost deviennent des commandes de ce bot ; la boucle (toutes les 5 min) démarre dans setup_hook.
try:
    import relay_notifyme
    relay_notifyme.installer(client, tree)
except Exception as e:
    relay_notifyme = None
    print(f"[POST] relay_notifyme.py non chargé : {e!r}")


TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("[ERREUR] Variable DISCORD_TOKEN manquante (Render : onglet Environment).")


async def principal():
    """Lance le bot. Si la connexion échoue (429 de Discord, token invalide, panne...), on ATTEND
    avant de quitter : quitter tout de suite fait redémarrer Render en boucle, et chaque
    redémarrage retente la connexion, ce qui prolonge un blocage 429."""
    global etat_connexion
    import traceback
    discord.utils.setup_logging()
    await demarrer_serveur_web()  # port ouvert tout de suite (Render), même si la connexion échoue
    attente = None
    try:
        async with client:
            await client.start(TOKEN)
    except discord.HTTPException as e:
        if e.status == 429:
            retry = None
            try:
                retry = float(e.response.headers.get("Retry-After", ""))
            except (AttributeError, TypeError, ValueError):
                pass
            attente = int(min(max(retry + 60, 1800) if retry else 1800, 3600))
            etat_connexion = f"Discord bloque l'adresse IP du serveur (429). Prochain essai dans {attente // 60} min."
            print(f"[ERREUR] Discord bloque la connexion (429). Pause de {attente // 60} min avant le prochain essai.")
        else:
            attente = 300
            etat_connexion = f"Erreur Discord {e.status} : {e}. Prochain essai dans 5 min."
            traceback.print_exc()
    except Exception as e:
        attente = 300
        etat_connexion = f"Erreur : {type(e).__name__}: {e}. Prochain essai dans 5 min."
        traceback.print_exc()
    if _script_relais_a_lancer:
        lancer_script_relais(_script_relais_a_lancer)  # ne revient pas (sauf Windows)
        return
    if attente:
        await asyncio.sleep(attente)
        raise SystemExit(1)  # Render relance le bot : un seul essai de connexion à chaque fois


try:
    asyncio.run(principal())
except KeyboardInterrupt:
    pass
