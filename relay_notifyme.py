"""
relay_notifyme.py — relais « Notify Me » → serveur principal, avec des salons nommés.

Deux façons de l'utiliser :
  1. Bot.py l'importe : /look, /post... deviennent des commandes du bot.
  2. Lancé seul (python relay_notifyme.py), ou lancé par Bot.py quand le bot n'est PAS dans le serveur
     principal : le script a son propre bot avec les mêmes commandes.

Principe (administrateurs) :
  1. Dans le serveur où NotifyMe poste :   /look channel:#salon name:ma-chaine
     -> enregistre ce salon sous un nom. Tu peux faire plusieurs /look (un par salon Notify Me).
  2. Dans le serveur principal :           /post name:ma-chaine channel:#annonces message:"..."
     -> relie ce nom à un salon d'annonce. Le même nom relie le /look et le /post.
  3. Toutes les 5 minutes, le bot regarde les salons « look ». Dès qu'un NOUVEAU message y apparaît, il le
     republie dans le salon du /post correspondant, avec TON message (variables : {link} {title} {text}).

Autres commandes : /stoplook name:  et  /stoppost name:

Aucun ID en dur.
"""
import asyncio
import json
import os
import re
import unicodedata
from collections import deque
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks


def charger_env():
    """En local : lit DISCORD_TOKEN.env / .env. Sur Render : les variables sont déjà là."""
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

try:
    import storage  # même stockage que Bot.py (MongoDB sur Render, fichiers .json en local)
except Exception as _e:  # storage.py absent : on retombe sur un simple fichier JSON
    storage = None
    print(f"[POST] storage indisponible ({_e}) : utilisation d'un fichier local.")


# ---------- RÉGLAGES ----------
INTERVALLE_MIN = 5                      # fréquence de vérification
MAX_POSTS_PAR_PASSAGE = 3               # anti-flood : max de messages republiés d'un coup
FICHIER_CONFIG = "post_config.json"
VERSION_COMMANDES = "2"                 # à changer si tu modifies les commandes (force la re-synchronisation)
MESSAGE_DEFAUT = "Yoo @everyone I just posted a new video!\n{link}"

RE_TIKTOK = re.compile(r"https?://(?:[\w-]+\.)?tiktok\.com/[^\s<>\"')\]]+", re.I)
RE_LIEN = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


# ---------- STOCKAGE ----------
CONFIG = None
VERROU = asyncio.Lock()
DEJA_POSTE = deque(maxlen=100)          # (salon, lien) déjà republiés : évite les doublons


def _lire() -> dict:
    if storage is not None:
        try:
            data = storage.charger(FICHIER_CONFIG)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"[POST] Lecture impossible : {e}")
            return {}
    try:
        with open(FICHIER_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def sauver():
    if storage is not None:
        storage.sauver(FICHIER_CONFIG, CONFIG)
        return
    with open(FICHIER_CONFIG, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)


def _section(nom: str) -> dict:
    global CONFIG
    if CONFIG is None:
        CONFIG = _lire()
    return CONFIG.setdefault(nom, {})


def looks() -> dict:
    """{clé du nom: {name, source_id, guild_id}} : les salons Notify Me enregistrés avec /look."""
    return _section("looks")


def routes() -> dict:
    """{'<serveur>_<clé du nom>': {guild_id, name, look, channel_id, message, last_id, watched_id}} : les /post."""
    return _section("routes")


def cle_nom(nom: str) -> str:
    """'Ma Chaîne TikTok!' -> 'ma-chaine-tiktok' (même clé pour /look et /post, sans tenir compte des accents/majuscules)."""
    n = unicodedata.normalize("NFKD", nom).lower()
    n = "".join(c for c in n if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", n).strip("-")


def cle_route(guild_id: int, nom: str) -> str:
    return f"{guild_id}_{cle_nom(nom)}"


# ---------- OUTILS PURS ----------
def extraire(message):
    """Lit un message (texte + embeds). Retourne (lien, titre, texte).
    lien = lien TikTok s'il y en a un, sinon le premier lien du message, sinon ''."""
    parties = [message.content or ""]
    titre = ""
    for e in message.embeds:
        parties += [e.url or "", e.title or "", e.description or ""]
        for f in e.fields:
            parties.append(f"{f.name} {f.value}")
        titre = titre or (e.title or "")
    texte = "\n".join(p for p in parties if p).strip()
    m = RE_TIKTOK.search(texte) or RE_LIEN.search(texte)
    lien = m.group(0).rstrip(".,;!") if m else ""
    return lien, titre.strip(), texte


def formater(modele: str, lien: str, titre: str, texte: str) -> str:
    return (
        modele.replace("{link}", lien)
        .replace("{title}", titre)
        .replace("{text}", texte)
    )[:2000]


async def derniere_id(salon) -> int:
    async for m in salon.history(limit=1):
        return m.id
    return 0


async def poster(dest, cfg: dict, message, forcer=False) -> bool:
    """Envoie dans `dest` le message personnalisé pour ce message de Notify Me."""
    lien, titre, texte = extraire(message)
    cle = (dest.id, lien)
    if lien and not forcer:
        if cle in DEJA_POSTE:
            return False
        DEJA_POSTE.append(cle)
    contenu = formater(cfg.get("message") or MESSAGE_DEFAUT, lien, titre, texte)
    if not contenu.strip():
        return False
    try:
        await dest.send(
            contenu,
            allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True),
        )
        print(f"[POST] Annonce envoyée dans #{dest.name} ({dest.guild.name}) : {lien or '(sans lien)'}")
        return True
    except discord.Forbidden:
        print(f"[POST] Pas la permission d'écrire dans #{dest.name}.")
    except discord.HTTPException as e:
        print(f"[POST] Erreur d'envoi : {e}")
    return False


# ---------- CLIENT (fourni par Bot.py, ou créé en mode autonome) ----------
client = None
tree = None

# ---------- SYNCHRONISATION DES COMMANDES (par serveur, sans doublons) ----------
def etat_sync() -> dict:
    s = _section("sync")
    if s.get("version") != VERSION_COMMANDES or os.getenv("FORCE_SYNC") == "1":
        s["version"] = VERSION_COMMANDES
        s["guilds"] = []
    s.setdefault("guilds", [])
    return s


async def synchroniser(guild):
    s = etat_sync()
    if guild.id in s["guilds"]:
        return
    try:
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)  # remplace les commandes du serveur par /post et /stoppost
        s["guilds"].append(guild.id)
        sauver()
        print(f"[INFO] Commandes /look /stoplook /post /stoppost enregistrées sur {guild.name}")
    except discord.Forbidden:
        print(f"[ERREUR] Accès refusé sur {guild.name} : réinvite le bot avec 'applications.commands'.")
    except Exception as e:
        print(f"[ERREUR] Synchronisation impossible sur {guild.name} : {e}")


