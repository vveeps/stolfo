from typing import Union

from discord import Message
from discord.embeds import Embed, EmptyEmbed, _EmptyEmbed
from discord.ext import commands

from player import QueuePlayer


class Context(commands.Context):
    color = 0xFEBABC
    message: Message
    voice_client: QueuePlayer

    def embed(
        self,
        title: str,
        description: Union[str, _EmptyEmbed] = EmptyEmbed,
        url: Union[str, _EmptyEmbed] = EmptyEmbed,
        thumbnail_url: Union[str, _EmptyEmbed] = EmptyEmbed,
        footer_text: Union[str, _EmptyEmbed] = EmptyEmbed,
        footer_icon_url: Union[str, _EmptyEmbed] = EmptyEmbed,
    ):
        ret = Embed(
            description=description,
            color=self.color,
            timestamp=self.message.created_at
        )
        ret.set_author(name=title, icon_url=self.author.display_avatar.url, url=url)
        ret.set_footer(text=footer_text, icon_url=footer_icon_url)
        ret.set_thumbnail(url=thumbnail_url)

        return ret
