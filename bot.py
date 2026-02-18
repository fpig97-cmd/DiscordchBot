import os
import sqlite3
import random
import string
import shutil
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(env_path)

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

CREATOR_ROBLOX_NICK = "DeSky_Lunarx"
CREATOR_ROBLOX_REAL = "Sky_Lunarx"
CREATOR_DISCORD_NAME = "Lunar"

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN이 .env에 설정되어 있지 않습니다.")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

error_logs = []
MAX_LOGS = 50

DB_PATH = os.path.join(BASE_DIR, "bot.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

# ---------- DB 테이블 ----------

cursor.execute(
    """CREATE TABLE IF NOT EXISTS users(
        discord_id INTEGER,
        guild_id INTEGER,
        roblox_nick TEXT,
        roblox_user_id INTEGER,
        code TEXT,
        expire_time TEXT,
        verified INTEGER DEFAULT 0,
        PRIMARY KEY(discord_id, guild_id)
    )"""
)

cursor.execute(
    """CREATE TABLE IF NOT EXISTS stats(
        guild_id INTEGER PRIMARY KEY,
        verify_count INTEGER DEFAULT 0,
        force_count INTEGER DEFAULT 0,
        cancel_count INTEGER DEFAULT 0
    )"""
)

cursor.execute(
    """CREATE TABLE IF NOT EXISTS settings(
        guild_id INTEGER PRIMARY KEY,
        role_id INTEGER,
        status_channel_id INTEGER,
        admin_role_id INTEGER
    )"""
)

cursor.execute(
    """CREATE TABLE IF NOT EXISTS bot_status(
        id INTEGER PRIMARY KEY,
        status_text TEXT,
        status_type INTEGER DEFAULT 0
    )"""
)

cursor.execute(
    """CREATE TABLE IF NOT EXISTS roblox_rank(
        id INTEGER PRIMARY KEY,
        rank_name TEXT,
        rank_value INTEGER
    )"""
)

# 이미 있는 DB에는 admin_role_id 컬럼이 없을 수 있으므로 추가 시도
try:
    cursor.execute("ALTER TABLE settings ADD COLUMN admin_role_id INTEGER")
except sqlite3.OperationalError:
    pass

conn.commit()

# ---------- 설정/권한 유틸 ----------


def get_guild_role_id(guild_id: int) -> Optional[int]:
    cursor.execute("SELECT role_id FROM settings WHERE guild_id=?", (guild_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def set_guild_role_id(guild_id: int, role_id: int) -> None:
    cursor.execute(
        """INSERT INTO settings(guild_id, role_id)
           VALUES(?, ?)
           ON CONFLICT(guild_id) DO UPDATE SET role_id=excluded.role_id""",
        (guild_id, role_id),
    )
    conn.commit()


def get_guild_status_channel_id(guild_id: int) -> Optional[int]:
    cursor.execute("SELECT status_channel_id FROM settings WHERE guild_id=?", (guild_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def set_guild_status_channel_id(guild_id: int, channel_id: int) -> None:
    cursor.execute(
        """INSERT INTO settings(guild_id, status_channel_id)
           VALUES(?, ?)
           ON CONFLICT(guild_id) DO UPDATE SET status_channel_id=excluded.status_channel_id""",
        (guild_id, channel_id),
    )
    conn.commit()


def get_guild_admin_role_id(guild_id: int) -> Optional[int]:
    cursor.execute("SELECT admin_role_id FROM settings WHERE guild_id=?", (guild_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def set_guild_admin_role_id(guild_id: int, role_id: Optional[int]) -> None:
    cursor.execute(
        """INSERT INTO settings(guild_id, admin_role_id)
           VALUES(?, ?)
           ON CONFLICT(guild_id) DO UPDATE SET admin_role_id=excluded.admin_role_id""",
        (guild_id, role_id),
    )
    conn.commit()


def is_admin(member: discord.Member) -> bool:
    # 디스코드 기본 관리자 권한
    if member.guild_permissions.administrator:
        return True

    # 커스텀 관리자 역할
    admin_role_id = get_guild_admin_role_id(member.guild.id)
    if admin_role_id:
        admin_role = member.guild.get_role(admin_role_id)
        if admin_role and admin_role in member.roles:
            return True

    return False


def is_owner(user_id: int) -> bool:
    return OWNER_ID > 0 and user_id == OWNER_ID


def add_error_log(error_msg: str) -> None:
    error_logs.append({"timestamp": datetime.now(timezone.utc), "message": error_msg})
    if len(error_logs) > MAX_LOGS:
        error_logs.pop(0)


def generate_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


ROBLOX_USERNAME_API = "https://users.roblox.com/v1/usernames/users"
ROBLOX_USER_API = "https://users.roblox.com/v1/users/{userId}"

# ---------- Roblox API ----------


async def roblox_get_group_rank_by_user_id(
    user_id: int, group_id: int = 34965893
) -> Optional[str]:
    """유저의 그룹 랭크 가져오기"""
    url = f"https://groups.roblox.com/v1/users/{user_id}/groups/roles"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

                for group_data in data.get("data", []):
                    if group_data["group"]["id"] == group_id:
                        return group_data["role"]["name"]

                return None
        except Exception as e:
            print(f"roblox_get_group_rank error: {repr(e)}")
            add_error_log(f"roblox_get_group_rank: {repr(e)}")
            return None


async def roblox_get_user_id_by_username(username: str) -> Optional[int]:
    payload = {"usernames": [username], "excludeBannedUsers": True}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                ROBLOX_USERNAME_API,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                results = data.get("data", [])
                return results[0].get("id") if results else None
        except Exception as e:
            add_error_log(f"roblox_get_user_id: {repr(e)}")
            return None


async def roblox_get_description_by_user_id(user_id: int) -> Optional[str]:
    url = ROBLOX_USER_API.format(userId=user_id)
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("description")
        except Exception as e:
            add_error_log(f"roblox_get_description: {repr(e)}")
            return None


# ---------- View ----------


class VerifyView(discord.ui.View):
    def __init__(self, code: str, expire_time: datetime, guild_id: int):
        super().__init__(timeout=300)
        self.code = code
        self.expire_time = expire_time
        self.guild_id = guild_id

    @discord.ui.button(label="인증하기", style=discord.ButtonStyle.green)
    async def verify_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction is None:
            return
        try:
            guild = bot.get_guild(self.guild_id)
            if guild is None:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ 서버 정보를 불러올 수 없습니다.", ephemeral=True
                    )
                return

            cursor.execute(
                "SELECT roblox_nick, roblox_user_id, expire_time, code FROM users WHERE discord_id=? AND guild_id=?",
                (interaction.user.id, self.guild_id),
            )
            data = cursor.fetchone()

            if not data:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ 인증 정보가 없습니다. 다시 /인증 명령어를 실행해주세요.",
                        ephemeral=True,
                    )
                return

            nick, roblox_user_id, expire_str, saved_code = data
            expire = datetime.fromisoformat(expire_str)

            if datetime.now() > expire:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ 인증 시간이 만료되었습니다. 다시 /인증 명령어를 실행해주세요.",
                        ephemeral=True,
                    )
                return

            if saved_code != self.code:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ 코드가 일치하지 않습니다.", ephemeral=True
                    )
                return

            if not roblox_user_id:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Roblox 계정 정보가 없습니다. 다시 /인증 명령어를 실행해주세요.",
                        ephemeral=True,
                    )
                return

            description = await roblox_get_description_by_user_id(roblox_user_id)
            if description is None:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ 로블록스 프로필을 불러올 수 없습니다. 잠시 후 다시 시도해주세요.",
                        ephemeral=True,
                    )
                return

            if self.code not in description:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ 프로필 설명란에 인증 코드가 없습니다. 정확히 입력했는지 확인해주세요.",
                        ephemeral=True,
                    )
                return

            role_id = get_guild_role_id(self.guild_id)
            if not role_id:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ 인증 역할이 설정되지 않았습니다. /설정 명령어를 사용해주세요.",
                        ephemeral=True,
                    )
                return

            role = guild.get_role(role_id)
            if role is None:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ 인증 역할을 찾을 수 없습니다.", ephemeral=True
                    )
                return

            member = guild.get_member(interaction.user.id)
            if member is None:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ 서버에서 유저 정보를 찾을 수 없습니다.", ephemeral=True
                    )
                return

            await member.add_roles(role)

            # 로블록스 그룹 랭크 가져오기 후 닉네임 변경
            try:
                rank_name = await roblox_get_group_rank_by_user_id(roblox_user_id)

                if rank_name:
                    await member.edit(nick=f"[{rank_name}] {nick}")
                else:
                    await member.edit(nick=nick)
            except discord.Forbidden:
                pass

            cursor.execute(
                "UPDATE users SET verified=1 WHERE discord_id=? AND guild_id=?",
                (interaction.user.id, self.guild_id),
            )
            cursor.execute(
                "INSERT OR IGNORE INTO stats(guild_id) VALUES(?)", (self.guild_id,)
            )
            cursor.execute(
                "UPDATE stats SET verify_count = verify_count + 1 WHERE guild_id=?",
                (self.guild_id,),
            )
            conn.commit()

            if not interaction.response.is_done():
                await interaction.response.send_message("✅ 인증 완료!", ephemeral=True)

        except Exception as e:
            print("verify_button error:", repr(e))
            add_error_log(f"verify_button: {repr(e)}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ 내부 오류가 발생했습니다.", ephemeral=True
                )


