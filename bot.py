import os
import time
import sqlite3
import asyncio
import discord
from discord.ext import commands
from discord import app_commands

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")
MAX_TEAM_SIZE = 22
TIER_CAPS = {
    "TOP 1-3": 4,
    "TOP 4-10": 4,
    "TOP 11-20": 4
}
WARN_COOLDOWN_SECONDS = 60  # over-cap DM warning cooldown per team

# cooldown memory
_last_warn_at: dict[int, float] = {}  # team_role_id -> last warn ts

# =========================
# DB SETUP
# =========================

conn = sqlite3.connect("roster.db")
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY,
    player_name TEXT NOT NULL,
    team_role_id INTEGER
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    signing_channel_id INTEGER,
    release_channel_id INTEGER
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS teams (
    team_role_id INTEGER PRIMARY KEY,
    team_name TEXT NOT NULL,
    manager_id INTEGER,
    co_manager_id INTEGER
)
""")

# per-guild configured rank roles
c.execute("""
CREATE TABLE IF NOT EXISTS guild_roles (
    guild_id INTEGER PRIMARY KEY,
    manager_role_id INTEGER,
    co_manager_role_id INTEGER,
    tier_1_3_role_id INTEGER,
    tier_4_10_role_id INTEGER,
    tier_11_20_role_id INTEGER
)
""")

# custom league-admin roles (multiple allowed)
c.execute("""
CREATE TABLE IF NOT EXISTS guild_admin_roles (
    guild_id INTEGER,
    role_id INTEGER,
    PRIMARY KEY (guild_id, role_id)
)
""")

conn.commit()

# =========================
# BOT
# =========================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# HELPERS
# =========================

def get_player_team(player_id: int):
    c.execute("SELECT team_role_id FROM players WHERE player_id=?", (player_id,))
    r = c.fetchone()
    return r[0] if r else None

def add_or_update_player(player_id: int, player_name: str, team_role_id: int | None):
    c.execute("SELECT 1 FROM players WHERE player_id=?", (player_id,))
    if c.fetchone():
        c.execute("UPDATE players SET player_name=?, team_role_id=? WHERE player_id=?",
                  (player_name, team_role_id, player_id))
    else:
        c.execute("INSERT INTO players (player_id, player_name, team_role_id) VALUES (?, ?, ?)",
                  (player_id, player_name, team_role_id))
    conn.commit()

def remove_player_from_team(player_id: int):
    c.execute("UPDATE players SET team_role_id=NULL WHERE player_id=?", (player_id,))
    conn.commit()

def get_team_roster(team_role_id: int):
    c.execute("SELECT player_name FROM players WHERE team_role_id=?", (team_role_id,))
    return [r[0] for r in c.fetchall()]

def get_guild_roles(guild_id: int):
    c.execute("""
        SELECT manager_role_id, co_manager_role_id, tier_1_3_role_id, tier_4_10_role_id, tier_11_20_role_id
        FROM guild_roles WHERE guild_id=?
    """, (guild_id,))
    r = c.fetchone()
    if not r:
        return {"manager": None, "co_manager": None, "t1_3": None, "t4_10": None, "t11_20": None}
    return {"manager": r[0], "co_manager": r[1], "t1_3": r[2], "t4_10": r[3], "t11_20": r[4]}

def set_guild_role(guild_id: int, key: str, role_id: int):
    cols = {
        "manager": "manager_role_id",
        "co_manager": "co_manager_role_id",
        "t1_3": "tier_1_3_role_id",
        "t4_10": "tier_4_10_role_id",
        "t11_20": "tier_11_20_role_id",
    }
    col = cols[key]
    c.execute(f"""
        INSERT INTO guild_roles (guild_id, {col}) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET {col}=excluded.{col}
    """, (guild_id, role_id))
    conn.commit()

def resolve_configured_role(guild: discord.Guild, role_id: int | None) -> discord.Role | None:
    return guild.get_role(role_id) if role_id else None

def ensure_rank_roles_exist(guild: discord.Guild):
    cfg = get_guild_roles(guild.id)
    return resolve_configured_role(guild, cfg["manager"]), resolve_configured_role(guild, cfg["co_manager"])

def get_player_category(member: discord.Member):
    cfg = get_guild_roles(member.guild.id)
    role_ids = {r.id for r in member.roles}

    if cfg["manager"] and cfg["manager"] in role_ids:
        return "manager"
    if cfg["co_manager"] and cfg["co_manager"] in role_ids:
        return "co_manager"

    if cfg["t1_3"] and cfg["t1_3"] in role_ids:
        return ("tiered", "TOP 1-3")
    if cfg["t4_10"] and cfg["t4_10"] in role_ids:
        return ("tiered", "TOP 4-10")
    if cfg["t11_20"] and cfg["t11_20"] in role_ids:
        return ("tiered", "TOP 11-20")

    # Fallback to names if not configured yet
    for r in member.roles:
        if r.name == "Manager":
            return "manager"
        if r.name == "Co-Manager":
            return "co_manager"
    for r in member.roles:
        if r.name in TIER_CAPS:
            return ("tiered", r.name)

    return "unranked"

def count_team_categories(team_role_id: int, guild: discord.Guild):
    manager_count = 0
    co_manager_count = 0
    tier_counts = {tier: 0 for tier in TIER_CAPS}
    unranked_count = 0
    for m in guild.members:
        if get_player_team(m.id) == team_role_id:
            cat = get_player_category(m)
            if cat == "manager":
                manager_count += 1
            elif cat == "co_manager":
                co_manager_count += 1
            elif isinstance(cat, tuple) and cat[0] == "tiered":
                tier_counts[cat[1]] += 1
            else:
                unranked_count += 1
    return manager_count, co_manager_count, tier_counts, unranked_count

# channel settings
def set_signing_channel(guild_id: int, channel_id: int):
    c.execute("""
    INSERT OR REPLACE INTO guild_settings (guild_id, signing_channel_id, release_channel_id)
    VALUES (?, ?, COALESCE((SELECT release_channel_id FROM guild_settings WHERE guild_id=?), NULL))
    """, (guild_id, channel_id, guild_id))
    conn.commit()

def set_release_channel(guild_id: int, channel_id: int):
    c.execute("""
    INSERT OR REPLACE INTO guild_settings (guild_id, signing_channel_id, release_channel_id)
    VALUES (?, COALESCE((SELECT signing_channel_id FROM guild_settings WHERE guild_id=?), NULL), ?)
    """, (guild_id, guild_id, channel_id))
    conn.commit()

def get_signing_channel(guild_id: int):
    c.execute("SELECT signing_channel_id FROM guild_settings WHERE guild_id=?", (guild_id,))
    r = c.fetchone()
    return r[0] if r else None

def get_release_channel(guild_id: int):
    c.execute("SELECT release_channel_id FROM guild_settings WHERE guild_id=?", (guild_id,))
    r = c.fetchone()
    return r[0] if r else None

# teams registry
def register_team(team_role_id: int, team_name: str):
    c.execute("""
    INSERT OR REPLACE INTO teams (team_role_id, team_name, manager_id, co_manager_id)
    VALUES (?, ?, COALESCE((SELECT manager_id FROM teams WHERE team_role_id=?), NULL),
                COALESCE((SELECT co_manager_id FROM teams WHERE team_role_id=?), NULL))
    """, (team_role_id, team_name, team_role_id, team_role_id))
    conn.commit()

def get_team_record(team_role_id: int):
    c.execute("SELECT team_role_id, team_name, manager_id, co_manager_id FROM teams WHERE team_role_id=?", (team_role_id,))
    r = c.fetchone()
    if not r:
        return None
    return {"team_role_id": r[0], "team_name": r[1], "manager_id": r[2], "co_manager_id": r[3]}

def set_team_manager(team_role_id: int, manager_id: int | None):
    c.execute("UPDATE teams SET manager_id=? WHERE team_role_id=?", (manager_id, team_role_id))
    conn.commit()

def set_team_co_manager(team_role_id: int, co_manager_id: int | None):
    c.execute("UPDATE teams SET co_manager_id=? WHERE team_role_id=?", (co_manager_id, team_role_id))
    conn.commit()

def is_user_manager_of_team(user_id: int, team_role_id: int) -> bool:
    rec = get_team_record(team_role_id)
    return bool(rec and rec["manager_id"] == user_id)

def is_user_co_manager_of_team(user_id: int, team_role_id: int) -> bool:
    rec = get_team_record(team_role_id)
    return bool(rec and rec["co_manager_id"] == user_id)

def is_user_on_team(user_id: int, team_role_id: int) -> bool:
    return get_player_team(user_id) == team_role_id

# custom admin roles
def get_admin_role_ids(guild_id: int) -> set[int]:
    c.execute("SELECT role_id FROM guild_admin_roles WHERE guild_id=?", (guild_id,))
    return {row[0] for row in c.fetchall()}

def add_admin_role(guild_id: int, role_id: int):
    c.execute("INSERT OR IGNORE INTO guild_admin_roles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id))
    conn.commit()

def remove_admin_role(guild_id: int, role_id: int):
    c.execute("DELETE FROM guild_admin_roles WHERE guild_id=? AND role_id=?", (guild_id, role_id))
    conn.commit()

def is_custom_admin(member: discord.Member) -> bool:
    guild = member.guild
    admin_ids = get_admin_role_ids(guild.id)
    if admin_ids and any(r.id in admin_ids for r in member.roles):
        return True
    # Safe fallbacks so you can't lock yourself out
    if member == guild.owner:
        return True
    if member.guild_permissions.administrator:
        return True
    return False

# =========================
# OVER-CAP WARNINGS
# =========================

def relevant_rank_role_ids(guild: discord.Guild):
    cfg = get_guild_roles(guild.id)
    return {rid for rid in [cfg["manager"], cfg["co_manager"], cfg["t1_3"], cfg["t4_10"], cfg["t11_20"]] if rid}

def get_team_staff(guild: discord.Guild, team_role_id: int):
    managers, co_managers = [], []
    for m in guild.members:
        if get_player_team(m.id) == team_role_id:
            cat = get_player_category(m)
            if cat == "manager":
                managers.append(m)
            elif cat == "co_manager":
                co_managers.append(m)
    return managers, co_managers

async def check_team_caps_and_warn(guild: discord.Guild, team_role_id: int):
    if team_role_id is None:
        return

    # cooldown
    now = time.time()
    if now - _last_warn_at.get(team_role_id, 0) < WARN_COOLDOWN_SECONDS:
        return

    _, _, tier_counts, _ = count_team_categories(team_role_id, guild)
    overages = []
    for tier, cap in TIER_CAPS.items():
        cnt = tier_counts[tier]
        if cnt > cap:
            names = []
            for m in guild.members:
                if get_player_team(m.id) == team_role_id:
                    cat = get_player_category(m)
                    if isinstance(cat, tuple) and cat[0] == "tiered" and cat[1] == tier:
                        names.append(m.display_name)
            overages.append((tier, cnt, cap, names))
    if not overages:
        return

    lines = [
        "⚠️ **Roster Cap Warning**",
        "Your team is currently **over the cap** in the following category(ies):",
    ]
    for tier, cnt, cap, names in overages:
        lines.append(f"- **{tier}**: {cnt}/{cap}  → Over by **{cnt - cap}**")
        if names:
            lines.append(f"  Players: {', '.join(names)}")
    lines.append("")
    lines.append("Please adjust your roster (release or reassign ranks) to return within the caps.")
    text = "\n".join(lines)

    managers, co_managers = get_team_staff(guild, team_role_id)
    recipients = managers + co_managers

    sent_any = False
    for u in recipients:
        try:
            dm = await u.create_dm()
            await dm.send(text)
            sent_any = True
        except discord.Forbidden:
            pass

    if not sent_any:
        sc_id = get_signing_channel(guild.id)
        channel = guild.get_channel(sc_id) if sc_id else guild.system_channel
        if channel:
            await channel.send(text)

    _last_warn_at[team_role_id] = now

# =========================
# EVENTS
# =========================

@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online!")
    try:
        synced = await bot.tree.sync()
        print(f"🔄 Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"⚠️ Sync failed: {e}")

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    before_ids = {r.id for r in before.roles}
    after_ids  = {r.id for r in after.roles}
    if before_ids == after_ids:
        return

    tracked = relevant_rank_role_ids(after.guild)
    changed_ids = before_ids ^ after_ids
    if tracked:
        if not (changed_ids & tracked):
            return
    else:
        # fallback by names if nothing configured
        before_names = {r.name for r in before.roles}
        after_names  = {r.name for r in after.roles}
        name_tracked = set(TIER_CAPS.keys()) | {"Manager", "Co-Manager"}
        if before_names == after_names or not ((before_names ^ after_names) & name_tracked):
            return

    team_role_id = get_player_team(after.id)
    if team_role_id is None:
        return
    await check_team_caps_and_warn(after.guild, team_role_id)

# =========================
# ADMIN: CUSTOM ADMIN ROLES
# =========================

@bot.tree.command(name="addadminrole", description="Add a role that can use league admin commands")
async def addadminrole(interaction: discord.Interaction, role: discord.Role):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return
    add_admin_role(interaction.guild.id, role.id)
    await interaction.response.send_message(f"✅ Added league admin role: {role.mention}", ephemeral=True)

@bot.tree.command(name="removeadminrole", description="Remove a role from league admins")
async def removeadminrole(interaction: discord.Interaction, role: discord.Role):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return
    remove_admin_role(interaction.guild.id, role.id)
    await interaction.response.send_message(f"✅ Removed league admin role: {role.mention}", ephemeral=True)

@bot.tree.command(name="viewadminroles", description="List roles that can use league admin commands")
async def viewadminroles(interaction: discord.Interaction):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return
    ids = list(get_admin_role_ids(interaction.guild.id))
    if not ids:
        await interaction.response.send_message(
            "ℹ️ No custom admin roles set yet.\n"
            "Tip: add one with `/addadminrole @Role`. Until then, the guild owner and users with Discord Administrator can act as admins.",
            ephemeral=True
        ); return
    mentions = [(interaction.guild.get_role(rid).mention if interaction.guild.get_role(rid) else f"`(deleted {rid})`") for rid in ids]
    await interaction.response.send_message("🛡️ League admin roles:\n• " + "\n• ".join(mentions), ephemeral=True)

# =========================
# ADMIN: CHANNELS
# =========================

@bot.tree.command(name="setsigningchannel", description="Set the channel for signings announcements")
async def setsigningchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return
    set_signing_channel(interaction.guild.id, channel.id)
    await interaction.response.send_message(f"✅ Signings channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="setreleasechannel", description="Set the channel for releases announcements")
async def setreleasechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return
    set_release_channel(interaction.guild.id, channel.id)
    await interaction.response.send_message(f"✅ Releases channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="viewsettings", description="View the current announcement channel settings")
async def viewsettings(interaction: discord.Interaction):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return
    gid = interaction.guild.id
    sc_id = get_signing_channel(gid)
    rc_id = get_release_channel(gid)
    sc = interaction.guild.get_channel(sc_id) if sc_id else None
    rc = interaction.guild.get_channel(rc_id) if rc_id else None
    embed = discord.Embed(title="📢 Current Channel Settings", color=discord.Color.green())
    embed.add_field(name="Signings Channel", value=sc.mention if sc else "❌ Not Set", inline=False)
    embed.add_field(name="Releases Channel", value=rc.mention if rc else "❌ Not Set", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# =========================
# ADMIN: ROLE CONFIG (Manager / Co-Manager / Tiers)
# =========================

@bot.tree.command(name="setmanagerrole", description="Set which role counts as Manager")
async def setmanagerrole(interaction: discord.Interaction, role: discord.Role):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return
    set_guild_role(interaction.guild.id, "manager", role.id)
    await interaction.response.send_message(f"✅ Manager role set to {role.mention}", ephemeral=True)

@bot.tree.command(name="setcomanagerrole", description="Set which role counts as Co-Manager")
async def setcomanagerrole(interaction: discord.Interaction, role: discord.Role):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return
    set_guild_role(interaction.guild.id, "co_manager", role.id)
    await interaction.response.send_message(f"✅ Co-Manager role set to {role.mention}", ephemeral=True)

@bot.tree.command(name="settierrole", description="Set which role counts for a given tier")
@app_commands.describe(tier="Choose the tier", role="Role that represents this tier")
@app_commands.choices(tier=[
    app_commands.Choice(name="TOP 1-3", value="t1_3"),
    app_commands.Choice(name="TOP 4-10", value="t4_10"),
    app_commands.Choice(name="TOP 11-20", value="t11_20"),
])
async def settierrole(interaction: discord.Interaction, tier: app_commands.Choice[str], role: discord.Role):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return
    set_guild_role(interaction.guild.id, tier.value, role.id)
    await interaction.response.send_message(f"✅ Tier **{tier.name}** role set to {role.mention}", ephemeral=True)

@bot.tree.command(name="viewroles", description="View the configured Manager/Co-Manager/Tier roles")
async def viewroles(interaction: discord.Interaction):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return
    cfg = get_guild_roles(interaction.guild.id)
    def fmt(rid):
        r = interaction.guild.get_role(rid) if rid else None
        return r.mention if r else "❌ Not Set"
    embed = discord.Embed(title="🛠️ Configured Rank Roles", color=discord.Color.orange())
    embed.add_field(name="Manager", value=fmt(cfg["manager"]), inline=False)
    embed.add_field(name="Co-Manager", value=fmt(cfg["co_manager"]), inline=False)
    embed.add_field(name="TOP 1-3", value=fmt(cfg["t1_3"]), inline=False)
    embed.add_field(name="TOP 4-10", value=fmt(cfg["t4_10"]), inline=False)
    embed.add_field(name="TOP 11-20", value=fmt(cfg["t11_20"]), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# =========================
# TEAMS: REGISTER / CREATE / MANAGE
# =========================

@bot.tree.command(name="registerteam", description="Add a pre-made role to the allowed pool of teams")
async def registerteam(interaction: discord.Interaction, team: discord.Role):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return
    register_team(team.id, team.name)
    await interaction.response.send_message(f"✅ Registered **{team.name}** as a selectable team.", ephemeral=True)

@bot.tree.command(name="createteam", description="Claim a registered team; grants you that team role (requires Manager rank role)")
@app_commands.describe(team="Select the pre-registered team role to manage")
async def createteam(interaction: discord.Interaction, team: discord.Role):
    guild = interaction.guild
    user = interaction.user

    # Team must be registered
    rec = get_team_record(team.id)
    if not rec:
        await interaction.response.send_message("❌ That team role is not registered yet. Ask a league admin to run `/registerteam` first.", ephemeral=True); return

    # Caller must ALREADY have the configured Manager rank role (eligibility)
    manager_role, co_manager_role = ensure_rank_roles_exist(guild)
    if manager_role is None:
        await interaction.response.send_message("❌ Missing required rank role: `Manager`. League admin: set it with `/setmanagerrole`.", ephemeral=True); return
    if manager_role not in user.roles:
        await interaction.response.send_message(f"❌ You need the {manager_role.mention} role to create/claim a team.", ephemeral=True); return

    # Team cannot already have a manager
    if rec["manager_id"]:
        if rec["manager_id"] == user.id:
            await interaction.response.send_message("ℹ️ You already manage this team.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ This team is already managed by someone else.", ephemeral=True)
        return

    # Enforce single manager on-roster
    m_count, cm_count, _, _ = count_team_categories(team.id, guild)
    if m_count >= 1:
        await interaction.response.send_message("❌ This team already has a Manager on-roster.", ephemeral=True); return

    # If user is on a different team, remove that role first
    current_team_id = get_player_team(user.id)
    if current_team_id and current_team_id != team.id:
        old_role = guild.get_role(current_team_id)
        try:
            if old_role:
                await user.remove_roles(old_role, reason="Moving to manage a new team")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to remove your old team role.", ephemeral=True); return

    # Grant the chosen team role (Manager role already present; do NOT change it)
    try:
        if team not in user.roles:
            await user.add_roles(team, reason="Claimed team as Manager")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to assign the team role.", ephemeral=True); return

    # Persist
    add_or_update_player(user.id, user.display_name, team.id)
    set_team_manager(team.id, user.id)

    await interaction.response.send_message(f"✅ You are now the **Manager** of **{team.name}** and have been given the team role.", ephemeral=True)

@bot.tree.command(name="setcomanager", description="(Manager only) Appoint a Co-Manager for your team")
@app_commands.describe(team="Your team role", user="Member to appoint as Co-Manager")
async def setcomanager(interaction: discord.Interaction, team: discord.Role, user: discord.Member):
    guild = interaction.guild
    if not is_user_manager_of_team(interaction.user.id, team.id):
        await interaction.response.send_message("❌ Only the current Manager of that team can set a Co-Manager.", ephemeral=True); return

    _, co_manager_role = ensure_rank_roles_exist(guild)
    if co_manager_role is None:
        await interaction.response.send_message("❌ Missing required rank role: `Co-Manager`.", ephemeral=True); return

    _, cm_count, _, _ = count_team_categories(team.id, guild)
    if cm_count >= 1:
        await interaction.response.send_message("❌ This team already has a Co-Manager on-roster.", ephemeral=True); return

    try:
        if not is_user_on_team(user.id, team.id):
            current_team_id = get_player_team(user.id)
            if current_team_id and current_team_id != team.id:
                old_role = guild.get_role(current_team_id)
                if old_role:
                    await user.remove_roles(old_role)
            await user.add_roles(team)
        await user.add_roles(co_manager_role)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to assign roles.", ephemeral=True); return

    add_or_update_player(user.id, user.display_name, team.id)
    set_team_co_manager(team.id, user.id)
    await interaction.response.send_message(f"✅ {user.mention} is now **Co-Manager** of **{team.name}**.", ephemeral=True)

@bot.tree.command(name="listteams", description="Show registered teams and who manages them")
async def listteams(interaction: discord.Interaction):
    guild = interaction.guild
    c.execute("SELECT team_role_id, team_name, manager_id, co_manager_id FROM teams ORDER BY team_name COLLATE NOCASE")
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("ℹ️ No teams are registered yet. League admins can use `/registerteam`.", ephemeral=True); return
    lines = []
    for role_id, team_name, manager_id, co_manager_id in rows:
        role = guild.get_role(role_id)
        role_text = role.mention if role else f"{team_name} (role missing)"
        mgr = guild.get_member(manager_id) if manager_id else None
        com = guild.get_member(co_manager_id) if co_manager_id else None
        mgr_text = mgr.display_name if mgr else "—"
        com_text = com.display_name if com else "—"
        lines.append(f"{role_text}\n  • Manager: {mgr_text}\n  • Co-Manager: {com_text}")
    embed = discord.Embed(title="📜 Registered Teams", description="\n\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(
    name="transferteam",
    description="(League admin) Transfer a team's Manager to another member"
)
@app_commands.describe(
    team="The team role to transfer",
    new_manager="The member who will become the new Manager",
    make_old_co_manager="If true, demote the old Manager to Co-Manager for this team"
)
async def transferteam(interaction: discord.Interaction, team: discord.Role, new_manager: discord.Member, make_old_co_manager: bool = False):
    if not is_custom_admin(interaction.user):
        await interaction.response.send_message("❌ You must be a league admin to use this.", ephemeral=True); return

    guild = interaction.guild
    rec = get_team_record(team.id)
    if not rec:
        await interaction.response.send_message("❌ That team role is not registered. Use `/registerteam` first.", ephemeral=True); return

    manager_role, co_manager_role = ensure_rank_roles_exist(guild)
    if manager_role is None:
        await interaction.response.send_message("❌ Missing required rank role: `Manager`.", ephemeral=True); return
    if make_old_co_manager and co_manager_role is None:
        await interaction.response.send_message("❌ Missing required rank role: `Co-Manager`.", ephemeral=True); return

    old_manager_member = guild.get_member(rec["manager_id"]) if rec["manager_id"] else None

    current_team_id = get_player_team(new_manager.id)
    if current_team_id and current_team_id != team.id:
        old_team_role = guild.get_role(current_team_id)
        try:
            if old_team_role:
                await new_manager.remove_roles(old_team_role)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Can't modify roles for the new manager (permissions).", ephemeral=True); return

    try:
        if team not in new_manager.roles:
            await new_manager.add_roles(team)
        if manager_role not in new_manager.roles:
            await new_manager.add_roles(manager_role)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Can't assign Manager/team roles to the new manager.", ephemeral=True); return

    add_or_update_player(new_manager.id, new_manager.display_name, team.id)
    set_team_manager(team.id, new_manager.id)

    if old_manager_member and old_manager_member.id != new_manager.id:
        try:
            if manager_role in old_manager_member.roles:
                await old_manager_member.remove_roles(manager_role)
            if make_old_co_manager:
                if team not in old_manager_member.roles:
                    await old_manager_member.add_roles(team)
                if co_manager_role not in old_manager_member.roles:
                    await old_manager_member.add_roles(co_manager_role)
                add_or_update_player(old_manager_member.id, old_manager_member.display_name, team.id)
                set_team_co_manager(team.id, old_manager_member.id)
            else:
                if rec["co_manager_id"] == old_manager_member.id:
                    set_team_co_manager(team.id, None)
        except discord.Forbidden:
            await interaction.response.send_message("⚠️ Transferred, but couldn't update roles for the old manager.", ephemeral=True); return

    await interaction.response.send_message(
        f"✅ Transferred **{team.name}** manager to **{new_manager.display_name}**"
        + (" and set the previous manager as **Co-Manager**." if (old_manager_member and make_old_co_manager) else "."),
        ephemeral=True
    )

# =========================
# SIGN / RELEASE / ROSTER
# =========================

@bot.tree.command(name="sign", description="Sign a player to your team (requires their approval)")
@app_commands.describe(team="Select the team role", player="Player to sign")
async def sign(interaction: discord.Interaction, team: discord.Role, player: discord.Member):
    guild = interaction.guild

    # Only that team's Manager or Co-Manager can sign to that team
    if not (is_user_manager_of_team(interaction.user.id, team.id) or is_user_co_manager_of_team(interaction.user.id, team.id)):
        await interaction.response.send_message("❌ You must be this team’s **Manager** or **Co-Manager** to sign players to it.", ephemeral=True); return

    manager_count, co_manager_count, tier_counts, unranked_count = count_team_categories(team.id, guild)
    total_roster = manager_count + co_manager_count + sum(tier_counts.values()) + unranked_count
    if total_roster >= MAX_TEAM_SIZE:
        await interaction.response.send_message(f"❌ Team roster is full ({MAX_TEAM_SIZE}).", ephemeral=True); return

    player_cat = get_player_category(player)
    if player_cat == "manager" and manager_count >= 1:
        await interaction.response.send_message("❌ Manager spot already filled.", ephemeral=True); return
    if player_cat == "co_manager" and co_manager_count >= 1:
        await interaction.response.send_message("❌ Co-Manager spot already filled.", ephemeral=True); return
    if isinstance(player_cat, tuple) and player_cat[0] == "tiered":
        tier_name = player_cat[1]
        if tier_counts[tier_name] >= TIER_CAPS[tier_name]:
            await interaction.response.send_message(f"❌ Tier {tier_name} is full.", ephemeral=True); return

    # DM approval for ALL players
    try:
        dm = await player.create_dm()
        await dm.send(f"✍️ Signing Request: You are being signed to **{team.name}**. Type `accept` or `decline`.")
        await interaction.response.send_message(f"✅ Signing request sent to {player.display_name}.", ephemeral=True)

        def check(m: discord.Message):
            return m.author == player and m.content.lower() in ("accept", "decline")

        try:
            msg = await bot.wait_for("message", check=check, timeout=86400)
            if msg.content.lower() == "accept":
                current_team_id = get_player_team(player.id)
                old_team_role = guild.get_role(current_team_id) if current_team_id else None
                if old_team_role:
                    await player.remove_roles(old_team_role)
                await player.add_roles(team)
                add_or_update_player(player.id, player.display_name, team.id)

                sc_id = get_signing_channel(guild.id)
                channel = guild.get_channel(sc_id) if sc_id else interaction.channel
                if old_team_role:
                    await channel.send(f"✍️ {team.name} has signed {player.display_name} from {old_team_role.name}!")
                else:
                    await channel.send(f"✍️ {team.name} has signed {player.display_name} (Free Agent)!")
                await dm.send(f"✅ You have been signed to {team.name}!")

                await check_team_caps_and_warn(guild, team.id)
            else:
                await dm.send("❌ You declined the signing.")
                await interaction.followup.send(f"{player.display_name} declined the signing.", ephemeral=True)
        except asyncio.TimeoutError:
            await dm.send("⏳ Signing request expired.")
            await interaction.followup.send(f"{player.display_name} did not respond. Signing expired.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ Cannot DM {player.display_name}. Signing cancelled.", ephemeral=True)

@bot.tree.command(name="release", description="Release a player from their team")
@app_commands.describe(player="Player to release")
async def release(interaction: discord.Interaction, player: discord.Member):
    team_id = get_player_team(player.id)
    if not team_id:
        await interaction.response.send_message("❌ Player is not on any team.", ephemeral=True); return

    if not (is_user_manager_of_team(interaction.user.id, team_id) or is_user_co_manager_of_team(interaction.user.id, team_id)):
        await interaction.response.send_message("❌ You must be this player’s team **Manager** or **Co-Manager** to release them.", ephemeral=True); return

    team_role = interaction.guild.get_role(team_id)
    await player.remove_roles(team_role)
    remove_player_from_team(player.id)

    rc_id = get_release_channel(interaction.guild.id)
    channel = interaction.guild.get_channel(rc_id) if rc_id else interaction.channel
    await channel.send(f"🗞️ {player.display_name} has been released from {team_role.name}.")
    await interaction.response.send_message(f"✅ Released {player.display_name} from {team_role.name}.")

@bot.tree.command(name="roster", description="Check a team's roster")
@app_commands.describe(team="Select the team role")
async def roster(interaction: discord.Interaction, team: discord.Role):
    guild = interaction.guild

    managers, co_managers = [], []
    tiered_players = {tier: [] for tier in TIER_CAPS}
    unranked_players = []

    for m in guild.members:
        if get_player_team(m.id) == team.id:
            cat = get_player_category(m)
            if cat == "manager":
                managers.append(m.display_name)
            elif cat == "co_manager":
                co_managers.append(m.display_name)
            elif isinstance(cat, tuple) and cat[0] == "tiered":
                tiered_players[cat[1]].append(m.display_name)
            else:
                unranked_players.append(m.display_name)

    total = len(managers) + len(co_managers) + sum(len(v) for v in tiered_players.values()) + len(unranked_players)
    remaining = MAX_TEAM_SIZE - total

    embed = discord.Embed(title=f"🏆 {team.name} Roster", color=discord.Color.blue())
    embed.description = f"**Total: {total}/{MAX_TEAM_SIZE} players**"

    embed.add_field(name=f"Managers ({len(managers)}/1)", value="\n".join(managers) if managers else "None", inline=False)
    embed.add_field(name=f"Co-Managers ({len(co_managers)}/1)", value="\n".join(co_managers) if co_managers else "None", inline=False)

    for tier, cap in TIER_CAPS.items():
        names = tiered_players[tier]
        embed.add_field(name=f"{tier} ({len(names)}/{cap})", value="\n".join(names) if names else "None", inline=False)

    embed.add_field(name="Unranked", value="\n".join(unranked_players) if unranked_players else "None", inline=False)
    embed.add_field(name="Remaining Spots", value=str(remaining), inline=False)

    await interaction.response.send_message(embed=embed)

# =========================
# RUN
# =========================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN env var not set.")
    bot.run(TOKEN)
