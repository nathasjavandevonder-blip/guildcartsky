import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import asyncio
import traceback
import logging
from datetime import datetime, timezone, timedelta
import os
from dotenv import load_dotenv
import json
import shutil

# ================= CONFIG =================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

print("TOKEN:", repr(TOKEN))
print("TOKEN LENGTH:", len(TOKEN))

CHANNEL_ID = 1517597432714887380
GUILD_CART_ROLE_ID = 1515472832748982444
UTC_CHANNEL_ID = 1518244845918097540
DB = "cart.db"
OFFICER_ROLE_NAME = "Officer"
GUILDMASTER_ROLE_NAME = "Guild Master"

TRUSTED_USERS = [
    176308213489205249
]

LOG_CHANNEL_ID = 1515304317723213956

BACKUP_FOLDER = "backups"

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


# ================= UTC =================

def today_utc():
    return datetime.now(timezone.utc).date()


def date_for_position(position):

    return today_utc() + timedelta(
        days=position - 1
    )


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

        await db.commit()


# ================= GLOBAL STATE ===============

queue_message = None
officer_message = None


# ================= QUEUE =================

async def get_queue():

    async with aiosqlite.connect(DB) as db:

        cursor = await db.execute(
            """
            SELECT user_id, position, hour, manual_name
            FROM carts
            ORDER BY position
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
            SELECT position,hour
            FROM carts
            WHERE user_id=?
            """,
            (user_id,)
        )

        return await cursor.fetchone()


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

    for uid, position, hour, manual_name in rows:
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

    global queue_message, officer_message

    if not queue_message:
        return

    try:
        await queue_message.edit(
            embed=await build_queue_embed(),
            view=CartView()
        )

    except Exception:
        traceback.print_exc()


# ================= EMBED =================

async def build_queue_embed():

    async with aiosqlite.connect(DB) as db:

        cursor = await db.execute(
            """
            SELECT user_id, position, hour, manual_name
            FROM carts
            ORDER BY position
            """
        )

        rows = await cursor.fetchall()

    embed = discord.Embed(
        title="🚚 SKY Guild Cart Queue (UTC)",
        colour=discord.Colour.green()
    )

    if not rows:

        embed.description = "Queue is empty."

        return embed

    text = ""

    for uid, pos, hour, manual_name in rows:

        if manual_name:

            mention = f"**{manual_name}**"

        else:

            try:

                user = await bot.fetch_user(uid)

                mention = user.mention

            except:

                mention = f"<@{uid}>"

        cart_date = date_for_position(pos)

        text += (
            f"📅 {cart_date} "
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
                    hour
                    )
                    VALUES(?,?,?)
                    """,
                    (
                        interaction.user.id,
                        position,
                        self.values[0]
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
                    SET hour=?
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
            os.listdir(BACKUP_FOLDER),
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

        await refresh_queue()

        await log_action(
            interaction.user,
            f"restored backup `{latest}`"
        )

        await interaction.response.send_message(
            f"Restored {latest}",
            ephemeral=True
        )


# ================= OFFICER PANEL (CLEAN HYBRID SYSTEM) =================

class MemberSelect(discord.ui.Select):

    def __init__(self, members, page: int = 0):

        self.members = members
        self.page = page

        page_members = paginate(members, page)

        options = []

        for member_data in page_members:

            if isinstance(member_data, dict):

                label = member_data.get("name", "Unknown")
                value = str(member_data.get("id"))

            else:

                label = getattr(member_data, "display_name", None) or str(member_data)
                value = str(getattr(member_data, "id", member_data))

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=value
                )
            )

        if not options:
            options.append(
                discord.SelectOption(
                    label="No members found",
                    value="none"
                )
            )

        super().__init__(
            placeholder=f"Select members (page {page + 1})",
            min_values=1,
            max_values=len(options),
            options=options,
            custom_id=f"member_select_page_{page}"
        )

    async def callback(self, interaction: discord.Interaction):

        if self.values[0] == "none":
            return await interaction.response.send_message(
                "No members available.",
                ephemeral=True
            )

        self.view.selected_members = [int(value) for value in self.values]

        name_lookup = {}

        for member_data in self.view.members:

            if isinstance(member_data, dict):
                name_lookup[int(member_data["id"])] = member_data["name"]

            else:
                name_lookup[int(member_data.id)] = member_data.display_name

        selected_names = []

        for value in self.values:

            selected_id = int(value)
            selected_names.append(
                name_lookup.get(selected_id, f"<@{selected_id}>")
            )

        await interaction.response.send_message(
            "Selected: " + ", ".join(selected_names),
            ephemeral=True
        )


class ManualAddModal(discord.ui.Modal, title="Add Name Manually"):

    name = discord.ui.TextInput(
        label="Name",
        placeholder="Type the name here",
        required=True,
        max_length=50
    )

    hour = discord.ui.TextInput(
        label="Hour UTC",
        placeholder="Example: 18:00",
        required=True,
        max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):

        hour_value = str(self.hour).strip()

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
                    manual_name
                )
                VALUES(?,?,?,?)
                """,
                (
                    manual_id,
                    position,
                    hour_value,
                    str(self.name)
                )
            )

            await db.commit()

        await compress_queue()
        await refresh_queue()
        await refresh_officer_panel(interaction.guild)

        await log_action(
            interaction.user,
            f"added manual name `{self.name}` at `{hour_value} UTC`"
        )

        await interaction.response.send_message(
            f"Added manual name: {self.name} at {hour_value} UTC",
            ephemeral=True
        )