# ---------- 명령어 ----------


@bot.tree.command(name="인증", description="로블록스 계정 인증을 시작합니다.")
@app_commands.describe(로블닉="로블록스 닉네임")
async def verify(interaction: discord.Interaction, 로블닉: str):
    await interaction.response.defer(ephemeral=True)

    role_id = get_guild_role_id(interaction.guild.id)
    if not role_id:
        await interaction.followup.send(
            "❌ 인증 역할이 설정되지 않았습니다. 관리자에게 /설정 명령어를 요청해주세요.",
            ephemeral=True,
        )
        return

    cursor.execute(
        "SELECT verified FROM users WHERE discord_id=? AND guild_id=?",
        (interaction.user.id, interaction.guild.id),
    )
    data = cursor.fetchone()
    if data and data[0] == 1:
        await interaction.followup.send("이미 인증된 사용자입니다.", ephemeral=True)
        return

    user_id = await roblox_get_user_id_by_username(로블닉)
    if not user_id:
        await interaction.followup.send(
            "❌ 해당 닉네임의 로블록스 계정을 찾을 수 없습니다.", ephemeral=True
        )
        return

    code = generate_code()
    expire_time = datetime.now() + timedelta(minutes=5)

    cursor.execute(
        """INSERT OR REPLACE INTO users(discord_id, guild_id, roblox_nick,
           roblox_user_id, code, expire_time, verified)
           VALUES(?,?,?,?,?,?,0)""",
        (interaction.user.id, interaction.guild.id, 로블닉, user_id, code, expire_time.isoformat()),
    )
    conn.commit()

    embed = discord.Embed(title="로블록스 인증", color=discord.Color.blue())
    embed.description = (
        f"> Roblox: `{로블닉}` (ID: `{user_id}`)\n"
        f"> 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "1️⃣ Roblox 프로필로 이동\n"
        "2️⃣ 설명란에 코드 입력\n"
        "3️⃣ '인증하기' 버튼 클릭\n\n"
        f"🔐 코드: `{code}`\n"
        "⏱ 남은 시간: 5분\n\n"
        "made by Lunar"
    )

    try:
        await interaction.user.send(
            embed=embed, view=VerifyView(code, expire_time, interaction.guild.id)
        )
        await interaction.followup.send("📩 DM을 확인해주세요.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ DM 전송 실패. DM 수신을 허용해주세요.", ephemeral=True
        )


