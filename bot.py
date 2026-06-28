import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import asyncio
import traceback
import logging
from datetime import datetime, timezone, timedelta, time
import os
from dotenv import load_dotenv
import json
import shutil

# ================= CONFIG =================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

print("Starting SKY Guild Cart...")

CHANNEL_ID = 1517597432714887380
GUILD_CART_ROLE_ID = 1515472832748982444
UTC_CHANNEL_ID = 1518244845918097540
DB = "sky_guild_cart.db"
OFFICER_ROLE_NAME = "Officer"
GUILDMASTER_ROLE_NAME = "Guild Master"

GUILDMASTER_ROLE_ID = 1515304697353994310
OFFICER_ROLE_ID = 1515305248653180949
MEMBER_ROLE_ID = 1515305910359298139

TRUSTED_USERS = [
    176308213489205249
]

LOG_CHANNEL_ID = 1515304317723213956

BACKUP_FOLDER = "backups"
PANEL_STATE_FILE = "panel_state.json"
BACKUP_KEEP_LAST = 30
CALENDAR_DAYS = 30

# ==========================================

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# 00:00 -> 23:00 UTC
CART_HOURS = [f"{hour:02d}:00" for hour in range(24)]

# Runs exactly at 00, 05, 10, 15, ... UTC.
MAINTENANCE_TIMES = [
    time(hour=hour, minute=minute, tzinfo=timezone.utc)
    for hour in range(24)
    for minute in range(0, 60, 5)
]

NIGHTLY_BACKUP_TIME = time(hour=0, minute=1, tzinfo=timezone.utc)


# ================= UTC =================

def today_utc():
    return datetime.now(timezone.utc).date()


def date_for_position(position):

    return today_utc() + timedelta(
        days=position - 1
    )


def default_cart_date(position):

    return date_for_position(position).isoformat()


def valid_date(value: str):

    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def valid_hour(value: str):
    return value in CART_HOURS


def format_cart_date_label(date_obj):
    return date_obj.strftime("%Y-%m-%d (%a)")


def migrate_database_name():

    old_db = "cart.db"

    if DB != old_db and os.path.exists(old_db) and not os.path.exists(DB):

        shutil.copy2(old_db, DB)

        print(f"Migrated database from {old_db} to {DB}.")




# ================= DATABASE =================

async def init_db():

    async with aiosqlite.connect(DB) as db:

        await db.execute("""
        CREATE TABLE IF NOT EXISTS carts(
            user_id INTEGER PRIMARY KEY,
            position INTEGER,
            hour TEXT,
            reminded INTEGER DEFAULT 0,
            manual_name TEXT
        )
        """)

        try:
            await db.execute(
                "ALTER TABLE carts ADD COLUMN manual_name TEXT"
            )
        except Exception:
            # Column already exists on upgraded databases
            pass

        try:
            await db.execute(
                "ALTER TABLE carts ADD COLUMN cart_date TEXT"
            )
        except Exception:
            # Column already exists on upgraded databases
            pass

        cursor = await db.execute(
            """
            SELECT user_id, position
            FROM carts
            WHERE cart_date IS NULL OR cart_date=''
            ORDER BY position
            """
        )

        rows = await cursor.fetchall()

        for user_id, position in rows:
            await db.execute(
                """
                UPDATE carts
                SET cart_date=?
                WHERE user_id=?
                """,
                (default_cart_date(position), user_id)
            )

        await db.commit()


# ================= GLOBAL STATE ===============

queue_message = None
backup_message = None
officer_message = None


# ================= QUEUE =================

async def get_queue():

    async with aiosqlite.connect(DB) as db:

        cursor = await db.execute(
            """
            SELECT user_id, position, hour, manual_name, cart_date
            FROM carts
            ORDER BY cart_date, hour, position
            """
        )

        return await cursor.fetchall()


async def get_all_members(guild=None):

    result = []
    seen_ids = set()

    if guild:

        guild_members = sorted(
            [
                member
                for member in guild.members
                if not member.bot
            ],
            key=lambda member: member.display_name.lower()
        )

        for member in guild_members:

            result.append({
                "id": member.id,
                "name": f"👤 {member.display_name}"
            })

            seen_ids.add(member.id)

    async with aiosqlite.connect(DB) as db:

        cursor = await db.execute(
            """
            SELECT user_id, manual_name
            FROM carts
            ORDER BY position
            """
        )

        rows = await cursor.fetchall()

    for uid, manual_name in rows:

        if manual_name:

            result.append({
                "id": uid,
                "name": f"📝 {manual_name}"
            })

        elif uid not in seen_ids:

            user = bot.get_user(uid)

            if user:

                result.append({
                    "id": uid,
                    "name": f"👤 {user.display_name}"
                })

                seen_ids.add(uid)

            else:

                result.append({
                    "id": uid,
                    "name": f"👤 <@{uid}>"
                })

                seen_ids.add(uid)

    return result


async def get_user(user_id):

    async with aiosqlite.connect(DB) as db:

        cursor = await db.execute(
            """
            SELECT position,hour,cart_date
            FROM carts
            WHERE user_id=?
            """,
            (user_id,)
        )

        return await cursor.fetchone()


async def date_is_available(cart_date: str, ignore_user_id: int | None = None):

    async with aiosqlite.connect(DB) as db:
        if ignore_user_id is None:
            cursor = await db.execute(
                """
                SELECT user_id
                FROM carts
                WHERE cart_date=?
                LIMIT 1
                """,
                (cart_date,)
            )
        else:
            cursor = await db.execute(
                """
                SELECT user_id
                FROM carts
                WHERE cart_date=? AND user_id!=?
                LIMIT 1
                """,
                (cart_date, ignore_user_id)
            )

        return await cursor.fetchone() is None


async def get_available_cart_dates(days: int = CALENDAR_DAYS, ignore_user_id: int | None = None):

    start = today_utc()
    date_objects = [start + timedelta(days=offset) for offset in range(days)]

    async with aiosqlite.connect(DB) as db:
        if ignore_user_id is None:
            cursor = await db.execute(
                """
                SELECT cart_date
                FROM carts
                WHERE date(cart_date) >= date(?)
                  AND date(cart_date) < date(?)
                """,
                (start.isoformat(), (start + timedelta(days=days)).isoformat())
            )
        else:
            cursor = await db.execute(
                """
                SELECT cart_date
                FROM carts
                WHERE user_id!=?
                  AND date(cart_date) >= date(?)
                  AND date(cart_date) < date(?)
                """,
                (ignore_user_id, start.isoformat(), (start + timedelta(days=days)).isoformat())
            )

        taken_dates = {row[0] for row in await cursor.fetchall()}

    return [date_obj for date_obj in date_objects if date_obj.isoformat() not in taken_dates]


def build_calendar_lines(available_dates):

    available_set = {date_obj.isoformat() for date_obj in available_dates}
    start = today_utc()
    lines = []

    for offset in range(CALENDAR_DAYS):
        date_obj = start + timedelta(days=offset)
        status = "✅" if date_obj.isoformat() in available_set else "❌"
        lines.append(f"{status} {format_cart_date_label(date_obj)}")

    return lines

def has_admin_access(member):

    if member.id in TRUSTED_USERS:
        return True

    role_ids = [role.id for role in member.roles]

    return (
        GUILDMASTER_ROLE_ID in role_ids
        or OFFICER_ROLE_ID in role_ids
    )


