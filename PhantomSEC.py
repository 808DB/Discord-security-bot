import discord
from discord.ext import commands, tasks
from discord import Intents, Permissions
import asyncio
from datetime import datetime, timedelta
import re
import collections

intents = Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# --- CONSTANTES ---
JOIN_THRESHOLD = 5           # nb max de joins dans JOIN_WINDOW avant lockdown
JOIN_WINDOW = 15             # secondes
SPAM_MSG_LIMIT = 5           # nb max msg dans SPAM_TIME_WINDOW
SPAM_TIME_WINDOW = 10        # secondes
MENTION_LIMIT = 5            # nb max mentions par message
SUSPICION_LIMIT = 70         # seuil suspicion (0-100)
SHADOWBAN_ROLE_NAME = "ShadowBanned"
MUTE_ROLE_NAME = "Muted"
LOCKED_GUILDS = set()
JOIN_TRACKER = {}            # {guild_id: [(user_id, datetime), ...]}
USER_PROFILES = {}           # {user_id: UserProfile}

# --- CLASSE USER PROFILE ---
class UserProfile:
    def __init__(self):
        self.msg_count = 0
        self.msg_times = collections.deque(maxlen=50)    # horodatages derniers messages
        self.mention_count = 0
        self.link_count = 0
        self.suspicion_score = 0
        self.shadowbanned = False
        self.muted = False
        self.history = []  # list of (datetime, action, details)

    def log_action(self, action, details=""):
        self.history.append((datetime.utcnow(), action, details))
        # Garder max 100 entrées
        if len(self.history) > 100:
            self.history.pop(0)

    def update_suspicion(self, delta):
        self.suspicion_score = min(100, max(0, self.suspicion_score + delta))


# --- UTILITAIRES ---
def is_suspect_link(text):
    blacklisted = ["nitro", "free", "airdrop", "gift", "steam", "verify"]
    text = text.lower()
    return any(word in text for word in blacklisted)

async def create_or_get_role(guild, role_name, permissions=None):
    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        try:
            role = await guild.create_role(name=role_name, permissions=permissions or discord.Permissions.none())
            # Restreindre role sur tous les salons
            for ch in guild.channels:
                await ch.set_permissions(role, send_messages=False, speak=False, add_reactions=False)
        except Exception as e:
            print(f"[ERROR] Role creation failed: {e}")
            return None
    return role


# --- EVENT BOT READY ---
@bot.event
async def on_ready():
    print(f"[PhantomGuard v2 Core] Connecté en tant que {bot.user} !")
    scan_joins.start()
    self_heal_roles.start()
    print("[INFO] Tâches de sécurité démarrées.")

# --- JOIN TRACKER & LOCKDOWN ---
@bot.event
async def on_member_join(member):
    guild_id = member.guild.id
    now = datetime.utcnow()
    JOIN_TRACKER.setdefault(guild_id, []).append((member.id, now))

@tasks.loop(seconds=10)
async def scan_joins():
    for guild in bot.guilds:
        joins = JOIN_TRACKER.get(guild.id, [])
        recent = [(uid, ts) for uid, ts in joins if (datetime.utcnow() - ts).total_seconds() < JOIN_WINDOW]
        if len(recent) >= JOIN_THRESHOLD and guild.id not in LOCKED_GUILDS:
            LOCKED_GUILDS.add(guild.id)
            await lockdown_guild(guild)
        JOIN_TRACKER[guild.id] = recent

async def lockdown_guild(guild):
    print(f"[ALERT] Raid détecté sur {guild.name} -> Verrouillage total.")
    # Lock tous les salons en lecture seule pour @everyone
    for channel in guild.channels:
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = False
            overwrite.speak = False
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
        except Exception as e:
            print(f"[ERROR] Echec lockdown {channel.name}: {e}")
    try:
        await guild.owner.send(f"[PhantomGuard] Raid détecté sur **{guild.name}**, verrouillage activé.")
    except:
        pass

# --- PROTECTIONS ANTI-SPAM, ANTI-MASS-MENTION, ANTI-LINKS ---
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    uid = message.author.id
    guild = message.guild
    now = datetime.utcnow()

    # Création profil utilisateur si besoin
    if uid not in USER_PROFILES:
        USER_PROFILES[uid] = UserProfile()
    profile = USER_PROFILES[uid]

    # Enregistrement message
    profile.msg_count += 1
    profile.msg_times.append(now)
    profile.mention_count += len(message.mentions)
    if re.search(r"https?://", message.content):
        profile.link_count += 1

    # Analyse comportementale basique
    suspicion_delta = 0
    # Spam messages (plus que limite dans fenêtre)
    last_msgs = [t for t in profile.msg_times if (now - t).total_seconds() < SPAM_TIME_WINDOW]
    if len(last_msgs) > SPAM_MSG_LIMIT:
        suspicion_delta += 30

    # Mass mention
    if len(message.mentions) >= MENTION_LIMIT:
        suspicion_delta += 40

    # Liens suspects
    if is_suspect_link(message.content):
        suspicion_delta += 40

    profile.update_suspicion(suspicion_delta)
    profile.log_action("MESSAGE", f"Suspicion +{suspicion_delta} (score {profile.suspicion_score})")

    # Actions en fonction du score suspicion
    if profile.suspicion_score >= SUSPICION_LIMIT:
        if not profile.muted:
            await apply_mute(guild, message.author, profile)
        elif profile.muted and not profile.shadowbanned:
            await apply_shadowban(guild, message.author, profile)
        return  # stop processing commands pour mute/shadowban

    # Shadowban : filtre invisible
    if profile.shadowbanned:
        # Supprime le message côté visible (message visible uniquement par admins)
        await message.delete()
        # Ré-émission en DM à admins (ou log serveur)
        log_channel = discord.utils.get(guild.text_channels, name="phantomguard-logs")
        if log_channel:
            embed = discord.Embed(title="Message Shadowban détecté",
                                  description=f"**Auteur:** {message.author} ({uid})\n**Contenu:** {message.content}",
                                  color=0xff0000, timestamp=now)
            await log_channel.send(embed=embed)
        return

    await bot.process_commands(message)

