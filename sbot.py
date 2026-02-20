from aiohttp import web

import json
import os
import asyncio
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import tasks
import yt_dlp

# =========================
# 토큰: 환경변수로만 받기
# Render/로컬에서 DISCORD_TOKEN 설정 필요
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수 설정 안 됨 (토큰을 환경변수로 넣어야 함)")

DATA_FILE = "sbot_data.json"

# -------------------------
# 경고 누적 처벌 단계
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

# -------------------------
# yt-dlp / FFMPEG 설정
# -------------------------
BASE_YTDLP_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "nocheckcertificate": True,
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# MUSIC[guild_id] = {"queue":[], "now":None, "lock":Lock(), "repeat":"off|one|all"}
MUSIC = {}


def get_music_state(guild_id: int):
    if guild_id not in MUSIC:
        MUSIC[guild_id] = {"queue": [], "now": None, "lock": asyncio.Lock(), "repeat": "off"}
    return MUSIC[guild_id]


def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {
            "log_channel_id": None,
            "music_channel_id": {},
            "warnings": {},
            "auto_channel_id": {},
            "auto_message": {},
        }
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    data.setdefault("log_channel_id", None)
    data.setdefault("music_channel_id", {})
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


async def log_action(guild: discord.Guild, text: str):
    ch_id = DATA.get("log_channel_id")
    if not ch_id or not guild:
        return
    ch = guild.get_channel(ch_id)
    if ch and isinstance(ch, discord.TextChannel):
        await ch.send(text)