def is_officer(member):

    role_ids = [role.id for role in member.roles]

    return (
        GUILDMASTER_ROLE_ID in role_ids
        or OFFICER_ROLE_ID in role_ids
    )

# ================= BACKUP HELPERS =================

def create_backup_folder():

    os.makedirs(BACKUP_FOLDER, exist_ok=True)


def prune_old_backups():

    create_backup_folder()

    backup_files = sorted(
        [
            os.path.join(BACKUP_FOLDER, filename)
            for filename in os.listdir(BACKUP_FOLDER)
            if filename.endswith(".db")
        ],
        key=os.path.getmtime,
        reverse=True
    )

    for old_file in backup_files[BACKUP_KEEP_LAST:]:
        try:
            os.remove(old_file)
        except Exception:
            traceback.print_exc()


async def create_backup():

    create_backup_folder()

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%d_%H%M%S"
    )

    backup_file = os.path.join(
        BACKUP_FOLDER,
        f"sky_guild_cart_{timestamp}.db"
    )

    shutil.copy2(DB, backup_file)

    prune_old_backups()

    return backup_file


# ================= MOD LOGGING =================

async def log_action(user, action):

    channel = bot.get_channel(LOG_CHANNEL_ID)

    if not channel:
        return

    try:

        await channel.send(
            f"⚜️ **GuildCart Log**\n"
            f"👤 {user.mention}\n"
            f"📌 {action}"
        )

    except Exception:
        traceback.print_exc()


async def compress_queue():

    rows = await get_queue()

    async with aiosqlite.connect(DB) as db:

        for index, row in enumerate(rows, start=1):

            uid = row[0]

            await db.execute(
                """
                UPDATE carts
                SET position=?
                WHERE user_id=?
                """,
                (index, uid)
            )

        await db.commit()


async def cleanup_expired_carts():

    """Remove carts that are already in the past using UTC date + hour."""

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    deleted = 0

    async with aiosqlite.connect(DB) as db:

        # Make sure old upgraded rows without cart_date are still usable.
        cursor = await db.execute(
            """
            SELECT user_id, position
            FROM carts
            WHERE cart_date IS NULL OR cart_date=''
            ORDER BY position
            """
        )

        missing_dates = await cursor.fetchall()

        for user_id, position in missing_dates:
            await db.execute(
                """
                UPDATE carts
                SET cart_date=?
                WHERE user_id=?
                """,
                (default_cart_date(position), user_id)
            )

        cursor = await db.execute(
            """
            DELETE FROM carts
            WHERE datetime(cart_date || ' ' || hour) < datetime(?)
            """,
            (now,)
        )

        deleted = cursor.rowcount
        await db.commit()

    if deleted:
        await compress_queue()

    return deleted


# ================= MOVE LOGIC =================

async def move_member(user_id: int, direction: str):

    rows = await get_queue()
    ids = [row[0] for row in rows]

    if user_id not in ids:
        return False

    index = ids.index(user_id)

    if direction == "up":
        if index == 0:
            return False
        new_index = index - 1

    elif direction == "down":
        if index == len(ids) - 1:
            return False
        new_index = index + 1

    else:
        return False

    ids[index], ids[new_index] = ids[new_index], ids[index]

    async with aiosqlite.connect(DB) as db:

        for pos, uid in enumerate(ids, start=1):
            await db.execute(
                """
                UPDATE carts
                SET position=?
                WHERE user_id=?
                """,
                (pos, uid)
            )

        await db.commit()

    return True


async def get_queue_position(user_id: int):

    rows = await get_queue()

    for uid, position, hour, manual_name, cart_date in rows:
        if uid == user_id:
            return position

    return None


# ================= PAGINATION HELPER =================

PAGE_SIZE = 25


def paginate(items, page: int):
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    return items[start:end]


async def refresh_queue():

    global queue_message, backup_message, officer_message

    try:
        if not queue_message:
            channel = bot.get_channel(CHANNEL_ID)
            state = load_panel_state()

            if channel and state.get("queue_message_id"):
                queue_message = await get_saved_message(
                    channel,
                    state.get("queue_message_id")
                )

        if not queue_message:
            return

        await queue_message.edit(
            embed=await build_queue_embed(),
            view=CartView()
        )

    except Exception:
        traceback.print_exc()


# ================= EMBED =================

async def build_queue_embed():

    await cleanup_expired_carts()

    async with aiosqlite.connect(DB) as db:

        cursor = await db.execute(
            """
            SELECT user_id, position, hour, manual_name, cart_date
            FROM carts
            ORDER BY cart_date, hour, position
            """
        )

        rows = await cursor.fetchall()

    embed = discord.Embed(
        title="🚚 SKY Guild Cart Queue (UTC)",
        colour=discord.Colour.green()
    )

    if not rows:

        embed.description = (
            "No carts are currently scheduled.\n\n"
            "Use ➕ **Join Queue** to claim the next available cart."
        )

        return embed

    text = ""

    for uid, pos, hour, manual_name, cart_date in rows:

        if manual_name:

            mention = f"**{manual_name}**"

        else:

            try:

                user = await bot.fetch_user(uid)

                mention = user.mention

            except:

                mention = f"<@{uid}>"

        if not cart_date:
            cart_date = default_cart_date(pos)

        today = datetime.now(timezone.utc).date()
        tomorrow = today + timedelta(days=1)

        try:
            cart_date_obj = datetime.strptime(str(cart_date), "%Y-%m-%d").date()
        except ValueError:
            cart_date_obj = None

        badge = ""

        if cart_date_obj == today:
            badge = "🔥 TODAY "

        elif cart_date_obj == tomorrow:
            badge = "🟡 TOMORROW "

        text += (
            f"{badge}📅 {cart_date} "
            f"🕒 {hour} UTC - "
            f"{mention}\n"
        )

    embed.description = text

    embed.set_footer(
        text=f"Total scheduled carts: {len(rows)}"
    )

    return embed


# ================= JOIN HOUR SELECT =================

class JoinHourSelect(discord.ui.Select):

    def __init__(self):

        options = [

            discord.SelectOption(
                label=f"{hour} UTC",
                value=hour
            )

            for hour in CART_HOURS

        ]

        super().__init__(
            placeholder="Choose a cart hour",
            options=options
        )

    async def callback(self, interaction):

        try:

            user = await get_user(
                interaction.user.id
            )

            if user:

                await interaction.response.send_message(
                    "⚠️ You are already in queue.",
                    ephemeral=True
                )

                return

            rows = await get_queue()

            position = len(rows) + 1

            async with aiosqlite.connect(DB) as db:

                await db.execute(
                    """
                    INSERT INTO carts(
                    user_id,
                    position,
                    hour,
                    cart_date
                    )
                    VALUES(?,?,?,?)
                    """,
                    (
                        interaction.user.id,
                        position,
                        self.values[0],
                        default_cart_date(position)
                    )
                )

                await db.commit()

            date = date_for_position(
                position
            )

            await interaction.response.send_message(

                f"✅ Added to queue.\n\n"
                f"📅 {date}\n"
                f"🕒 {self.values[0]} UTC",

                ephemeral=True
            )

            await refresh_queue()

        except:

            traceback.print_exc()


class JoinHourView(discord.ui.View):

    def __init__(self):

        super().__init__(
            timeout=60
        )

        self.add_item(
            JoinHourSelect()
        )