@bot.tree.command(name="인증해제", description="유저 인증 해제 (관리자)")
@app_commands.describe(유저="해제할 유저")
async def unverify(interaction: discord.Interaction, 유저: discord.Member):
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    cursor.execute(
        "UPDATE users SET verified=0 WHERE discord_id=? AND guild_id=?",
        (유저.id, interaction.guild.id),
    )
    cursor.execute(
        "INSERT OR IGNORE INTO stats(guild_id) VALUES(?)", (interaction.guild.id,)
    )
    cursor.execute(
        "UPDATE stats SET cancel_count = cancel_count + 1 WHERE guild_id=?",
        (interaction.guild.id,),
    )
    conn.commit()

    role_id = get_guild_role_id(interaction.guild.id)
    role = interaction.guild.get_role(role_id) if role_id else None
    if role and role in 유저.roles:
        try:
            await 유저.remove_roles(role, reason="인증 해제")
        except discord.Forbidden:
            await interaction.followup.send("⚠ 역할 제거 권한 없음", ephemeral=True)
            return

    await interaction.followup.send(f"✅ {유저.mention} 인증 해제 완료", ephemeral=True)


@bot.tree.command(name="설정", description="인증 역할 설정 (관리자)")
@app_commands.describe(역할="인증 역할")
async def configure(interaction: discord.Interaction, 역할: discord.Role):
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    bot_member = interaction.guild.me
    if bot_member.top_role <= 역할:
        await interaction.response.send_message(
            "❌ 봇의 최상위 역할보다 위의 역할은 설정할 수 없습니다.", ephemeral=True
        )
        return

    set_guild_role_id(interaction.guild.id, 역할.id)
    await interaction.response.send_message(
        f"✅ 인증 역할을 {역할.mention}로 설정했습니다.", ephemeral=True
    )


