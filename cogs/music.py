import asyncio
from io import StringIO
from traceback import format_exception
from typing import Type, Union

from async_timeout import timeout
from discord import Color, File, Member, VoiceState
from discord.embeds import _EmptyEmbed, EmptyEmbed
from discord.ext import commands
from discord.ext.commands import Cog, CommandError, CommandInvokeError
from pomice import Playlist, Track, TrackEndEvent, TrackStartEvent
from wavelink import WaitQueue

from bot import Bot
from config import LOG_CHANNEL
from context import Context
from player import QueuePlayer as Player


def format_time(milliseconds: int) -> str:
    hours, rem = divmod(milliseconds // 1000, 3600)
    minutes, seconds = divmod(rem, 60)

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class UserError(CommandError):
    def __init__(self, message: str):
        self.message = message


class Music(Cog):
    def __init__(self, bot: Bot):
        self.bot = bot

    async def cog_before_invoke(self, ctx: Context):
        if (is_guild := ctx.guild is not None):
            await self.ensure_voice(ctx)
        return is_guild

    async def cog_command_error(self, ctx: Context, error: Type[CommandError]):
        if isinstance(error, UserError):
            await ctx.send(embed=ctx.embed(error.message))
        elif isinstance(error, CommandInvokeError):
            error = error.original
            embed = ctx.embed(f"{error.__class__.__name__}: {error}")
            embed.color = Color(0xFF0E0E)
            await ctx.send(embed=embed)

            log = self.bot.get_channel(LOG_CHANNEL)
            full_traceback = "".join(
                format_exception(type(error), error, error.__traceback__, chain=True)
            )

            embed = ctx.embed(
                "Command exception caught!",
                f"```python\n{full_traceback}\n```" if len(full_traceback) <= 4000 else EmptyEmbed
            )
            embed.add_field(name="Message", value=f"`{ctx.message.content}`")
            embed.add_field(name="Guild", value=f"{ctx.guild.name} ({ctx.guild.id})")

            if len(full_traceback) > 4000:
                file = File(StringIO(full_traceback), "traceback.txt")
                return await log.send(embed=embed, file=file)

            await log.send(embed=embed)

    @Cog.listener()
    async def on_voice_state_update(self, member: Member, before: VoiceState, after: VoiceState):
        if member.id == self.bot.user.id \
           and not after.channel \
           and member.guild.voice_client is not None:
            player: Player = member.guild.voice_client

            player.queue.clear()
            await player.stop()
            await player.disconnect(force=True)

    def get_embed_thumbnail(self, track: Track) -> Union[str, _EmptyEmbed]:
        if thumbnail := track.info.get("thumbnail"):
            return thumbnail
        else:
            return EmptyEmbed

    def format_queue(self, queue: WaitQueue) -> str:
        items = []
        for i, track in enumerate(queue):
            items.append(
                f"**{i + 1}: [{track.title}]({track.uri}) **"
                f"[{'stream' if track.is_stream else format_time(track.length)}] "
                f"({track.ctx.author.mention})"
            )

        return items

    @Cog.listener()
    async def on_pomice_track_start(self, event: TrackStartEvent):
        track = event.player.current
        ctx = track.ctx

        if track.is_stream:
            length = "🔴 Live"
        else:
            length = format_time(track.length)

        embed = ctx.embed(
            f"Now playing: {track.title}",
            url=track.uri,
            thumbnail_url=self.get_embed_thumbnail(track)
        )
        embed.add_field(name="Duration", value=length)
        embed.add_field(name="Requested by", value=ctx.author.mention)

        await ctx.send(embed=embed, delete_after=track.length / 1000)

    @Cog.listener()
    async def on_pomice_track_end(self, event: TrackEndEvent):
        player = event.player
        try:
            async with timeout(300):
                await player.play(await player.queue.get_wait())
        except asyncio.TimeoutError:
            await player.disconnect(force=True)

    async def ensure_voice(self, ctx: Context):
        should_connect = ctx.command.name in ("play",)

        if not ctx.author.voice or not ctx.author.voice.channel:
            raise UserError("You're not connected to a voice channel!")

        if not ctx.voice_client:
            if not should_connect:
                raise UserError("I'm not connected to a voice channel!")

            permissions = ctx.author.voice.channel.permissions_for(ctx.me)

            if not permissions.connect:
                raise UserError(
                    "I'm missing permissions to connect to your voice channel!"
                )

            if not permissions.speak:
                raise UserError("I'm missing permissions to speak in your voice channel!")

            await ctx.author.voice.channel.connect(cls=Player)
        else:
            if int(ctx.voice_client.channel.id) != ctx.author.voice.channel.id:
                raise UserError("You need to be in my voice channel to use this!")

    @commands.command(aliases=["p"])
    @commands.max_concurrency(1, commands.BucketType.guild, wait=True)
    async def play(self, ctx: Context, *, query: str):
        """Queues one or multiple tracks. Can be used to resume the player if paused."""
        player = ctx.voice_client

        search = await self.bot.pomice.get_tracks(query, ctx)
        if isinstance(search, Playlist):
            tracks = search.tracks
            first_position = len(player.queue) + 1

            for track in tracks:
                player.queue.put(track)

            last_position = len(player.queue)

            embed = ctx.embed(
                f"Queued {search.name} - {search.track_count} tracks",
                url=search.url if search.url else EmptyEmbed,
                thumbnail_url=search.thumbnail
            )

            if any(t.is_stream for t in tracks):
                embed.add_field(name="# of tracks", value=len(tracks))
            else:
                embed.add_field(
                    name="Duration",
                    value=format_time(sum(t.length for t in tracks))
                )

            embed.add_field(name="Position in queue", value=f"{first_position}-{last_position}")

            await ctx.send(embed=embed)
        else:
            track = search[0]
            player.queue.put(track)

            if player.is_playing():
                if track.is_stream:
                    length = "🔴 Live"
                else:
                    length = format_time(track.length)

                embed = ctx.embed(
                    f"Queued {track.name}",
                    url=track.url,
                    thumbnail_url=self.get_embed_thumbnail(track)
                )
                embed.add_field(name="Duration", value=length)
                embed.add_field(name="Position in queue", value=len(player.queue))

                await ctx.send(embed=embed)

        if not player.is_playing():
            await player.play(player.queue.get())

    @commands.command(aliases=["dc", "stop", "leave"])
    async def disconnect(self, ctx: Context):
        """Disconnects the player from its voice channel."""
        player = ctx.voice_client
        channel_name = player.channel.name

        player.queue.clear()
        await player.stop()
        await player.disconnect(force=True)

        await ctx.send(embed=ctx.embed(f"Disconnected from {channel_name}!"))

    @commands.command(aliases=["s"])
    async def skip(self, ctx: Context):
        """Skips the currently playing track."""
        player = ctx.voice_client

        if not player.is_playing:
            return await ctx.send(embed=ctx.embed("Nothing is playing!"))

        await ctx.send(embed=ctx.embed(f"Skipped {player.source.title}", url=player.source.uri))
        await player.stop()

    @commands.command(aliases=["q"])
    async def queue(self, ctx: Context):
        """Displays the player's queue."""
        player = ctx.voice_client

        if not player.queue:
            embed = ctx.embed("Queue is empty!")
            return await ctx.send(embed=embed)

        queue_items = self.format_queue(player.queue)

        current = player.current
        if current.is_stream:
            current_pos = "stream"
        else:
            current_pos = f"{format_time(player.position)}/{format_time(current.length)}"

        queue_items.insert(
            0,
            f"**▶ [{current.title}]({current.uri}) **"
            f"[{current_pos}] "
            f"({current.ctx.author.mention})"
        )

        q_length = f"{len(player.queue)} track{'' if len(player.queue) == 1 else 's'}"
        if any(t.is_stream for t in player.queue):
            q_duration = ""
        else:
            total = format_time(
                sum(t.length for t in player.queue) + (current.length - player.position)
            )
            q_duration = f" ({total})"

        await ctx.send(
            embed=ctx.embed(f"Queue - {q_length}{q_duration}", "\n".join(queue_items)[:4000])
        )

    @commands.command(aliases=["np", "current", "now", "song"])
    async def nowplaying(self, ctx: Context):
        """Shows info about the currently playing track."""
        player = ctx.voice_client
        track = player.current

        if track.is_stream:
            position = "🔴 Live"
        else:
            position = f"{format_time(player.position)}/{format_time(track.length)}"

        embed = ctx.embed(
            track.title,
            url=track.uri,
            thumbnail_url=self.get_embed_thumbnail(track)
        )
        embed.add_field(name="Position", value=position)
        embed.add_field(name="Uploader", value=track.author)
        embed.add_field(name="Requested by", value=track.ctx.author.mention)

        await ctx.send(embed=embed)

    @commands.command(aliases=["nuke"])
    async def clear(self, ctx: Context):
        """Clears the player's queue."""
        player = ctx.voice_client

        if not player.queue:
            return await ctx.send(embed=ctx.embed("There's nothing to clear!"))

        amount = len(player.queue)
        player.queue.clear()
        await ctx.send(embed=ctx.embed(f"Cleared {amount} song{'' if amount == 1 else 's'}!"))

    @commands.command(aliases=["r"])
    async def remove(self, ctx: Context, index: int):
        """Removes a song from the player's queue."""
        player = ctx.voice_client

        if not player.queue:
            return await ctx.send(embed=ctx.embed("The queue is empty!"))

        if index < 1 or index > len(player.queue):
            if len(player.queue) == 1:
                desc = f"Did you mean `{ctx.prefix}{ctx.invoked_with} 1`?"
            else:
                desc = f"Valid track numbers are `1-{len(player.queue)}`."

            return await ctx.send(embed=ctx.embed(f"Invalid track number!", desc))

        track = player.queue[index - 1]
        del player.queue[index - 1]

        embed = ctx.embed(f"Removed {track.title}", url=track.uri)
        embed.add_field(name="Requested by", value=track.ctx.author.mention)
        await ctx.send(embed=embed)

    @commands.command()
    async def move(self, ctx: Context, _from: int, _to: int):
        """Moves a song from the first given position to the second one."""
        player: Player = ctx.voice_client

        try:  # just silently returning on out of range input for now
            player.queue[_from - 1]
            player.queue[_to - 1]
        except IndexError:
            return

        track = player.queue[_from - 1]
        del player.queue[_from - 1]
        player.queue.put_at_index(_to - 1, track)

        await ctx.send(embed=ctx.embed(f"Moved {track.title} to position {_to}"))


def setup(bot: Bot):
    bot.add_cog(Music(bot))
