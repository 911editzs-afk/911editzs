"""
Watchdog bot — surveille le bot principal via heartbeat partagé (MongoDB / JSON).

Si le heartbeat du bot principal est trop vieux → active le lockdown
(tous les salons cachés sauf le salon de statut configuré avec /setupoffline).

Quand le heartbeat revient → lève le lockdown.

Variables d'environnement requises :
  DISCORD_TOKEN_WATCHDOG  (ou DISCORD_TOKEN)  → token du DEUXIÈME bot
  MONGODB_URI             (optionnel, même que le bot principal si tu l'utilises)
  PORT                    (Render Web Service)

Déploie ce fichier comme un SECOND service Render (Web Service),
dans le même repo ou un repo séparé, avec le même storage / MongoDB.
"""

import asyncio
import json
import os
import signal
import time
import traceback
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import tasks


# ---------- ENV ----------
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

# Token du WATCHDOG (différent du bot principal !)
TOKEN = os.getenv("DISCORD_TOKEN_WATCHDOG") or os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("[WATCHDOG] DISCORD_TOKEN_WATCHDOG (ou DISCORD_TOKEN) manquant.")

# Même stockage que le bot principal
try:
    import storage
except Exception as e:
    storage = None
    print(f"[WATCHDOG] storage non chargé ({e}) → fallback fichiers JSON locaux.")


FICHIER_HEARTBEAT = "bot_heartbeat.json"
FICHIER_LOCKDOWN = "lockdown_config.json"

# Si le dernier heartbeat a plus de X secondes → considéré offline
HEARTBEAT_TIMEOUT = int(os.getenv("HEARTBEAT_TIMEOUT", "180"))  # 3 minutes
CHECK_INTERVAL = int(os.getenv("WATCHDOG_INTERVAL", "30"))       # vérifie toutes les 30 s

MESSAGE_OFFLINE_DEFAUT = (
    "# 🔴 The Peak bot is offline\n\n"
    "The bot temporarily lost connection.\n"
    "All channels are hidden for safety until it comes back online.\n\n"
    "Please wait — everything will be restored automatically."
)
MESSAGE_ONLINE_DEFAUT = (
    "# 🟢 The Peak bot is back online\n\n"
    "All channels have been restored. You can use the server normally again."
)


def charger(nom: str) -> dict:
    if storage is not None:
        try:
            data = storage.charger(nom)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"[WATCHDOG] storage.charger({nom}) : {e}")
    chemin = nom if nom.endswith(".json") else nom + ".json"
    try:
        with open(chemin, "r", encoding="utf-8") as f:
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
            print(f"[WATCHDOG] storage.sauver({nom}) : {e}")
    chemin = nom if nom.endswith(".json") else nom + ".json"
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- DISCORD CLIENT ----------
intents = discord.Intents.default()
intents.guilds = True

client = discord.Client(
    intents=intents,
    status=discord.Status.online,
    activity=discord.CustomActivity(name="Watching main bot"),
)

_web_demarre = False
etat = "démarrage…"


async def demarrer_serveur_web():
    global _web_demarre
    port = os.getenv("PORT")
    if not port or _web_demarre:
        return
    _web_demarre = True
    from aiohttp import web

    async def ok(_request):
        hb = charger(FICHIER_HEARTBEAT)
        age = "?"
        if hb.get("ts"):
            age = f"{int(time.time() - float(hb['ts']))}s ago"
        ready = "oui" if client.is_ready() else "non"
        return web.Response(
            text=f"Watchdog OK\nDiscord ready: {ready}\nLast main heartbeat: {age}\nState: {etat}"
        )

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    print(f"[WATCHDOG] Serveur web sur le port {port}")


# ---------- LOCKDOWN (copie légère de la logique du bot principal) ----------
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
        if item.get("type") == "role":
            target = guild.get_role(item["id"])
        else:
            target = guild.get_member(item["id"])
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


