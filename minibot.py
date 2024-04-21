from __future__ import annotations

import configparser
import datetime
import re
import shlex

import disnake
import disnake.abc
import peewee
import pytz
import requests

# https://www.nytimes.com/badges/games/mini.html?d=2024-04-20&t=78&c=514e32e822ac4633fccf33efc3da2698
BADGE_PATTERN = re.compile(
    r"https://www\.nytimes\.com/badges/games/mini\.html"
    r"\?d=(\d{4}-\d{2}-\d{2})"
    r"&t=(\d+)"
    r"&c=([a-fA-F0-9]+)"
)

US_EASTERN = pytz.timezone("US/Eastern")

database = peewee.SqliteDatabase("minibot.db")


def get_mini_crossword_image_base() -> bytes:
    return requests.get("https://www.nytimes.com/badges/games/mini.jpg").content


def get_mini_crossword_image(c: str, d: str, t: str) -> bytes:
    return requests.get(f"https://www.nytimes.com/badges/games/mini.jpg?c={c}&d={d}&t={t}").content


def format_time(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}:{seconds:02d}"


def today(timezone: pytz.tzinfo) -> datetime.date:
    """Get the current day in the given timezone."""

    now_timezone = timezone.normalize(datetime.datetime.now(timezone))
    return now_timezone.date()


class Solve(peewee.Model):
    """A puzzle solve time for a user."""

    user_id = peewee.BigIntegerField()
    guild_id = peewee.BigIntegerField()
    timestamp = peewee.TimestampField(default=datetime.datetime.now, utc=True)

    date = peewee.DateField()
    seconds = peewee.SmallIntegerField()
    checksum = peewee.CharField(max_length=32)

    class Meta:
        database = database
        indexes = ((("user_id", "guild_id", "date"), True),)


class Snowflake(disnake.abc.Snowflake):
    """Lightweight container for an ID."""

    def __init__(self, id_: int) -> None:
        self.id = id_


class Bot(disnake.Client):
    """Custom implementation of a command handler."""

    prefix: str
    debug_channel_id: int | None
    king_role_id: int | None

    def __init__(self, config: configparser.ConfigParser) -> None:
        """Propagate config sections to plugins."""

        intents = disnake.Intents(messages=True, message_content=True)
        super().__init__(intents=intents)

        self.token = config.get("discord", "token")
        debug_channel_id = config.get("discord", "debug_channel_id", fallback=None)
        self.debug_channel_id = int(debug_channel_id) if debug_channel_id is not None else None
        king_role_id = config.get("discord", "king_role_id", fallback=None)
        self.king_role_id = int(king_role_id) if king_role_id is not None else None

    def run(self, *args: object, **kwargs: object) -> None:
        """Pass token if it's been configured."""

        token = {"token": self.token} if self.token is not None else {}
        return super().run(*args, **kwargs, **token)

    async def get_leaderboard(self, guild: disnake.Guild, date: datetime.date) -> str:
        """Construct the leaderboard for a guild and date."""

        solves = Solve.filter(guild_id=guild.id, date=date).order_by(Solve.seconds.asc(), Solve.timestamp.asc())
        members = await guild.get_or_fetch_members([solve.user_id for solve in solves])

        leaderboard = []
        position = 1
        last_seconds = None
        for solve, member in zip(solves, members):
            tag = " :crown:"
            if last_seconds is not None and solve.seconds > last_seconds:
                position += 1
                tag = ""
            leaderboard.append(f"{position}. {member.display_name} ({format_time(solve.seconds)}){tag}")
            last_seconds = solve.seconds

        if leaderboard:
            return "\n".join(leaderboard)

        return f"No leaderboard for {date.year:04d}-{date.month:02d}-{date.day:02d}"

    async def on_message(self, message: disnake.Message):
        """Listen for crossword messages."""

        if match := BADGE_PATTERN.match(message.content):
            d, t, c = match.groups()
            if get_mini_crossword_image(c, d, t) == get_mini_crossword_image_base():
                await message.add_reaction("\N{NO ENTRY SIGN}")
                return

            seconds = int(t)
            date = datetime.datetime.strptime(d, "%Y-%m-%d").date()
            solve, created = Solve.get_or_create(
                user_id=message.author.id,
                guild_id=message.guild.id,
                date=date,
                defaults=dict(seconds=seconds, checksum=c),
            )

            if not created:
                if solve.seconds != seconds:
                    solve.seconds = seconds
                    solve.checksum = c
                    solve.save()

            embed = disnake.Embed(
                title=f"{message.author.display_name} solved the {d} mini in {format_time(seconds)}",
                description=await self.get_leaderboard(message.guild, date),
                # color=disnake.Colour.brand_green() if new_elo >= player.elo else disnake.Colour.brand_red(),
            )
            embed.set_thumbnail(url=f"https://www.nytimes.com/badges/games/mini.jpg?c={c}&d={d}&t={t}")
            embed.set_footer(text="This bot only detects messages that start with a solve link")

            await message.channel.send(embed=embed)
            await message.delete()

            return

        elif message.content.startswith("%nyt "):
            parts = shlex.split(message.content[5:].strip())
            if len(parts) == 0:
                return

            if parts[0] == "l" or parts[0] == "leaderboard":
                if len(parts) >= 2:
                    try:
                        date = datetime.datetime.strptime(parts[1], "%Y-%m-%d")
                    except ValueError:
                        await message.reply()
                        return
                else:
                    date = today(US_EASTERN)

                leaderboard = await self.get_leaderboard(message.guild, date)
                await message.channel.send(leaderboard)


def main():
    config = configparser.ConfigParser()
    config.read("minibot.conf")

    database.connect()
    database.create_tables([Solve], safe=True)

    bot = Bot(config)

    # Print an add link based on configuration
    client_id = config["discord"]["client_id"]
    print(
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        "&permissions=2415930432"
        "&scope=bot"
    )

    bot.run()


if __name__ == "__main__":
    main()