# ================= EDIT HOUR =================

class EditHourSelect(discord.ui.Select):

    def __init__(self):

        options = [

            discord.SelectOption(
                label=f"{hour} UTC",
                value=hour
            )

            for hour in CART_HOURS

        ]

        super().__init__(
            placeholder="Choose a new hour",
            options=options
        )

    async def callback(self, interaction):

        try:

            user = await get_user(
                interaction.user.id
            )

            if not user:

                await interaction.response.send_message(
                    "You are not in queue.",
                    ephemeral=True
                )

                return

            async with aiosqlite.connect(DB) as db:

                await db.execute(
                    """
                    UPDATE carts
                    SET hour=?, reminded=0
                    WHERE user_id=?
                    """,
                    (
                        self.values[0],
                        interaction.user.id
                    )
                )

                await db.commit()

            await interaction.response.send_message(
                f"✅ Hour changed to "
                f"{self.values[0]} UTC",
                ephemeral=True
            )

            await refresh_queue()

        except:

            traceback.print_exc()


class EditHourView(discord.ui.View):

    def __init__(self):

        super().__init__(
            timeout=60
        )

        self.add_item(
            EditHourSelect()
        )

# ================= BUTTONS =================

class CartView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    # -------- JOIN --------

    @discord.ui.button(
        label="Join Queue",
        emoji="➕",
        style=discord.ButtonStyle.green,
        custom_id="join_queue"
    )
    async def join_button(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button):

        await interaction.response.send_message(
            "Choose a cart hour:",
            view=JoinHourView(),
            ephemeral=True
        )

    # -------- EDIT HOUR --------

    @discord.ui.button(
        label="Edit Hour",
        emoji="✏️",
        style=discord.ButtonStyle.blurple,
        custom_id="edit_hour"
    )
    async def edit_button(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button):

        user = await get_user(
            interaction.user.id
        )

        if not user:

            await interaction.response.send_message(
                "You are not in queue.",
                ephemeral=True
            )

            return

        await interaction.response.send_message(
            "Choose a new hour:",
            view=EditHourView(),
            ephemeral=True
        )

    # -------- POSTPONE --------
    # ONLY changes hour, NOT position

    @discord.ui.button(
        label="Postpone Hour",
        emoji="⏩",
        style=discord.ButtonStyle.secondary,
        custom_id="postpone_hour"
    )
    async def postpone_button(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button):

        user = await get_user(
            interaction.user.id
        )

        if not user:

            await interaction.response.send_message(
                "You are not in queue.",
                ephemeral=True
            )

            return

        await interaction.response.send_message(
            "Choose your new hour:",
            view=EditHourView(),
            ephemeral=True
        )

    # -------- VIEW --------

    @discord.ui.button(
        label="View Queue",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
        custom_id="view_queue"
    )
    async def view_button(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button):

        embed = await build_queue_embed()

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    # -------- LEAVE --------

    @discord.ui.button(
        label="Leave Queue",
        emoji="❌",
        style=discord.ButtonStyle.red,
        custom_id="leave_queue"
    )
    async def leave_button(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button):

        async with aiosqlite.connect(DB) as db:

            await db.execute(
                """
                DELETE FROM carts
                WHERE user_id=?
                """,
                (
                    interaction.user.id,
                )
            )

            await db.commit()

        await compress_queue()
        await refresh_queue()

        await interaction.response.send_message(
            "❌ Removed from queue.",
            ephemeral=True
        )


class OfficerView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Backup Queue",
        emoji="💾",
        style=discord.ButtonStyle.green,
        custom_id="backup_queue"
    )
    async def backup_queue(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button):

        if not has_admin_access(interaction.user):
            return await interaction.response.send_message(
                "No permission.",
                ephemeral=True
            )

        backup_file = await create_backup()

        await log_action(
            interaction.user,
            f"created backup `{os.path.basename(backup_file)}`"
        )

        await interaction.response.send_message(
            "Backup created.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Restore Backup",
        emoji="♻️",
        style=discord.ButtonStyle.blurple,
        custom_id="restore_backup"
    )
    async def restore_backup(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button):

        if not has_admin_access(interaction.user):
            return await interaction.response.send_message(
                "No permission.",
                ephemeral=True
            )

        create_backup_folder()

        files = sorted(
            [
                filename
                for filename in os.listdir(BACKUP_FOLDER)
                if filename.endswith(".db")
            ],
            reverse=True
        )

        if not files:
            return await interaction.response.send_message(
                "No backups found.",
                ephemeral=True
            )

        latest = files[0]

        await create_backup()

        shutil.copy2(
            os.path.join(BACKUP_FOLDER, latest),
            DB
        )

        await init_db()
        await refresh_queue()

        await log_action(
            interaction.user,
            f"restored backup `{latest}`"
        )

        await interaction.response.send_message(
            f"Restored {latest}",
            ephemeral=True
        )


# ================= OFFICER PANEL (SEARCH MODAL SYSTEM) =================

async def get_searchable_members(guild, include_manual=True):

    result = []
    seen_ids = set()

    if guild:
        for member in guild.members:
            if member.bot:
                continue

            result.append({
                "id": member.id,
                "name": member.display_name,
                "label": f"👤 {member.display_name}",
                "is_manual": False
            })
            seen_ids.add(member.id)

    if include_manual:
        async with aiosqlite.connect(DB) as db:
            cursor = await db.execute(
                """
                SELECT user_id, manual_name
                FROM carts
                WHERE manual_name IS NOT NULL AND manual_name!=''
                ORDER BY manual_name
                """
            )
            rows = await cursor.fetchall()

        for uid, manual_name in rows:
            if uid not in seen_ids:
                result.append({
                    "id": uid,
                    "name": manual_name,
                    "label": f"📝 {manual_name}",
                    "is_manual": True
                })
                seen_ids.add(uid)

    return sorted(result, key=lambda item: item["name"].lower())


async def find_member_matches(guild, query, include_manual=True):

    query = query.strip().lower()

    if not query:
        return []

    members = await get_searchable_members(
        guild,
        include_manual=include_manual
    )

    exact = [
        member for member in members
        if member["name"].lower() == query
    ]

    starts = [
        member for member in members
        if member not in exact
        and member["name"].lower().startswith(query)
    ]

    contains = [
        member for member in members
        if member not in exact
        and member not in starts
        and query in member["name"].lower()
    ]

    return (exact + starts + contains)[:25]


