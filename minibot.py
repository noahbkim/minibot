from __future__ import annotations

import configparser
import csv
import datetime
import io
import re
import shlex
from dataclasses import dataclass, field

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

# I solved the 4/21/2024 New York Times Mini Crossword in 0:52! https://www.nytimes.com/crosswords/game/mini
NEW_PATTERN = re.compile(
    r"I solved the (\d{1,2}/\d{1,2}/\d{4}) New York Times Mini Crossword in ((?:\d+:)?\d{1,2}:\d{2})!"
    r" https://www\.nytimes\.com/crosswords/game/mini"
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


def format_date(date: datetime.date) -> str:
    return f"{date.year:04d}-{date.month:02d}-{date.day:02d}"


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


@dataclass
class LeaderboardEntry:
    """A user on the leaderboard."""

    position: int
    member: disnake.Member
    seconds: int


@dataclass
class Leaderboard:
    """Structured leaderboard data."""

    date: datetime.date
    entries: list[LeaderboardEntry] = field(default_factory=list)

    def render(self) -> str:
        """Render the leaderboard in a message."""

        if not self.entries:
            return f"No leaderboard for {format_date(self.date)}"

        rows = []
        for entry in self.entries:
            end = " :crown:" if entry.position == 1 else ""
            rows.append(f"{entry.position}. {entry.member.display_name} ({format_time(entry.seconds)}){end}")

        return "\n".join(rows)


class Bot(disnake.Client):
    """Custom implementation of a command handler."""

    prefix: str
    debug_channel_id: int | None
    king_role_id: int | None
    king_role: disnake.Role | None

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

    async def get_leaderboard(self, guild: disnake.Guild, date: datetime.date) -> Leaderboard:
        """Construct the leaderboard for a guild and date."""

        solves = Solve.filter(guild_id=guild.id, date=date).order_by(Solve.seconds.asc(), Solve.timestamp.asc())
        await guild.get_or_fetch_members([solve.user_id for solve in solves])

        leaderboard = Leaderboard(date)
        position = 1
        last_seconds = None
        for solve in solves:
            if last_seconds is not None and solve.seconds > last_seconds:
                position += 1
            member = guild.get_member(solve.user_id)
            leaderboard.entries.append(LeaderboardEntry(position, member, solve.seconds))
            last_seconds = solve.seconds

        return leaderboard

    async def on_mini_crossword_solve(
        self,
        message: disnake.Message,
        date: datetime.date,
        seconds: int,
        checksum: str = "",
    ) -> None:
        """Save the solve and print the leaderboard."""

        solve, created = Solve.get_or_create(
            user_id=message.author.id,
            guild_id=message.guild.id,
            date=date,
            defaults=dict(seconds=seconds, checksum=checksum),
        )

        if not created:
            if solve.seconds != seconds:
                solve.seconds = seconds
                solve.checksum = checksum
                solve.save()

        leaderboard = await self.get_leaderboard(message.guild, date)
        color: disnake.Color | None = None
        for entry in leaderboard.entries:
            if entry.position == 1:
                if entry.member.id == message.author.id:
                    color = disnake.Color.gold()

        embed = disnake.Embed(
            title=f"{message.author.display_name} solved the {format_date(date)} mini in {format_time(seconds)}",
            description=leaderboard.render(),
            color=color,
        )
        embed.set_thumbnail(
            url=f"https://www.nytimes.com/badges/games/mini.jpg?c={checksum}&d={format_date(date)}&t={seconds}"
        )

        await message.channel.send(embed=embed)
        await message.delete()

    async def on_message(self, message: disnake.Message):
        """Listen for crossword messages."""

        if message.author.bot:
            return

        if match := BADGE_PATTERN.match(message.content):
            d, t, c = match.groups()
            if get_mini_crossword_image(c, d, t) == get_mini_crossword_image_base():
                await message.add_reaction("\N{NO ENTRY SIGN}")
                return

            await self.on_mini_crossword_solve(
                message,
                date=datetime.datetime.strptime(d, "%Y-%m-%d").date(),
                seconds=int(t),
                checksum=c,
            )
            return

        elif match := NEW_PATTERN.match(message.content):
            d, t = match.groups()
            time = sum(60 ** i * x for i, x in enumerate(map(int, reversed(t.split(":")))))
            await self.on_mini_crossword_solve(
                message,
                date=datetime.datetime.strptime(d, "%m/%d/%Y").date(),
                seconds=time,
            )
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
                await message.channel.send(leaderboard.render())

            elif parts[0] == "d" or parts[0] == "dump":
                display_names = {}
                fp = io.StringIO()
                writer = csv.writer(fp)
                writer.writerow(("display_name", "timestamp", "date", "seconds"))
                for solve in Solve.filter(Solve.guild_id == message.guild.id):
                    if (display_name := display_names.get(solve.user_id)) is None:
                        display_name = display_names[solve.user_id] = (await self.fetch_user(solve.user_id)).display_name
                    writer.writerow((display_name, solve.timestamp, solve.date, solve.seconds))
                bfp = io.BytesIO(fp.getvalue().encode())
                await message.reply(file=disnake.File(bfp, "solves.csv"))


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
