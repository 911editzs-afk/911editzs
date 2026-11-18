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


async def demarrer_serveur_web():
    """Mini serveur web : Render (offre Web Service) exige un port ouvert, et un site de ping
    (UptimeRobot...) peut l'appeler pour empêcher le bot de s'endormir. Inactif sans variable PORT."""
    port = os.getenv("PORT")
    if not port:
        return
    from aiohttp import web

    async def ok(_request):
        return web.Response(text="Bot en ligne ✅")

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
        await demarrer_serveur_web()  # utile seulement sur Render (Web Service)
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

CHANNEL_ID = 1420172051448201216  # salon réception mis à jour[cite: 2]
DUREE_TIMEOUT = timedelta(hours=1)
SPAM_MAX_MESSAGES = 5            # plus de 5 messages...
SPAM_FENETRE = 5                 # ...en moins de 5 secondes
DUREE_TIMEOUT_SPAM = timedelta(hours=1)
SPAM_ANALYSE = 30                # le bot relit les 30 dernières secondes pour tout effacer
FICHIER_MOTS = "mots_interdits.txt"  # liste LDNOOBW fusionnée (un mot par ligne)

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


def charger_mots():
    mots = list(MOTS_SUPPLEMENTAIRES)
    if os.path.exists(FICHIER_MOTS):
        with open(FICHIER_MOTS, "r", encoding="utf-8") as f:
            mots += [l.strip() for l in f if l.strip() and not l.startswith("#")]
    else:
        print(f"[ATTENTION] {FICHIER_MOTS} introuvable : seule la liste de base est utilisée.")
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
_vus = set()
for _m in charger_mots():
    _n = normaliser(_m).strip()
    if _n and _n not in _vus and _n not in MOTS_AUTORISES:
        _vus.add(_n)
        MOTIFS.append((construire_motif(_m), _n))
        MOTS_NORM.append(_n)


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


async def get_salon(guild):
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


async def synchroniser(guild):
    """Enregistre les commandes slash uniquement sur ce serveur (pas de doublons)."""
    try:
        for c in COMMANDES:
            tree.add_command(c, guild=guild, override=True)
        commandes = await tree.sync(guild=guild)
        print(f"[INFO] {len(commandes)} commande(s) slash enregistrée(s) sur {guild.name}")
    except discord.Forbidden:
        print(
            f"[ERREUR] Accès refusé sur {guild.name} : réinvite le bot avec la case "
            f"'applications.commands' cochée."
        )
    except Exception as e:
        print(f"[ERREUR] Synchronisation impossible sur {guild.name} : {e}")


@client.event
async def on_ready():
    global nettoyage_fait, honey_view_registered
    print(f"Connecté en tant que {client.user} | {len(MOTIFS)} mots interdits chargés")
    await client.change_presence(status=discord.Status.online, activity=discord.CustomActivity(name=STATUT))
    if not honey_view_registered:
        client.add_view(HoneypotCounterView())
        honey_view_registered = True
    if not nettoyage_fait:
        nettoyage_fait = True
        COMMANDES.extend(tree.get_commands())
        # Supprime les anciennes commandes globales (c'est elles qui créaient le doublon)
        tree.clear_commands(guild=None)
        try:
            await tree.sync()
            print("[INFO] Anciennes commandes globales supprimées (anti-doublon)")
        except Exception as e:
            print(f"[ERREUR] Nettoyage des commandes globales impossible : {e}")
    for guild in client.guilds:
        await synchroniser(guild)


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
    if announce is None:
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


@tree.command(name="setupstat", description="Crée la catégorie de statistiques avec 3 compteurs de rôles")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    role1="Premier rôle à compter", texte1="Texte affiché pour le premier compteur",
    role2="Deuxième rôle à compter", texte2="Texte affiché pour le deuxième compteur",
    role3="Troisième rôle à compter", texte3="Texte affiché pour le troisième compteur",
    categorie="Nom de la catégorie (facultatif)",
)
async def setupstat(
    interaction: discord.Interaction,
    role1: discord.Role, texte1: str,
    role2: discord.Role, texte2: str,
    role3: discord.Role, texte3: str,
    categorie: str = "📊 Server Stats",
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Réservé aux administrateurs.", ephemeral=True)
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


@tree.command(name="setuphoney", description="Crée un salon piège : celui qui y écrit est banni puis débanni")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def setuphoney(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Réservé aux administrateurs.", ephemeral=True)
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


# ---------- FILTRE ----------
@client.event
async def on_message(message):
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

    if message.content == "!ping":
        await message.channel.send("Pong !")

        

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


class DemandeVocalView(discord.ui.View):
    def __init__(self, guild_id, owner_id, user_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.user_id = user_id

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.success, emoji="✅")
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
                content=f"✅ **{member.display_name}** a été accepté dans ton salon privé.",
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

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.danger, emoji="❌")
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
        name=f"🔒・Salon de {owner.display_name}",
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
    description="Expulse une personne de ton salon vocal privé."
)
@app_commands.guild_only()
@app_commands.describe(user="La personne à expulser de ton vocal privé.")
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
            "❌ Tu n'as pas de salon vocal privé actif.", ephemeral=True
        )
        return

    if user not in room.members:
        await interaction.response.send_message(
            f"❌ **{user.display_name}** n'est pas dans ton salon vocal.", ephemeral=True
        )
        return

    if user.id == owner.id:
        await interaction.response.send_message(
            "❌ Tu ne peux pas t'expulser toi-même de ton propre salon.", ephemeral=True
        )
        return

    try:
        # Retirer aussi l'autorisation d'accès pour qu'il ne puisse pas
        # revenir immédiatement dans le salon privé.
        await room.set_permissions(user, overwrite=None)
        await user.move_to(None, reason=f"Expulsé du vocal par {owner}")
        await interaction.response.send_message(
            f"✅ **{user.display_name}** a été expulsé de ton salon vocal.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Je n'ai pas les permissions nécessaires pour expulser cette personne.", ephemeral=True
        )
    except Exception as e:
        print(f"[VOCAL] Erreur kick vocal : {e}")
        await interaction.response.send_message(
            "❌ Une erreur est survenue pendant l'expulsion.", ephemeral=True
        )