async def perform_officer_action(interaction, action, member_ids, date_value=None, hour_value=None):

    if not has_admin_access(interaction.user):
        return await interaction.response.send_message(
            "No permission.",
            ephemeral=True
        )

    if action == "add":

        if not valid_date(date_value):
            return await interaction.response.send_message(
                "Invalid date. Use YYYY-MM-DD, example: 2026-07-01.",
                ephemeral=True
            )

        if hour_value not in CART_HOURS:
            return await interaction.response.send_message(
                "Invalid hour. Use format like 18:00, 19:00, 20:00.",
                ephemeral=True
            )

        added = 0

        async with aiosqlite.connect(DB) as db:

            rows = await get_queue()
            existing_ids = {row[0] for row in rows}
            position = len(rows)

            for member_id in member_ids:

                if member_id in existing_ids:
                    continue

                position += 1

                await db.execute(
                    """
                    INSERT OR IGNORE INTO carts(
                        user_id,
                        position,
                        hour,
                        cart_date
                    )
                    VALUES(?,?,?,?)
                    """,
                    (
                        member_id,
                        position,
                        hour_value,
                        date_value
                    )
                )

                added += 1

            await db.commit()

        await compress_queue()
        await refresh_queue()
        await refresh_officer_panel(interaction.guild)

        await log_action(
            interaction.user,
            f"added {added} member(s) at `{date_value} {hour_value} UTC`"
        )

        return await interaction.response.send_message(
            f"Added {added} member(s) at {date_value} {hour_value} UTC.",
            ephemeral=True
        )

    if action == "remove":

        removed = 0

        async with aiosqlite.connect(DB) as db:
            for member_id in member_ids:
                cursor = await db.execute(
                    """
                    DELETE FROM carts
                    WHERE user_id=?
                    """,
                    (member_id,)
                )
                removed += cursor.rowcount

            await db.commit()

        await compress_queue()
        await refresh_queue()
        await refresh_officer_panel(interaction.guild)

        await log_action(
            interaction.user,
            f"removed {removed} member(s) from the queue"
        )

        return await interaction.response.send_message(
            f"Removed {removed} member(s).",
            ephemeral=True
        )

    if action == "up":

        moved = 0

        for member_id in member_ids:
            if await move_member(member_id, "up"):
                moved += 1

        await refresh_queue()
        await refresh_officer_panel(interaction.guild)

        await log_action(
            interaction.user,
            f"moved {moved} member(s) up"
        )

        return await interaction.response.send_message(
            f"Moved {moved} member(s) up.",
            ephemeral=True
        )

    if action == "down":

        moved = 0

        for member_id in reversed(member_ids):
            if await move_member(member_id, "down"):
                moved += 1

        await refresh_queue()
        await refresh_officer_panel(interaction.guild)

        await log_action(
            interaction.user,
            f"moved {moved} member(s) down"
        )

        return await interaction.response.send_message(
            f"Moved {moved} member(s) down.",
            ephemeral=True
        )

    if action == "edit_datetime":

        if not valid_date(date_value):
            return await interaction.response.send_message(
                "Invalid date. Use YYYY-MM-DD, example: 2026-07-01.",
                ephemeral=True
            )

        if hour_value not in CART_HOURS:
            return await interaction.response.send_message(
                "Invalid hour. Use format like 18:00, 19:00, 20:00.",
                ephemeral=True
            )

        updated = 0

        async with aiosqlite.connect(DB) as db:
            for member_id in member_ids:
                cursor = await db.execute(
                    """
                    UPDATE carts
                    SET cart_date=?, hour=?, reminded=0
                    WHERE user_id=?
                    """,
                    (date_value, hour_value, member_id)
                )
                updated += cursor.rowcount

            await db.commit()

        await refresh_queue()
        await refresh_officer_panel(interaction.guild)

        await log_action(
            interaction.user,
            f"changed date/hour for {updated} member(s) to `{date_value} {hour_value} UTC`"
        )

        return await interaction.response.send_message(
            f"Updated {updated} member(s) to {date_value} at {hour_value} UTC.",
            ephemeral=True
        )