@bot.tree.command(name="관리자지정", description="관리자 역할을 설정하거나 해제합니다. (개발자)")
@app_commands.describe(역할="관리자 역할 (비워두면 해제)")
async def set_admin_role(
    interaction: discord.Interaction, 역할: Optional[discord.Role] = None
):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    if 역할 is not None:
        bot_member = interaction.guild.me
        if bot_member.top_role <= 역할:
            await interaction.response.send_message(
                "❌ 봇의 최상위 역할보다 위의 역할은 설정할 수 없습니다.", ephemeral=True
            )
            return

        set_guild_admin_role_id(interaction.guild.id, 역할.id)
        await interaction.response.send_message(
            f"✅ 관리자 역할을 {역할.mention}로 설정했습니다.", ephemeral=True
        )
    else:
        set_guild_admin_role_id(interaction.guild.id, None)
        await interaction.response.send_message(
            "✅ 관리자 역할 설정을 해제했습니다.", ephemeral=True
        )


@bot.tree.command(name="핑", description="봇의 응답 속도를 확인합니다.")
async def ping(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 핑: {latency_ms} ms", ephemeral=True)


@bot.tree.command(name="제작자", description="봇 제작자 정보를 표시합니다.")
async def creator_info(interaction: discord.Interaction):
    user = interaction.user
    now = datetime.now(timezone.utc)
    created_at = user.created_at.replace(tzinfo=timezone.utc)
    days = (now - created_at).days

    embed = discord.Embed(title="봇 제작자 정보", color=discord.Color.gold())
    embed.add_field(
        name="제작자 로블록스 디스플레이 닉네임",
        value=CREATOR_ROBLOX_NICK,
        inline=False,
    )
    embed.add_field(
        name="제작자 로블록스 실제 닉네임", value=CREATOR_ROBLOX_REAL, inline=False
    )
    embed.add_field(
        name="제작자 디스코드 닉네임", value=CREATOR_DISCORD_NAME, inline=False
    )
    embed.add_field(
        name="유저 디코 계정 생성 일수", value=f"{days}일", inline=False
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="명단리스트", description="서버의 모든 역할 이름과 ID를 표시합니다.")
async def role_list(interaction: discord.Interaction):
    guild = interaction.guild
    lines = []
    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        lines.append(f"{role.name} (`{role.id}`)")

    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n... (생략)"

    embed = discord.Embed(
        title="역할 목록", description=text or "역할이 없습니다.", color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="통계", description="봇 사용 통계를 확인합니다.")
async def stats(interaction: discord.Interaction):
    cursor.execute(
        "SELECT verify_count, cancel_count FROM stats WHERE guild_id=?",
        (interaction.guild.id,),
    )
    row = cursor.fetchone()

    verify_count = row[0] if row else 0
    cancel_count = row[1] if row else 0

    embed = discord.Embed(title="봇 통계", color=discord.Color.blurple())
    embed.add_field(name="인증 완료", value=str(verify_count), inline=True)
    embed.add_field(name="인증 해제", value=str(cancel_count), inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="서버정보", description="서버 기본 정보를 표시합니다.")
async def server_info(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "❌ 그룹 정보를 불러올 수 없습니다.", ephemeral=True
        )
        return

    cursor.execute(
        "SELECT COUNT(*) FROM users WHERE guild_id=? AND verified=1", (guild.id,)
    )
    verified_count = cursor.fetchone()[0]

    embed = discord.Embed(
        title="서버 정보",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="서버 이름", value=guild.name, inline=False)
    embed.add_field(name="멤버 수", value=str(guild.member_count), inline=True)
    embed.add_field(name="인증된 유저 수", value=str(verified_count), inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="인증확인", description="프로필에 입력한 코드를 확인합니다.")
async def verify_check(interaction: discord.Interaction):
    cursor.execute(
        "SELECT roblox_nick, code, expire_time FROM users WHERE discord_id=? AND guild_id=?",
        (interaction.user.id, interaction.guild.id),
    )
    data = cursor.fetchone()

    if not data:
        await interaction.response.send_message(
            "❌ 인증 정보가 없습니다. /인증 명령어를 먼저 실행해주세요.", ephemeral=True
        )
        return

    nick, code, expire_str = data
    expire = datetime.fromisoformat(expire_str)
    remaining = (expire - datetime.now()).total_seconds()

    if remaining <= 0:
        await interaction.response.send_message(
            "❌ 인증 시간이 만료되었습니다. /인증 명령어를 다시 실행해주세요.", ephemeral=True
        )
        return

    minutes = int(remaining // 60)
    seconds = int(remaining % 60)

    embed = discord.Embed(title="인증 확인", color=discord.Color.blurple())
    embed.add_field(name="로블닉", value=nick, inline=False)
    embed.add_field(name="입력할 코드", value=f"`{code}`", inline=False)
    embed.add_field(name="남은 시간", value=f"{minutes}분 {seconds}초", inline=False)
    embed.add_field(
        name="안내",
        value="프로필 설명란에 위 코드를 입력하고 '인증하기' 버튼을 눌러주세요.",
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="명령어목록", description="모든 명령어 목록을 확인합니다.")
async def command_list(interaction: discord.Interaction):
    embed = discord.Embed(title="봇 명령어 목록", color=discord.Color.blurple())

    embed.add_field(
        name="🔐 인증 명령어",
        value="`/인증` `/인증해제` `/인증확인` `/설정`",
        inline=False,
    )
    embed.add_field(
        name="📊 정보 명령어",
        value="`/핑` `/제작자` `/명단리스트` `/통계` `/서버정보` `/명령어목록`",
        inline=False,
    )
    embed.add_field(
        name="👨‍💼 관리자 명령어",
        value="`/유저검색` `/일괄닉네임변경` `/데이터초기화`",
        inline=False,
    )
    embed.add_field(
        name="👨‍💻 개발자 명령어",
        value="`/공지` `/봇상태` `/백업생성` `/오류로그` `/시스템정보` `/관리자지정`",
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="유저검색", description="로블록스 또는 디스코드 유저를 검색합니다. (관리자)"
)
@app_commands.describe(검색어="로블닉 또는 디코 닉네임")
async def user_search(interaction: discord.Interaction, 검색어: str):
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    member = interaction.guild.get_member_named(검색어)
    member_id = member.id if member else -1

    cursor.execute(
        "SELECT discord_id, roblox_nick, verified FROM users "
        "WHERE guild_id=? AND (roblox_nick LIKE ? OR discord_id=?)",
        (interaction.guild.id, f"%{검색어}%", member_id),
    )
    results = cursor.fetchall()

    if not results:
        await interaction.followup.send("❌ 검색 결과가 없습니다.", ephemeral=True)
        return

    embed = discord.Embed(title="유저 검색 결과", color=discord.Color.blurple())
    for discord_id, roblox_nick, verified in results:
        status = "✅ 인증된 유저 입니다." if verified else "❌ 미인증 유저 입니다."
        embed.add_field(
            name=roblox_nick,
            value=f"Discord ID: {discord_id}\n상태: {status}",
            inline=False,
        )

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="일괄닉네임변경", description="모든 인증 유저의 닉네임을 갱신합니다. (관리자)"
)
async def bulk_nickname_update(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    cursor.execute(
        "SELECT discord_id, roblox_nick, roblox_user_id "
        "FROM users WHERE guild_id=? AND verified=1",
        (interaction.guild.id,),
    )
    users_data = cursor.fetchall()

    if not users_data:
        await interaction.followup.send("❌ 인증된 유저가 없습니다.", ephemeral=True)
        return

    updated_count = 0
    failed_count = 0

    for discord_id, roblox_nick, roblox_user_id in users_data:
        try:
            member = interaction.guild.get_member(discord_id)
            if member and roblox_user_id:
                rank_name = await roblox_get_group_rank_by_user_id(roblox_user_id)

                if rank_name:
                    await member.edit(nick=f"[{rank_name}] {roblox_nick}")
                else:
                    await member.edit(nick=roblox_nick)
                updated_count += 1
        except discord.Forbidden:
            failed_count += 1
        except Exception as e:
            print(f"닉네임 변경 실패 (discord_id={discord_id}): {repr(e)}")
            failed_count += 1

    result_text = f"✅ {updated_count}명의 닉네임을 갱신했습니다."
    if failed_count > 0:
        result_text += f"\n⚠ {failed_count}명 변경 실패 (권한 부족 등)"

    await interaction.followup.send(result_text, ephemeral=True)


@bot.tree.command(
    name="데이터초기화", description="모든 봇 데이터를 초기화합니다. (개발자)"
)
async def reset_all_data(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    view = discord.ui.View(timeout=30)

    async def confirm_callback(i: discord.Interaction):
        if i.user.id != interaction.user.id:
            await i.response.send_message("❌ 명령어 실행자만 사용할 수 있습니다.", ephemeral=True)
            return
        cursor.execute("DELETE FROM users")
        cursor.execute("DELETE FROM stats")
        cursor.execute("DELETE FROM settings")
        conn.commit()
        await i.response.edit_message(
            content="✅ 모든 데이터가 삭제되었습니다.", view=None
        )

    async def cancel_callback(i: discord.Interaction):
        if i.user.id != interaction.user.id:
            await i.response.send_message("❌ 명령어 실행자만 사용할 수 있습니다.", ephemeral=True)
            return
        await i.response.edit_message(content="❌ 취소되었습니다.", view=None)

    confirm_button = discord.ui.Button(label="초기화", style=discord.ButtonStyle.danger)
    cancel_button = discord.ui.Button(label="취소", style=discord.ButtonStyle.secondary)
    confirm_button.callback = confirm_callback
    cancel_button.callback = cancel_callback
    view.add_item(confirm_button)
    view.add_item(cancel_button)

    await interaction.response.send_message(
        "⚠ 정말 모든 데이터를 삭제할까요?", view=view, ephemeral=True
    )


@bot.tree.command(
    name="공지", description="인증된 모든 유저에게 공지를 보냅니다. (개발자)"
)
@app_commands.describe(
    제목="공지 제목", 내용="공지 내용", 색상="색상 (파랑/초록/빨강/주황/노랑/자주색/분홍/회색)"
)
async def announce(
    interaction: discord.Interaction, 제목: str, 내용: str, 색상: str = "파랑"
):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "❌ 그룹 정보를 불러올 수 없습니다.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    color_map = {
        "파랑": discord.Color.blue(),
        "초록": discord.Color.green(),
        "빨강": discord.Color.red(),
        "주황": discord.Color.orange(),
        "노랑": discord.Color.gold(),
        "자주색": discord.Color.purple(),
        "분홍": discord.Color.magenta(),
        "회색": discord.Color.greyple(),
    }
    embed_color = color_map.get(색상, discord.Color.blue())

    cursor.execute(
        "SELECT DISTINCT discord_id FROM users WHERE guild_id=? AND verified=1",
        (guild.id,),
    )
    user_ids = [row[0] for row in cursor.fetchall()]

    if not user_ids:
        await interaction.followup.send("❌ 인증된 유저가 없습니다.", ephemeral=True)
        return

    embed = discord.Embed(
        title=제목,
        description=내용,
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"서버: {guild.name}")

    sent_count = 0
    failed_count = 0

    for user_id in user_ids:
        try:
            user = await bot.fetch_user(user_id)
            await user.send(embed=embed)
            sent_count += 1
        except (discord.Forbidden, discord.NotFound):
            failed_count += 1
        except Exception as e:
            print(f"공지 전송 실패 (user_id={user_id}): {repr(e)}")
            failed_count += 1

    result_text = f"✅ {sent_count}명에게 공지를 전송했습니다."
    if failed_count > 0:
        result_text += f"\n⚠ {failed_count}명에게는 DM 전송에 실패했습니다."

    await interaction.followup.send(result_text, ephemeral=True)


@bot.tree.command(name="백업생성", description="현재 DB를 백업합니다. (개발자)")
async def backup_db(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"bot_{timestamp}.db"
    backup_path = os.path.join(BASE_DIR, backup_name)

    try:
        conn.commit()
        shutil.copy2(DB_PATH, backup_path)
    except Exception as e:
        await interaction.response.send_message(
            f"❌ 백업 중 오류가 발생했습니다: {e}", ephemeral=True
        )
        add_error_log(f"backup_db: {repr(e)}")
        return

    await interaction.response.send_message(
        f"✅ 백업 완료: `{backup_name}`", ephemeral=True
    )


@bot.tree.command(name="오류로그", description="최근 오류 로그를 확인합니다. (개발자)")
async def error_log(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    if not error_logs:
        await interaction.response.send_message(
            "❌ 오류 로그가 없습니다.", ephemeral=True
        )
        return

    embed = discord.Embed(title="오류 로그", color=discord.Color.red())
    for log in error_logs[-10:]:
        timestamp = log["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        message = log["message"][:100]
        embed.add_field(name=timestamp, value=f"`{message}`", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="시스템정보", description="봇 시스템 정보를 확인합니다. (개발자)")
async def system_info(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM users WHERE verified=1")
    verified_users = cursor.fetchone()[0]

    cursor.execute("SELECT SUM(verify_count) FROM stats")
    total_verifications = cursor.fetchone()[0] or 0

    embed = discord.Embed(title="시스템 정보", color=discord.Color.blurple())
    embed.add_field(name="총 등록 유저", value=str(total_users), inline=True)
    embed.add_field(name="인증된 유저", value=str(verified_users), inline=True)
    embed.add_field(name="총 인증 횟수", value=str(total_verifications), inline=True)
    embed.add_field(name="봇 업타임", value="계산 중...", inline=True)
    embed.add_field(
        name="DB 파일 크기",
        value=f"{os.path.getsize(DB_PATH) / 1024:.2f} KB",
        inline=True,
    )
    embed.add_field(name="오류 로그 개수", value=str(len(error_logs)), inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="봇상태", description="봇의 상태를 변경합니다. (개발자)")
@app_commands.describe(상태="상태 선택 (준비중/정상/중지/오류수정중)")
async def bot_status(interaction: discord.Interaction, 상태: str):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    상태_옵션 = {
        "준비중": {
            "emoji": "🟠",
            "color": discord.Color.orange(),
            "text": "서비스 준비중",
        },
        "정상": {
            "emoji": "🟢",
            "color": discord.Color.green(),
            "text": "정상 작동",
        },
        "중지": {"emoji": "🔴", "color": discord.Color.red(), "text": "중지 상태"},
        "오류수정중": {
            "emoji": "🟥",
            "color": discord.Color.red(),
            "text": "오류 수정중",
        },
    }

    if 상태 not in 상태_옵션:
        await interaction.response.send_message(
            "❌ 상태는 '준비중', '정상', '중지', '오류수정중' 중 하나여야 합니다.",
            ephemeral=True,
        )
        return

    상태_정보 = 상태_옵션[상태]
    emoji = 상태_정보["emoji"]
    color = 상태_정보["color"]
    text = 상태_정보["text"]

    await bot.change_presence(activity=discord.Game(f"{emoji} {text}"))

    cursor.execute(
        "INSERT OR REPLACE INTO bot_status(id, status_text) VALUES(1, ?)", (상태,)
    )
    conn.commit()

    embed = discord.Embed(
        title=f"{emoji} 봇 상태",
        description=f"**{text}**",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="상태", value=상태, inline=True)
    embed.add_field(
        name="변경 시간", value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), inline=True
    )
    embed.set_footer(text="상태 채널")

    status_channel_id = get_guild_status_channel_id(interaction.guild.id)
    if status_channel_id:
        status_channel = interaction.guild.get_channel(status_channel_id)
        if status_channel:
            try:
                async for msg in status_channel.history(limit=1):
                    if msg.author == bot.user:
                        await msg.delete()

                await status_channel.send(embed=embed)
            except discord.Forbidden:
                await interaction.response.send_message(
                    "⚠ 상태 채널에 메시지를 보낼 권한이 없습니다.", ephemeral=True
                )
                return
        else:
            await interaction.response.send_message(
                "⚠ 상태 채널을 찾을 수 없습니다.", ephemeral=True
            )
            return
    else:
        await interaction.response.send_message(
            "⚠ 상태 채널이 설정되지 않았습니다. /상태채널설정을 사용해주세요.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ 봇 상태를 '{text}'로 변경했습니다.", ephemeral=True
    )


@bot.tree.command(
    name="상태채널설정", description="봇 상태를 표시할 채널을 설정합니다. (개발자)"
)
@app_commands.describe(채널="상태 채널")
async def set_status_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    set_guild_status_channel_id(interaction.guild.id, 채널.id)
    await interaction.response.send_message(
        f"✅ 상태 채널을 {채널.mention}로 설정했습니다.", ephemeral=True
    )


@bot.tree.command(
    name="봇랭크갱신", description="봇의 로블록스 랭크를 갱신합니다. (개발자)"
)
@app_commands.describe(랭크명="랭크 이름", 랭크값="랭크 값 (0-255)")
async def update_bot_rank(
    interaction: discord.Interaction, 랭크명: str, 랭크값: int
):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    if not (0 <= 랭크값 <= 255):
        await interaction.response.send_message(
            "❌ 랭크 값은 0~255 사이여야 합니다.", ephemeral=True
        )
        return

    cursor.execute(
        "INSERT OR REPLACE INTO roblox_rank(id, rank_name, rank_value) VALUES(1, ?, ?)",
        (랭크명, 랭크값),
    )
    conn.commit()

    await interaction.response.send_message(
        f"✅ 봇 랭크를 '{랭크명}' (값: {랭크값})로 갱신했습니다.", ephemeral=True
    )


@bot.tree.command(
    name="로그지우기", description="로그 채널의 메시지를 삭제합니다. (개발자)"
)
@app_commands.describe(채널="삭제할 채널", 개수="삭제할 메시지 개수")
async def clear_logs(
    interaction: discord.Interaction, 채널: discord.TextChannel, 개수: int = 10
):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    if 개수 > 100:
        개수 = 100

    await interaction.response.defer(ephemeral=True)

    try:
        deleted = await 채널.purge(limit=개수)
        await interaction.followup.send(
            f"✅ {len(deleted)}개의 메시지를 삭제했습니다.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send("⚠ 메시지 삭제 권한이 없습니다.", ephemeral=True)


@bot.tree.command(
    name="일괄인증삭제", description="모든 유저의 인증을 삭제합니다. (개발자)"
)
async def bulk_unverify(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    view = discord.ui.View(timeout=30)

    async def confirm_callback(i: discord.Interaction):
        if i.user.id != interaction.user.id:
            await i.response.send_message("❌ 명령어 실행자만 사용할 수 있습니다.", ephemeral=True)
            return
        cursor.execute("UPDATE users SET verified=0")
        cursor.execute("DELETE FROM stats")
        conn.commit()
        await i.response.edit_message(
            content="✅ 모든 유저의 인증이 삭제되었습니다.", view=None
        )

    async def cancel_callback(i: discord.Interaction):
        if i.user.id != interaction.user.id:
            await i.response.send_message("❌ 명령어 실행자만 사용할 수 있습니다.", ephemeral=True)
            return
        await i.response.edit_message(content="❌ 취소되었습니다.", view=None)

    confirm_button = discord.ui.Button(label="정말 삭제", style=discord.ButtonStyle.danger)
    cancel_button = discord.ui.Button(label="취소", style=discord.ButtonStyle.secondary)
    confirm_button.callback = confirm_callback
    cancel_button.callback = cancel_callback
    view.add_item(confirm_button)
    view.add_item(cancel_button)

    await interaction.response.send_message(
        "⚠ 모든 유저의 인증을 삭제할까요?", view=view, ephemeral=True
    )


@bot.tree.command(name="재동기화", description="봇 명령어를 재동기화합니다. (개발자)")
async def resync_commands(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        if interaction.guild:
            synced = await bot.tree.sync(guild=interaction.guild)
            await interaction.followup.send(
                f"✅ {len(synced)}개 명령어를 동기화했습니다.", ephemeral=True
            )
        else:
            synced = await bot.tree.sync()
            await interaction.followup.send(
                f"✅ 전역으로 {len(synced)}개 명령어를 동기화했습니다.", ephemeral=True
            )
    except Exception as e:
        await interaction.followup.send(
            f"❌ 동기화 중 오류가 발생했습니다: {e}", ephemeral=True
        )


@bot.tree.command(name="확인", description="데이터 초기화 확인 (개발자)")
async def confirm_action(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    cursor.execute("SELECT COUNT(*) FROM users")
    user_count = cursor.fetchone()[0]

    embed = discord.Embed(title="현재 데이터 상태", color=discord.Color.blurple())
    embed.add_field(name="등록된 유저", value=str(user_count), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="확인삭제", description="일괄 인증 삭제 확인 (개발자)")
async def confirm_unverify(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("❌ 개발자만 사용할 수 있습니다.", ephemeral=True)
        return

    cursor.execute("SELECT COUNT(*) FROM users WHERE verified=1")
    verified_count = cursor.fetchone()[0]

    embed = discord.Embed(title="현재 인증 상태", color=discord.Color.blurple())
    embed.add_field(name="인증된 유저", value=str(verified_count), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- 태스크 / 이벤트 ----------


@tasks.loop(minutes=5)
async def auto_sync():
    print("자동 동기화 완료")


@bot.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()

    await bot.change_presence(activity=discord.Game("🟢 정상 작동중 입니다."))
    if not auto_sync.is_running():
        auto_sync.start()

    print(f"봇 실행 완료: {bot.user} (ID: {bot.user.id})")


async def main():
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