@tree.command(
    name="setupvoc",
    description="Configure le système de salons vocaux privés."
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    trigger="Le salon vocal que les membres rejoignent pour créer leur salon privé."
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
                "❌ Je n'ai pas la permission de créer une catégorie.", ephemeral=True
            )
            return
        except Exception as e:
            print(f"[VOCAL] Erreur création catégorie : {e}")
            await interaction.response.send_message("❌ Impossible de créer la catégorie.", ephemeral=True)
            return

    VOCAL_CONFIG[str(guild.id)] = {
        "trigger_id": trigger.id,
        "category_id": category.id,
    }
    sauvegarder_config_vocaux()

    await interaction.response.send_message(
        f"✅ **Système vocal configuré !**\n\n"
        f"🎙️ Vocal déclencheur : {trigger.mention}\n"
        f"📁 Catégorie : **{category.name}**\n\n"
        f"Quand quelqu'un rejoint {trigger.mention}, un salon privé et une salle d'attente seront créés automatiquement.",
        ephemeral=True,
    )


@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

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

    # 2. Un membre rejoint une salle d'attente.
    if after.channel and after.channel.id in SALONS_VOCAUX:
        data = SALONS_VOCAUX.get(after.channel.id)
        if not data or not data.get("waiting"):
            return

        owner_id = data.get("owner_id")
        room_id = data.get("room_id")
        waiting_id = after.channel.id

        if member.id == owner_id or member.id in DEMANDES_VOCAL:
            return

        owner = guild.get_member(owner_id)
        if owner is None:
            return

        DEMANDES_VOCAL[member.id] = {
            "guild_id": guild.id,
            "owner_id": owner_id,
            "room_id": room_id,
            "waiting_id": waiting_id,
        }

        room = guild.get_channel(room_id)
        if room is None:
            DEMANDES_VOCAL.pop(member.id, None)
            return

        try:
            # La demande est envoyée dans le chat du vocal privé, pas en DM.
            # Le propriétaire voit donc la demande directement dans le chat
            # de son salon vocal et peut accepter/refuser avec les boutons.
            await room.send(
                f"🔔 **Demande d'accès à ton salon**\n\n"
                f"👤 **{member.display_name}** veut rejoindre ton salon privé.\n\n"
                f"📍 Il/elle est actuellement dans **{after.channel.name}**.\n\n"
                f"Choisis une action :",
                view=DemandeVocalView(guild.id, owner_id, member.id),
            )
        except discord.Forbidden:
            print(f"[VOCAL] Impossible d'envoyer la demande dans le chat de {room.name}.")
        except Exception as e:
            print(f"[VOCAL] Erreur envoi demande dans le chat vocal : {e}")
        return

    # 3. Le propriétaire quitte son salon : suppression des salons temporaires.
    if before.channel and before.channel.id in SALONS_VOCAUX:
        data = SALONS_VOCAUX.get(before.channel.id)
        if not data or data.get("owner_id") != member.id:
            return

        if data.get("waiting"):
            room_id = data.get("room_id")
            waiting_id = before.channel.id
        else:
            room_id = before.channel.id
            waiting_id = data.get("waiting_id")

        room = guild.get_channel(room_id)
        waiting = guild.get_channel(waiting_id)

        await asyncio.sleep(2)

        if room and any(m.id == member.id for m in room.members):
            return

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
                await room.delete(reason="Salon vocal temporaire terminé")
            except Exception as e:
                print(f"[VOCAL] Erreur suppression salon : {e}")

        if waiting:
            try:
                await waiting.delete(reason="Salle d'attente temporaire terminée")
            except Exception as e:
                print(f"[VOCAL] Erreur suppression attente : {e}")

        SALONS_VOCAUX.pop(room_id, None)
        SALONS_VOCAUX.pop(waiting_id, None)
        print(f"[VOCAL] Salon de {member.display_name} supprimé.")


TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("[ERREUR] Variable DISCORD_TOKEN manquante (Render : onglet Environment).")
client.run(TOKEN)
