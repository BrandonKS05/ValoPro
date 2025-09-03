import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

INTENTS = discord.Intents.default()
BOT = commands.Bot(command_prefix="!", intents=INTENTS)

@BOT.event
async def on_ready():
    try:
        await BOT.tree.sync()
    except Exception:
        pass
    print(f"Logged in as {BOT.user}")

async def main():
    async with BOT:
        await BOT.load_extension("cogs.tracker")
        await BOT.start(os.getenv("DISCORD_TOKEN"))

if __name__ == "__main__":
    asyncio.run(main())