class MatchSelect(discord.ui.Select):

    def __init__(self, matches, action, date_value=None, hour_value=None):

        self.matches = matches
        self.action = action
        self.date_value = date_value
        self.hour_value = hour_value

        options = []

        for match in matches[:25]:
            options.append(
                discord.SelectOption(
                    label=match["label"][:100],
                    value=str(match["id"])
                )
            )

        super().__init__(
            placeholder="Multiple matches found, choose the correct member...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        member_id = int(self.values[0])

        await perform_officer_action(
            interaction,
            self.action,
            [member_id],
            self.date_value,
            self.hour_value
        )


class MatchSelectView(discord.ui.View):

    def __init__(self, matches, action, date_value=None, hour_value=None):
        super().__init__(timeout=60)
        self.add_item(
            MatchSelect(
                matches,
                action,
                date_value,
                hour_value
            )
        )


class OfficerSearchModal(discord.ui.Modal):

    member_name = discord.ui.TextInput(
        label="Member name",
        placeholder="Type the first letters, example: ata",
        required=True,
        max_length=50
    )

    def __init__(self, action):
        self.action = action
        title = {
            "remove": "Remove Member",
            "up": "Move Member Up",
            "down": "Move Member Down",
        }.get(action, "Find Member")
        super().__init__(title=title)

    async def on_submit(self, interaction: discord.Interaction):

        if not has_admin_access(interaction.user):
            return await interaction.response.send_message(
                "No permission.",
                ephemeral=True
            )

        matches = await find_member_matches(
            interaction.guild,
            str(self.member_name),
            include_manual=True
        )

        if not matches:
            return await interaction.response.send_message(
                "No member found with that name.",
                ephemeral=True
            )

        if len(matches) == 1:
            return await perform_officer_action(
                interaction,
                self.action,
                [matches[0]["id"]]
            )

        await interaction.response.send_message(
            "Multiple members found. Choose the correct one:",
            view=MatchSelectView(matches, self.action),
            ephemeral=True
        )


class OfficerDateHourSearchModal(discord.ui.Modal):

    member_name = discord.ui.TextInput(
        label="Member name",
        placeholder="Type the first letters, example: ata",
        required=True,
        max_length=50
    )

    cart_date = discord.ui.TextInput(
        label="Date",
        placeholder="YYYY-MM-DD",
        required=True,
        max_length=10
    )

    hour = discord.ui.TextInput(
        label="Hour UTC",
        placeholder="Example: 18:00",
        required=True,
        max_length=5
    )

    def __init__(self, action):
        self.action = action
        title = {
            "add": "Add Member",
            "edit_datetime": "Edit Date + Hour",
        }.get(action, "Find Member")
        super().__init__(title=title)

    async def on_submit(self, interaction: discord.Interaction):

        if not has_admin_access(interaction.user):
            return await interaction.response.send_message(
                "No permission.",
                ephemeral=True
            )

        date_value = str(self.cart_date).strip()
        hour_value = str(self.hour).strip()

        if not valid_date(date_value):
            return await interaction.response.send_message(
                "Invalid date. Use YYYY-MM-DD, example: 2026-07-01.",
                ephemeral=True
            )

        if hour_value not in CART_HOURS:
            return await interaction.response.send_message(
                "Invalid hour. Use format like 18:00, 19:00, 20:00.",
                ephemeral=True
            )

        matches = await find_member_matches(
            interaction.guild,
            str(self.member_name),
            include_manual=True
        )

        if not matches:
            return await interaction.response.send_message(
                "No member found with that name.",
                ephemeral=True
            )

        if len(matches) == 1:
            return await perform_officer_action(
                interaction,
                self.action,
                [matches[0]["id"]],
                date_value,
                hour_value
            )

        await interaction.response.send_message(
            "Multiple members found. Choose the correct one:",
            view=MatchSelectView(
                matches,
                self.action,
                date_value,
                hour_value
            ),
            ephemeral=True
        )


class ManualAddModal(discord.ui.Modal, title="Add Name Manually"):

    name = discord.ui.TextInput(
        label="Name",
        placeholder="Type the name here",
        required=True,
        max_length=50
    )

    cart_date = discord.ui.TextInput(
        label="Date",
        placeholder="YYYY-MM-DD",
        required=True,
        max_length=10
    )

    hour = discord.ui.TextInput(
        label="Hour UTC",
        placeholder="Example: 18:00",
        required=True,
        max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):

        date_value = str(self.cart_date).strip()
        hour_value = str(self.hour).strip()

        if not valid_date(date_value):
            return await interaction.response.send_message(
                "Invalid date. Use YYYY-MM-DD, example: 2026-07-01.",
                ephemeral=True
            )

        if hour_value not in CART_HOURS:
            return await interaction.response.send_message(
                "Invalid hour. Use format like 18:00, 19:00, 20:00.",
                ephemeral=True
            )

        rows = await get_queue()
        position = len(rows) + 1
        manual_id = -int(datetime.now().timestamp())

        async with aiosqlite.connect(DB) as db:
            await db.execute(
                """
                INSERT INTO carts(
                    user_id,
                    position,
                    hour,
                    manual_name,
                    cart_date
                )
                VALUES(?,?,?,?,?)
                """,
                (
                    manual_id,
                    position,
                    hour_value,
                    str(self.name),
                    date_value
                )
            )

            await db.commit()

        await compress_queue()
        await refresh_queue()
        await refresh_officer_panel(interaction.guild)

        await log_action(
            interaction.user,
            f"added manual name `{self.name}` at `{date_value} {hour_value} UTC`"
        )

        await interaction.response.send_message(
            f"Added manual name: {self.name} at {date_value} {hour_value} UTC",
            ephemeral=True
        )


class OfficerActionButton(discord.ui.Button):

    def __init__(self, label, action, style):
        super().__init__(
            label=label,
            style=style,
            custom_id=f"officer_action_{action}"
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction):

        if not has_admin_access(interaction.user):
            return await interaction.response.send_message(
                "No permission.",
                ephemeral=True
            )

        if self.action == "backup":
            backup_file = await create_backup()

            await log_action(
                interaction.user,
                f"created backup `{os.path.basename(backup_file)}`"
            )

            return await interaction.response.send_message(
                "✅ Backup created.",
                ephemeral=True
            )

        if self.action == "restore":
            create_backup_folder()

            files = sorted(
                [
                    filename
                    for filename in os.listdir(BACKUP_FOLDER)
                    if filename.endswith(".db")
                ],
                reverse=True
            )

            if not files:
                return await interaction.response.send_message(
                    "No backups found.",
                    ephemeral=True
                )

            latest = files[0]

            await create_backup()

            shutil.copy2(
                os.path.join(BACKUP_FOLDER, latest),
                DB
            )

            await init_db()
            await refresh_queue()
            await refresh_officer_panel(interaction.guild)

            await log_action(
                interaction.user,
                f"restored backup `{latest}`"
            )

            return await interaction.response.send_message(
                f"✅ Restored `{latest}`.",
                ephemeral=True
            )

        if self.action == "add_manual":
            return await interaction.response.send_modal(
                ManualAddModal()
            )

        if self.action in ("add", "edit_datetime"):
            return await interaction.response.send_modal(
                OfficerDateHourSearchModal(self.action)
            )

        return await interaction.response.send_modal(
            OfficerSearchModal(self.action)
        )


class OfficerPanelView(discord.ui.View):

    def __init__(self):

        super().__init__(timeout=None)

        self.add_item(OfficerActionButton("➕ Add", "add", discord.ButtonStyle.green))
        self.add_item(OfficerActionButton("📝 Manual", "add_manual", discord.ButtonStyle.green))
        self.add_item(OfficerActionButton("➖ Remove", "remove", discord.ButtonStyle.red))
        self.add_item(OfficerActionButton("⬆️ Up", "up", discord.ButtonStyle.secondary))
        self.add_item(OfficerActionButton("⬇️ Down", "down", discord.ButtonStyle.secondary))
        self.add_item(OfficerActionButton("🗓 Edit Date/Hour", "edit_datetime", discord.ButtonStyle.blurple))
        self.add_item(OfficerActionButton("💾 Backup", "backup", discord.ButtonStyle.green))
        self.add_item(OfficerActionButton("♻️ Restore", "restore", discord.ButtonStyle.blurple))


async def refresh_officer_panel(guild):

    global officer_message

    if not officer_message or not guild:
        return

    try:

        officer_embed = discord.Embed(
            title="⚜️ Officer Panel",
            description=(
                "Manage the queue using the buttons below.\n\n"
                "➕ Add member to queue\n"
                "📝 Add manual entry\n"
                "➖ Remove member\n"
                "⬆️ Move member up\n"
                "⬇️ Move member down\n"
                "🕒 Edit hour only\n"
                "🗓 Edit date and hour\n"
                "💾 Create backup\n"
                "♻️ Restore backup"
            ),
            colour=discord.Colour.gold()
        )

        await officer_message.edit(
            embed=officer_embed,
            view=OfficerPanelView()
        )

    except Exception:
        traceback.print_exc()



# ================= CALENDAR JOIN + OFFICER FLOW OVERRIDES =================

class CalendarDateSelect(discord.ui.Select):

    def __init__(self, dates, label_prefix: str, mode: str, member_id: int | None = None, manual_name: str | None = None):
        self.mode = mode
        self.member_id = member_id
        self.manual_name = manual_name

        options = [
            discord.SelectOption(
                label=format_cart_date_label(date_obj),
                value=date_obj.isoformat()
            )
            for date_obj in dates
        ]

        super().__init__(
            placeholder=f"Select cart date ({label_prefix})",
            options=options,
            min_values=1,
            max_values=1
        )

    async def callback(self, interaction: discord.Interaction):
        cart_date = self.values[0]

        ignore_user_id = self.member_id if self.mode == "officer_edit_datetime" else None

        if not await date_is_available(cart_date, ignore_user_id=ignore_user_id):
            return await interaction.response.send_message(
                "❌ This date is already taken. Choose another date.",
                ephemeral=True
            )

        await interaction.response.send_message(
            f"📅 Selected date: **{cart_date}**\n\nNow choose a cart hour:",
            view=CalendarHourView(
                self.mode,
                cart_date,
                member_id=self.member_id,
                manual_name=self.manual_name
            ),
            ephemeral=True
        )


class CalendarDateView(discord.ui.View):

    def __init__(self, available_dates, mode: str, member_id: int | None = None, manual_name: str | None = None):
        super().__init__(timeout=60)

        first_half = available_dates[:15]
        second_half = available_dates[15:30]

        if first_half:
            self.add_item(CalendarDateSelect(first_half, "first half of month", mode, member_id, manual_name))

        if second_half:
            self.add_item(CalendarDateSelect(second_half, "second half of month", mode, member_id, manual_name))


class CalendarHourSelect(discord.ui.Select):

    def __init__(self, mode: str, cart_date: str | None = None, member_id: int | None = None, manual_name: str | None = None):
        self.mode = mode
        self.cart_date = cart_date
        self.member_id = member_id
        self.manual_name = manual_name

        options = [discord.SelectOption(label=f"{hour} UTC", value=hour) for hour in CART_HOURS]
        super().__init__(placeholder="Choose cart hour", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        hour = self.values[0]

        if self.mode == "join":
            existing = await get_user(interaction.user.id)
            if existing:
                return await interaction.response.send_message("⚠️ You are already in queue.", ephemeral=True)

            if not await date_is_available(self.cart_date):
                return await interaction.response.send_message("❌ This date is already taken. Choose another date.", ephemeral=True)

            rows = await get_queue()
            position = len(rows) + 1

            async with aiosqlite.connect(DB) as db:
                await db.execute(
                    """
                    INSERT INTO carts(user_id, position, hour, cart_date)
                    VALUES(?,?,?,?)
                    """,
                    (interaction.user.id, position, hour, self.cart_date)
                )
                await db.commit()

            await refresh_queue()
            await refresh_officer_panel(interaction.guild)
            await log_action(interaction.user, f"joined the queue at `{self.cart_date} {hour} UTC`")
            return await interaction.response.send_message(
                f"✅ Added to queue.\n\n📅 {self.cart_date}\n🕒 {hour} UTC",
                ephemeral=True
            )

        if self.mode == "member_edit_hour":
            existing = await get_user(interaction.user.id)
            if not existing:
                return await interaction.response.send_message("You are not in queue.", ephemeral=True)

            async with aiosqlite.connect(DB) as db:
                await db.execute(
                    "UPDATE carts SET hour=?, reminded=0 WHERE user_id=?",
                    (hour, interaction.user.id)
                )
                await db.commit()

            await refresh_queue()
            await refresh_officer_panel(interaction.guild)
            await log_action(interaction.user, f"changed their cart hour to `{hour} UTC`")
            return await interaction.response.send_message(f"✅ Hour changed to {hour} UTC.", ephemeral=True)

        if self.mode == "officer_add":
            if not has_admin_access(interaction.user):
                return await interaction.response.send_message("No permission.", ephemeral=True)

            if not await date_is_available(self.cart_date):
                return await interaction.response.send_message("❌ This date is already taken. Choose another date.", ephemeral=True)

            rows = await get_queue()
            existing_ids = {row[0] for row in rows}
            if self.member_id in existing_ids:
                return await interaction.response.send_message("⚠️ This member is already in queue.", ephemeral=True)

            position = len(rows) + 1
            async with aiosqlite.connect(DB) as db:
                await db.execute(
                    """
                    INSERT INTO carts(user_id, position, hour, cart_date)
                    VALUES(?,?,?,?)
                    """,
                    (self.member_id, position, hour, self.cart_date)
                )
                await db.commit()

            await compress_queue()
            await refresh_queue()
            await refresh_officer_panel(interaction.guild)
            await log_action(interaction.user, f"added <@{self.member_id}> at `{self.cart_date} {hour} UTC`")
            return await interaction.response.send_message(
                f"✅ Added <@{self.member_id}> at {self.cart_date} {hour} UTC.",
                ephemeral=True
            )

        if self.mode == "officer_manual":
            if not has_admin_access(interaction.user):
                return await interaction.response.send_message("No permission.", ephemeral=True)

            if not await date_is_available(self.cart_date):
                return await interaction.response.send_message("❌ This date is already taken. Choose another date.", ephemeral=True)

            rows = await get_queue()
            position = len(rows) + 1
            manual_id = -int(datetime.now(timezone.utc).timestamp() * 1000)

            async with aiosqlite.connect(DB) as db:
                await db.execute(
                    """
                    INSERT INTO carts(user_id, position, hour, manual_name, cart_date)
                    VALUES(?,?,?,?,?)
                    """,
                    (manual_id, position, hour, self.manual_name[:50], self.cart_date)
                )
                await db.commit()

            await compress_queue()
            await refresh_queue()
            await refresh_officer_panel(interaction.guild)
            await log_action(interaction.user, f"added manual name `{self.manual_name}` at `{self.cart_date} {hour} UTC`")
            return await interaction.response.send_message(
                f"✅ Added `{self.manual_name}` at {self.cart_date} {hour} UTC.",
                ephemeral=True
            )

        if self.mode == "officer_edit_hour":
            if not has_admin_access(interaction.user):
                return await interaction.response.send_message("No permission.", ephemeral=True)

            async with aiosqlite.connect(DB) as db:
                cursor = await db.execute(
                    "UPDATE carts SET hour=?, reminded=0 WHERE user_id=?",
                    (hour, self.member_id)
                )
                await db.commit()

            await refresh_queue()
            await refresh_officer_panel(interaction.guild)
            await log_action(interaction.user, f"changed hour for `{self.member_id}` to `{hour} UTC`")
            return await interaction.response.send_message(f"✅ Updated {cursor.rowcount} member(s).", ephemeral=True)

        if self.mode == "officer_edit_datetime":
            if not has_admin_access(interaction.user):
                return await interaction.response.send_message("No permission.", ephemeral=True)

            if not await date_is_available(self.cart_date, ignore_user_id=self.member_id):
                return await interaction.response.send_message("❌ This date is already taken. Choose another date.", ephemeral=True)

            async with aiosqlite.connect(DB) as db:
                cursor = await db.execute(
                    "UPDATE carts SET cart_date=?, hour=?, reminded=0 WHERE user_id=?",
                    (self.cart_date, hour, self.member_id)
                )
                await db.commit()

            await refresh_queue()
            await refresh_officer_panel(interaction.guild)
            await log_action(interaction.user, f"changed date/hour for `{self.member_id}` to `{self.cart_date} {hour} UTC`")
            return await interaction.response.send_message(f"✅ Updated {cursor.rowcount} member(s).", ephemeral=True)


class CalendarHourView(discord.ui.View):

    def __init__(self, mode: str, cart_date: str | None = None, member_id: int | None = None, manual_name: str | None = None):
        super().__init__(timeout=60)
        self.add_item(CalendarHourSelect(mode, cart_date, member_id, manual_name))


async def send_calendar_date_picker(interaction: discord.Interaction, mode: str, member_id: int | None = None, manual_name: str | None = None):

    ignore_user_id = member_id if mode == "officer_edit_datetime" else None
    available_dates = await get_available_cart_dates(ignore_user_id=ignore_user_id)

    if not available_dates:
        return await interaction.response.send_message(
            f"❌ No available cart dates in the next {CALENDAR_DAYS} days.",
            ephemeral=True
        )

    embed = discord.Embed(
        title="📅 Choose Cart Date",
        description=(
            f"Select an available date from today through the next {CALENDAR_DAYS} days.\n"
            "Only one cart can be scheduled per date. Taken dates are marked with ❌ below."
        ),
        colour=discord.Colour.green()
    )

    embed.add_field(
        name="Month calendar",
        value="\n".join(build_calendar_lines(available_dates)),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        view=CalendarDateView(available_dates, mode, member_id, manual_name),
        ephemeral=True
    )


async def send_join_date_picker(interaction: discord.Interaction):

    existing = await get_user(interaction.user.id)
    if existing:
        return await interaction.response.send_message("⚠️ You are already in queue.", ephemeral=True)

    await send_calendar_date_picker(interaction, "join")


class CartView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Join Queue", emoji="➕", style=discord.ButtonStyle.green, custom_id="join_queue")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_join_date_picker(interaction)

    @discord.ui.button(label="Edit Hour", emoji="✏️", style=discord.ButtonStyle.blurple, custom_id="edit_hour")
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await get_user(interaction.user.id):
            return await interaction.response.send_message("You are not in queue.", ephemeral=True)

        await interaction.response.send_message(
            "Choose your new hour. Your cart date will stay the same:",
            view=CalendarHourView("member_edit_hour", member_id=interaction.user.id),
            ephemeral=True
        )

    @discord.ui.button(label="View Queue", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="view_queue")
    async def view_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=await build_queue_embed(), ephemeral=True)

    @discord.ui.button(label="Leave Queue", emoji="❌", style=discord.ButtonStyle.red, custom_id="leave_queue")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB) as db:
            await db.execute("DELETE FROM carts WHERE user_id=?", (interaction.user.id,))
            await db.commit()

        await compress_queue()
        await refresh_queue()
        await refresh_officer_panel(interaction.guild)
        await interaction.response.send_message("❌ Removed from queue.", ephemeral=True)


