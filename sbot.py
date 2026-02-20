from aiohttp import web

import json
import os
import asyncio
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import tasks

# =========================
# 토큰: 환경변수로만 받기
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수 설정 안 됨 (토큰을 환경변수로 넣어야 함)")

DATA_FILE = "sbot_data.json"

# -------------------------
# 경고 누적 처벌 단계 (3회부터 적용)
# 3회: 5분, 4회: 10분, 5회: 1시간, 6회: 1일, 7회: 1주, 8회: 강퇴
# -------------------------
WARN_TIMEOUT_MINUTES = {
    3: 5,
    4: 10,
    5: 60,
    6: 24 * 60,
    7: 7 * 24 * 60,
}
WARN_KICK_AT = 8

AUTO_DELETE_SECONDS = 10  # 자동메시지만 삭제 딜레이


# =========================
# 데이터 저장/로드
# =========================
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {
            "log_channel_id": {},     # log_channel_id[guild_id] = channel_id
            "auto_channel_id": {},    # auto_channel_id[guild_id] = channel_id
            "auto_message": {},       # auto_message[guild_id] = "문구"
            "warnings": {},           # warnings[guild_id][user_id] = [ ... ]
        }

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    data.setdefault("log_channel_id", {})
    data.setdefault("auto_channel_id", {})
    data.setdefault("auto_message", {})
    data.setdefault("warnings", {})
    return data


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


DATA = load_data()


def _gid(guild_id: int) -> str:
    return str(guild_id)


def _uid(user_id: int) -> str:
    return str(user_id)


# =========================
# 디스코드 기본 세팅
# =========================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# =========================
# Interaction 안전 응답 (40060 방지)
# =========================
async def safe_reply(interaction: discord.Interaction, content: str, *, ephemeral: bool = True):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(content, ephemeral=ephemeral)
        return await interaction.response.send_message(content, ephemeral=ephemeral)
    except discord.errors.HTTPException as e:
        print(f"[safe_reply] failed: {e}")


# =========================
# 길드/채널 헬퍼
# =========================
def get_guild_by_id(guild_id: int) -> discord.Guild | None:
    return client.get_guild(guild_id)


def is_text_channel(ch) -> bool:
    return isinstance(ch, discord.TextChannel)


def ensure_channel_belongs_to_guild(ch: discord.TextChannel, guild_id: int) -> bool:
    return ch.guild and ch.guild.id == guild_id


# =========================
# 로그 채널(공개 메시지, 길드별)
# =========================
async def log_action(guild: discord.Guild, text: str):
    if not guild:
        return
    gid = _gid(guild.id)
    ch_id = DATA.get("log_channel_id", {}).get(gid)
    if not ch_id:
        return

    ch = guild.get_channel(int(ch_id))
    if ch and is_text_channel(ch):
        try:
            await ch.send(text)  # ✅ 모두가 보는 로그
        except Exception as e:
            print(f"[log_action] failed: {e}")


# =========================
# 10분마다 자동 메시지 + 10초 후 삭제 (길드별)
# =========================
@tasks.loop(minutes=10)
async def auto_message_task():
    auto_map = DATA.get("auto_channel_id", {})
    msg_map = DATA.get("auto_message", {})

    if not auto_map:
        return

    for gid_str, ch_id in list(auto_map.items()):
        try:
            guild_id = int(gid_str)
            channel_id = int(ch_id)
        except Exception:
            continue

        guild = get_guild_by_id(guild_id)
        if not guild:
            continue

        ch = guild.get_channel(channel_id)
        if not (ch and is_text_channel(ch)):
            continue

        msg = msg_map.get(gid_str, "10분마다 자동 메시지")
        try:
            sent = await ch.send(msg)
            try:
                await sent.delete(delay=AUTO_DELETE_SECONDS)
            except Exception:
                pass
        except Exception as e:
            print(f"[auto_message] send failed guild={guild_id}: {e}")


@auto_message_task.before_loop
async def before_auto_message_task():
    await client.wait_until_ready()


