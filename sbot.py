from aiohttp import web

import json
import os
import asyncio
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import tasks

# =========================
# 토큰: 환경변수로만 받기 (절대 코드/파일에 저장 금지)
# Render/로컬에서 DISCORD_TOKEN 설정 필요
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


def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {
            "log_channel_id": None,
            "warnings": {},
            "auto_channel_id": {},
            "auto_message": {},
        }
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    data.setdefault("log_channel_id", None)
    data.setdefault("warnings", {})
    data.setdefault("auto_channel_id", {})
    data.setdefault("auto_message", {})
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
# Discord 기본 세팅
# =========================
intents = discord.Intents.default()
intents.members = True
# /clear는 purge라 message_content 없어도 됨(슬래시 명령어 기반).
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def log_action(guild: discord.Guild, text: str):
    ch_id = DATA.get("log_channel_id")
    if not ch_id or not guild:
        return
    ch = guild.get_channel(int(ch_id))
    if ch and isinstance(ch, discord.TextChannel):
        try:
            await ch.send(text)  # ✅ 로그는 삭제 안 함
        except Exception as e:
            print(f"[log_action] failed: {e}")


# =========================================================
# Render/UptimeRobot용 웹서버 (포트 바인딩 필수)
# =========================================================
async def _handle_root(request):
    return web.Response(text="ok")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _handle_root)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))  # Render가 PORT 환경변수로 줌
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


# =========================================================
# 10분마다 자동 메시지 태스크 (길드별 설정)
# =========================================================
@tasks.loop(minutes=10)
async def auto_message_task():
    auto_map = DATA.get("auto_channel_id", {})
    msg_map = DATA.get("auto_message", {})

    if not auto_map:
        return

    for guild in client.guilds:
        gid = _gid(guild.id)
        ch_id = auto_map.get(gid)
        if not ch_id:
            continue

        ch = guild.get_channel(int(ch_id))
        if ch and isinstance(ch, discord.TextChannel):
            msg_text = msg_map.get(gid, "10분마다 자동 메시지")
            try:
                sent = await ch.send(msg_text)
                await sent.delete(delay=10)  # ✅ 자동메시지만 10초 뒤 삭제
            except discord.Forbidden:
                # 삭제 권한 없으면 그냥 보내기만 하고 끝
                pass
            except Exception as e:
                print(f"[auto_message] send failed guild={guild.id}: {e}")


@auto_message_task.before_loop
async def before_auto_message_task():
    await client.wait_until_ready()


@client.event
async def on_ready():
    guild_id = os.getenv("1332296150086189110")  # 서버 ID를 환경변수로
    if guild_id:
        guild = discord.Object(id=int(guild_id))
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)   # ✅ 서버 전용 즉시 등록
        print(f"[SYNC] guild synced: {len(synced)} commands")
    else:
        synced = await tree.sync()              # 글로벌(느림)
        print(f"[SYNC] global synced: {len(synced)} commands")

    await client.change_presence(activity=discord.Game("대박박하는 중"))
    print(f"Logged in as {client.user}")

# =========================================================
# 1) 설정/로그/자동메시지
# =========================================================
@tree.command(name="setlog", description="관리 로그를 남길 채널 지정")
@app_commands.checks.has_permissions(manage_guild=True)
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    DATA["log_channel_id"] = channel.id
    save_data(DATA)
    await interaction.response.send_message(f"로그 채널을 {channel.mention} 로 설정했어.", ephemeral=True)


@tree.command(name="setauto", description="10분마다 자동 메시지 보낼 채널/문구 설정(길드별)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setauto(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: str = "10분마다 자동 메시지",
):
    gid = _gid(interaction.guild.id)
    DATA.setdefault("auto_channel_id", {})
    DATA.setdefault("auto_message", {})

    DATA["auto_channel_id"][gid] = channel.id
    DATA["auto_message"][gid] = message
    save_data(DATA)

    await interaction.response.send_message(
        f"자동메시지 채널: {channel.mention}\n문구: {message}\n(10분마다 자동으로 나감)",
        ephemeral=True,
    )


@tree.command(name="delauto", description="자동 메시지 설정 삭제(길드별)")
@app_commands.checks.has_permissions(manage_guild=True)
async def delauto(interaction: discord.Interaction):
    gid = _gid(interaction.guild.id)
    DATA.setdefault("auto_channel_id", {})
    DATA.setdefault("auto_message", {})

    DATA["auto_channel_id"].pop(gid, None)
    DATA["auto_message"].pop(gid, None)
    save_data(DATA)

    await interaction.response.send_message("이 길드의 자동메시지 설정 삭제함.", ephemeral=True)