class OfficerMemberSearchModal(discord.ui.Modal):

    member_name = discord.ui.TextInput(
        label="Member name",
        placeholder="Type first letters, full name, mention, or ID",
        required=True,
        max_length=100
    )

    def __init__(self, mode: str):
        self.mode = mode
        titles = {
            "officer_add": "Add Member",
            "officer_edit_hour": "Edit Member Hour",
            "officer_edit_datetime": "Edit Member Date + Hour",
            "remove": "Remove Member",
            "up": "Move Member Up",
            "down": "Move Member Down",
        }
        super().__init__(title=titles.get(mode, "Find Member"))

    async def on_submit(self, interaction: discord.Interaction):
        if not has_admin_access(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)

        queued_only = self.mode in {"officer_edit_hour", "officer_edit_datetime", "remove", "up", "down"}
        matches = await find_member_matches(interaction.guild, str(self.member_name), include_manual=True)

        if queued_only:
            queued_ids = {row[0] for row in await get_queue()}
            matches = [match for match in matches if match["id"] in queued_ids]

        if not matches:
            return await interaction.response.send_message("No member found with that name.", ephemeral=True)

        if len(matches) > 1:
            return await interaction.response.send_message(
                "Multiple members found. Type more letters and try again.",
                ephemeral=True
            )

        member_id = matches[0]["id"]

        if self.mode == "officer_add":
            await send_calendar_date_picker(interaction, "officer_add", member_id=member_id)
            return

        if self.mode == "officer_edit_hour":
            await interaction.response.send_message(
                "Choose the new hour. The current cart date will stay the same:",
                view=CalendarHourView("officer_edit_hour", member_id=member_id),
                ephemeral=True
            )
            return

        if self.mode == "officer_edit_datetime":
            await send_calendar_date_picker(interaction, "officer_edit_datetime", member_id=member_id)
            return

        await perform_officer_action(interaction, self.mode, [member_id])