# --- MUTE & SHADOWBAN ---
async def apply_mute(guild, user, profile):
    mute_role = await create_or_get_role(guild, MUTE_ROLE_NAME, permissions=discord.Permissions(send_messages=False))
    if not mute_role:
        return
    try:
        await user.add_roles(mute_role, reason="PhantomGuard - Auto mute suspicion élevée")
        profile.muted = True
        profile.log_action("MUTE", "Auto mute déclenché")
        print(f"[PhantomGuard] {user} mute automatique")
    except Exception as e:
        print(f"[ERROR] Impossible de mute {user}: {e}")

async def apply_shadowban(guild, user, profile):
    sb_role = await create_or_get_role(guild, SHADOWBAN_ROLE_NAME)
    if not sb_role:
        return
    try:
        # Ajoute rôle Shadowban, retire mute pour switch
        mute_role = discord.utils.get(guild.roles, name=MUTE_ROLE_NAME)
        if mute_role and mute_role in user.roles:
            await user.remove_roles(mute_role, reason="Passage en Shadowban")
            profile.muted = False

        await user.add_roles(sb_role, reason="PhantomGuard - Shadowban activé")
        profile.shadowbanned = True
        profile.log_action("SHADOWBAN", "Auto shadowban déclenché")
        print(f"[PhantomGuard] {user} shadowban automatique")
    except Exception as e:
        print(f"[ERROR] Impossible d'appliquer shadowban à {user}: {e}")

# --- SELF-HEALING DES RÔLES ---
@tasks.loop(minutes=5)
async def self_heal_roles():
    for guild in bot.guilds:
        # Check et recrée mute role si disparu
        mute_role = discord.utils.get(guild.roles, name=MUTE_ROLE_NAME)
        if not mute_role:
            print(f"[PhantomGuard] Rôle {MUTE_ROLE_NAME} absent sur {guild.name}, recréation...")
            await create_or_get_role(guild, MUTE_ROLE_NAME, permissions=discord.Permissions(send_messages=False))

        # Check et recrée shadowban role si disparu
        sb_role = discord.utils.get(guild.roles, name=SHADOWBAN_ROLE_NAME)
        if not sb_role:
            print(f"[PhantomGuard] Rôle {SHADOWBAN_ROLE_NAME} absent sur {guild.name}, recréation...")
            await create_or_get_role(guild, SHADOWBAN_ROLE_NAME)

# --- COMMANDES ADMIN ---
@bot.command()
@commands.has_permissions(administrator=True)
async def resetmute(ctx):
    guild = ctx.guild
    role = discord.utils.get(guild.roles, name=MUTE_ROLE_NAME)
    if not role:
        await ctx.send("⚠️ Rôle 'Muted' introuvable.")
        return
    removed = 0
    for member in guild.members:
        if role in member.roles:
            try:
                await member.remove_roles(role, reason="Reset mute")
                removed += 1
            except:
                pass
    for profile in USER_PROFILES.values():
        profile.muted = False
    await ctx.send(f"✅ {removed} membres unmute, cache remis à zéro.")

@bot.command()
@commands.has_permissions(administrator=True)
async def resetspam(ctx):
    for profile in USER_PROFILES.values():
        profile.msg_times.clear()
        profile.suspicion_score = 0
        profile.log_action("RESET", "Cache spam remis à zéro")
    await ctx.send("✅ Cache spam et suspicion remis à zéro.")

@bot.command()
@commands.has_permissions(administrator=True)
async def unlock(ctx):
    if ctx.guild.id not in LOCKED_GUILDS:
        await ctx.send("Aucune alerte de raid active.")
        return
    for channel in ctx.guild.channels:
        try:
            overwrite = channel.overwrites_for(ctx.guild.default_role)
            overwrite.send_messages = True
            overwrite.speak = True
            await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        except Exception as e:
            print(f"[ERROR] Failed to unlock {channel.name}: {e}")
    LOCKED_GUILDS.remove(ctx.guild.id)
    await ctx.send("🔓 Serveur déverrouillé.")

@bot.command()
@commands.has_permissions(administrator=True)
async def phantominfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    profile = USER_PROFILES.get(member.id)
    if not profile:
        await ctx.send(f"Aucune donnée pour {member.display_name}.")
        return
    embed = discord.Embed(title=f"PhantomGuard Stats pour {member.display_name}", color=0x00ff00)
    embed.add_field(name="Score Suspicion", value=str(profile.suspicion_score))
    embed.add_field(name="Messages récents", value=str(len(profile.msg_times)))
    embed.add_field(name="Mentions dans messages", value=str(profile.mention_count))
    embed.add_field(name="Liens détectés", value=str(profile.link_count))
    embed.add_field(name="Muted", value=str(profile.muted))
    embed.add_field(name="ShadowBanned", value=str(profile.shadowbanned))
    last_actions = "\n".join(f"{t.strftime('%Y-%m-%d %H:%M:%S')} - {a} - {d}" for t,a,d in profile.history[-5:])
    embed.add_field(name="Historique récent", value=last_actions or "Aucun")
    await ctx.send(embed=embed)

# --- RUN BOT ---
TOKEN = "TON_TOKEN_ICI"  # Remplace par ton token réel
bot.run(TOKEN)