class ActionSelect(discord.ui.Select):

    def __init__(self):

        options = [
            discord.SelectOption(label="Add Member", value="add"),
            discord.SelectOption(label="Add Name Manually", value="add_manual"),
            discord.SelectOption(label="Remove Member", value="remove"),
            discord.SelectOption(label="Move Up", value="up"),
            discord.SelectOption(label="Move Down", value="down"),
        ]

        super().__init__(
            placeholder="Select action...",
            options=options,
            custom_id="action_select"
        )

    async def callback(self, interaction: discord.Interaction):

        view = self.view
        action = self.values[0]

        if action == "add_manual":
            return await view.handle_action(
                interaction,
                action,
                []
            )

        if not view.selected_members:
            return await interaction.response.send_message(
                "Select at least one member first.",
                ephemeral=True
            )

        await view.handle_action(
            interaction,
            action,
            view.selected_members
        )

def has_admin_access(member):

    if member.id in TRUSTED_USERS:
        return True

    return any(
        role.name in [
            "Officer",
            "Guild Master"
        ]
        for role in member.roles
    )

class OfficerPanelView(discord.ui.View):

    def __init__(self, members, page: int = 0):

        super().__init__(timeout=None)

        self.members = members
        self.page = page
        self.selected_members = []

        self.refresh_ui()

    def refresh_ui(self):

        self.clear_items()

        self.add_item(MemberSelect(self.members, self.page))
        self.add_item(ActionSelect())
        self.add_item(PrevButton(self))
        self.add_item(NextButton(self))

    async def handle_action(self, interaction, action, member_ids):

        if not has_admin_access(interaction.user):
            return await interaction.response.send_message(
                "No permission.",
                ephemeral=True
            )
            
        if action == "add_manual":

            return await interaction.response.send_modal(
                ManualAddModal()
            )

        if action == "add":

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
                            hour
                        )
                        VALUES(?,?,?)
                        """,
                        (
                            member_id,
                            position,
                            "00:00"
                        )
                    )

                    added += 1

                await db.commit()

            await compress_queue()
            await refresh_queue()
            await refresh_officer_panel(interaction.guild)

            await log_action(
                interaction.user,
                f"added {added} member(s) to the queue"
            )

            return await interaction.response.send_message(
                f"Added {added} member(s). Default hour: 00:00 UTC.",
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
                        (
                            member_id,
                        )
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


class PrevButton(discord.ui.Button):

    def __init__(self, panel):
        super().__init__(
            label="⬅️ Prev",
            style=discord.ButtonStyle.gray,
            custom_id="officer_prev"
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):

        if self.panel.page > 0:
            self.panel.page -= 1

        self.panel.selected_members = []
        self.panel.refresh_ui()

        await interaction.response.edit_message(view=self.panel)


class NextButton(discord.ui.Button):

    def __init__(self, panel):
        super().__init__(
            label="➡️ Next",
            style=discord.ButtonStyle.gray,
            custom_id="officer_next"
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):

        max_page = max(
            (len(self.panel.members) - 1) // PAGE_SIZE,
            0
        )

        if self.panel.page < max_page:
            self.panel.page += 1

        self.panel.selected_members = []
        self.panel.refresh_ui()

        await interaction.response.edit_message(view=self.panel)


async def refresh_officer_panel(guild):

    global officer_message

    if not officer_message or not guild:
        return

    try:

        members = await get_all_members(guild)

        officer_embed = discord.Embed(
            title="⚜️ Officer Panel",
            description=
                "Select one or more members.\n"
                "Then choose Add, Remove, Move Up, or Move Down.",
            colour=discord.Colour.red()
        )

        await officer_message.edit(
            embed=officer_embed,
            view=OfficerPanelView(members)
        )

    except Exception:
        traceback.print_exc()


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

    await interaction.response.send_message(
        "Choose a cart hour:",
        view=JoinHourView(),
        ephemeral=True
    )


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

# ================= REMINDERS =================

@tasks.loop(minutes=1)
async def reminder_task():

    try:

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
                       manual_name
                FROM carts
                """
            )

            users = await cursor.fetchall()

            for uid, pos, hour, reminded, manual_name in users:

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

                        date = date_for_position(
                            pos
                        )

                        if channel:

                            await channel.send(

                                f"{role.mention}\n\n"

                                f"🔔 **Guild Cart Reminder**\n\n"

                                f"📅 {date}\n"
                                f"🕒 {hour} UTC\n\n"

                                f"Current owner: {owner}\n\n"

                                f"Today's cart starts in 15 minutes!"

                            )

                        await db.execute(
                            """
                            UPDATE carts
                            SET reminded=1
                            WHERE user_id=?
                            """,
                            (
                                uid,
                            )
                        )

                        await db.commit()

                    except:

                        traceback.print_exc()

    except:

        traceback.print_exc()

