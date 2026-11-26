"""
Watchdog bot — reste TOUJOURS offline (invisible) sur Discord.

Surveille un salon (commande /look) dans le serveur Notify Me.
Le bot principal y écrit " la " chaque minute (/write).

Si 3 lectures de suite sans nouveau message → lockdown du serveur principal
(tous les salons cachés sauf règles + salon statut).
Quand un nouveau heartbeat apparaît → unlock.

Le propriétaire du serveur principal reçoit un message privé si le lockdown
se déclenche.

Variables d'environnement :
  DISCORD_TOKEN_WATCHDOG  (ou DISCORD_TOKEN) — token de CE bot
  MONGODB_URI             — même que le bot principal (partage lockdown_config)
  PORT                    — Render
"""

import asyncio
import json
import os
import signal
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks


def charger_env():
    for nom in ("DISCORD_TOKEN.env", ".env"):
        if not os.path.exists(nom):
            continue
        with open(nom, "r", encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if ligne and not ligne.startswith("#") and "=" in ligne:
                    cle, valeur = ligne.split("=", 1)
                    os.environ.setdefault(cle.strip(), valeur.strip().strip('"').strip("'"))


charger_env()

TOKEN = os.getenv("DISCORD_TOKEN_WATCHDOG") or os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("[WATCHDOG] DISCORD_TOKEN_WATCHDOG (ou DISCORD_TOKEN) manquant.")

try:
    import storage
except Exception as e:
    storage = None
    print(f"[WATCHDOG] storage non chargé ({e}) → fichiers JSON locaux.")


FICHIER_LOCKDOWN = "lockdown_config.json"
FICHIER_SCAN_PERMS = "lockdown_permissions_scan.json"
FICHIER_LOOK = "watchdog_look.json"

SALONS_TOUJOURS_CACHES = {
    1430894382923518074, 1572700047978791003, 1572698656077910027,
    1484907833215549470, 1561015186926731284,
}
SALONS_BOOSTERS = {1549867572713881720, 1540071405197070356}
ROLE_BOOSTER_VC = 1540096120615866478


CHECK_INTERVAL = 50
FAILS_BEFORE_LOCK = 3
HEARTBEAT_MAX_AGE = 180


def charger(nom: str) -> dict:
    if storage is not None:
        try:
            data = storage.charger(nom)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"[WATCHDOG] storage.charger({nom}): {e}")
    try:
        with open(nom, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def sauver(nom: str, data: dict):
    if storage is not None:
        try:
            storage.sauver(nom, data)
            return
        except Exception as e:
            print(f"[WATCHDOG] storage.sauver({nom}): {e}")
    with open(nom, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True


class Watchdog(discord.Client):
    def __init__(self):
        super().__init__(
            intents=intents,
            status=discord.Status.offline,
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await demarrer_serveur_web()


client = Watchdog()
tree = client.tree

_web_demarre = False
etat = "démarrage…"
_echec_consecutifs = 0
_dernier_msg_id_vu = None
LOOK = charger(FICHIER_LOOK)


async def demarrer_serveur_web():
    global _web_demarre
    port = os.getenv("PORT")
    if not port or _web_demarre:
        return
    _web_demarre = True
    from aiohttp import web

    async def ok(_request):
        ready = "oui" if client.is_ready() else "non"
        look = LOOK.get("channel_id", "?")
        return web.Response(
            text=(
                f"Watchdog OK (status=offline)\nDiscord ready: {ready}\n"
                f"Looking at channel: {look}\nFails: {_echec_consecutifs}/{FAILS_BEFORE_LOCK}\n"
                f"State: {etat}"
            )
        )

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    print(f"[WATCHDOG] Web server on port {port}")


def _serialiser_ow(channel) -> list:
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


def _appliquer_ow(guild, data_list: list) -> dict:
    overwrites = {}
    for item in data_list or []:
        target = guild.get_role(item["id"]) if item.get("type") == "role" else guild.get_member(item["id"])
        if target is None:
            continue
        try:
            overwrites[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(item.get("allow", 0)),
                discord.Permissions(item.get("deny", 0)),
            )
        except Exception:
            continue
    return overwrites


def _ids_proteges(cfg: dict) -> set:
    ids = set()
    if cfg.get("status_channel_id"):
        ids.add(int(cfg["status_channel_id"]))
    if cfg.get("rules_channel_id"):
        ids.add(int(cfg["rules_channel_id"]))
    return ids


async def activer_lockdown(guild: discord.Guild, cfg: dict, raison: str) -> bool:
    if not cfg.get("status_channel_id") or cfg.get("locked"):
        return bool(cfg.get("locked"))

    proteges = set()
    if cfg.get("status_channel_id"):
        proteges.add(int(cfg["status_channel_id"]))
    if cfg.get("rules_channel_id"):
        proteges.add(int(cfg["rules_channel_id"]))

    status_ch = guild.get_channel(int(cfg["status_channel_id"]))
    if status_ch is None:
        try:
            status_ch = await guild.fetch_channel(int(cfg["status_channel_id"]))
        except Exception:
            print(f"[WATCHDOG] Status channel missing on {guild.name}")
            return False

    role_booster = guild.get_role(ROLE_BOOSTER_VC)

    for ch in list(guild.channels):
        if ch.id in proteges:
            continue
        try:
            await ch.set_permissions(
                guild.default_role, view_channel=False,
                reason=f"Watchdog lockdown: {raison}",
            )
            # Uniquement sur les salons boosters : deny le rôle booster
            if ch.id in SALONS_BOOSTERS and role_booster is not None:
                await ch.set_permissions(
                    role_booster, view_channel=False,
                    reason=f"Watchdog lockdown: hide booster channels only",
                )
        except Exception as e:
            print(f"[WATCHDOG] hide #{getattr(ch, 'name', ch.id)}: {e}")

    try:
        await status_ch.set_permissions(
            guild.default_role,
            view_channel=True, send_messages=False, add_reactions=False,
            reason=f"Watchdog lockdown: {raison}",
        )
    except Exception as e:
        print(f"[WATCHDOG] status channel show: {e}")

    rid = cfg.get("rules_channel_id")
    if rid:
        rules_ch = guild.get_channel(int(rid))
        if rules_ch is not None:
            try:
                await rules_ch.set_permissions(
                    guild.default_role, view_channel=True,
                    reason="Watchdog: keep rules visible",
                )
            except Exception as e:
                print(f"[WATCHDOG] rules visible: {e}")

    cfg["locked"] = True
    cfg["locked_at"] = datetime.now(timezone.utc).isoformat()
    cfg["locked_reason"] = raison
    return True


async def desactiver_lockdown(guild: discord.Guild, cfg: dict) -> bool:
    """Restaure depuis lockdown_permissions_scan.json (même fichier que /scan)."""
    if not cfg.get("locked"):
        return False

    scan = charger(FICHIER_SCAN_PERMS)
    channels = (scan or {}).get("channels") or {}
    if not channels:
        print("[WATCHDOG] Pas de scan — unlock partiel (view_channel=True partout sauf always-hidden)")
        proteges = set()
        if cfg.get("status_channel_id"):
            proteges.add(int(cfg["status_channel_id"]))
        for ch in guild.channels:
            if ch.id in proteges or ch.id in SALONS_TOUJOURS_CACHES:
                continue
            try:
                await ch.set_permissions(
                    guild.default_role, view_channel=True,
                    reason="Watchdog unlock (no scan)",
                )
            except Exception as e:
                print(f"[WATCHDOG] unlock #{getattr(ch, 'name', ch.id)}: {e}")
    else:
        for cid, info in channels.items():
            ch = guild.get_channel(int(cid))
            if ch is None:
                continue
            try:
                overwrites = _appliquer_ow(guild, info.get("overwrites") or [])
                await ch.edit(overwrites=overwrites, reason="Watchdog: restore from /scan")
            except Exception as e:
                print(f"[WATCHDOG] restore #{info.get('name', cid)}: {e}")

    for cid in SALONS_TOUJOURS_CACHES:
        ch = guild.get_channel(cid)
        if ch is None:
            continue
        try:
            await ch.set_permissions(
                guild.default_role, view_channel=False,
                reason="Watchdog: always hidden",
            )
        except Exception:
            pass

    # Cacher le salon statut (bot online)
    sid = cfg.get("status_channel_id")
    if sid:
        status_ch = guild.get_channel(int(sid))
        if status_ch is not None:
            try:
                await status_ch.set_permissions(
                    guild.default_role, view_channel=False,
                    reason="Watchdog: hide status (bot online)",
                )
            except Exception as e:
                print(f"[WATCHDOG] hide status: {e}")

    cfg["locked"] = False
    cfg["snapshot"] = {}
    cfg.pop("locked_at", None)
    cfg.pop("locked_reason", None)
    return True


async def prevenir_owner(guild: discord.Guild, raison: str):
    try:
        owner = guild.owner
        if owner is None:
            owner = await guild.fetch_member(guild.owner_id)
        if owner is None:
            return
        await owner.send(
            f"🚨 **Watchdog alert** on **{guild.name}**\n"
            f"The main bot seems offline.\n"
            f"**Action:** lockdown activated (only rules + status channel visible).\n"
            f"**Reason:** {raison}\n"
            f"Channels will be restored automatically when the main bot writes again."
        )
    except Exception as e:
        print(f"[WATCHDOG] Impossible de DM le owner : {e}")


@tasks.loop(seconds=CHECK_INTERVAL)
async def verif_look():
    global etat, _echec_consecutifs, _dernier_msg_id_vu

    # Recharge /look depuis Mongo (au cas où)
    try:
        look_db = charger(FICHIER_LOOK)
        if look_db.get("channel_id"):
            LOOK.update(look_db)
    except Exception:
        pass

    channel_id = LOOK.get("channel_id")
    if not channel_id:
        etat = "pas de /look configuré"
        return

    channel = client.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(channel_id))
        except Exception as e:
            print(f"[WATCHDOG] Salon /look introuvable : {e}")
            etat = f"salon introuvable: {e}"
            return

    heartbeat_ok = False
    try:
        async for msg in channel.history(limit=5):
            contenu = (msg.content or "").strip()
            if contenu.replace(" ", "") == "la":
                created = msg.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
                if age < 0:
                    age = -age
                if age <= HEARTBEAT_MAX_AGE:
                    heartbeat_ok = True
                    _dernier_msg_id_vu = msg.id
                    break
    except Exception as e:
        print(f"[WATCHDOG] Lecture history : {e}")
        etat = f"erreur lecture: {e}"
        return

    lockdown_all = charger(FICHIER_LOCKDOWN)
    modifie = False

    if heartbeat_ok:
        _echec_consecutifs = 0
        etat = f"OK — heartbeat vu (msg {_dernier_msg_id_vu})"
        for guild in client.guilds:
            cfg = lockdown_all.get(str(guild.id))
            if cfg and cfg.get("enabled") and cfg.get("locked"):
                if await desactiver_lockdown(guild, cfg):
                    lockdown_all[str(guild.id)] = cfg
                    modifie = True
                    print(f"[WATCHDOG] Unlock {guild.name}")
    else:
        _echec_consecutifs += 1
        etat = f"pas de heartbeat ({_echec_consecutifs}/{FAILS_BEFORE_LOCK})"
        print(f"[WATCHDOG] {etat}")

        if _echec_consecutifs >= FAILS_BEFORE_LOCK:
            for guild in client.guilds:
                cfg = lockdown_all.get(str(guild.id))
                if not cfg or not cfg.get("enabled") or cfg.get("locked"):
                    continue
                raison = f"No heartbeat for {_echec_consecutifs} checks (~{_echec_consecutifs * CHECK_INTERVAL}s)"
                if await activer_lockdown(guild, cfg, raison):
                    lockdown_all[str(guild.id)] = cfg
                    modifie = True
                    print(f"[WATCHDOG] LOCKDOWN {guild.name}")
                    await prevenir_owner(guild, raison)

    if modifie:
        sauver(FICHIER_LOCKDOWN, lockdown_all)


@verif_look.before_loop
async def _avant_verif():
    await client.wait_until_ready()


@tree.command(
    name="look",
    description="Channel to watch for the main bot heartbeat (same as /write on main bot)",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="The heartbeat channel in the Notify Me server")
async def look_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrators only.", ephemeral=True)
        return
    LOOK["channel_id"] = channel.id
    LOOK["guild_id"] = interaction.guild.id
    sauver(FICHIER_LOOK, LOOK)
    global _echec_consecutifs
    _echec_consecutifs = 0
    await interaction.response.send_message(
        f"✅ Watching {channel.mention} every **{CHECK_INTERVAL}s**.\n"
        f"After **{FAILS_BEFORE_LOCK}** empty checks → lockdown on the main server.\n"
        f"Main bot must use `/write` on this same channel.",
        ephemeral=True,
    )


@tree.command(
    name="watchstatus",
    description="Show watchdog status",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def watchstatus_cmd(interaction: discord.Interaction):
    ch = f"<#{LOOK['channel_id']}>" if LOOK.get("channel_id") else "_not set_"
    await interaction.response.send_message(
        f"**Watchdog** (always offline on Discord)\n"
        f"Looking at: {ch}\n"
        f"Fails: **{_echec_consecutifs}** / {FAILS_BEFORE_LOCK}\n"
        f"State: {etat}",
        ephemeral=True,
    )


@client.event
async def on_ready():
    print(f"[WATCHDOG] Ready as {client.user} (status=offline) | {len(client.guilds)} guild(s)")
    try:
        await client.change_presence(status=discord.Status.offline)
    except Exception:
        pass

    # 1) Commandes sur chaque serveur
    for g in client.guilds:
        try:
            tree.copy_global_to(guild=g)
            synced = await tree.sync(guild=g)
            print(f"[WATCHDOG] {len(synced)} cmd(s) on {g.name}: {[c.name for c in synced]}")
        except Exception as e:
            print(f"[WATCHDOG] sync {g.name}: {e}")

    # 2) Enlever les commandes GLOBALES côté Discord (évite le doublon)
    #    sans perdre les définitions locales
    try:
        saved = list(tree.get_commands())
        tree.clear_commands(guild=None)
        await tree.sync()
        for cmd in saved:
            try:
                tree.add_command(cmd)
            except Exception:
                pass
        print("[WATCHDOG] Commandes globales Discord effacées (1 seul /look par serveur)")
    except Exception as e:
        print(f"[WATCHDOG] clear global: {e}")

    if not verif_look.is_running():
        verif_look.start()



async def principal():
    await demarrer_serveur_web()

    def _stop(signum, frame):
        print(f"[WATCHDOG] signal {signum}")
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    except Exception:
        pass

    try:
        async with client:
            await client.start(TOKEN)
    except Exception as e:
        print(f"[WATCHDOG] {e!r}")
        traceback.print_exc()
        await asyncio.sleep(60)
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        asyncio.run(principal())
    except KeyboardInterrupt:
        pass