# =========================
# Render 포트 바인딩용 웹서버
# =========================
async def _handle_root(request):
    return web.Response(text="ok")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _handle_root)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[web] listening on 0.0.0.0:{port}")


# =========================
# 준비 완료
# =========================
@client.event
async def on_ready():
    try:
        await tree.sync()
    except Exception as e:
        print(f"[sync] failed: {e}")

    await client.change_presence(activity=discord.Game("대박박하는 중"))
    print(f"Logged in as {client.user}")

    if not auto_message_task.is_running():
        auto_message_task.start()


# =========================================================
# 1) 설정 - 현재 서버용(길드ID 생략)
# =========================================================
@tree.command(name="setlog", description="(현재 서버) 로그 채널 설정(채널 선택)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        return await safe_reply(interaction, "서버에서만 가능.", ephemeral=True)

    gid = interaction.guild.id
    if not ensure_channel_belongs_to_guild(channel, gid):
        return await safe_reply(interaction, "그 채널이 현재 서버 채널이 아님.", ephemeral=True)

    DATA.setdefault("log_channel_id", {})
    DATA["log_channel_id"][_gid(gid)] = channel.id
    save_data(DATA)

    await safe_reply(interaction, f"로그 채널 설정 완료: {channel.mention}", ephemeral=True)
    await log_action(interaction.guild, f"📝 로그 채널 설정: {channel.mention} (관리자: {interaction.user.mention})")


@tree.command(name="setauto", description="(현재 서버) 10분 자동메시지 설정(채널 선택, 10초 후 삭제)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setauto(interaction: discord.Interaction, channel: discord.TextChannel, message: str = "10분마다 자동 메시지"):
    if not interaction.guild:
        return await safe_reply(interaction, "서버에서만 가능.", ephemeral=True)

    gid = interaction.guild.id
    if not ensure_channel_belongs_to_guild(channel, gid):
        return await safe_reply(interaction, "그 채널이 현재 서버 채널이 아님.", ephemeral=True)

    gid_str = _gid(gid)
    DATA.setdefault("auto_channel_id", {})
    DATA.setdefault("auto_message", {})
    DATA["auto_channel_id"][gid_str] = channel.id
    DATA["auto_message"][gid_str] = message
    save_data(DATA)

    await safe_reply(
        interaction,
        f"자동메시지 설정 완료: {channel.mention}\n문구: {message}\n(10분마다 나가고 10초 뒤 삭제됨)",
        ephemeral=True,
    )
    await log_action(interaction.guild, f"⏱️ 자동메시지 설정: {channel.mention} (관리자: {interaction.user.mention})")


@tree.command(name="delauto", description="(현재 서버) 자동메시지 해제")
@app_commands.checks.has_permissions(manage_guild=True)
async def delauto(interaction: discord.Interaction):
    if not interaction.guild:
        return await safe_reply(interaction, "서버에서만 가능.", ephemeral=True)

    gid = interaction.guild.id
    gid_str = _gid(gid)

    DATA.setdefault("auto_channel_id", {})
    DATA.setdefault("auto_message", {})
    DATA["auto_channel_id"].pop(gid_str, None)
    DATA["auto_message"].pop(gid_str, None)
    save_data(DATA)

    await safe_reply(interaction, "이 서버 자동메시지 해제 완료.", ephemeral=True)
    await log_action(interaction.guild, f"🗑️ 자동메시지 해제 (관리자: {interaction.user.mention})")


# =========================================================
# 2) 설정 - 길드ID 지정용(다른 서버도 바로 설정)
# =========================================================
@tree.command(name="setlog_g", description="(길드ID 지정) 로그 채널 설정")
@app_commands.checks.has_permissions(manage_guild=True)
async def setlog_g(interaction: discord.Interaction, guild_id: str, channel: discord.TextChannel):
    if not guild_id.isdigit():
        return await safe_reply(interaction, "guild_id는 숫자만.", ephemeral=True)

    gid = int(guild_id)
    guild = get_guild_by_id(gid)
    if not guild:
        return await safe_reply(interaction, "그 길드ID를 봇이 못 찾음(봇이 그 서버에 있어야 함).", ephemeral=True)

    if not ensure_channel_belongs_to_guild(channel, gid):
        return await safe_reply(interaction, "그 채널이 입력한 길드ID의 채널이 아님.", ephemeral=True)

    DATA.setdefault("log_channel_id", {})
    DATA["log_channel_id"][_gid(gid)] = channel.id
    save_data(DATA)

    await safe_reply(interaction, f"로그 채널 설정 완료: **{guild.name}** / {channel.mention}", ephemeral=True)
    await log_action(guild, f"📝 로그 채널 설정: {channel.mention} (관리자: {interaction.user.mention})")


@tree.command(name="setauto_g", description="(길드ID 지정) 10분 자동메시지 설정(10초 후 삭제)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setauto_g(interaction: discord.Interaction, guild_id: str, channel: discord.TextChannel, message: str = "10분마다 자동 메시지"):
    if not guild_id.isdigit():
        return await safe_reply(interaction, "guild_id는 숫자만.", ephemeral=True)

    gid = int(guild_id)
    guild = get_guild_by_id(gid)
    if not guild:
        return await safe_reply(interaction, "그 길드ID를 봇이 못 찾음(봇이 그 서버에 있어야 함).", ephemeral=True)

    if not ensure_channel_belongs_to_guild(channel, gid):
        return await safe_reply(interaction, "그 채널이 입력한 길드ID의 채널이 아님.", ephemeral=True)

    gid_str = _gid(gid)
    DATA.setdefault("auto_channel_id", {})
    DATA.setdefault("auto_message", {})
    DATA["auto_channel_id"][gid_str] = channel.id
    DATA["auto_message"][gid_str] = message
    save_data(DATA)

    await safe_reply(
        interaction,
        f"자동메시지 설정 완료: **{guild.name}** / {channel.mention}\n문구: {message}\n(10분마다 나가고 10초 뒤 삭제됨)",
        ephemeral=True,
    )
    await log_action(guild, f"⏱️ 자동메시지 설정: {channel.mention} (관리자: {interaction.user.mention})")


@tree.command(name="delauto_g", description="(길드ID 지정) 자동메시지 해제")
@app_commands.checks.has_permissions(manage_guild=True)
async def delauto_g(interaction: discord.Interaction, guild_id: str):
    if not guild_id.isdigit():
        return await safe_reply(interaction, "guild_id는 숫자만.", ephemeral=True)

    gid = int(guild_id)
    guild = get_guild_by_id(gid)
    if not guild:
        return await safe_reply(interaction, "그 길드ID를 봇이 못 찾음(봇이 그 서버에 있어야 함).", ephemeral=True)

    gid_str = _gid(gid)
    DATA.setdefault("auto_channel_id", {})
    DATA.setdefault("auto_message", {})
    DATA["auto_channel_id"].pop(gid_str, None)
    DATA["auto_message"].pop(gid_str, None)
    save_data(DATA)

    await safe_reply(interaction, f"자동메시지 해제 완료: **{guild.name}**", ephemeral=True)
    await log_action(guild, f"🗑️ 자동메시지 해제 (관리자: {interaction.user.mention})")


# =========================================================
# 3) 관리: 메시지 삭제 /clear
# =========================================================
@tree.command(name="clear", description="현재 채널 메시지 여러 개 삭제")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, count: app_commands.Range[int, 1, 100]):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await safe_reply(interaction, "텍스트 채널에서만 가능.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await channel.purge(limit=count)
        await interaction.followup.send(f"{len(deleted)}개 삭제했어.", ephemeral=True)
        await log_action(interaction.guild, f"🧹 메시지 삭제: {len(deleted)}개 (채널: {channel.mention}, 실행: {interaction.user.mention})")
    except discord.Forbidden:
        await interaction.followup.send("권한 부족(봇에 '메시지 관리' 권한 필요).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"실패: {e}", ephemeral=True)


# =========================================================
# 4) 관리: 경고 시스템 (+ 누적 자동 처벌)
# =========================================================
@tree.command(name="warn", description="유저 경고 1회 추가(3회부터 자동 처벌)")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
    if member == interaction.user:
        return await safe_reply(interaction, "자기 자신은 안 돼.", ephemeral=True)

    gid = _gid(interaction.guild.id)
    uid = _uid(member.id)

    DATA.setdefault("warnings", {})
    DATA["warnings"].setdefault(gid, {})
    DATA["warnings"][gid].setdefault(uid, [])

    DATA["warnings"][gid][uid].append(
        {"by": str(interaction.user.id), "reason": reason or "", "ts": discord.utils.utcnow().isoformat()}
    )
    save_data(DATA)

    total = len(DATA["warnings"][gid][uid])

    await safe_reply(interaction, f"{member.mention} 경고 추가됨. (누적 {total})", ephemeral=True)
    await log_action(interaction.guild, f"⚠️ 경고: {member.mention} (누적 {total}회) (실행: {interaction.user.mention}) 사유: {reason or '없음'}")

    if total >= WARN_KICK_AT:
        try:
            await member.kick(reason=f"Warn reached {total}. {reason or ''}".strip())
            await log_action(interaction.guild, f"👢 자동 강퇴: {member.mention} (경고 {total}회 도달)")
        except discord.Forbidden:
            await log_action(interaction.guild, f"❌ 자동 강퇴 실패(권한): {member.mention} (경고 {total}회)")
        return

    minutes = WARN_TIMEOUT_MINUTES.get(total)
    if minutes:
        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        current_until = getattr(member, "communication_disabled_until", None)
        if current_until and current_until > until:
            return
        try:
            await member.timeout(until, reason=f"Warn reached {total}. {reason or ''}".strip())
            await log_action(interaction.guild, f"🔇 자동 타임아웃: {member.mention} {minutes}분 (경고 {total}회)")
        except discord.Forbidden:
            await log_action(interaction.guild, f"❌ 자동 타임아웃 실패(권한): {member.mention} (경고 {total}회)")


@tree.command(name="warnings", description="유저 경고 내역/누적 확인")
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    gid = _gid(interaction.guild.id)
    uid = _uid(member.id)
    items = DATA.get("warnings", {}).get(gid, {}).get(uid, [])

    if not items:
        return await safe_reply(interaction, f"{member.mention} 경고 없음.", ephemeral=True)

    lines = []
    start_index = max(1, len(items) - 9)
    for i, w in enumerate(items[-10:], start=start_index):
        r = w.get("reason", "")
        ts = w.get("ts", "")
        lines.append(f"{i}. {ts} | 사유: {r if r else '(없음)'}")

    msg = f"**{member.mention} 경고 누적: {len(items)}**\n" + "\n".join(lines)
    await safe_reply(interaction, msg, ephemeral=True)


@tree.command(name="clearwarnings", description="유저 경고 전부 삭제")
@app_commands.checks.has_permissions(moderate_members=True)
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    gid = _gid(interaction.guild.id)
    uid = _uid(member.id)

    if DATA.get("warnings", {}).get(gid, {}).get(uid) is None:
        return await safe_reply(interaction, "삭제할 경고가 없어.", ephemeral=True)

    DATA["warnings"][gid].pop(uid, None)
    save_data(DATA)

    await safe_reply(interaction, f"{member.mention} 경고 삭제 완료.", ephemeral=True)
    await log_action(interaction.guild, f"🧽 경고 삭제: {member.mention} (실행: {interaction.user.mention})")


# =========================================================
# 공통 에러 처리
# =========================================================
@setlog.error
@setauto.error
@delauto.error
@setlog_g.error
@setauto_g.error
@delauto_g.error
@clear.error
@warn.error
@warnings.error
@clearwarnings.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        return await safe_reply(interaction, "그 명령어 쓸 권한이 없어.", ephemeral=True)
    return await safe_reply(interaction, f"에러: {error}", ephemeral=True)


# =========================
# 실행 (Render 포트 바인딩 포함)
# =========================
async def main():
    await start_web_server()
    await client.start(TOKEN)

asyncio.run(main())
