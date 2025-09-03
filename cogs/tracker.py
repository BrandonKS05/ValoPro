import os
import time
import asyncio
import logging
import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from utils.vlr_api import get_upcoming, get_live, normalize_match, filter_matches

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("valopro.tracker")

ALERT_LEAD_MINUTES = int(os.getenv("ALERT_LEAD_MINUTES", "30"))

def fmt_delta(seconds: int) -> str:
    if seconds <= 0:
        return "now"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m"

def parse_unix(ts: str | int | None) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, int):
        return ts
    # try "YYYY-MM-DD HH:MM:SS"
    try:
        return int(dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return None

def make_embed_from_match(m: dict, live: bool=False) -> discord.Embed:
    # Support both normalized and flat dicts
    t1 = m.get('team1')
    t2 = m.get('team2')
    team1 = t1 if isinstance(t1, str) else (t1 or {}).get('name', '?')
    team2 = t2 if isinstance(t2, str) else (t2 or {}).get('name', '?')

    title = f"{team1} vs {team2}"
    desc_parts = []
    ev = m.get('event') or m.get('match_event')
    if ev:
        desc_parts.append(f"**Event:** {ev}")
    if m.get('region'):
        desc_parts.append(f"**Region:** {m['region']}")
    desc = " · ".join(desc_parts) if desc_parts else " "
    url = m.get('url') or m.get('match_page')
    if url:
        desc += f"\n[Match page]({url})"

    color = 0x5865F2 if live else 0x2ecc71
    emb = discord.Embed(title=title, description=desc, color=color)

    if live and m.get('score'):
        emb.add_field(name="Score", value=f"`{m['score']}`", inline=False)
    else:
        tu = m.get('time_unix')
        if not tu:
            tu = parse_unix(m.get('unix_timestamp'))
        if tu:
            emb.add_field(name="Starts In", value=f"{fmt_delta(int(tu) - int(time.time()))}", inline=True)

    emb.set_footer(text="Data via vlrggapi (unofficial)")
    return emb

def safe_normalize(m, *, live_flag: bool | None = None) -> dict | None:
    """Normalize only if m is a dict; if normalize_match fails, coerce minimal fields."""
    if not isinstance(m, dict):
        log.info("Skipping non-dict placeholder: %r", m)
        return None
    try:
        nm = normalize_match(m)
        if live_flag is not None:
            nm["live"] = live_flag
        return nm
    except Exception as e:
        log.warning("normalize_match failed: %s on %r", e, m)
        nm = {}
        nm["team1"] = m.get("team1") or m.get("team_1") or "?"
        nm["team2"] = m.get("team2") or m.get("team_2") or "?"
        nm["event"] = m.get("event") or m.get("match_event") or ""
        nm["region"] = m.get("region") or ""
        nm["url"] = m.get("url") or m.get("match_page") or ""
        s1, s2 = m.get("score1"), m.get("score2")
        if s1 is not None and s2 is not None:
            nm["score"] = f"{s1}-{s2}"
        tu = m.get("time_unix")
        if not tu:
            tu = parse_unix(m.get("unix_timestamp"))
        if tu:
            nm["time_unix"] = tu
        if live_flag is not None:
            nm["live"] = live_flag
        return nm

async def quick_ack(interaction: discord.Interaction, content: str, *, ephemeral: bool=True) -> tuple[discord.Message, bool]:
    """
    Try to respond to the interaction immediately.
    If the interaction is already expired (10062), fall back to a normal channel message.
    Returns (message, is_interaction_message).
    """
    try:
        await interaction.response.send_message(content, ephemeral=ephemeral)
        msg = await interaction.original_response()
        return msg, True
    except discord.NotFound:
        # Interaction token expired; fall back to channel message
        mention = interaction.user.mention if interaction.user else ""
        msg = await interaction.channel.send(f"{mention} {content}")
        return msg, False
    except Exception as e:
        log.warning("quick_ack failed with %s; falling back to channel.send", e)
        mention = interaction.user.mention if interaction.user else ""
        msg = await interaction.channel.send(f"{mention} {content}")
        return msg, False

async def safe_edit(msg: discord.Message, *, content: str | None=None, embeds: list[discord.Embed] | None=None):
    """Edit a previously sent message; if edit fails, try sending a fresh one."""
    try:
        await msg.edit(content=content if content is not None else discord.utils.MISSING,
                       embeds=embeds if embeds is not None else discord.utils.MISSING,
                       attachments=[])
    except Exception as e:
        log.warning("msg.edit failed: %s; sending new message", e)
        if content and embeds:
            await msg.channel.send(content, embeds=embeds)
        elif content:
            await msg.channel.send(content)
        elif embeds:
            await msg.channel.send(embeds=embeds)

class Tracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._announced_ids: set[str] = set()
        self.session: aiohttp.ClientSession | None = None
        self.announce_loop.start()

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ---------- Shared helper for upcoming ----------
    async def _send_upcoming(self, interaction: discord.Interaction, count: int, filter_kw: str | None):
        msg, _ = await quick_ack(interaction, "Searching upcoming…", ephemeral=True)
        count = max(1, min(count, 10))

        raw = await get_upcoming(self.session) or []
        norm = [n for m in raw if (n := safe_normalize(m))]

        if filter_kw:
            filtered = filter_matches(norm, filter_kw)
            if filtered and not isinstance(filtered[0], dict):
                idmap = {str(m.get("match_id")): m for m in norm if isinstance(m, dict) and m.get("match_id")}
                norm = [idmap.get(str(mid)) for mid in filtered if idmap.get(str(mid))]
            else:
                norm = filtered

        norm = [m for m in norm if isinstance(m, dict)]
        norm.sort(key=lambda x: (x.get("time_unix") or 0))
        norm = norm[:count]

        if not norm:
            await safe_edit(msg, content="No upcoming matches found with that filter.", embeds=[])
            return

        await safe_edit(
            msg,
            content=f"Showing {len(norm)} upcoming:",
            embeds=[make_embed_from_match(m) for m in norm]
        )

    # -------------------- Commands --------------------

    @app_commands.command(name="next", description="Show upcoming pro matches")
    @app_commands.describe(count="How many to show (1-10)", filter="Filter by team/event/region keyword")
    async def next_matches(self, interaction: discord.Interaction, count: int = 5, filter: str | None = None):
        try:
            await self._send_upcoming(interaction, count, filter)
        except Exception as e:
            log.exception("Error in /next")
            try:
                msg, _ = await quick_ack(interaction, "Error occurred while fetching upcoming.", ephemeral=True)
                await safe_edit(msg, content=f"Error: `{e}`", embeds=[])
            except Exception:
                pass

    @app_commands.command(name="upcoming", description="Show upcoming pro matches (alias of /next)")
    @app_commands.describe(count="How many to show (1-10)", filter="Filter by team/event/region keyword")
    async def upcoming_matches(self, interaction: discord.Interaction, count: int = 5, filter: str | None = None):
        try:
            await self._send_upcoming(interaction, count, filter)
        except Exception as e:
            log.exception("Error in /upcoming")
            try:
                msg, _ = await quick_ack(interaction, "Error occurred while fetching upcoming.", ephemeral=True)
                await safe_edit(msg, content=f"Error: `{e}`", embeds=[])
            except Exception:
                pass

    @app_commands.command(name="live", description="Show live matches and current scores")
    async def live_matches(self, interaction: discord.Interaction):
        msg, _ = await quick_ack(interaction, "Checking live matches…", ephemeral=True)

        raw = await get_live(self.session) or []
        norm = [n for m in raw if (n := safe_normalize(m, live_flag=True))]
        if not norm:
            await safe_edit(msg, content="No live matches right now.", embeds=[])
            return

        await safe_edit(
            msg,
            content=f"Live matches ({min(len(norm),10)} shown):",
            embeds=[make_embed_from_match(m, live=True) for m in norm[:10]]
        )

    @app_commands.command(name="find", description="Search upcoming + live matches")
    @app_commands.describe(query="Team, event, or region keyword (use 'all' or '*' to list upcoming)")
    async def find_matches(self, interaction: discord.Interaction, query: str):
        try:
            msg, _ = await quick_ack(interaction, f"Searching for `{query}`…", ephemeral=True)

            async def fetch_data():
                live_raw = await get_live(self.session) or []
                up_raw = await get_upcoming(self.session) or []
                return live_raw, up_raw
            try:
                live_raw, up_raw = await asyncio.wait_for(fetch_data(), timeout=12)
            except asyncio.TimeoutError:
                await safe_edit(msg, content="⚠️ Timed out fetching data from VLR. Try again later.", embeds=[])
                return

            live_norm = [n for m in live_raw if (n := safe_normalize(m, live_flag=True))]
            up_norm   = [n for m in up_raw  if (n := safe_normalize(m, live_flag=False))]
            all_matches = live_norm + up_norm

            if query.strip().lower() in {"all", "*"}:
                upcoming_sorted = sorted([m for m in up_norm if isinstance(m, dict)],
                                         key=lambda x: (x.get("time_unix") or 0))[:10]
                if not upcoming_sorted:
                    await safe_edit(msg, content="No upcoming matches found.", embeds=[])
                    return
                await safe_edit(
                    msg,
                    content=f"Showing next {len(upcoming_sorted)} upcoming:",
                    embeds=[make_embed_from_match(m) for m in upcoming_sorted]
                )
                return

            hits = filter_matches(all_matches, query)
            if hits and not isinstance(hits[0], dict):
                id_to_match = {str(m.get("match_id")): m for m in all_matches if isinstance(m, dict) and m.get("match_id")}
                hits = [id_to_match.get(str(h)) for h in hits if id_to_match.get(str(h))]

            if not hits:
                q = query.lower()
                def match_simple(m: dict) -> bool:
                    return any(((m.get(k) or "").lower().find(q) != -1) for k in ("team1", "team2", "event", "region"))
                hits = [m for m in all_matches if isinstance(m, dict) and match_simple(m)]

            hits = [m for m in hits if isinstance(m, dict)]
            hits.sort(key=lambda x: (x.get("time_unix") or 0))
            hits = hits[:10]

            if not hits:
                await safe_edit(msg, content=f"No matches found for `{query}`.", embeds=[])
                return

            await safe_edit(
                msg,
                content=f"Results for `{query}` ({len(hits)} found):",
                embeds=[make_embed_from_match(m, live=m.get("live", False)) for m in hits]
            )

        except Exception as e:
            log.exception("Error in /find")
            try:
                msg, _ = await quick_ack(interaction, "Error occurred while searching.", ephemeral=True)
                await safe_edit(msg, content=f"Error: `{e}`", embeds=[])
            except Exception:
                pass

    @app_commands.command(name="ping", description="Quick health check")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message("pong")

    @tasks.loop(minutes=10)
    async def announce_loop(self):
        channel_id = os.getenv("ALERT_CHANNEL_ID")
        if not channel_id or not self.session:
            return
        ch = self.bot.get_channel(int(channel_id))
        if not ch:
            return
        try:
            raw = await get_upcoming(self.session) or []
            now = int(time.time())
            for m in raw:
                nm = safe_normalize(m)
                if not nm or not nm.get('time_unix'):
                    continue
                seconds = int(nm['time_unix']) - now
                if 0 <= seconds <= ALERT_LEAD_MINUTES * 60:
                    mid = nm.get('match_id')
                    if mid and mid in self._announced_ids:
                        continue
                    await ch.send(embed=make_embed_from_match(nm))
                    if mid:
                        self._announced_ids.add(mid)
        except Exception:
            pass

    @announce_loop.before_loop
    async def _before(self):
        await self.bot.wait_for("ready")

async def setup(bot: commands.Bot):
    await bot.add_cog(Tracker(bot))