async def activer_lockdown(guild: discord.Guild, cfg: dict, raison: str) -> bool:
    if not cfg.get("status_channel_id"):
        return False
    if cfg.get("locked"):
        return True

    status_ch = guild.get_channel(int(cfg["status_channel_id"]))
    if status_ch is None:
        try:
            status_ch = await guild.fetch_channel(int(cfg["status_channel_id"]))
        except Exception:
            print(f"[WATCHDOG] Salon statut introuvable sur {guild.name}")
            return False

    snapshot = {}
    for ch in guild.channels:
        if ch.id == status_ch.id:
            continue
        try:
            snapshot[str(ch.id)] = _serialiser_ow(ch)
        except Exception:
            continue

    me = guild.me
    for ch in list(guild.channels):
        if ch.id == status_ch.id:
            continue
        try:
            ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            if me:
                ow[me] = discord.PermissionOverwrite(
                    view_channel=True, manage_channels=True, manage_permissions=True,
                    send_messages=True, embed_links=True,
                )
            await ch.edit(overwrites=ow, reason=f"Watchdog lockdown: {raison}")
        except Exception as e:
            print(f"[WATCHDOG] Impossible de cacher #{getattr(ch, 'name', ch.id)}: {e}")

    try:
        ow_status = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=False, add_reactions=False,
            ),
        }
        if me:
            ow_status[me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True,
                embed_links=True, manage_channels=True,
            )
        await status_ch.edit(overwrites=ow_status, reason=f"Watchdog lockdown: {raison}")
    except Exception as e:
        print(f"[WATCHDOG] Config salon statut : {e}")

    texte = cfg.get("message_offline") or MESSAGE_OFFLINE_DEFAUT
    embed = discord.Embed(description=texte, color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Reason: {raison}")
    try:
        msg_id = cfg.get("status_msg_id")
        if msg_id:
            try:
                msg = await status_ch.fetch_message(int(msg_id))
                await msg.edit(content=None, embed=embed)
            except Exception:
                msg = await status_ch.send(embed=embed)
                cfg["status_msg_id"] = msg.id
        else:
            msg = await status_ch.send(embed=embed)
            cfg["status_msg_id"] = msg.id
    except Exception as e:
        print(f"[WATCHDOG] Message offline : {e}")

    cfg["locked"] = True
    cfg["snapshot"] = snapshot
    cfg["locked_at"] = datetime.now(timezone.utc).isoformat()
    cfg["locked_reason"] = raison
    return True


async def desactiver_lockdown(guild: discord.Guild, cfg: dict) -> bool:
    if not cfg.get("locked"):
        return False

    snapshot = cfg.get("snapshot") or {}
    for ch_id, ow_data in snapshot.items():
        ch = guild.get_channel(int(ch_id))
        if ch is None:
            continue
        try:
            await ch.edit(overwrites=_appliquer_ow(guild, ow_data), reason="Watchdog: main bot back online")
        except Exception as e:
            print(f"[WATCHDOG] Restauration #{getattr(ch, 'name', ch_id)}: {e}")

    status_id = cfg.get("status_channel_id")
    status_ch = guild.get_channel(int(status_id)) if status_id else None
    if status_ch is not None:
        texte = cfg.get("message_online") or MESSAGE_ONLINE_DEFAUT
        embed = discord.Embed(description=texte, color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        try:
            msg_id = cfg.get("status_msg_id")
            if msg_id:
                try:
                    msg = await status_ch.fetch_message(int(msg_id))
                    await msg.edit(content=None, embed=embed)
                except Exception:
                    msg = await status_ch.send(embed=embed)
                    cfg["status_msg_id"] = msg.id
            else:
                msg = await status_ch.send(embed=embed)
                cfg["status_msg_id"] = msg.id
        except Exception as e:
            print(f"[WATCHDOG] Message online : {e}")

    cfg["locked"] = False
    cfg["snapshot"] = {}
    cfg.pop("locked_at", None)
    cfg.pop("locked_reason", None)
    return True


# ---------- BOUCLE DE SURVEILLANCE ----------
@tasks.loop(seconds=CHECK_INTERVAL)
async def verif_heartbeat():
    global etat
    try:
        hb = charger(FICHIER_HEARTBEAT)
        ts = float(hb.get("ts") or 0)
        age = time.time() - ts if ts else 999999
        principal_online = age < HEARTBEAT_TIMEOUT

        lockdown_all = charger(FICHIER_LOCKDOWN)
        modifie = False

        for guild in client.guilds:
            gid = str(guild.id)
            cfg = lockdown_all.get(gid)
            if not cfg or not cfg.get("enabled") or not cfg.get("status_channel_id"):
                continue

            if not principal_online and not cfg.get("locked"):
                print(f"[WATCHDOG] Main bot offline (heartbeat {int(age)}s) → lockdown sur {guild.name}")
                if await activer_lockdown(guild, cfg, f"Main bot offline (no heartbeat for {int(age)}s)"):
                    lockdown_all[gid] = cfg
                    modifie = True
                    etat = f"LOCKDOWN actif (heartbeat age={int(age)}s)"

            elif principal_online and cfg.get("locked"):
                # Ne lever le lockdown que si c'est le watchdog qui l'avait mis
                # (ou toujours : le principal est revenu)
                print(f"[WATCHDOG] Main bot online → unlock sur {guild.name}")
                if await desactiver_lockdown(guild, cfg):
                    lockdown_all[gid] = cfg
                    modifie = True
                    etat = f"OK — main bot online (heartbeat {int(age)}s ago)"

            elif principal_online:
                etat = f"OK — main bot online (heartbeat {int(age)}s ago)"
            else:
                etat = f"LOCKDOWN — main offline (heartbeat {int(age)}s ago)"

        if modifie:
            sauver(FICHIER_LOCKDOWN, lockdown_all)

    except Exception as e:
        print(f"[WATCHDOG] Erreur verif : {e!r}")
        traceback.print_exc()


@verif_heartbeat.before_loop
async def _avant_verif():
    await client.wait_until_ready()


@client.event
async def on_ready():
    print(f"[WATCHDOG] Connecté en tant que {client.user} | {len(client.guilds)} serveur(s)")
    if not verif_heartbeat.is_running():
        verif_heartbeat.start()


async def principal():
    await demarrer_serveur_web()

    def _stop(signum, frame):
        print(f"[WATCHDOG] Signal {signum} — arrêt")
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
        print(f"[WATCHDOG] Erreur : {e!r}")
        traceback.print_exc()
        await asyncio.sleep(60)
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        asyncio.run(principal())
    except KeyboardInterrupt:
        pass