@tasks.loop(minutes=15)
async def update_utc_channel():

    try:

        channel = bot.get_channel(
            UTC_CHANNEL_ID
        )

        if channel is None:
            return

        utc_time = datetime.now(
            timezone.utc
        ).strftime(
            "%H:%M"
        )

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

    # send panels
    try:
        channel = bot.get_channel(CHANNEL_ID)

        if not channel:
            return

        # ================= CART PANEL =================

        queue_message = await channel.send(
            embed=await build_queue_embed(),
            view=CartView()
        )

        # ================= BACKUP PANEL =================

        backup_embed = discord.Embed(
            title="💾 Backup Panel",
            description=
                "💾 Backup Queue\n"
                "♻️ Restore Backup",
            colour=discord.Colour.blurple()
        )

        await channel.send(
            embed=backup_embed,
            view=OfficerView()
        )

        # ================= OFFICER PANEL =================

        members = await get_all_members(channel.guild)

        officer_embed = discord.Embed(
            title="⚜️ Officer Panel",
            description=
                "Select one or more members.\n"
                "Then choose Add, Remove, Move Up, or Move Down.",
            colour=discord.Colour.red()
        )

        officer_message = await channel.send(
            embed=officer_embed,
            view=OfficerPanelView(members)
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

    await init_db()

    async with bot:

        await bot.start(
            TOKEN
        )


if __name__ == "__main__":

    asyncio.run(
        main()
    )