# ---------- SURVEILLANCE TOUTES LES 5 MINUTES ----------
@tasks.loop(minutes=INTERVALLE_MIN)
async def verif_notify():
    try:
        async with VERROU:
            modifie = False
            for cle, cfg in list(routes().items()):
                look = looks().get(cfg.get("look", ""))
                if look is None:
                    print(f"[POST] « {cfg.get('name')} » : le /look de ce nom n'existe plus.")
                    continue
                source = client.get_channel(look["source_id"])
                dest = client.get_channel(cfg.get("channel_id") or 0)
                if source is None or dest is None:
                    print(f"[POST] « {cfg.get('name')} » : salon Notify Me ou salon d'annonce introuvable.")
                    continue

                if cfg.get("last_id") is None or cfg.get("watched_id") != source.id:
                    # Première observation (ou /look déplacé) : on note le dernier message sans rien poster
                    cfg["last_id"] = await derniere_id(source)
                    cfg["watched_id"] = source.id
                    modifie = True
                    continue

                try:
                    nouveaux = [
                        m async for m in source.history(
                            limit=20, after=discord.Object(id=cfg["last_id"]), oldest_first=True
                        )
                    ]
                except discord.Forbidden:
                    print(f"[POST] Je ne peux pas lire l'historique de #{source.name} ({source.guild.name}).")
                    continue
                if not nouveaux:
                    continue

                cfg["last_id"] = nouveaux[-1].id
                modifie = True
                a_poster = [
                    m for m in nouveaux
                    if m.author.id != client.user.id and (m.content or m.embeds)
                ][-MAX_POSTS_PAR_PASSAGE:]
                for m in a_poster:
                    await poster(dest, cfg, m)
                    await asyncio.sleep(1)
            if modifie:
                sauver()
    except Exception as e:  # la boucle ne doit jamais s'arrêter
        import traceback
        print(f"[POST] Erreur dans la vérification : {e!r}")
        traceback.print_exc()


@verif_notify.before_loop
async def _avant_verif_notify():
    await client.wait_until_ready()


# ---------- COMMANDES ----------
async def _est_admin_de(guild, user_id) -> bool:
    membre = guild.get_member(user_id)
    if membre is None:
        try:
            membre = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return False
    return bool(membre.guild_permissions.administrator or guild.owner_id == membre.id)