# =========================================================
# 2) 관리: 킥/밴/언밴/타임아웃
# =========================================================
@tree.command(name="kick", description="유저를 킥")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
    if member == interaction.user:
        return await interaction.response.send_message("자기 자신은 안 돼.", ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"{member} 킥 완료.", ephemeral=True)
        await log_action(interaction.guild, f"👢 KICK: {member} by {interaction.user} | reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("권한 부족(봇 역할 위치/권한 확인).", ephemeral=True)


@tree.command(name="ban", description="유저를 밴")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
    if member == interaction.user:
        return await interaction.response.send_message("자기 자신은 안 돼.", ephemeral=True)
    try:
        await member.ban(reason=reason, delete_message_days=0)
        await interaction.response.send_message(f"{member} 밴 완료.", ephemeral=True)
        await log_action(interaction.guild, f"⛔ BAN: {member} by {interaction.user} | reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("권한 부족(봇 역할 위치/권한 확인).", ephemeral=True)


@tree.command(name="unban", description="밴 해제(유저ID 또는 name#discrim)")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user: str):
    guild = interaction.guild
    bans = [b async for b in guild.bans()]

    target = None
    if user.isdigit():
        uid = int(user)
        for b in bans:
            if b.user.id == uid:
                target = b.user
                break
    else:
        for b in bans:
            if f"{b.user.name}#{b.user.discriminator}" == user:
                target = b.user
                break

    if not target:
        return await interaction.response.send_message("해당 밴 유저를 못 찾았어.", ephemeral=True)

    await guild.unban(target)
    await interaction.response.send_message(f"{target} 언밴 완료.", ephemeral=True)
    await log_action(guild, f"✅ UNBAN: {target} by {interaction.user}")


@tree.command(name="timeout", description="유저 타임아웃(분 단위)")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 10080],
    reason: str | None = None,
):
    if member == interaction.user:
        return await interaction.response.send_message("자기 자신은 안 돼.", ephemeral=True)

    until = discord.utils.utcnow() + timedelta(minutes=minutes)
    try:
        await member.timeout(until, reason=reason)
        await interaction.response.send_message(f"{member} 타임아웃 {minutes}분 완료.", ephemeral=True)
        await log_action(interaction.guild, f"🔇 TIMEOUT: {member} {minutes}m by {interaction.user} | reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("권한 부족(봇 역할 위치/권한 확인).", ephemeral=True)


# =========================================================
# 3) 관리: 청소/잠금/해제/역할
# =========================================================
@tree.command(name="clear", description="메시지 여러 개 삭제(최대 100개)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, count: app_commands.Range[int, 1, 100]):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("텍스트 채널에서만 가능.", ephemeral=True)

    # 중요: purge는 봇에 '메시지 관리' 권한이 있어야 하고,
    # 채널 권한에서 봇이 해당 권한을 갖고 있어야 함.
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await channel.purge(limit=count)
        await interaction.followup.send(f"{len(deleted)}개 삭제했어.", ephemeral=True)
        await log_action(interaction.guild, f"🧹 CLEAR: {len(deleted)} msgs in #{channel} by {interaction.user}")
    except discord.Forbidden:
        await interaction.followup.send("삭제 권한이 없어. (봇 권한: 메시지 관리/메시지 읽기 확인)", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"삭제 실패: {e}", ephemeral=True)


@tree.command(name="lock", description="현재 채널 잠금(기본 역할 전송 금지)")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("텍스트 채널에서만 가능.", ephemeral=True)

    everyone = interaction.guild.default_role
    overwrite = channel.overwrites_for(everyone)
    overwrite.send_messages = False
    await channel.set_permissions(everyone, overwrite=overwrite)

    await interaction.response.send_message("채널 잠금 완료.", ephemeral=True)
    await log_action(interaction.guild, f"🔒 LOCK: #{channel} by {interaction.user}")


@tree.command(name="unlock", description="현재 채널 잠금 해제(기본 역할 전송 허용)")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("텍스트 채널에서만 가능.", ephemeral=True)

    everyone = interaction.guild.default_role
    overwrite = channel.overwrites_for(everyone)
    overwrite.send_messages = None
    await channel.set_permissions(everyone, overwrite=overwrite)

    await interaction.response.send_message("채널 잠금 해제 완료.", ephemeral=True)
    await log_action(interaction.guild, f"🔓 UNLOCK: #{channel} by {interaction.user}")


@tree.command(name="role_add", description="유저에게 역할 추가")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_add(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    try:
        await member.add_roles(role)
        await interaction.response.send_message(f"{member.mention} 에게 {role.mention} 추가 완료.", ephemeral=True)
        await log_action(interaction.guild, f"➕ ROLE_ADD: {role} to {member} by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("권한 부족(봇 역할이 해당 역할보다 위여야 함).", ephemeral=True)


@tree.command(name="role_remove", description="유저에게서 역할 제거")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_remove(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    try:
        await member.remove_roles(role)
        await interaction.response.send_message(f"{member.mention} 에서 {role.mention} 제거 완료.", ephemeral=True)
        await log_action(interaction.guild, f"➖ ROLE_REMOVE: {role} from {member} by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("권한 부족(봇 역할이 해당 역할보다 위여야 함).", ephemeral=True)


# =========================================================
# 4) 관리: 경고 시스템 (+ 누적 자동 처벌)
# =========================================================
@tree.command(name="warn", description="유저 경고 1회 추가(3회부터 자동 처벌)")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
    if member == interaction.user:
        return await interaction.response.send_message("자기 자신은 안 돼.", ephemeral=True)

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

    await interaction.response.send_message(f"{member} 경고 추가됨. (누적 {total})", ephemeral=True)
    await log_action(interaction.guild, f"⚠️ WARN: {member} now {total} by {interaction.user} | reason: {reason}")

    # 8회부터 강퇴
    if total >= WARN_KICK_AT:
        try:
            await member.kick(reason=f"Warn reached {total}. {reason or ''}".strip())
            await log_action(interaction.guild, f"👢 AUTO-KICK: {member} at warnings={total} by {interaction.user}")
        except discord.Forbidden:
            await log_action(interaction.guild, f"❌ AUTO-KICK FAILED(Forbidden): {member} warnings={total}")
        return

    # 3~7회 타임아웃
    minutes = WARN_TIMEOUT_MINUTES.get(total)
    if minutes:
        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        current_until = getattr(member, "communication_disabled_until", None)
        if current_until and current_until > until:
            return
        try:
            await member.timeout(until, reason=f"Warn reached {total}. {reason or ''}".strip())
            await log_action(
                interaction.guild,
                f"🔇 AUTO-TIMEOUT: {member} {minutes}m at warnings={total} by {interaction.user}",
            )
        except discord.Forbidden:
            await log_action(interaction.guild, f"❌ AUTO-TIMEOUT FAILED(Forbidden): {member} warnings={total}")


@tree.command(name="warnings", description="유저 경고 내역/누적 확인")
@app_commands.checks.has_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    gid = _gid(interaction.guild.id)
    uid = _uid(member.id)
    items = DATA.get("warnings", {}).get(gid, {}).get(uid, [])

    if not items:
        return await interaction.response.send_message(f"{member} 경고 없음.", ephemeral=True)

    lines = []
    start_index = max(1, len(items) - 9)
    for i, w in enumerate(items[-10:], start=start_index):
        r = w.get("reason", "")
        ts = w.get("ts", "")
        lines.append(f"{i}. {ts} | reason: {r if r else '(없음)'}")

    msg = f"**{member} 경고 누적: {len(items)}**\n" + "\n".join(lines)
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="clearwarnings", description="유저 경고 전부 삭제")
@app_commands.checks.has_permissions(moderate_members=True)
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    gid = _gid(interaction.guild.id)
    uid = _uid(member.id)

    if DATA.get("warnings", {}).get(gid, {}).get(uid) is None:
        return await interaction.response.send_message("삭제할 경고가 없어.", ephemeral=True)

    DATA["warnings"][gid].pop(uid, None)
    save_data(DATA)

    await interaction.response.send_message(f"{member} 경고 삭제 완료.", ephemeral=True)
    await log_action(interaction.guild, f"🧽 CLEARWARN: {member} by {interaction.user}")


# =========================================================
# 에러 처리(권한 부족 메시지)
# =========================================================
@setlog.error
@setauto.error
@delauto.error
@kick.error
@ban.error
@unban.error
@timeout.error
@clear.error
@lock.error
@unlock.error
@role_add.error
@role_remove.error
@warn.error
@warnings.error
@clearwarnings.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        if interaction.response.is_done():
            return await interaction.followup.send("그 명령어 쓸 권한이 없어.", ephemeral=True)
        return await interaction.response.send_message("그 명령어 쓸 권한이 없어.", ephemeral=True)

    msg = f"에러: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# =========================================================
# 엔트리포인트
# =========================================================
async def main():
    await start_web_server()
    await client.start(TOKEN)


asyncio.run(main())