class ManualNameModal(discord.ui.Modal, title="Add Name Manually"):

    name = discord.ui.TextInput(
        label="Name",
        placeholder="Type the name here",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not has_admin_access(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)

        await send_calendar_date_picker(interaction, "officer_manual", manual_name=str(self.name))


class OfficerActionButton(discord.ui.Button):

    def __init__(self, label, action, style):
        super().__init__(label=label, style=style, custom_id=f"officer_action_{action}")
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        if not has_admin_access(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)

        if self.action == "backup":
            backup_file = await create_backup()
            await log_action(interaction.user, f"created backup `{os.path.basename(backup_file)}`")
            return await interaction.response.send_message("✅ Backup created.", ephemeral=True)

        if self.action == "restore":
            create_backup_folder()
            files = sorted([filename for filename in os.listdir(BACKUP_FOLDER) if filename.endswith(".db")], reverse=True)
            if not files:
                return await interaction.response.send_message("No backups found.", ephemeral=True)

            latest = files[0]
            await create_backup()
            shutil.copy2(os.path.join(BACKUP_FOLDER, latest), DB)
            await init_db()
            await refresh_queue()
            await refresh_officer_panel(interaction.guild)
            await log_action(interaction.user, f"restored backup `{latest}`")
            return await interaction.response.send_message(f"✅ Restored `{latest}`.", ephemeral=True)

        if self.action == "add_manual":
            return await interaction.response.send_modal(ManualNameModal())

        if self.action == "add":
            return await interaction.response.send_modal(OfficerMemberSearchModal("officer_add"))

        if self.action == "edit_hour":
            return await interaction.response.send_modal(OfficerMemberSearchModal("officer_edit_hour"))

        if self.action == "edit_datetime":
            return await interaction.response.send_modal(OfficerMemberSearchModal("officer_edit_datetime"))

        return await interaction.response.send_modal(OfficerMemberSearchModal(self.action))


class OfficerPanelView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

        self.add_item(OfficerActionButton("➕ Add", "add", discord.ButtonStyle.green))
        self.add_item(OfficerActionButton("📝 Manual", "add_manual", discord.ButtonStyle.green))
        self.add_item(OfficerActionButton("➖ Remove", "remove", discord.ButtonStyle.red))
        self.add_item(OfficerActionButton("⬆️ Up", "up", discord.ButtonStyle.secondary))
        self.add_item(OfficerActionButton("⬇️ Down", "down", discord.ButtonStyle.secondary))
        self.add_item(OfficerActionButton("🕒 Edit Hour", "edit_hour", discord.ButtonStyle.blurple))
        self.add_item(OfficerActionButton("🗓 Edit Date/Hour", "edit_datetime", discord.ButtonStyle.blurple))
        self.add_item(OfficerActionButton("💾 Backup", "backup", discord.ButtonStyle.green))
        self.add_item(OfficerActionButton("♻️ Restore", "restore", discord.ButtonStyle.blurple))

# ================= COMMAND GROUP =================

cart = app_commands.Group(
    name="cart",
    description="Guild Cart commands"
)


# -------- JOIN --------

@cart.command(
    name="join",
    description="Join the queue"
)
async def join_command(
        interaction: discord.Interaction):

    user = await get_user(
        interaction.user.id
    )

    if user:

        await interaction.response.send_message(
            "⚠️ You are already in queue.",
            ephemeral=True
        )

        return

    await send_join_date_picker(interaction)


# -------- EDIT --------

@cart.command(
    name="edit",
    description="Change your hour"
)
async def edit_command(
        interaction: discord.Interaction):

    user = await get_user(
        interaction.user.id
    )

    if not user:

        await interaction.response.send_message(
            "You are not in queue.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        "Choose a new hour:",
        view=EditHourView(),
        ephemeral=True
    )


# -------- POSTPONE --------

@cart.command(
    name="postpone",
    description="Change your hour"
)
async def postpone_command(
        interaction: discord.Interaction):

    user = await get_user(
        interaction.user.id
    )

    if not user:

        await interaction.response.send_message(
            "You are not in queue.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        "Choose your new hour:",
        view=EditHourView(),
        ephemeral=True
    )


# -------- LEAVE --------

@cart.command(
    name="leave",
    description="Leave queue"
)
async def leave_command(
        interaction: discord.Interaction):

    async with aiosqlite.connect(DB) as db:

        await db.execute(
            """
            DELETE FROM carts
            WHERE user_id=?
            """,
            (
                interaction.user.id,
            )
        )

        await db.commit()

    await compress_queue()
    await refresh_queue()

    await interaction.response.send_message(
        "❌ Removed from queue."
    )


# -------- LIST --------

@cart.command(
    name="list",
    description="Show queue"
)
async def list_command(
        interaction: discord.Interaction):

    embed = await build_queue_embed()

    await interaction.response.send_message(
        embed=embed
    )


bot.tree.add_command(cart)


@tasks.loop(time=MAINTENANCE_TIMES)
async def cleanup_task():

    try:

        deleted = await cleanup_expired_carts()

        # Refresh every maintenance tick, even when nothing was deleted.
        # This keeps TODAY / TOMORROW labels and the public queue message current.
        await refresh_queue()

        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            await refresh_officer_panel(channel.guild)

        if deleted:
            print(f"Cleaned up {deleted} expired cart(s).")
        else:
            print("Queue maintenance refresh completed.")

    except Exception:
        traceback.print_exc()

@tasks.loop(time=NIGHTLY_BACKUP_TIME)
async def nightly_backup_task():

    try:

        backup_file = await create_backup()

        print(
            f"Nightly backup created: {os.path.basename(backup_file)}"
        )

    except Exception:
        traceback.print_exc()


# ================= REMINDERS =================

@tasks.loop(minutes=1)
async def reminder_task():

    try:

        await cleanup_expired_carts()

        now = datetime.now(
            timezone.utc
        )

        current_time = now.strftime(
            "%H:%M"
        )

        channel = bot.get_channel(
            CHANNEL_ID
        )

        role = None

        if channel:

            role = channel.guild.get_role(
                GUILD_CART_ROLE_ID
            )

        async with aiosqlite.connect(DB) as db:

            cursor = await db.execute(
                """
                SELECT user_id,
                       position,
                       hour,
                       reminded,
                       manual_name,
                       cart_date
                FROM carts
                """
            )

            users = await cursor.fetchall()

            for uid, pos, hour, reminded, manual_name, cart_date in users:

                if not cart_date:
                    cart_date = default_cart_date(pos)

                if cart_date != today_utc().isoformat():
                    continue

                hour_dt = datetime.strptime(
                    hour,
                    "%H:%M"
                )

                reminder_time = (
                    hour_dt -
                    timedelta(minutes=15)
                ).strftime(
                    "%H:%M"
                )

                # reset reminder after midnight

                if current_time == "00:00":

                    await db.execute(
                        """
                        UPDATE carts
                        SET reminded=0
                        """
                    )

                    await db.commit()

                if (
                        reminder_time == current_time
                        and reminded == 0
                ):

                    try:

                        if manual_name:

                            owner = manual_name

                        else:

                            user = await bot.fetch_user(
                                uid
                            )

                            owner = user.mention

                        date = cart_date

                        if channel:

                            msg = await channel.send(

                             f"{role.mention}\n\n"

                             f"🔔 **Guild Cart Reminder**\n\n"

                            f"📅 {date}\n"
                            f"🕒 {hour} UTC\n\n"

                            f"Current owner: {owner}\n\n"

                            f"Today's cart starts in 15 minutes!"

                             )


                    asyncio.create_task(delete_reminder(msg))

                await db.commit()

             except:

                traceback.print_exc()

    except:

        traceback.print_exc()
        
    async def delete_reminder(message):
        await asyncio.sleep(3600)  # 1 uur
        try:
            await message.delete()
        except:
            pass
        


# ================= PANEL STATE =================

def load_panel_state():

    try:

        if not os.path.exists(PANEL_STATE_FILE):
            return {}

        with open(PANEL_STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)

    except Exception:
        traceback.print_exc()
        return {}


def save_panel_state(state):

    try:

        with open(PANEL_STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=4)

    except Exception:
        traceback.print_exc()


async def get_saved_message(channel, message_id):

    if not message_id:
        return None

    try:
        return await channel.fetch_message(int(message_id))

    except discord.NotFound:
        return None

    except discord.Forbidden:
        return None

    except Exception:
        traceback.print_exc()
        return None


async def upsert_panel_message(channel, state, key, embed, view):

    message = await get_saved_message(
        channel,
        state.get(key)
    )

    if message:

        await message.edit(
            embed=embed,
            view=view
        )

        return message

    message = await channel.send(
        embed=embed,
        view=view
    )

    state[key] = message.id
    save_panel_state(state)

    return message


@tasks.loop(minutes=1)
async def update_utc_channel():

    try:

        channel = bot.get_channel(
            UTC_CHANNEL_ID
        )

        if channel is None:
            return

        now = datetime.now(timezone.utc)

        minute = (now.minute // 15) * 15

        utc_time = f"{now.hour:02d}:{minute:02d}"

        new_name = f"🕒 UTC {utc_time}"

        if channel.name != new_name:

            await channel.edit(
                name=new_name
            )

            print(
                f"Updated UTC channel to {new_name}"
            )

    except:

        traceback.print_exc()

# ================= READY =================

@bot.event
async def on_ready():

    global queue_message, officer_message

    print("=" * 50)
    print(f"Logged in as {bot.user}")
    print("=" * 50)

    # persistent views
    try:
        bot.add_view(CartView())
        bot.add_view(OfficerView())
        bot.add_view(OfficerPanelView())

        print("Persistent views loaded.")

    except Exception:
        traceback.print_exc()

    # sync commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands.")

    except Exception:
        traceback.print_exc()

    # background tasks
    if not reminder_task.is_running():
        reminder_task.start()

    if not update_utc_channel.is_running():
        update_utc_channel.start()

    if not cleanup_task.is_running():
        cleanup_task.start()

    if not nightly_backup_task.is_running():
        nightly_backup_task.start()

    # send panels
    try:
        channel = bot.get_channel(CHANNEL_ID)

        if not channel:
            return

        state = load_panel_state()

        # ================= CART PANEL =================

        queue_message = await upsert_panel_message(
            channel,
            state,
            "queue_message_id",
            await build_queue_embed(),
            CartView()
        )

        # ================= OLD BACKUP PANEL CLEANUP =================
        # Backup and restore are now integrated into the Officer Panel.
        # This keeps the SKY bot cleaner by avoiding a separate Backup Panel message.

        old_backup_message = await get_saved_message(
            channel,
            state.get("backup_message_id")
        )

        if old_backup_message:
            try:
                await old_backup_message.delete()
            except Exception:
                traceback.print_exc()

        if state.get("backup_message_id"):
            state["backup_message_id"] = None
            save_panel_state(state)

        # ================= OFFICER PANEL =================

        officer_embed = discord.Embed(
            title="⚜️ Officer Panel",
            description=(
                "Manage the queue using the buttons below.\n\n"
                "➕ Add member to queue\n"
                "📝 Add manual entry\n"
                "➖ Remove member\n"
                "⬆️ Move member up\n"
                "⬇️ Move member down\n"
                "🕒 Edit hour only\n"
                "🗓 Edit date and hour\n"
                "💾 Create backup\n"
                "♻️ Restore backup"
            ),
            colour=discord.Colour.gold()
        )

        officer_message = await upsert_panel_message(
            channel,
            state,
            "officer_message_id",
            officer_embed,
            OfficerPanelView()
        )

    except Exception:
        traceback.print_exc()


# ================= ERRORS =================

@bot.event
async def on_error(event, *args, **kwargs):
    traceback.print_exc()


@bot.tree.error
async def on_app_command_error(interaction, error):
    traceback.print_exception(
        type(error),
        error,
        error.__traceback__
    )


# ================= START =================
async def main():

    migrate_database_name()

    await init_db()

    async with bot:

        await bot.start(
            TOKEN
        )


if __name__ == "__main__":

    asyncio.run(
        main()
    )

