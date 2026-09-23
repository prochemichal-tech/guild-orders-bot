import unicodedata
import discord
from discord.ext import commands

# Kanál může být pojmenovaný s diakritikou i bez ní.
RULES_CHANNEL_NAMES = {"potvrzeni-pravidel"}
TRIAL_ROLE_NAMES = {"trial", "🌱 trial"}
ACCEPTED_TEXTS = {"souhlasim", "souhlasim s pravidly", "souhlas"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().strip().split())


def _is_rules_channel(channel: discord.abc.GuildChannel) -> bool:
    return _norm(getattr(channel, "name", "")) in RULES_CHANNEL_NAMES


def _find_trial_role(guild: discord.Guild):
    for role in guild.roles:
        if _norm(role.name) in {_norm(name) for name in TRIAL_ROLE_NAMES}:
            return role
    return None


class RulesVerification(commands.Cog):
    """Potvrzení pravidel -> role Trial -> smazání potvrzovací zprávy."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if not _is_rules_channel(message.channel):
            return

        # V potvrzovacím kanálu necháme jen správné potvrzení; ostatní zprávy smažeme.
        if _norm(message.content) not in ACCEPTED_TEXTS:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            try:
                await message.author.send(
                    "❌ Pro potvrzení pravidel napiš do kanálu **Souhlasím**."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        trial_role = _find_trial_role(message.guild)
        if trial_role is None:
            print(f"[RULES] Na serveru {message.guild.id} nebyla nalezena role Trial.")
            return

        member = message.author
        if not isinstance(member, discord.Member):
            return

        try:
            if trial_role not in member.roles:
                await member.add_roles(
                    trial_role,
                    reason="Hráč potvrdil pravidla guildy.",
                )

            # Potvrzení se po zpracování smaže, aby kanál zůstal čistý.
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            # Soukromé potvrzení; pokud má hráč DM vypnuté, nic se neděje.
            try:
                await member.send(
                    "✅ Pravidla byla potvrzena a byla ti přidělena role **Trial**. "
                    "Vítej v guildě **The Blood Tribute 4ever**! ⚔️"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

            print(f"[RULES] {member} potvrdil pravidla a dostal roli {trial_role.name}.")

        except discord.Forbidden:
            print(
                "[RULES] Bot nemá právo přidělit roli Trial. "
                "Zkontroluj Manage Roles a že role bota je nad Trial."
            )
        except discord.HTTPException as exc:
            print(f"[RULES] Discord chyba: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(RulesVerification(bot))