@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    channel="A channel where NotifyMe posts your videos",
    name="A name for it (you will use the same name in /post)",
)
async def look_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    name: app_commands.Range[str, 1, 32],
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    cle = cle_nom(name)
    if not cle:
        await interaction.response.send_message("❌ Invalid name: use letters or numbers.", ephemeral=True)
        return
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.read_message_history):
        await interaction.response.send_message(
            f"❌ In {channel.mention} I need **View Channel** and **Read Message History**.", ephemeral=True
        )
        return
    async with VERROU:
        existait = cle in looks()
        looks()[cle] = {"name": name.strip(), "source_id": channel.id, "guild_id": interaction.guild.id}
        try:
            sauver()
        except Exception as e:
            await interaction.response.send_message(f"❌ Storage error: {e}", ephemeral=True)
            return
    await interaction.response.send_message(
        f"✅ {'Updated' if existait else 'Saved'}: **{name.strip()}** → {channel.mention}.\n"
        f"Now, in the server where you want the announcement, run `/post name:{name.strip()} channel: message:`.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(name="The look to remove")
async def stoplook_cmd(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    cle = cle_nom(name)
    async with VERROU:
        look = looks().get(cle)
        if look is None or look.get("guild_id") != interaction.guild.id:
            await interaction.response.send_message("❌ No look with this name in this server.", ephemeral=True)
            return
        looks().pop(cle)
        try:
            sauver()
        except Exception as e:
            await interaction.response.send_message(f"❌ Storage error: {e}", ephemeral=True)
            return
    await interaction.response.send_message(
        f"✅ **{look['name']}** is no longer watched.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    name="The name you gave to the Notify Me channel with /look",
    channel="Channel of this server where the announcement is posted",
    message="Your message. Use {link}, {title} and {text}. Optional.",
    now="Also post the latest Notify Me message right now, to test. Optional.",
)
async def post_cmd(
    interaction: discord.Interaction,
    name: str,
    channel: discord.TextChannel,
    message: Optional[app_commands.Range[str, 1, 1500]] = None,
    now: bool = False,
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    guild = interaction.guild
    cle = cle_nom(name)
    look = looks().get(cle)
    if look is None:
        noms = ", ".join(f"`{l['name']}`" for l in looks().values()) or "none yet"
        await interaction.response.send_message(
            f"❌ No look named **{name.strip()}**. Available: {noms}.\n"
            "Create one with `/look` in the server where NotifyMe posts.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    src = client.get_channel(look["source_id"])
    if not isinstance(src, discord.TextChannel):
        await interaction.response.send_message(
            f"❌ I can't see the channel of **{look['name']}** anymore. Run `/look` again.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    if src.id == channel.id:
        await interaction.response.send_message("❌ The Notify Me channel and the announcement channel must be different.", ephemeral=True)
        return

    # Sécurité : pour lire un autre serveur, il faut y être administrateur aussi
    if src.guild.id != guild.id and not await _est_admin_de(src.guild, interaction.user.id):
        await interaction.response.send_message(
            f"❌ You must also be an **administrator of {src.guild.name}** to read its channel.", ephemeral=True
        )
        return

    p_src = src.permissions_for(src.guild.me)
    if not (p_src.view_channel and p_src.read_message_history):
        await interaction.response.send_message(
            f"❌ In **#{src.name}** ({src.guild.name}) I need **View Channel** and **Read Message History**.", ephemeral=True
        )
        return
    p_dest = channel.permissions_for(guild.me)
    if not (p_dest.view_channel and p_dest.send_messages):
        await interaction.response.send_message(
            f"❌ In {channel.mention} I need **View Channel** and **Send Messages**.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    async with VERROU:
        rk = cle_route(guild.id, name)
        ancien = routes().get(rk, {})
        cfg = {
            "guild_id": guild.id,
            "name": look["name"],
            "look": cle,
            "channel_id": channel.id,
            "message": message if message is not None else (ancien.get("message") or MESSAGE_DEFAUT),
            "last_id": ancien.get("last_id"),
            "watched_id": ancien.get("watched_id"),
        }
        if cfg["last_id"] is None or cfg["watched_id"] != src.id:
            # Les vidéos déjà postées ne seront pas annoncées : seules les prochaines le seront
            cfg["last_id"] = await derniere_id(src)
            cfg["watched_id"] = src.id
        routes()[rk] = cfg
        try:
            sauver()
        except Exception as e:
            await interaction.followup.send(f"❌ Storage error: {e}", ephemeral=True)
            return

    etat = ""
    if now:
        dernier = None
        async for m in src.history(limit=1):
            dernier = m
        if dernier is None:
            etat = "\n⚠️ Nothing to post yet: the Notify Me channel is empty."
        elif await poster(channel, cfg, dernier, forcer=True):
            etat = f"\n📨 Latest Notify Me message posted in {channel.mention}."
        else:
            etat = "\n⚠️ I couldn't post it (empty message or missing permission)."

    apercu = formater(cfg["message"], "https://www.tiktok.com/@you/video/123", "Video title", "")
    await interaction.followup.send(
        f"✅ **{look['name']}**: watching **#{src.name}** ({src.guild.name}) every {INTERVALLE_MIN} min → "
        f"new messages are posted in {channel.mention}.{etat}\n"
        f"Preview:\n>>> {apercu}"[:1990],
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(name="The /post to stop")
async def stoppost_cmd(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    async with VERROU:
        cfg = routes().pop(cle_route(interaction.guild.id, name), None)
        if cfg is None:
            await interaction.response.send_message("❌ No `/post` with this name on this server.", ephemeral=True)
            return
        try:
            sauver()
        except Exception as e:
            await interaction.response.send_message(f"❌ Storage error: {e}", ephemeral=True)
            return
    await interaction.response.send_message(
        f"✅ Stopped **{cfg['name']}**. Use `/post` to start again.", ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def autocomplete_looks(interaction: discord.Interaction, current: str):
    saisie = current.lower()
    return [
        app_commands.Choice(name=l["name"][:100], value=l["name"][:100])
        for l in looks().values() if saisie in l["name"].lower()
    ][:25]


async def autocomplete_looks_ici(interaction: discord.Interaction, current: str):
    saisie = current.lower()
    return [
        app_commands.Choice(name=l["name"][:100], value=l["name"][:100])
        for l in looks().values()
        if l.get("guild_id") == interaction.guild_id and saisie in l["name"].lower()
    ][:25]


async def autocomplete_posts_ici(interaction: discord.Interaction, current: str):
    saisie = current.lower()
    return [
        app_commands.Choice(name=r["name"][:100], value=r["name"][:100])
        for r in routes().values()
        if r.get("guild_id") == interaction.guild_id and saisie in r["name"].lower()
    ][:25]


def installer(c, t):
    """Branche /look, /stoplook, /post et /stoppost sur le bot `c` et son arbre de commandes `t`.
    Bot.py l'appelle au chargement ; il démarre ensuite la boucle avec verif_notify.start()."""
    global client, tree
    client, tree = c, t

    def ajouter(nom, callback, description, autocompletes):
        cmd = app_commands.Command(name=nom, callback=callback, description=description)
        for param, fonction in autocompletes.items():
            cmd.autocomplete(param)(fonction)
        t.add_command(cmd)

    ajouter("look", look_cmd, "Save a Notify Me channel under a name (use the same name in /post)", {})
    ajouter("stoplook", stoplook_cmd, "Stop watching a saved Notify Me channel", {"name": autocomplete_looks_ici})
    ajouter("post", post_cmd, "Repost the new Notify Me messages here, with your own message", {"name": autocomplete_looks})
    ajouter("stoppost", stoppost_cmd, "Stop a /post", {"name": autocomplete_posts_ici})


# ---------- MODE AUTONOME (python relay_notifyme.py, ou lancé par Bot.py hors du serveur principal) ----------
async def demarrer_serveur_web():
    """Render (Web Service) exige un port ouvert. Inactif sans variable PORT."""
    port = os.getenv("PORT")
    if not port:
        return
    from aiohttp import web

    async def ok(_request):
        if client.is_ready():
            return web.Response(text=f"Relay mode ✅ ({len(client.guilds)} server(s))")
        return web.Response(text="Relay mode ⏳ connecting...")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    print(f"[WEB] Serveur de ping démarré sur le port {port}")


async def principal():
    """Si la connexion échoue (429, token invalide...), on attend avant de quitter :
    quitter tout de suite fait redémarrer Render en boucle."""
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("[ERREUR] Variable DISCORD_TOKEN manquante.")
    discord.utils.setup_logging()

    intents = discord.Intents.default()
    intents.message_content = True  # nécessaire pour lire le texte des messages de NotifyMe

    class RelaisBot(discord.Client):
        async def setup_hook(self):
            await demarrer_serveur_web()
            verif_notify.start()

    c = RelaisBot(intents=intents, status=discord.Status.online)
    installer(c, app_commands.CommandTree(c))

    @c.event
    async def on_ready():
        print(f"[RELAIS] Connecté en tant que {c.user} — mode relais Notify Me ({len(c.guilds)} serveur(s))")
        for g in c.guilds:
            await synchroniser(g)

    @c.event
    async def on_guild_join(guild):
        await synchroniser(guild)

    try:
        async with c:
            await c.start(token)
    except Exception as e:
        status = getattr(e, "status", None)
        attente = 1800 if status == 429 else 300
        print(f"[ERREUR] Connexion impossible ({type(e).__name__}: {e}). Nouvel essai dans {attente // 60} min.")
        await asyncio.sleep(attente)
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        asyncio.run(principal())
    except KeyboardInterrupt:
        pass
