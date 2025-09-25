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

# Pips #38 Hard 🔴
# 3:46

BADGE_PATTERN = re.compile(
    r"Pips #(\d+) (\w+) .+"
    "\n"
    r"((?:\d+:)?\d{1,2}:\d{2})" , re.MULTILINE
)

US_EASTERN = pytz.timezone("US/Eastern")

database = peewee.SqliteDatabase("pipbot.db")

def format_time(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}:{seconds:02d}"


class Solve(peewee.Model):
    """A puzzle solve time for a user."""

    user_id = peewee.BigIntegerField()
    guild_id = peewee.BigIntegerField()
    timestamp = peewee.TimestampField(default=datetime.datetime.now, utc=True)
    
    difficulty = peewee.CharField(max_length=32)
    version = peewee.IntegerField()

    seconds = peewee.SmallIntegerField()

    class Meta:
        database = database
        indexes = ((("user_id", "guild_id", "difficulty", "version"), True),)


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

    version: int
    difficulty: str
    entries: list[LeaderboardEntry] = field(default_factory=list)

    def render(self) -> str:
        """Render the leaderboard in a message."""

        if not self.entries:
            return f"No leaderboard for {self.version}"

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

        intents = disnake.Intents(messages=True, message_content=True, guilds = True)
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

    async def get_leaderboard(self, guild: disnake.Guild, version: int, difficulty: str) -> Leaderboard:
        """Construct the leaderboard for a guild and version."""

        solves = Solve.filter(
            guild_id=guild.id,
            difficulty=difficulty,
            version=version,
        ).order_by(
            Solve.seconds.asc(),
            Solve.timestamp.asc(),
        )
        
        await guild.get_or_fetch_members([solve.user_id for solve in solves])

        leaderboard = Leaderboard(version, difficulty)
        position = 1
        last_seconds = None
        for solve in solves:
            if last_seconds is not None and solve.seconds > last_seconds:
                position += 1
            member = guild.get_member(solve.user_id)
            leaderboard.entries.append(LeaderboardEntry(position, member, solve.seconds))
            last_seconds = solve.seconds

        return leaderboard

    async def on_pip_solve(
        self,
        message: disnake.Message,
        version: int,
        difficulty: str,
        seconds: int,
    ) -> None:
        """Save the solve and print the leaderboard."""

        solve, created = Solve.get_or_create(
            user_id=message.author.id,
            guild_id=message.guild.id,
            version=version,
            difficulty=difficulty,
            defaults=dict(seconds=seconds),
        )

        if not created:
            if solve.seconds != seconds:
                solve.seconds = seconds
                solve.save()

        leaderboard = await self.get_leaderboard(message.guild, version, difficulty)
        color: disnake.Color | None = None
        for entry in leaderboard.entries:
            if entry.position == 1:
                if entry.member.id == message.author.id:
                    color = disnake.Color.gold()

        embed = disnake.Embed(
            title=f"{message.author.display_name} solved the #{version} {difficulty} in {format_time(seconds)}",
            description=leaderboard.render(),
            color=color,
        )

        await message.channel.send(embed=embed)
        await message.delete()

    async def on_message(self, message: disnake.Message):
        """Listen for pip messages."""

        if message.author.bot:
            return

        elif match := BADGE_PATTERN.match(message.content):
            version, difficulty, time = match.groups()
            seconds = sum(60 ** i * x for i, x in enumerate(map(int, reversed(time.split(":")))))
            await self.on_pip_solve(
                message,
                version=int(version),
                difficulty = difficulty,
                seconds=seconds
            )
            return

        elif message.content.startswith("%pip "):
            parts = shlex.split(message.content[5:].strip())
            if len(parts) == 0:
                return

            elif parts[0] == "d" or parts[0] == "dump":
                display_names = {}
                fp = io.StringIO()
                writer = csv.writer(fp)
                writer.writerow(("display_name", "timestamp", "version", "difficulty", "seconds"))
                for solve in Solve.filter(Solve.guild_id == message.guild.id):
                    if (display_name := display_names.get(solve.user_id)) is None:
                        display_name = display_names[solve.user_id] = (await self.fetch_user(solve.user_id)).display_name
                    writer.writerow((display_name, solve.timestamp, solve.version, solve.difficulty, solve.seconds))
                bfp = io.BytesIO(fp.getvalue().encode())
                await message.reply(file=disnake.File(bfp, "solves.csv"))
                return
                
            else:
                await message.reply("Commands are `l/leaderboard`, `cl/crossword-leaderboard`, and `d/dump`")
                return


def main():
    config = configparser.ConfigParser()
    config.read("pipbot.conf")

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