def _is_url(s: str) -> bool:
    s = (s or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def ytdlp_extract(query: str, source: str = "auto") -> dict:
    """
    source:
      - "auto": URL이면 그대로, 아니면 SoundCloud 검색으로 시도
      - "soundcloud": SoundCloud 검색/URL 위주
      - "direct": URL만 허용
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("빈 query")

    if source == "direct":
        if not _is_url(q):
            raise ValueError("direct 모드는 URL만 가능")
        yq = q
        yopts = dict(BASE_YTDLP_OPTS)

    elif source == "soundcloud":
        yopts = dict(BASE_YTDLP_OPTS)
        # SoundCloud 검색 prefix: scsearch1:<query>
        yq = q if _is_url(q) else f"scsearch1:{q}"

    else:  # auto
        yopts = dict(BASE_YTDLP_OPTS)
        # 유튜브는 요즘 봇체크/쿠키 문제로 자주 막힘 → 기본은 SoundCloud 검색으로
        yq = q if _is_url(q) else f"scsearch1:{q}"

    with yt_dlp.YoutubeDL(yopts) as ydl:
        info = ydl.extract_info(yq, download=False)

    if "entries" in info:
        info = info["entries"][0]

    return {
        "title": info.get("title", "unknown"),
        "webpage_url": info.get("webpage_url"),
        "stream_url": info.get("url"),
    }


intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # 전용 음악 채널에서 메시지로 자동재생하려면 필요

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# =========================================================
# Render 포트 바인딩용 웹서버 (UptimeRobot용)
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
# Voice helpers
# =========================================================
async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("먼저 음성채널 들어가라", ephemeral=True)
        return None

    vc = interaction.guild.voice_client
    if vc and vc.channel != interaction.user.voice.channel:
        await vc.move_to(interaction.user.voice.channel)
        return vc

    if not vc:
        vc = await interaction.user.voice.channel.connect()
    return vc


async def play_next(guild: discord.Guild):
    state = get_music_state(guild.id)
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return

    async with state["lock"]:
        if vc.is_playing() or vc.is_paused():
            return

        if state["repeat"] == "one" and state["now"]:
            track = state["now"]
        else:
            if not state["queue"]:
                state["now"] = None
                return
            track = state["queue"].pop(0)
            state["now"] = track

        source = discord.FFmpegPCMAudio(track["stream_url"], **FFMPEG_OPTS)

        def after_play(err):
            if state["repeat"] == "all":
                state["queue"].append(track)

            fut = asyncio.run_coroutine_threadsafe(play_next(guild), client.loop)
            try:
                fut.result()
            except Exception:
                pass

        vc.play(source, after=after_play)


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
            msg = msg_map.get(gid, "10분마다 자동 메시지")
            try:
                await ch.send(msg)
            except Exception as e:
                print(f"[auto_message] send failed guild={guild.id}: {e}")


@auto_message_task.before_loop
async def before_auto_message_task():
    await client.wait_until_ready()


@client.event
async def on_ready():
    await tree.sync()
    await client.change_presence(activity=discord.Game("대박박하는 중"))
    print(f"Logged in as {client.user}")

    # Render/UptimeRobot용 포트 열기
    await start_web_server()

    if not auto_message_task.is_running():
        auto_message_task.start()


# =========================================================
# 1) 설정/로그
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
# 3) 관리: 청소(/clear) - 원하는 개수만큼 (최대 500)
# =========================================================
@tree.command(name="clear", description="메시지 여러 개 삭제(최대 500, 100개씩 나눠 삭제)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, count: app_commands.Range[int, 1, 500]):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("텍스트 채널에서만 가능.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    remaining = int(count)
    total_deleted = 0

    while remaining > 0:
        batch = min(remaining, 100)  # 디코 purge는 보통 100 단위가 안정적
        deleted = await channel.purge(limit=batch)
        total_deleted += len(deleted)
        remaining -= batch

        # 너무 빠르게 치면 레이트리밋 걸릴 수 있어서 살짝 텀
        await asyncio.sleep(0.7)

        # 더 이상 지울 게 없으면 종료
        if len(deleted) == 0:
            break

    await interaction.followup.send(f"{total_deleted}개 삭제했어.", ephemeral=True)
    await log_action(interaction.guild, f"🧹 CLEAR: {total_deleted} msgs in #{channel} by {interaction.user}")


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

    if total >= WARN_KICK_AT:
        try:
            await member.kick(reason=f"Warn reached {total}. {reason or ''}".strip())
            await log_action(interaction.guild, f"👢 AUTO-KICK: {member} at warnings={total} by {interaction.user}")
        except discord.Forbidden:
            await log_action(interaction.guild, f"❌ AUTO-KICK FAILED(Forbidden): {member} warnings={total}")
        return

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
# 5) 음악: SoundCloud 중심 + URL 재생 지원
# =========================================================
@tree.command(name="setmusic", description="음악 자동재생 전용 채널 지정(길드별 저장)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setmusic(interaction: discord.Interaction, channel: discord.TextChannel):
    gid = _gid(interaction.guild.id)
    DATA.setdefault("music_channel_id", {})
    DATA["music_channel_id"][gid] = channel.id
    save_data(DATA)
    await interaction.response.send_message(f"음악 전용 채널: {channel.mention}", ephemeral=True)


@tree.command(name="repeat", description="반복 모드 설정(off / one / all)")
async def repeat(interaction: discord.Interaction, mode: str):
    mode = mode.lower().strip()
    if mode not in ("off", "one", "all"):
        return await interaction.response.send_message("mode는 off / one / all 중 하나로 써.", ephemeral=True)
    state = get_music_state(interaction.guild.id)
    state["repeat"] = mode
    await interaction.response.send_message(f"반복 모드: **{mode}**", ephemeral=True)


@tree.command(name="join", description="내가 있는 음성채널로 들어와")
async def join(interaction: discord.Interaction):
    vc = await ensure_voice(interaction)
    if vc and not interaction.response.is_done():
        await interaction.response.send_message("들어감", ephemeral=True)


@tree.command(name="play", description="기본: SoundCloud 검색(제목) 또는 URL 재생(큐에 추가)")
async def play(interaction: discord.Interaction, query: str):
    vc = await ensure_voice(interaction)
    if not vc:
        return

    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_running_loop()
    try:
        track = await loop.run_in_executor(None, ytdlp_extract, query, "auto")
    except Exception as e:
        return await interaction.followup.send(f"추출 실패: {e}", ephemeral=True)

    state = get_music_state(interaction.guild.id)
    state["queue"].append(track)

    await interaction.followup.send(f"큐 추가됨: **{track['title']}**", ephemeral=True)
    await play_next(interaction.guild)


@tree.command(name="playsc", description="SoundCloud에서만 검색/재생(제목 또는 SoundCloud URL)")
async def playsc(interaction: discord.Interaction, query: str):
    vc = await ensure_voice(interaction)
    if not vc:
        return

    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_running_loop()
    try:
        track = await loop.run_in_executor(None, ytdlp_extract, query, "soundcloud")
    except Exception as e:
        return await interaction.followup.send(f"SoundCloud 추출 실패: {e}", ephemeral=True)

    state = get_music_state(interaction.guild.id)
    state["queue"].append(track)

    await interaction.followup.send(f"(SC) 큐 추가됨: **{track['title']}**", ephemeral=True)
    await play_next(interaction.guild)


@tree.command(name="playurl", description="직접 오디오 URL(mp3/m3u8/ogg 등) 재생(큐에 추가)")
async def playurl(interaction: discord.Interaction, url: str):
    vc = await ensure_voice(interaction)
    if not vc:
        return

    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_running_loop()
    try:
        track = await loop.run_in_executor(None, ytdlp_extract, url, "direct")
    except Exception as e:
        return await interaction.followup.send(f"URL 추출 실패: {e}", ephemeral=True)

    state = get_music_state(interaction.guild.id)
    state["queue"].append(track)

    await interaction.followup.send(f"(URL) 큐 추가됨: **{track['title']}**", ephemeral=True)
    await play_next(interaction.guild)


@tree.command(name="skip", description="현재 곡 스킵")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        return await interaction.response.send_message("지금 음성채널에 없음", ephemeral=True)

    if vc.is_playing() or vc.is_paused():
        vc.stop()
        await interaction.response.send_message("스킵함", ephemeral=True)
    else:
        await interaction.response.send_message("재생 중 아님", ephemeral=True)


@tree.command(name="stop", description="재생 중지 + 큐 비우기")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        return await interaction.response.send_message("지금 음성채널에 없음", ephemeral=True)

    state = get_music_state(interaction.guild.id)
    state["queue"].clear()
    state["now"] = None

    if vc.is_playing() or vc.is_paused():
        vc.stop()

    await interaction.response.send_message("중지 + 큐 비움", ephemeral=True)


@tree.command(name="leave", description="음성채널 나가기")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        return await interaction.response.send_message("지금 음성채널에 없음", ephemeral=True)

    state = get_music_state(interaction.guild.id)
    state["queue"].clear()
    state["now"] = None

    if vc.is_playing() or vc.is_paused():
        vc.stop()
    await vc.disconnect()
    await interaction.response.send_message("나감", ephemeral=True)


@tree.command(name="now", description="지금 재생 중인 곡")
async def now(interaction: discord.Interaction):
    state = get_music_state(interaction.guild.id)
    cur = state["now"]
    if not cur:
        return await interaction.response.send_message("지금 재생 중인 곡 없음", ephemeral=True)
    await interaction.response.send_message(f"지금: **{cur['title']}**", ephemeral=True)


@tree.command(name="queue", description="대기열 보기(최대 10개)")
async def queue(interaction: discord.Interaction):
    state = get_music_state(interaction.guild.id)
    q = state["queue"]
    if not q:
        return await interaction.response.send_message("대기열 비었음", ephemeral=True)

    lines = [f"{i}. {t['title']}" for i, t in enumerate(q[:10], start=1)]
    await interaction.response.send_message("대기열:\n" + "\n".join(lines), ephemeral=True)


# =========================================================
# 6) 전용 채널에서: 메시지로 자동 재생 (/play 없이)
#   - 기본은 SoundCloud 검색
#   - URL이면 URL 재생
# =========================================================
@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    gid = _gid(message.guild.id)
    music_map = DATA.get("music_channel_id", {})
    music_ch_id = music_map.get(gid)

    if not music_ch_id or message.channel.id != music_ch_id:
        return

    content = (message.content or "").strip()
    if not content:
        return

    if content.startswith("/") or content.startswith("!"):
        return

    if not message.author.voice or not message.author.voice.channel:
        return await message.channel.send("먼저 음성채널 들어가라")

    try:
        vc = message.guild.voice_client
        if not vc:
            vc = await message.author.voice.channel.connect()
        elif vc.channel != message.author.voice.channel:
            await vc.move_to(message.author.voice.channel)

        loop = asyncio.get_running_loop()
        src = "direct" if _is_url(content) else "soundcloud"
        track = await loop.run_in_executor(None, ytdlp_extract, content, src)

        state = get_music_state(message.guild.id)
        state["queue"].append(track)

        await message.channel.send(f"큐 추가됨: **{track['title']}**")
        await play_next(message.guild)

    except Exception as e:
        await message.channel.send(f"실패: {e}")


# =========================================================
# 에러 처리(권한 부족 메시지)
# =========================================================
@setlog.error
@setmusic.error
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
# 메인 엔트리
# =========================================================
async def main():
    await client.start(TOKEN)


asyncio.run(main())
