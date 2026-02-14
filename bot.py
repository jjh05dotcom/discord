import os
import random
import urllib.parse
import aiohttp
import discord
from discord.ext import commands

TOKEN = "MTQ3MjE3NDgyODcyNjM5MTA3MA.GYFQq_.yDoCs7kovpGFZKP9_mtgeqYmKovPHv-4rHypFM"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DANBOORU_URL = "https://danbooru.donmai.us/posts.json"
CACHE_LIMIT = 50  # 한 번에 받아올 개수(너무 크게 하지 마)


async def fetch_danbooru_posts(tags: str, limit: int = CACHE_LIMIT):
    # tags: "tag1 tag2" 형태로 들어오면 +로 바꿔서 요청
    # rating:e = explicit만
    safe_tags = " ".join(tags.split()).replace(" ", "+")
    query_tags = f"{safe_tags}+rating:e" if safe_tags else "rating:e"

    params = {"tags": query_tags, "limit": limit}

    async with aiohttp.ClientSession() as session:
        async with session.get(DANBOORU_URL, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Danbooru HTTP {resp.status}: {text[:200]}")
            data = await resp.json()

    # file_url 있는 것만 추림
    posts = [p for p in data if p.get("file_url")]
    return posts


def post_to_embed(post: dict, tags: str):
    post_id = post.get("id")
    file_url = post.get("file_url")
    source = f"https://danbooru.donmai.us/posts/{post_id}" if post_id else "https://danbooru.donmai.us/"

    embed = discord.Embed(
        title="Danbooru 결과",
        description=f"tags: `{tags}`  |  rating: `e`",
        url=source,
    )
    embed.set_image(url=file_url)
    embed.set_footer(text=f"post_id: {post_id}")
    return embed


class R18View(discord.ui.View):
    def __init__(self, owner_id: int, tags: str):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.tags = tags
        self.posts: list[dict] = []
        self.idx = 0

    async def ensure_posts(self):
        if not self.posts:
            self.posts = await fetch_danbooru_posts(self.tags)
            random.shuffle(self.posts)
            self.idx = 0

    def current_post(self):
        if not self.posts:
            return None
        return self.posts[self.idx]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 버튼은 명령어 친 사람만 누르게
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("지혁이만 조작 가능함", ephemeral=True)
            return False
        # NSFW 채널 제한
        if interaction.channel and hasattr(interaction.channel, "is_nsfw") and not interaction.channel.is_nsfw():
            await interaction.response.send_message("NSFW 채널에서만 사용 가능", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="다음", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.ensure_posts()
        if not self.posts:
            return await interaction.response.send_message("검색 결과 없음", ephemeral=True)

        self.idx += 1
        if self.idx >= len(self.posts):
            # 다 쓰면 다시 가져와서 무한
            self.posts = await fetch_danbooru_posts(self.tags)
            random.shuffle(self.posts)
            self.idx = 0

        embed = post_to_embed(self.current_post(), self.tags)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.secondary)
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.posts = await fetch_danbooru_posts(self.tags)
        random.shuffle(self.posts)
        self.idx = 0
        if not self.posts:
            return await interaction.response.send_message("검색 결과 없음", ephemeral=True)

        embed = post_to_embed(self.current_post(), self.tags)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="닫기", style=discord.ButtonStyle.danger)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.delete()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.command()
async def r18(ctx: commands.Context, *, tags: str = ""):
    # NSFW 채널 제한
    if hasattr(ctx.channel, "is_nsfw") and not ctx.channel.is_nsfw():
        return await ctx.send("NSFW 채널에서만 사용 가능")

    view = R18View(owner_id=ctx.author.id, tags=tags)
    await view.ensure_posts()

    if not view.posts:
        return await ctx.send("검색 결과 없음")

    embed = post_to_embed(view.current_post(), tags)
    await ctx.send(embed=embed, view=view)


bot.run(TOKEN)