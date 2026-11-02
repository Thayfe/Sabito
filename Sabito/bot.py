import asyncio
import hmac
import json
import os
import random
import secrets
import sqlite3
import time
from typing import Optional
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import catalog
import economy
import progress
import titles
from logic import compute_payouts, odds

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
RIOT_KEY = os.environ["RIOT_API_KEY"]
PLATFORM = os.getenv("RIOT_PLATFORM", "br1")      # br1, na1, euw1...
REGION = os.getenv("RIOT_REGION", "americas")     # americas, europe, asia, sea

START_BALANCE = 1000
DAILY_AMOUNT = 2_500
BET_WINDOW = 4 * 60
VERIFY_TTL = 10 * 60
RESET_TTL = 24 * 3600
RESULT_TIMEOUT = 3 * 3600
HOUSE_ODD = float(os.getenv("HOUSE_ODD", "2.00"))  # odd fixa da casa (ranqueadas) quando só um lado tem apostas
HOUSE_ODD_UNRANKED = float(os.getenv("HOUSE_ODD_UNRANKED", "1.50"))  # idem, para Normal e demais filas não ranqueadas
POLL_LIVE = 45
POLL_RESULT = 60

QUEUES = {420: "Ranqueada Solo/Duo", 440: "Ranqueada Flex", 400: "Normal Draft",
          430: "Normal Blind", 480: "Swiftplay", 490: "Quickplay", 700: "Clash"}
# Só estas filas (Summoner's Rift 5v5) geram aposta. Para liberar outras, adicione o ID aqui.
ALLOWED_QUEUES = {420, 440, 400, 430, 480, 490, 700}
# Partidas personalizadas aparecem como TESTE: anúncio e resultado funcionam, apostas ficam desativadas.
ANNOUNCE_CUSTOM_TESTS = True
# DEBUG_POLL=1 no .env imprime no console por que cada jogador vinculado foi (ou não) anunciado.
DEBUG_POLL = os.getenv("DEBUG_POLL", "0") == "1"

NO_PING = discord.AllowedMentions.none()


def dbg(*args):
    if DEBUG_POLL:
        print("[poll_live]", *args)


def queue_name(qid):
    return QUEUES.get(qid, "Fila " + str(qid))


def house_odd_for(g):
    return HOUSE_ODD if (g["queue"] or "").startswith("Ranqueada") else HOUSE_ODD_UNRANKED


def fmt_num(n):
    return "{:,}".format(int(n)).replace(",", ".")


def fmt_signed(n):
    return ("+" if n >= 0 else "-") + fmt_num(abs(n))


def deny(inter, text):
    return inter.response.send_message(text, ephemeral=True)


def guarded(label, fn, default=None):
    try:
        return fn()
    except Exception as e:
        print(label, "erro:", repr(e))
        return default


db = sqlite3.connect("bot.db")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS config(guild_id INTEGER PRIMARY KEY, channel_id INTEGER);
CREATE TABLE IF NOT EXISTS users(guild_id INTEGER, discord_id INTEGER, balance INTEGER, last_daily REAL DEFAULT 0,
    PRIMARY KEY(guild_id, discord_id));
CREATE TABLE IF NOT EXISTS accounts(guild_id INTEGER, discord_id INTEGER, puuid TEXT, riot_id TEXT,
    PRIMARY KEY(guild_id, discord_id), UNIQUE(guild_id, puuid));
CREATE TABLE IF NOT EXISTS pending(guild_id INTEGER, discord_id INTEGER, puuid TEXT, riot_id TEXT,
    icon INTEGER, expires REAL, PRIMARY KEY(guild_id, discord_id));
CREATE TABLE IF NOT EXISTS games(gid INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, game_id INTEGER,
    discord_id INTEGER, puuid TEXT, riot_id TEXT, champion TEXT, queue TEXT, channel_id INTEGER,
    message_id INTEGER, status TEXT, close_at REAL, result TEXT, test INTEGER DEFAULT 0,
    players_json TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS bets(gid INTEGER, discord_id INTEGER, side TEXT, amount INTEGER,
    PRIMARY KEY(gid, discord_id));
CREATE TABLE IF NOT EXISTS resets(guild_id INTEGER PRIMARY KEY, channel_id INTEGER, message_id INTEGER,
    expires REAL);
CREATE TABLE IF NOT EXISTS reset_votes(guild_id INTEGER, discord_id INTEGER, PRIMARY KEY(guild_id, discord_id));
CREATE INDEX IF NOT EXISTS idx_games_status ON games(status);
CREATE INDEX IF NOT EXISTS idx_games_guild_game ON games(guild_id, game_id);
""")
for ddl in ("ALTER TABLE games ADD COLUMN test INTEGER DEFAULT 0",
            "ALTER TABLE config ADD COLUMN role_id INTEGER",
            "ALTER TABLE games ADD COLUMN players_json TEXT DEFAULT '[]'"):
    try:
        db.execute(ddl)
    except sqlite3.OperationalError:
        pass
db.commit()
progress.init(db)
economy.init(db)
titles.init(db)
MIGRATION_SUMMARY = progress.migrate_titles(db)
if MIGRATION_SUMMARY:
    print("migrate_titles: {} jogador(es) reembolsado(s)".format(len(MIGRATION_SUMMARY)))


def get_game(gid):
    return db.execute("SELECT * FROM games WHERE gid=?", (gid,)).fetchone()


def get_cfg(guild_id):
    return db.execute("SELECT * FROM config WHERE guild_id=?", (guild_id,)).fetchone()


def balance(guild_id, user_id):
    row = db.execute("SELECT balance FROM users WHERE guild_id=? AND discord_id=?", (guild_id, user_id)).fetchone()
    if row is None:
        db.execute("INSERT INTO users(guild_id, discord_id, balance) VALUES(?,?,?)", (guild_id, user_id, START_BALANCE))
        db.commit()
        return START_BALANCE
    return row["balance"]


def add_balance(guild_id, user_id, delta, commit=True):
    db.execute("INSERT OR IGNORE INTO users(guild_id, discord_id, balance) VALUES(?,?,?)",
               (guild_id, user_id, START_BALANCE))
    db.execute("UPDATE users SET balance = balance + ? WHERE guild_id=? AND discord_id=?",
               (delta, guild_id, user_id))
    if commit:
        db.commit()


def active_games(guild_id):
    return db.execute("SELECT COUNT(*) c FROM games WHERE guild_id=? AND test=0 AND status IN ('open','closed')",
                      (guild_id,)).fetchone()["c"]


class RiotError(Exception):
    # só guarda o status: a repr do ClientResponseError vaza a chave nos headers
    def __init__(self, status):
        super().__init__("Riot API respondeu HTTP {}".format(status))
        self.status = status


class RiotAuthError(RiotError):
    # 401/403: chave ausente, inválida ou expirada (a de desenvolvimento expira a cada 24h)
    pass


_auth_warned_at = 0.0


def warn_riot_auth(e):
    global _auth_warned_at
    if time.time() - _auth_warned_at > 600:
        _auth_warned_at = time.time()
        print("⚠️ A Riot recusou a chave (HTTP {}). Gere uma nova em https://developer.riotgames.com, "
              "atualize RIOT_API_KEY e reinicie o bot. Chaves de desenvolvimento expiram a cada 24h."
              .format(e.status))


class Riot:
    def __init__(self):
        self.session = None

    async def get(self, host, path):
        url = "https://" + host + ".api.riotgames.com" + path
        for _ in range(3):
            async with self.session.get(url, headers={"X-Riot-Token": RIOT_KEY}) as r:
                if r.status == 404:
                    return None
                if r.status == 429:
                    await asyncio.sleep(int(r.headers.get("Retry-After", "2")))
                    continue
                if r.status in (401, 403):
                    raise RiotAuthError(r.status)
                if r.status >= 400:
                    raise RiotError(r.status)
                return await r.json()
        return None

    def puuid_by_riot_id(self, name, tag):
        return self.get(REGION, "/riot/account/v1/accounts/by-riot-id/" + quote(name) + "/" + quote(tag))

    async def profile_icon(self, puuid):
        data = await self.get(PLATFORM, "/lol/summoner/v4/summoners/by-puuid/" + puuid)
        return data["profileIconId"] if data else None

    def active_game(self, puuid):
        return self.get(PLATFORM, "/lol/spectator/v5/active-games/by-summoner/" + puuid)

    def league_entries(self, puuid):
        return self.get(PLATFORM, "/lol/league/v4/entries/by-puuid/" + puuid)

    def match(self, game_id):
        return self.get(REGION, "/lol/match/v5/matches/" + PLATFORM.upper() + "_" + str(game_id))


riot = Riot()
champions = {}
ddragon_version = "14.24.1"  # fallback; atualizado em load_champions


def icon_url(icon_id):
    return "https://ddragon.leagueoflegends.com/cdn/" + ddragon_version + "/img/profileicon/" + str(icon_id) + ".png"


async def load_champions():
    global ddragon_version
    try:
        async with riot.session.get("https://ddragon.leagueoflegends.com/api/versions.json") as r:
            ddragon_version = version = (await r.json())[0]
        async with riot.session.get(
                "https://ddragon.leagueoflegends.com/cdn/" + version + "/data/pt_BR/champion.json") as r:
            data = await r.json()
        for c in data["data"].values():
            champions[int(c["key"])] = c["name"]
    except Exception as e:
        print("Falha ao carregar campeões:", e)


TIER_PT = {"IRON": "Ferro", "BRONZE": "Bronze", "SILVER": "Prata", "GOLD": "Ouro", "PLATINUM": "Platina",
           "EMERALD": "Esmeralda", "DIAMOND": "Diamante", "MASTER": "Mestre", "GRANDMASTER": "Grão-Mestre",
           "CHALLENGER": "Desafiante"}
APEX = {"MASTER", "GRANDMASTER", "CHALLENGER"}


def rank_label(entries):
    by_queue = {e.get("queueType"): e for e in entries or [] if isinstance(e, dict)}
    entry = by_queue.get("RANKED_SOLO_5x5") or by_queue.get("RANKED_FLEX_SR")
    if not entry:
        return "Sem elo"
    tier = (entry.get("tier") or "").upper()
    tier_pt = TIER_PT.get(tier, tier.title() if tier else "Sem elo")
    lp = int(entry.get("leaguePoints") or 0)
    if tier in APEX:
        return f"{tier_pt} · {lp} LP"
    return f"{tier_pt} {entry.get('rank') or ''} · {lp} LP"


async def build_players_snapshot(data):
    players = []
    for p in data.get("participants", []):
        puuid, entries = p.get("puuid"), None
        if puuid:
            try:
                entries = await riot.league_entries(puuid)
            except Exception:
                entries = None
            await asyncio.sleep(0.08)
        players.append({
            "puuid": puuid,
            "riot_id": p.get("riotId") or p.get("summonerName") or "Jogador",
            "champion": champions.get(p.get("championId"), "Desconhecido"),
            "team_id": int(p.get("teamId", 0)),
            "slot": len(players),
            "rank": rank_label(entries),
        })
    return players


def load_players(g):
    try:
        players = json.loads(g["players_json"] or "[]")
        return players if isinstance(players, list) else []
    except Exception:
        return []


def forbidden_sides_for_user(g, user_id):
    players = load_players(g)
    if not players:
        return set()
    linked = {r["puuid"] for r in db.execute(
        "SELECT puuid FROM accounts WHERE guild_id=? AND discord_id=?", (g["guild_id"], user_id))}
    tracked = next((p for p in players if p.get("puuid") == g["puuid"]), None)
    if not linked or not tracked:
        return set()
    return {"lose" if p.get("team_id") == tracked.get("team_id") else "win"
            for p in players if p.get("puuid") in linked}


def pools(gid):
    out = {"win": (0, 0), "lose": (0, 0)}
    for r in db.execute("SELECT side, SUM(amount) s, COUNT(*) n FROM bets WHERE gid=? GROUP BY side", (gid,)):
        out[r["side"]] = (r["s"], r["n"])
    return out


def fmt_odd(o):
    return "{:.2f}x".format(o) if o else "—"


def effective_odds(p, house_odd=HOUSE_ODD):
    has_win, has_lose = p["win"][0] > 0, p["lose"][0] > 0
    if has_win and has_lose:
        return (*odds(p["win"][0], p["lose"][0]), False)
    return (house_odd, house_odd, True) if has_win or has_lose else (None, None, False)


def roster_field(players, team_id):
    team = sorted((p for p in players if p.get("team_id") == team_id), key=lambda p: p.get("slot", 999))
    if not team:
        return "Nenhum jogador identificado."
    return "\n".join("**{}** — `{}` · `{}`".format(p.get("riot_id", "Jogador"), p.get("champion", "Desconhecido"),
                                                 p.get("rank", "Sem elo")) for p in team)


def build_embed(g):
    p = pools(g["gid"])
    house_odd = house_odd_for(g)
    o_win, o_lose, by_house = effective_odds(p, house_odd)
    test, status = bool(g["test"]), g["status"]
    players = load_players(g)

    if status == "open":
        color = discord.Color.orange() if test else discord.Color.gold()
        title = "🧪 TESTE" if test else "🎮 PARTIDA"
        subtitle = ("Apostas desativadas neste teste." if test
                    else "Apostas encerram <t:" + str(int(g["close_at"])) + ":R>")
    elif status == "closed":
        color, title = discord.Color.blurple(), "🧪 TESTE" if test else "🔒 APOSTAS ENCERRADAS"
        subtitle = "Aguardando o resultado da partida."
    else:
        color, title = discord.Color.green(), "🧪 TESTE FINALIZADO" if test else "🏁 RESULTADO"
        subtitle = g["result"] or ""

    e = discord.Embed(title=f"{title} · {g['riot_id']}", color=color, description=f"`{g['queue']}`\n{subtitle}")

    if players:
        tid = next((x.get("team_id") for x in players if x.get("puuid") == g["puuid"]), None)
        if tid in (100, 200):
            own, enemy = ("🔵", "🔴") if tid == 100 else ("🔴", "🔵")
            teams = ((own + " SEU TIME", tid), (enemy + " TIME INIMIGO", 300 - tid))
        else:
            teams = (("SEU TIME", 100), ("TIME INIMIGO", 200))
        for name, team_id in teams:
            e.add_field(name=name, value=roster_field(players, team_id), inline=True)

    house = " · 🏦 Casa" if by_house else ""
    e.add_field(name="💰 APOSTAS", inline=False, value=(
        f"**✅ Vitória**  ·  {p['win'][0]} pts · {p['win'][1]} apostas · **{fmt_odd(o_win)}**{house}\n"
        f"**❌ Derrota**  ·  {p['lose'][0]} pts · {p['lose'][1]} apostas · **{fmt_odd(o_lose)}**{house}"))

    if status != "done" and not test:
        if p["win"][0] and p["lose"][0]:
            foot = "Dois lados: odd = pool total ÷ pool do lado."
        elif p["win"][0] or p["lose"][0]:
            foot = "Um lado: a casa cobre o outro em odd fixa de {:.2f}x.".format(house_odd)
        else:
            foot = "Sem apostas: a primeira aposta entra contra a odd fixa da casa ({:.2f}x).".format(house_odd)
        e.set_footer(text=foot)
    return e


FORBIDDEN_MSG = {
    frozenset({"lose"}): "Você tem uma conta vinculada nesta partida e não pode apostar na **Derrota** do seu time.",
    frozenset({"win"}): "Você tem uma conta vinculada no time adversário e não pode apostar na **Vitória** desse mercado.",
}
FORBIDDEN_BOTH = "Você não pode apostar contra o time em que uma das suas contas vinculadas está jogando."


def title_line(title, is_new):
    if not title or not is_new:
        return ""
    return "\n🏷️ **Poder desta aposta:** {} — {}".format(title_label(title.id), title.desc)


class AmountModal(discord.ui.Modal):
    def __init__(self, gid, side):
        super().__init__(title="Valor da aposta")
        self.gid, self.side = gid, side
        self.amount = discord.ui.TextInput(label="Quantos pontos?", placeholder="100, 1k, 200k, 1,5m ou tudo", max_length=15)
        self.add_item(self.amount)

    async def on_submit(self, inter: discord.Interaction):
        g, uid = get_game(self.gid), inter.user.id
        if g and g["test"]:
            return await deny(inter, "🧪 Esta é uma partida de **teste** (personalizada): as apostas estão desativadas.")
        if not g or g["status"] != "open" or time.time() > g["close_at"]:
            return await deny(inter, "As apostas já foram encerradas.")
        if g["shadow_by"] and g["shadow_by"] != uid and not titles.is_immune(db, g["guild_id"], uid):
            return await deny(inter, "🌑 <@{}> usou a **Sombra**: só ele(a) pode apostar nesta partida.".format(
                g["shadow_by"]))

        guild_id = g["guild_id"]
        forbidden = forbidden_sides_for_user(g, uid)
        existing = db.execute("SELECT side FROM bets WHERE gid=? AND discord_id=?", (self.gid, uid)).fetchone()

        rolled = False
        t_eff = titles.effective_title(db, guild_id, uid)
        if t_eff and t_eff.name == "O Incerto":
            if existing:
                self.side = existing["side"]
            else:
                allowed = [x for x in ("win", "lose") if x not in forbidden]
                if allowed:
                    self.side, rolled = random.choice(allowed), True

        if self.side in forbidden:
            return await deny(inter, FORBIDDEN_MSG.get(frozenset(forbidden), FORBIDDEN_BOTH))
        wallet_now = balance(guild_id, uid)
        amount = economy.parse_amount(self.amount.value, wallet_now)
        if amount is None or amount <= 0:
            return await deny(inter, "Valor inválido (ou saldo zerado). Use um número, como `1000`, `1k`, `200k`, "
                                     "`1,5m`, ou `tudo`.")
        if existing and existing["side"] != self.side:
            return await deny(inter, "Você já apostou no outro lado.")
        if wallet_now < amount:
            return await deny(inter, "Saldo insuficiente.")

        add_balance(guild_id, uid, -amount, commit=False)
        if existing:
            db.execute("UPDATE bets SET amount = amount + ? WHERE gid=? AND discord_id=?", (amount, self.gid, uid))
        else:
            db.execute("INSERT INTO bets(gid, discord_id, side, amount) VALUES(?,?,?,?)",
                       (self.gid, uid, self.side, amount))
        db.commit()

        o_win, o_lose, by_house = effective_odds(pools(self.gid), house_odd_for(g))
        my_odd = o_win if self.side == "win" else o_lose
        mine = db.execute("SELECT amount FROM bets WHERE gid=? AND discord_id=?", (self.gid, uid)).fetchone()["amount"]
        wallet, is_new = balance(guild_id, uid), existing is None
        unlocked = guarded("on_bet_placed", lambda: progress.on_bet_placed(db, guild_id, uid, mine, wallet, is_new), [])
        title = guarded("titles.on_place", lambda: titles.on_place(db, guild_id, self.gid, uid, is_new, wallet, mine))

        template = ("🏦 A casa está cobrindo o outro lado em odd fixa **{}** · retorno potencial: **{} pts**."
                    if by_house else
                    "Odd atual: **{}** · retorno estimado: **{} pts** (pode mudar enquanto os dois lados recebem apostas).")
        extra = template.format(fmt_odd(my_odd), int(mine * my_odd))
        await inter.response.edit_message(embed=build_embed(g))
        await inter.followup.send(
            "Aposta de **{}** em **{}** registrada (total seu: {}). Saldo: {}\n{}".format(
                amount, "Vitória" if self.side == "win" else "Derrota", mine, wallet,
                ("\n🎲 **O Incerto** sorteou o lado por você!" if rolled else "")
                + extra + title_line(title, is_new) + unlock_text(unlocked)),
            ephemeral=True)


class BetView(discord.ui.View):
    def __init__(self, gid):
        super().__init__(timeout=None)
        self.gid = gid
        for side, label, style in (("win", "Vitória", discord.ButtonStyle.success),
                                   ("lose", "Derrota", discord.ButtonStyle.danger)):
            btn = discord.ui.Button(label=label, style=style, custom_id="bet:" + side + ":" + str(gid))
            btn.callback = self.make_callback(side)
            self.add_item(btn)
        power = discord.ui.Button(label="Poder", emoji="✨", style=discord.ButtonStyle.secondary,
                                  custom_id="power:" + str(gid))
        power.callback = self.power_callback
        self.add_item(power)

    async def power_callback(self, inter: discord.Interaction):
        await use_title_power(inter, self.gid)

    def make_callback(self, side):
        async def callback(inter: discord.Interaction):
            await inter.response.send_modal(AmountModal(self.gid, side))
        return callback


class AnnulSelect(discord.ui.Select):
    def __init__(self, gid, owner_id, targets, guild):
        self.gid, self.owner_id = gid, owner_id
        opts = []
        for uid, amount in targets[:25]:
            m = guild.get_member(uid) if guild else None
            opts.append(discord.SelectOption(label=(m.display_name if m else str(uid))[:80], value=str(uid),
                                             description="Apostou {} pts".format(fmt_num(amount))))
        super().__init__(placeholder="Quem você quer anular?", options=opts)

    async def callback(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
            return await deny(inter, "Esse menu não é seu.")
        g = get_game(self.gid)
        res = titles.use_annul(db, g["guild_id"], inter.user.id, g, int(self.values[0]))
        if not res["ok"]:
            return await inter.response.edit_message(content=res["error"], view=None)
        await inter.response.edit_message(
            content="🦈 Aposta de <@{}> **anulada** ({} pts devolvidos a ele). O Predador recarrega à meia-noite.".format(
                res["target"], fmt_num(res["amount"])), view=None)
        await edit_message(g, view=BetView(self.gid))


class AnnulView(discord.ui.View):
    def __init__(self, gid, owner_id, targets, guild):
        super().__init__(timeout=60)
        self.add_item(AnnulSelect(gid, owner_id, targets, guild))


async def use_title_power(inter: discord.Interaction, gid):
    g = get_game(gid)
    if not g or g["status"] != "open" or time.time() > g["close_at"]:
        return await deny(inter, "As apostas já foram encerradas.")
    if g["test"]:
        return await deny(inter, "🧪 Partida de teste: sem apostas e sem poderes.")
    guild_id, uid = g["guild_id"], inter.user.id
    t = titles.effective_title(db, guild_id, uid)
    name = t.name if t else None

    def answer(res, ok_text):
        return deny(inter, ok_text if res["ok"] else res["error"])

    if name == "Caçador de Odds":
        res = titles.use_odds(db, guild_id, uid, g)
        return await answer(res, "🎯 Odd da sua aposta subiu para **5x**: se vencer, você recebe pelo menos "
                                 "**{}** pts.".format(fmt_num(res.get("stake", 0) * 5)))
    if name == "13":
        res = titles.use_odd13(db, guild_id, uid, g)
        return await answer(res, "🍀 Odd da sua aposta subiu para **13x**: se vencer, você recebe pelo menos "
                                 "**{}** pts.".format(fmt_num(res.get("stake", 0) * 13)))
    if name == "Senhor do Destino":
        return await answer(titles.use_destino(db, guild_id, uid, g),
                            "🔮 **Destino selado**: quando a partida terminar, ela conta como vitória para todos "
                            "que apostaram nela (quem perderia recebe stake x odd do seu lado).")
    if name == "Odeio perder":
        return await answer(titles.use_hate(db, guild_id, uid, g),
                            "😤 **Odeio perder** armado: se você perder esta aposta, todos os vencedores "
                            "perdem o pagamento. O uso do dia só é gasto se isso acontecer.")
    if name == "Sombra":
        res = titles.use_shadow(db, guild_id, uid, g)
        if not res["ok"]:
            return await deny(inter, res["error"])
        await deny(inter, "🌑 A partida foi **fechada** para os outros apostadores. Só você pode apostar agora.")
        try:
            await inter.channel.send("🌑 <@{}> usou a **Sombra**: só ele(a) pode apostar nesta partida.".format(uid),
                                     allowed_mentions=NO_PING)
        except Exception as e:
            print("sombra aviso erro:", repr(e))
        return
    if name == "Predador":
        res = titles.annul_targets(db, guild_id, uid, g)
        if not res["ok"]:
            return await deny(inter, res["error"])
        return await inter.response.send_message(
            "🦈 Escolha o adversário que terá a aposta anulada (a stake dele é devolvida):",
            view=AnnulView(gid, uid, res["targets"], inter.guild), ephemeral=True)

    if t:
        who = "Seu título {} não tem poder ativo — ele age sozinho no fim da partida.".format(title_label(t.id))
    elif progress.get_stats(db, guild_id, uid)["equipped_title"] == catalog.MIMIC_ID:
        who = "Você equipou o O Viajante, mas ainda não escolheu o poder: use `/imitar`."
    else:
        who = "Você não tem um título equipado."
    await deny(inter, who + "\nPoderes com botão: **Caçador de Odds**, **13**, **Senhor do Destino**, **Predador**, **Sombra** e "
                           "**Odeio perder**. Veja todos em `/titulos`.")


def required_voters(guild_id):
    return {r["discord_id"] for r in db.execute("SELECT discord_id FROM accounts WHERE guild_id=?", (guild_id,))}


def voted_ids(guild_id):
    return {r["discord_id"] for r in db.execute("SELECT discord_id FROM reset_votes WHERE guild_id=?", (guild_id,))}


def clear_reset(guild_id):
    db.execute("DELETE FROM resets WHERE guild_id=?", (guild_id,))
    db.execute("DELETE FROM reset_votes WHERE guild_id=?", (guild_id,))
    db.commit()


def reset_embed(guild_id, expires):
    voted = voted_ids(guild_id)
    status = "\n".join(("✅ " if u in voted else "⏳ ") + "<@" + str(u) + ">" for u in sorted(required_voters(guild_id)))
    return discord.Embed(
        title="⚠️ Resetar todos os saldos?", color=discord.Color.red(),
        description="Todos os saldos voltarão para **{} pts**.\nÉ preciso que **todos** com conta vinculada "
                    "confirmem.\nExpira <t:{}:R>.\n\n{}".format(START_BALANCE, int(expires), status))


class ResetView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        for label, style, key, cb in (("Confirmar reset", discord.ButtonStyle.danger, "confirm", self.on_confirm),
                                      ("Cancelar", discord.ButtonStyle.secondary, "cancel", self.on_cancel)):
            btn = discord.ui.Button(label=label, style=style, custom_id="reset:{}:{}".format(key, guild_id))
            btn.callback = cb
            self.add_item(btn)

    async def on_confirm(self, inter: discord.Interaction):
        gid = self.guild_id
        r = db.execute("SELECT * FROM resets WHERE guild_id=?", (gid,)).fetchone()
        if not r or time.time() > r["expires"]:
            clear_reset(gid)
            return await deny(inter, "Esse pedido expirou ou foi cancelado.")
        voters = required_voters(gid)
        if inter.user.id not in voters:
            return await deny(inter, "Só quem tem conta do LoL vinculada pode votar neste reset.")
        db.execute("INSERT OR IGNORE INTO reset_votes VALUES(?,?)", (gid, inter.user.id))
        db.commit()

        if not voters <= voted_ids(gid):
            return await inter.response.edit_message(embed=reset_embed(gid, r["expires"]))
        if active_games(gid):
            await inter.response.edit_message(embed=reset_embed(gid, r["expires"]))
            return await inter.followup.send(
                "Todos confirmaram, mas há partidas com apostas em andamento. "
                "Quando terminarem, clique em Confirmar de novo para concluir.", ephemeral=True)

        db.execute("UPDATE users SET balance=?, bank=0, bank_min=0 WHERE guild_id=?", (START_BALANCE, gid))
        clear_reset(gid)
        await inter.response.edit_message(view=None, embed=discord.Embed(
            title="✅ Saldos resetados", color=discord.Color.green(),
            description="Todos confirmaram. Os saldos voltaram para **{} pts**.".format(START_BALANCE)))

    async def on_cancel(self, inter: discord.Interaction):
        if not inter.user.guild_permissions.manage_guild:
            return await deny(inter, "Só quem gerencia o servidor pode cancelar.")
        clear_reset(self.guild_id)
        await inter.response.edit_message(view=None, embed=discord.Embed(
            title="Reset cancelado", color=discord.Color.light_grey(), description="Nenhum saldo foi alterado."))


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self._synced = False

    async def sync_guild(self, guild):
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception as e:
            print("sync_guild erro:", guild.id, repr(e))

    async def on_ready(self):
        if self._synced:  # on_ready pode disparar de novo após reconexões
            return
        self._synced = True
        try:  # apaga comandos globais antigos para não duplicarem com os de cada servidor
            await self.http.bulk_upsert_global_commands(self.application_id, [])
        except Exception as e:
            print("limpeza dos comandos globais falhou:", repr(e))
        for guild in self.guilds:
            await self.sync_guild(guild)
        await self.announce_migration()

    async def announce_migration(self):
        by_guild = {}
        for (g, u), (n, coins) in MIGRATION_SUMMARY.items():
            by_guild.setdefault(g, []).append((u, n, coins))
        for g, items in by_guild.items():
            cfg = get_cfg(g)
            ch = self.get_channel(cfg["channel_id"]) if cfg else None
            if not ch:
                continue
            items.sort(key=lambda x: -x[2])
            lines = ["<@{}> · {} título(s) → **+{} pts**".format(u, n, fmt_num(c)) for u, n, c in items[:15]]
            if len(items) > 15:
                lines.append("… e mais {} jogador(es).".format(len(items) - 15))
            text = ("🏷️ **Os títulos foram reformulados!** Quem tinha títulos que deixaram de existir foi "
                    "reembolsado em moedas, de acordo com a raridade. Veja os novos poderes em `/titulos`.\n\n"
                    + "\n".join(lines))
            try:
                await ch.send(text[:2000], allowed_mentions=NO_PING)
            except Exception as e:
                print("announce_migration erro:", repr(e))

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)

    async def setup_hook(self):
        riot.session = aiohttp.ClientSession()
        await load_champions()
        for g in db.execute("SELECT gid, message_id FROM games WHERE status='open'"):
            self.add_view(BetView(g["gid"]), message_id=g["message_id"])
        for r in db.execute("SELECT guild_id, message_id FROM resets"):
            self.add_view(ResetView(r["guild_id"]), message_id=r["message_id"])
        for loop in (poll_live, poll_results, month_loop, bank_loop, pec_loop):
            loop.start()


bot = Bot()


def cmd(name, description, perms=False, **params):
    def deco(fn):
        if params:
            fn = app_commands.describe(**params)(fn)
        fn = app_commands.guild_only()(fn)
        if perms:
            fn = app_commands.default_permissions(manage_guild=True)(fn)
        return bot.tree.command(name=name, description=description)(fn)
    return deco


@cmd("canal",
     "Define o canal dos anúncios e, opcionalmente, o cargo marcado nas apostas",
     perms=True,
     canal="Canal onde as partidas serão anunciadas",
     cargo="Cargo que será marcado quando uma aposta for aberta",
     remover_cargo="Marque para parar de marcar qualquer cargo")
async def canal(inter: discord.Interaction, canal: Optional[discord.TextChannel] = None,
                cargo: Optional[discord.Role] = None, remover_cargo: bool = False):
    cfg = get_cfg(inter.guild_id)
    if not canal and not cargo and not remover_cargo:
        return await deny(inter, "Informe um `canal`, um `cargo` ou marque `remover_cargo`.")
    if not canal and not (cfg and cfg["channel_id"]):
        return await deny(inter, "Defina primeiro o `canal` de anúncios.")
    if cargo and remover_cargo:
        return await deny(inter, "Escolha `cargo` **ou** `remover_cargo`, não os dois.")
    if cargo and cargo.is_default():
        return await deny(inter, "Não dá para usar @everyone como cargo de aposta.")
    channel_id = canal.id if canal else cfg["channel_id"]
    role_id = None if remover_cargo else (cargo.id if cargo else (cfg["role_id"] if cfg else None))
    db.execute("INSERT INTO config(guild_id, channel_id, role_id) VALUES(?,?,?) ON CONFLICT(guild_id)"
               " DO UPDATE SET channel_id=excluded.channel_id, role_id=excluded.role_id",
               (inter.guild_id, channel_id, role_id))
    db.commit()
    text = "Canal configurado: <#{}>".format(channel_id)
    if role_id:
        text += "\nCargo marcado ao abrir apostas: <@&{}>".format(role_id)
        role = inter.guild.get_role(role_id)
        if role and not role.mentionable and not inter.guild.me.guild_permissions.mention_everyone:
            text += ("\n⚠️ Esse cargo não é mencionável e o bot não tem a permissão **Mencionar @everyone, @here e "
                     "todos os cargos**. Ative uma das duas coisas, senão a marcação não vai notificar ninguém.")
    else:
        text += "\nNenhum cargo será marcado."
    await inter.response.send_message(text, ephemeral=True, allowed_mentions=NO_PING)


@cmd("vincular", "Vincula sua conta do LoL (Nome#TAG)")
async def vincular(inter: discord.Interaction, riot_id: str):
    if "#" not in riot_id:
        return await deny(inter, "Use o formato Nome#TAG.")
    await inter.response.defer(ephemeral=True)
    name, tag = riot_id.rsplit("#", 1)
    try:
        acc = await riot.puuid_by_riot_id(name.strip(), tag.strip())
    except RiotAuthError as e:
        warn_riot_auth(e)
        return await inter.followup.send("⚠️ A chave da API da Riot do bot está inválida ou expirada. "
                                         "Avise o dono do bot para renová-la e tente de novo.")
    except RiotError as e:
        return await inter.followup.send("A Riot está instável agora (HTTP {}). Tente de novo em instantes.".format(e.status))
    if not acc:
        return await inter.followup.send("Conta não encontrada.")
    if db.execute("SELECT 1 FROM accounts WHERE guild_id=? AND puuid=?", (inter.guild_id, acc["puuid"])).fetchone():
        return await inter.followup.send("Essa conta já está vinculada neste servidor.")
    try:
        current = await riot.profile_icon(acc["puuid"])
    except RiotError as e:
        return await inter.followup.send("Não consegui consultar a Riot (HTTP {}). Tente de novo.".format(e.status))
    icon = random.choice([i for i in range(0, 29) if i != current])
    riot_name = acc["gameName"] + "#" + acc["tagLine"]
    db.execute("INSERT OR REPLACE INTO pending VALUES(?,?,?,?,?,?)",
               (inter.guild_id, inter.user.id, acc["puuid"], riot_name, icon, time.time() + VERIFY_TTL))
    db.commit()
    embed = discord.Embed(
        title="Verificação de conta", color=discord.Color.gold(),
        description="Para provar que **" + riot_name + "** é sua, troque seu ícone de perfil "
                    "no LoL pelo ícone da imagem abaixo (é um dos ícones iniciais, gratuitos).\n\n"
                    "Depois use `/confirmar` em até 10 minutos. A Riot pode levar alguns segundos "
                    "para atualizar o ícone.")
    embed.set_image(url=icon_url(icon))
    await inter.followup.send(embed=embed)


@cmd("confirmar", "Confirma a troca de ícone e conclui a vinculação")
async def confirmar(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    p = db.execute("SELECT * FROM pending WHERE guild_id=? AND discord_id=?", (inter.guild_id, inter.user.id)).fetchone()
    if not p or time.time() > p["expires"]:
        return await inter.followup.send("Nenhuma vinculação pendente. Use `/vincular` de novo.")
    try:
        icon = await riot.profile_icon(p["puuid"])
    except RiotError as e:
        return await inter.followup.send("Não consegui consultar a Riot (HTTP {}). Tente de novo.".format(e.status))
    if icon != p["icon"]:
        embed = discord.Embed(
            description="Seu ícone atual ainda não é o esperado. Troque para o ícone abaixo e tente "
                        "`/confirmar` de novo em instantes.", color=discord.Color.red())
        embed.set_image(url=icon_url(p["icon"]))
        return await inter.followup.send(embed=embed)
    try:
        db.execute("INSERT OR REPLACE INTO accounts VALUES(?,?,?,?)",
                   (inter.guild_id, inter.user.id, p["puuid"], p["riot_id"]))
    except sqlite3.IntegrityError:
        return await inter.followup.send("Essa conta acabou de ser vinculada por outra pessoa neste servidor.")
    db.execute("DELETE FROM pending WHERE guild_id=? AND discord_id=?", (inter.guild_id, inter.user.id))
    db.commit()
    await inter.followup.send("✅ Conta **" + p["riot_id"] + "** vinculada! Você já pode trocar o ícone de volta.")


@cmd("desvincular", "Remove sua conta vinculada")
async def desvincular(inter: discord.Interaction):
    db.execute("DELETE FROM accounts WHERE guild_id=? AND discord_id=?", (inter.guild_id, inter.user.id))
    db.commit()
    await deny(inter, "Conta desvinculada.")


@cmd("contas", "Mostra as contas do LoL vinculadas neste servidor")
async def contas(inter: discord.Interaction):
    gid = inter.guild_id
    rows = db.execute("SELECT discord_id, riot_id FROM accounts WHERE guild_id=? ORDER BY riot_id COLLATE NOCASE",
                      (gid,)).fetchall()
    pend = db.execute("SELECT discord_id, riot_id FROM pending WHERE guild_id=? AND expires>?",
                      (gid, time.time())).fetchall()
    cfg = get_cfg(gid)
    playing = {r["discord_id"] for r in db.execute(
        "SELECT discord_id FROM games WHERE guild_id=? AND status IN ('open','closed')", (gid,))}

    desc = "\n".join(("🎮 " if r["discord_id"] in playing else "• ") + "<@{}> — **{}**".format(
        r["discord_id"], r["riot_id"]) for r in rows) or "Nenhuma conta vinculada."
    if pend:
        desc += "\n\n**Aguardando `/confirmar`:**\n" + "\n".join(
            "⏳ <@{}> — {}".format(r["discord_id"], r["riot_id"]) for r in pend)
    embed = discord.Embed(title="Contas vinculadas ({})".format(len(rows)), description=desc[:4000],
                          color=discord.Color.blurple())
    if cfg:
        embed.add_field(name="Canal de anúncios", value="<#{}>".format(cfg["channel_id"]))
        if cfg["role_id"]:
            embed.add_field(name="Cargo marcado nas apostas", value="<@&{}>".format(cfg["role_id"]))
    else:
        embed.add_field(name="⚠️ Canal de anúncios",
                        value="Não configurado. Use `/canal`; sem isso o bot não monitora ninguém.")
    embed.set_footer(text="🎮 = com aposta em andamento")
    await inter.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PING)


@cmd("saldo", "Mostra seu saldo de pontos")
async def saldo(inter: discord.Interaction):
    wallet = balance(inter.guild_id, inter.user.id)
    economy.accrue(db, inter.guild_id, inter.user.id)
    db.commit()
    bank = economy.bank_info(db, inter.guild_id, inter.user.id)["bank"]
    await inter.response.send_message(
        "💰 Saldo de {}: **{}** pts · 🏦 Banco: **{}** pts".format(inter.user.mention, fmt_num(wallet), fmt_num(bank)),
        allowed_mentions=NO_PING)


@cmd("daily", "Resgata pontos diários")
async def daily(inter: discord.Interaction):
    gid, uid = inter.guild_id, inter.user.id
    balance(gid, uid)
    last = db.execute("SELECT last_daily FROM users WHERE guild_id=? AND discord_id=?", (gid, uid)).fetchone()[0]
    if time.time() - last < 86400:
        restante = int(86400 - (time.time() - last))
        return await deny(inter, "Volte em {}h{}min.".format(restante // 3600, (restante % 3600) // 60))
    bonus, bonus_title = titles.daily_bonus(db, gid, uid, DAILY_AMOUNT)
    total = DAILY_AMOUNT + bonus
    reinc = titles.reinc_bonus(db, gid, uid, total)
    total += reinc
    add_balance(gid, uid, total)
    db.execute("UPDATE users SET last_daily=? WHERE guild_id=? AND discord_id=?", (time.time(), gid, uid))
    db.commit()
    unlocked = guarded("on_daily", lambda: progress.on_daily(db, gid, uid, total), [])
    await inter.response.send_message(
        "🎁 {} resgatou **+{} pts**{}! Saldo: {}".format(
            inter.user.mention, fmt_num(total), bonus_note(bonus_title, bonus) + (" (🔁 Reencarnação +{})".format(fmt_num(reinc)) if reinc else ""),
            fmt_num(balance(gid, uid)))
        + unlock_text(unlocked), allowed_mentions=NO_PING)


@cmd("ranking", "Top 10 de patrimônio (carteira + banco) do servidor")
async def ranking(inter: discord.Interaction):
    rows = db.execute(
        "SELECT u.discord_id, u.balance + COALESCE(u.bank, 0) AS worth, s.equipped_title FROM users u"
        " LEFT JOIN stats s ON s.guild_id=u.guild_id AND s.discord_id=u.discord_id WHERE u.guild_id=?"
        " ORDER BY worth DESC LIMIT 10", (inter.guild_id,)).fetchall()
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        label = title_label(r["equipped_title"]) if r["equipped_title"] else None
        lines.append("{} <@{}> — **{}** pts{}".format(
            medals[i] if i < 3 else "`{:>2}.`".format(i + 1), r["discord_id"], fmt_num(r["worth"]),
            " · " + label if label else ""))
    e = discord.Embed(title="🏆 Ranking de patrimônio", color=discord.Color.gold(),
                      description="\n".join(lines) or "Vazio.")
    e.set_footer(text="Carteira + banco")
    await inter.response.send_message(embed=e, allowed_mentions=NO_PING)


@cmd("resetar_saldos", "Pede o reset de todos os saldos (exige confirmação de todos os vinculados)", perms=True)
async def resetar_saldos(inter: discord.Interaction):
    gid = inter.guild_id
    r = db.execute("SELECT * FROM resets WHERE guild_id=?", (gid,)).fetchone()
    if r and time.time() < r["expires"]:
        return await deny(inter, "Já existe um pedido de reset pendente neste servidor.")
    if r:
        clear_reset(gid)
    if not required_voters(gid):
        return await deny(inter, "Nenhuma conta vinculada neste servidor.")
    if active_games(gid):
        return await deny(inter, "Há partidas com apostas em andamento. Aguarde terminarem para pedir o reset.")
    expires = time.time() + RESET_TTL
    await inter.response.send_message(embed=reset_embed(gid, expires), view=ResetView(gid))
    msg = await inter.original_response()
    db.execute("INSERT INTO resets VALUES(?,?,?,?)", (gid, msg.channel.id, msg.id, expires))
    db.commit()


WIPE_TTL = 5 * 60
WIPE_MAX_TRIES = 3
WIPE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # sem 0/O/1/I para não confundir ao copiar
WIPE_TABLES = ("stats", "achievements", "titles", "target_hits", "title_kv", "cooldowns", "titles_removed",
               "pending", "resets", "reset_votes", "cards")   # todas têm guild_id (bets/games são tratadas à parte)
wipe_request = {}


def wipe_database(guild_id):
    # tudo numa transação: se falhar, nada muda
    try:
        db.execute("DELETE FROM bets WHERE gid IN (SELECT gid FROM games WHERE guild_id=?)", (guild_id,))
        db.execute("DELETE FROM games WHERE guild_id=?", (guild_id,))
        for table in WIPE_TABLES:
            db.execute("DELETE FROM {} WHERE guild_id=?".format(table), (guild_id,))
        db.execute("DELETE FROM users WHERE guild_id=?", (guild_id,))
        db.execute("INSERT INTO users(guild_id, discord_id, balance) SELECT guild_id, discord_id, ? FROM accounts"
                   " WHERE guild_id=?", (START_BALANCE, guild_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return db.execute("SELECT COUNT(*) c FROM accounts WHERE guild_id=?", (guild_id,)).fetchone()["c"]


class WipeModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Confirmar reset total")
        self.password = discord.ui.TextInput(label="Senha mostrada no console do bot", max_length=32)
        self.add_item(self.password)

    async def on_submit(self, inter: discord.Interaction):
        req = wipe_request
        if not req or time.time() > req["expires"]:
            wipe_request.clear()
            return await deny(inter, "Esse pedido expirou ou foi cancelado. Use `/resetar_tudo` de novo.")
        if inter.user.id != req["user_id"] or inter.guild_id != req["guild_id"]:
            return await deny(inter, "Só quem pediu o reset (neste servidor) pode confirmar.")
        typed = self.password.value.strip().upper().encode()
        if not hmac.compare_digest(typed, req["password"].encode()):
            req["tries"] += 1
            left = WIPE_MAX_TRIES - req["tries"]
            print("[reset_tudo] senha incorreta por {} ({} tentativa(s) restante(s))".format(inter.user, max(left, 0)))
            if left <= 0:
                wipe_request.clear()
                return await deny(inter, "❌ Senha incorreta. Limite de tentativas atingido: pedido cancelado.")
            return await deny(inter, "❌ Senha incorreta. Restam **{}** tentativa(s).".format(left))
        if active_games(req["guild_id"]):
            return await deny(inter, "Há partidas com apostas em andamento. Aguarde terminarem (ou use "
                                     "`/encerrar_aposta`) e confirme de novo.")
        gid = req["guild_id"]
        wipe_request.clear()
        try:
            n = wipe_database(gid)
        except Exception as e:
            print("[reset_tudo] ERRO:", repr(e))
            return await deny(inter, "❌ Falha ao resetar o banco (nada foi alterado). Veja o console.")
        print("[reset_tudo] servidor {} resetado por {} ({})".format(gid, inter.user, inter.user.id))
        await inter.response.send_message(embed=discord.Embed(
            title="✅ Servidor resetado", color=discord.Color.green(),
            description="Tudo foi zerado, exceto as **{}** conta(s) vinculada(s).\n"
                        "Cada uma voltou com **{}** pts.".format(n, fmt_num(START_BALANCE))))


class WipeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=WIPE_TTL)

    @discord.ui.button(label="Inserir senha", emoji="🔑", style=discord.ButtonStyle.danger)
    async def enter(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(WipeModal())


@cmd("resetar_tudo",
     "Reseta TODO o servidor, mantendo só as contas vinculadas (senha gerada no console do bot)", perms=True)
async def resetar_tudo(inter: discord.Interaction):
    if not inter.user.guild_permissions.manage_guild:
        return await deny(inter, "Só quem gerencia o servidor pode usar isso.")
    if active_games(inter.guild_id):
        return await deny(inter, "Há partidas com apostas em andamento. Aguarde terminarem (ou use "
                                 "`/encerrar_aposta`) para pedir o reset.")
    password = "".join(secrets.choice(WIPE_ALPHABET) for _ in range(8))
    wipe_request.clear()
    wipe_request.update(password=password, user_id=inter.user.id, guild_id=inter.guild_id, expires=time.time() + WIPE_TTL, tries=0)
    print("\n" + "=" * 60)
    print("[reset_tudo] {} ({}) pediu o RESET TOTAL do servidor {}.".format(inter.user, inter.user.id, inter.guild_id))
    print("[reset_tudo] SENHA: {}   (vale {} min)".format(password, WIPE_TTL // 60))
    print("=" * 60 + "\n", flush=True)
    await inter.response.send_message(
        "⚠️ **Reset total deste servidor**\n"
        "Será apagado: saldos e banco (voltam para **{}** pts), CLT, estatísticas, conquistas, títulos, "
        "fragmentos, cooldowns e histórico de partidas/apostas **deste servidor** (os outros não são afetados).\n"
        "Será mantido: contas vinculadas e canal/cargo configurados.\n\n"
        "Uma senha foi gerada e enviada ao **console do bot**. Clique no botão e digite-a "
        "(vale **{} min**, {} tentativas).".format(fmt_num(START_BALANCE), WIPE_TTL // 60, WIPE_MAX_TRIES),
        view=WipeView(), ephemeral=True)


@cmd("encerrar_aposta", "Encerra as apostas em andamento e devolve os pontos a todos", perms=True)
async def encerrar_aposta(inter: discord.Interaction):
    if not inter.user.guild_permissions.manage_guild:
        return await deny(inter, "Só quem gerencia o servidor pode usar isso.")
    games = db.execute("SELECT * FROM games WHERE guild_id=? AND status IN ('open','closed')",
                       (inter.guild_id,)).fetchall()
    if not games:
        return await deny(inter, "Não há apostas em andamento.")

    await inter.response.defer(ephemeral=True)
    reason = "🚫 Aposta encerrada por " + inter.user.display_name + ". Todos os pontos foram devolvidos."
    games_n = refunded_n = points = 0
    for g in games:
        bets = db.execute("SELECT discord_id, amount FROM bets WHERE gid=?", (g["gid"],)).fetchall()
        if await cancel_game(g, reason):
            games_n += 1
            refunded_n += len(bets)
            points += sum(b["amount"] for b in bets)
    await inter.followup.send("✅ {} aposta(s) encerrada(s). {} apostador(es) reembolsado(s), {} pts devolvidos.".format(
        games_n, refunded_n, points))


def title_label(title_id):
    t = catalog.TITLES_BY_ID.get(title_id)
    return "{} {}".format(catalog.RARITY_EMOJI[t.rarity], t.name) if t else None


def bonus_note(title, bonus):
    return " (🏷️ {} +{})".format(title_label(title.id), fmt_num(bonus)) if title and bonus else ""


def power_text(guild_id, user_id, equipped):
    if not equipped:
        return ""
    if equipped.id == catalog.MIMIC_ID:
        m = titles.mimic_of(db, guild_id, user_id)
        if not m:
            return "\n-# 🎭 Escolha qual poder imitar com `/imitar`."
        return "\n-# 🎭 Imitando {} — {}".format(title_label(m.id), m.desc)
    return "\n-# ⚡ " + equipped.desc


def unlock_text(unlocked):
    if not unlocked:
        return ""
    return "\n\n" + "\n".join("🏆 **Conquista desbloqueada:** {} **{}** — {}".format(
        catalog.RARITY_EMOJI[a.rarity], a.name, a.desc) for a in unlocked)


async def announce_unlocks(channel_id, items):
    channel = bot.get_channel(channel_id) if items else None
    if not channel:
        return
    lines = ["🏆 <@{}> desbloqueou {} **{}** — {}".format(uid, catalog.RARITY_EMOJI[a.rarity], a.name, a.desc)
             for uid, a in items]
    try:
        await channel.send("\n".join(lines)[:2000], allowed_mentions=NO_PING)
    except Exception as e:
        print("announce_unlocks erro:", repr(e))


def build_bet_results(g, bets, payouts, outcome, flipped=frozenset()):
    pool = {"win": 0, "lose": 0}
    for _, side, amount in bets:
        pool[side] += amount
    two_sided, total = pool["win"] > 0 and pool["lose"] > 0, pool["win"] + pool["lose"]
    return [progress.BetResult(uid, amount, payouts.get(uid, 0), side == outcome or uid in flipped,
                               (total / pool[side]) if two_sided else None, g["puuid"])
            for uid, side, amount in bets]


def cooldown_lines(guild_id, uid):
    now = time.time()

    def line(label, left_at):
        if left_at <= now:
            return "{} ✅ pronto".format(label)
        return "{} ⏳ <t:{}:R>".format(label, int(left_at))

    work_left = economy.cooldown_left(db, guild_id, uid, "work", economy.WORK_CD, now)
    lines = [line("⚒️ **Trabalho**", now + work_left)]
    jail = economy.cooldown_left(db, guild_id, uid, "jail", 0, now)
    if jail > 0:
        lines.append("🚔 **Crime** preso, solto <t:{}:R>".format(int(now + jail)))
    else:
        crime_left = economy.cooldown_left(db, guild_id, uid, "crime", economy.CRIME_CD, now)
        lines.append(line("🦹 **Crime**", now + crime_left))
    if progress.get_stats(db, guild_id, uid)["equipped_title"] == catalog.MIMIC_ID:
        swap = titles.cd_left(db, guild_id, uid, "cd_imitar", progress.MIMIC_CD, now)
        lines.append(line("🎭 **Trocar imitação**", now + swap))
    ps = titles.power_status(db, guild_id, uid, now)
    if ps:
        label = "✨ **{}**".format(ps["title"].name)
        if ps["ready"]:
            extra = " ({}/{} usos hoje)".format(ps["left"], ps["max"]) if ps["max"] > 1 else ""
            lines.append("{} ✅ pronto{}".format(label, extra))
        else:
            lines.append(line(label, ps["at"]))
    return "\n".join(lines)


def build_profile_embed(guild_id, member):
    uid = member.id
    bal = balance(guild_id, uid)
    economy.accrue(db, guild_id, uid)
    progress.check_wealth(db, guild_id, uid, "view")
    db.commit()
    s = progress.get_stats(db, guild_id, uid)
    keys = progress.unlocked_keys(db, guild_id, uid)
    acc = db.execute("SELECT riot_id FROM accounts WHERE guild_id=? AND discord_id=?", (guild_id, uid)).fetchone()
    owned = progress.owned_titles(db, guild_id, uid)

    equipped = catalog.TITLES_BY_ID.get(s["equipped_title"])
    e = discord.Embed(color=catalog.RARITY_COLORS[equipped.rarity] if equipped else 0x5865F2)
    e.set_author(name="Perfil de " + member.display_name, icon_url=member.display_avatar.url)
    e.set_thumbnail(url=member.display_avatar.url)
    e.description = ("**{}**".format(title_label(equipped.id)) + power_text(guild_id, uid, equipped)
                     if equipped else "*Nenhum título equipado — abra caixas na `/loja`!*")

    decided = s["wins"] + s["losses"]
    rate = "{:.0f}%".format(100 * s["wins"] / decided) if decided else "—"
    for name, value in (
            ("🎮 Conta do LoL", acc["riot_id"] if acc else "Nenhuma vinculada"),
            ("💰 Saldo", "**{}** pts".format(fmt_num(bal))),
            ("📈 Lucro total", "**{}** pts".format(fmt_signed(s["profit"]))),
            ("🎯 Apostas", "{} feitas · ✅ {} · ❌ {} · {}".format(fmt_num(s["total_bets"]), s["wins"], s["losses"], rate)),
            ("🔥 Sequência", "Atual **{}** · Melhor **{}**".format(s["streak"], s["best_streak"])),
            ("🏦 Banco", "**{}** pts".format(fmt_num(economy.bank_info(db, guild_id, uid)["bank"]))),
            ("📦 Caixas abertas", fmt_num(s["boxes_opened"])),
            ("♻️ Fragmentos", fmt_num(s["fragments"])),
            ("🏷️ Títulos", "{}/{}".format(len(owned), len(catalog.TITLES)))):
        e.add_field(name=name, value=value, inline=True)

    e.add_field(name="⏱️ Cooldowns", value=cooldown_lines(guild_id, uid), inline=False)
    cs = economy.card_status(db, guild_id, uid)
    e.add_field(name="💳 Cartão", value=(
        "Nível **{}/{}** · limite de **{}** pts/dia".format(cs["level"], economy.CARD_MAX_LEVEL, fmt_num(cs["limit"]))
        if cs else "Sem cartão (compre na `/loja`)"), inline=False)
    rl = titles.reinc_level(db, guild_id, uid)
    if rl:
        e.add_field(name="🔁 Reencarnação", value="{} — **+{}%** em todo dinheiro ganho".format(
            title_label(catalog.REINC_IDS[rl - 1]), rl * 10), inline=False)

    rarest = catalog.rarest_by_category(keys)
    lines = ["{} **{}** — {}".format(emoji, label, "{} {}".format(
        catalog.RARITY_EMOJI[rarest[cat].rarity], rarest[cat].name) if cat in rarest else "—")
             for cat, emoji, label in catalog.CATEGORIES]
    e.add_field(name="🏆 Conquistas ({}/{}) · mais raras por categoria".format(len(keys), len(catalog.ACHIEVEMENTS)),
                value="\n".join(lines), inline=False)
    return e


def build_achievements_embed(guild_id, member):
    keys = progress.unlocked_keys(db, guild_id, member.id)
    e = discord.Embed(title="🏆 Conquistas de {}".format(member.display_name), color=discord.Color.gold(),
                      description="**{}/{}** desbloqueadas".format(len(keys), len(catalog.ACHIEVEMENTS)))
    for cat, emoji, label in catalog.CATEGORIES:
        items = [a for a in catalog.ACHIEVEMENTS if a.cat == cat]
        lines = ["{} {} **{}** — {}".format("✅" if a.key in keys else "🔒", catalog.RARITY_EMOJI[a.rarity],
                                           a.name, a.desc) for a in items]
        e.add_field(name="{} {} ({}/{})".format(emoji, label, sum(a.key in keys for a in items), len(items)),
                    value="\n".join(lines)[:1024], inline=False)
    return e


@cmd("perfil",
     "Mostra o perfil: título, conta do LoL, saldo, conquistas e mais",
     usuario="De quem ver o perfil (padrão: você)")
async def perfil(inter: discord.Interaction, usuario: Optional[discord.Member] = None):
    await inter.response.send_message(embed=build_profile_embed(inter.guild_id, usuario or inter.user))


@cmd("conquistas",
     "Lista todas as conquistas e quais você já desbloqueou",
     usuario="De quem ver as conquistas (padrão: você)")
async def conquistas(inter: discord.Interaction, usuario: Optional[discord.Member] = None):
    await inter.response.send_message(embed=build_achievements_embed(inter.guild_id, usuario or inter.user))


class WorldRequiemView(discord.ui.View):
    def __init__(self, owner_id, guild_id):
        super().__init__(timeout=120)
        self.owner_id, self.guild_id = owner_id, guild_id

    async def interaction_check(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
            await deny(inter, "Só quem equipou o World Requiem pode confirmar.")
            return False
        return True

    @discord.ui.button(label="Ativar World Requiem", emoji="💀", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        if active_games(self.guild_id):
            return await deny(inter, "Há partidas com apostas em andamento. Aguarde terminarem para ativar.")
        try:
            res = progress.world_requiem(db, self.guild_id, self.owner_id, START_BALANCE)
        except Exception as e:
            print("[world_requiem] ERRO:", repr(e))
            return await deny(inter, "❌ Falha ao ativar (nada foi alterado). Veja o console.")
        if not res["ok"]:
            return await deny(inter, res["error"])
        print("[world_requiem] servidor {} resetado por {} ({}) -> {}".format(
            self.guild_id, inter.user, self.owner_id, res["title"].name))
        self.stop()
        await inter.response.edit_message(content="💀 **World Requiem ativado.** Você recebeu {}.".format(
            title_label(res["title"].id)), view=None)
        e = discord.Embed(title="💀 WORLD REQUIEM", color=catalog.RARITY_COLORS[6], description=(
            "{} ativou o **World Requiem**.\n\nO saldo e os títulos de **{}** conta(s) vinculada(s) foram resetados "
            "(saldo volta para **{}** pts).{}\n\n{} ganhou {} — {}".format(
                inter.user.mention, res["accounts"], fmt_num(START_BALANCE),
                " **{}** Imortal(is) imune(s).".format(res["immune"]) if res["immune"] else "",
                inter.user.mention, title_label(res["title"].id), res["title"].desc)))
        try:
            await inter.channel.send(embed=e, allowed_mentions=NO_PING)
        except Exception as ex:
            print("[world_requiem] não consegui anunciar:", repr(ex))

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await inter.response.edit_message(content="Cancelado. Nada foi alterado.", view=None)


async def ask_world_requiem(inter: discord.Interaction):
    gid, uid = inter.guild_id, inter.user.id
    if catalog.WORLD_REQUIEM_ID not in progress.owned_titles(db, gid, uid):
        return await deny(inter, "Você não possui esse título.")
    level = titles.reinc_level(db, gid, uid)
    if level >= len(catalog.REINC_IDS):
        return await deny(inter, "Você já tem a **Sétima Reencarnação**: o World Requiem está bloqueado para você.")
    if active_games(gid):
        return await deny(inter, "Há partidas com apostas em andamento. Aguarde terminarem para ativar.")
    affected, immune = progress.world_requiem_targets(db, gid, uid)
    n = len(affected)
    nxt = catalog.TITLES_BY_ID[catalog.REINC_IDS[level]]
    imunes = " e **{}** Imortal(is) é(são) imune(s)".format(len(immune)) if immune else ""
    await inter.response.send_message(
        "💀 **World Requiem**\n"
        "Ao confirmar, **{}** conta(s) vinculada(s) deste servidor (inclusive a sua) perdem **todo o saldo** "
        "(carteira e banco, voltam para **{}** pts) e **todos os títulos**. Você também perde o World Requiem.\n"
        "As Reencarnações de cada um são mantidas{}. Estatísticas, conquistas e fragmentos não mudam.\n\n"
        "Você recebe {}.\n**Isso não pode ser desfeito.**".format(n, fmt_num(START_BALANCE), imunes, title_label(nxt.id)),
        view=WorldRequiemView(uid, gid), ephemeral=True)


class TitlesView(discord.ui.View):
    def __init__(self, viewer_id, guild_id, member, rarity=None):
        super().__init__(timeout=300)
        self.viewer_id, self.guild_id, self.member = viewer_id, guild_id, member
        if rarity is None:
            owned = progress.owned_titles(db, guild_id, member.id)
            eq = catalog.TITLES_BY_ID.get(progress.get_stats(db, guild_id, member.id)["equipped_title"])
            rarity = eq.rarity if eq else max(
                (catalog.TITLES_BY_ID[x].rarity for x in owned if x in catalog.TITLES_BY_ID), default=0)
        self.rarity = rarity
        self.rebuild()

    async def interaction_check(self, inter: discord.Interaction):
        if inter.user.id != self.viewer_id:
            await deny(inter, "Use `/titulos` para abrir a sua própria coleção.")
            return False
        return True

    def rebuild(self):
        self.clear_items()
        owned = progress.owned_titles(db, self.guild_id, self.member.id)
        opts = [discord.SelectOption(
            label=catalog.RARITY_NAMES[r], value=str(r), emoji=catalog.RARITY_EMOJI[r], default=(r == self.rarity),
            description="{}/{} coletados".format(sum(x.id in owned for x in catalog.TITLES_BY_RARITY[r]),
                                                 len(catalog.TITLES_BY_RARITY[r])))
            for r in range(catalog.N_RARITIES - 1, -1, -1)]
        sel = discord.ui.Select(placeholder="Ver outra raridade…", options=opts, row=0)
        sel.callback = self.on_rarity
        self.add_item(sel)
        if self.viewer_id == self.member.id:
            mine = [x for x in catalog.TITLES_BY_RARITY[self.rarity] if x.id in owned and x.id not in catalog.PASSIVE_IDS]
            if mine:
                esel = discord.ui.Select(placeholder="Equipar um título desta raridade…", row=1, options=[
                    discord.SelectOption(label=x.name, value=x.id, description=x.desc[:100]) for x in mine])
                esel.callback = self.on_equip
                self.add_item(esel)
            un = discord.ui.Button(label="Desequipar", emoji="❌", style=discord.ButtonStyle.secondary, row=2)
            un.callback = self.on_unequip
            self.add_item(un)

    def embed(self):
        gid, mid, r = self.guild_id, self.member.id, self.rarity
        owned = progress.owned_titles(db, gid, mid)
        eq = catalog.TITLES_BY_ID.get(progress.get_stats(db, gid, mid)["equipped_title"])
        lines = ["✅ {}**{}** — {}".format("▸ " if eq and x.id == eq.id else "", x.name, x.desc) if x.id in owned
                 else "🔒 *{}* — {}".format(x.name, x.desc) for x in catalog.TITLES_BY_RARITY[r]]
        e = discord.Embed(
            title="{} {} · Títulos de {}".format(catalog.RARITY_EMOJI[r], catalog.RARITY_NAMES[r],
                                                  self.member.display_name),
            color=catalog.RARITY_COLORS[r], description="\n".join(lines)[:4000])
        e.add_field(name="📚 Coleção ({}/{})".format(len(owned), len(catalog.TITLES)),
                    value=" · ".join("{} {}/{}".format(
                        catalog.RARITY_EMOJI[i], sum(x.id in owned for x in catalog.TITLES_BY_RARITY[i]),
                        len(catalog.TITLES_BY_RARITY[i])) for i in range(catalog.N_RARITIES)), inline=False)
        e.add_field(name="🏷️ Equipado",
                    value=((title_label(eq.id) + power_text(gid, mid, eq)) if eq else "—"), inline=False)
        e.set_footer(text="▸ = equipado · Só o título equipado vale e ele fica fixo na aposta "
                          "no momento em que você aposta.")
        return e

    async def on_rarity(self, inter: discord.Interaction):
        self.rarity = int(inter.data["values"][0])
        self.rebuild()
        await inter.response.edit_message(embed=self.embed(), view=self)

    async def _change(self, inter, tid):
        if tid == catalog.WORLD_REQUIEM_ID:
            await ask_world_requiem(inter)
            return False
        res = progress.change_title(db, self.guild_id, self.viewer_id, tid)
        if not res["ok"]:
            return await deny(inter, res["error"])
        self.rebuild()
        await inter.response.edit_message(embed=self.embed(), view=self)
        return True

    async def on_equip(self, inter: discord.Interaction):
        tid = inter.data["values"][0]
        if await self._change(inter, tid) and tid == catalog.MIMIC_ID \
                and not titles.mimic_of(db, self.guild_id, self.viewer_id):
            await inter.followup.send("🎭 Agora escolha qual poder imitar com `/imitar`.", ephemeral=True)

    async def on_unequip(self, inter: discord.Interaction):
        await self._change(inter, "")


@cmd("titulos",
     "Coleção de títulos e seus poderes (navegue por raridade e equipe)",
     usuario="De quem ver os títulos (padrão: você)")
async def titulos(inter: discord.Interaction, usuario: Optional[discord.Member] = None):
    view = TitlesView(inter.user.id, inter.guild_id, usuario or inter.user)
    await inter.response.send_message(embed=view.embed(), view=view)


@cmd("equipar", "Equipa um título que você possui", titulo="Título que você quer exibir no perfil")
async def equipar(inter: discord.Interaction, titulo: str):
    t = progress.find_owned_title(db, inter.guild_id, inter.user.id, titulo)
    if not t:
        return await deny(inter, "Você não possui esse título. Veja os seus em `/titulos`.")
    if t.id == catalog.WORLD_REQUIEM_ID:
        return await ask_world_requiem(inter)
    res = progress.change_title(db, inter.guild_id, inter.user.id, t.id)
    if not res["ok"]:
        return await deny(inter, res["error"])
    await deny(inter, "✅ Título equipado: **{}**{}".format(title_label(t.id), power_text(inter.guild_id, inter.user.id, t)))


def _title_choices(pool):
    pool = sorted(pool, key=lambda t: (-t.rarity, t.name))
    return [app_commands.Choice(name=title_label(t.id), value=t.id) for t in pool][:25]


@equipar.autocomplete("titulo")
async def equipar_autocomplete(inter: discord.Interaction, current: str):
    cur = current.lower()
    return _title_choices(catalog.TITLES_BY_ID[t] for t in progress.owned_titles(db, inter.guild_id, inter.user.id)
                          if t in catalog.TITLES_BY_ID and t not in catalog.PASSIVE_IDS
                          and cur in catalog.TITLES_BY_ID[t].name.lower())


@cmd("imitar", "Com o título O Viajante, escolha qual poder imitar", titulo="Título cujo poder você quer imitar")
async def imitar(inter: discord.Interaction, titulo: str):
    gid, uid = inter.guild_id, inter.user.id
    if catalog.MIMIC_ID not in progress.owned_titles(db, gid, uid):
        return await deny(inter, "Só quem tem o título **O Viajante** pode imitar poderes.")
    text = titulo.strip().lower()
    target = next((x for x in catalog.TITLES if x.id not in catalog.NOT_IMITABLE and (x.id == text or x.name.lower() == text)),
                  None)
    if not target:
        return await deny(inter, "Título não encontrado. Comece a digitar para ver as opções.")
    res = progress.change_mimic(db, gid, uid, target.id)
    if not res["ok"]:
        return await deny(inter, res["error"])
    if res.get("same"):
        return await deny(inter, "🎭 Você já está imitando {}.".format(title_label(target.id)))
    equipped = progress.get_stats(db, gid, uid)["equipped_title"] == catalog.MIMIC_ID
    await deny(inter, "🎭 Imitando {} — {}\n{}\nPróxima troca possível <t:{}:R>.".format(
        title_label(target.id), target.desc,
        "Vale para as **próximas apostas** (as já feitas mantêm o poder de quando você apostou)." if equipped
        else "Equipe o **O Viajante** com `/equipar` para ativar.", int(res["next"])))


@imitar.autocomplete("titulo")
async def imitar_autocomplete(inter: discord.Interaction, current: str):
    return _title_choices(x for x in catalog.TITLES if x.id not in catalog.NOT_IMITABLE and current.lower() in x.name.lower())


@cmd("desequipar", "Remove o título equipado")
async def desequipar(inter: discord.Interaction):
    res = progress.change_title(db, inter.guild_id, inter.user.id, "")
    await deny(inter, "Título removido." if res["ok"] else res["error"])


def box_cost(guild_id, user_id, key):
    return titles.box_price(db, guild_id, user_id, catalog.BOXES[key]["price"])


def shop_embed(guild_id, user_id):
    s = progress.get_stats(db, guild_id, user_id)
    e = discord.Embed(
        title="🛒 Loja", color=discord.Color.gold(),
        description="Saldo: **{}** pts · Fragmentos: **{}**\nTítulo repetido vira fragmentos: {}".format(
            fmt_num(balance(guild_id, user_id)), fmt_num(s["fragments"]),
            " ".join("{}{}".format(catalog.RARITY_EMOJI[i], v) for i, v in enumerate(catalog.FRAGMENT_VALUE))))
    for key, box in catalog.BOXES.items():
        chances = " · ".join("{} {:g}%".format(catalog.RARITY_EMOJI[i], w) for i, w in enumerate(box["weights"]) if w)
        cost = box_cost(guild_id, user_id, key)
        price_txt = ("~~{}~~ {}".format(fmt_num(box["price"]), fmt_num(cost)) if cost != box["price"]
                     else fmt_num(cost))
        e.add_field(name="{} {} — {} pts".format(box["emoji"], box["name"], price_txt),
                    value="{}\n♻️ ou {} fragmentos".format(chances, fmt_num(box["frags"])), inline=False)
    e.add_field(name="💼 CLT — carreira", value=clt_field(guild_id, user_id), inline=False)
    e.add_field(name="💳 Cartão — transferências", value=card_field(guild_id, user_id), inline=False)
    return e


def fmt_job(j):
    return "**{}** ({}–{} pts)".format(j.name, fmt_num(j.pay_min), fmt_num(j.pay_max))


def clt_field(guild_id, user_id):
    info = economy.clt_info(db, guild_id, user_id)
    lvl, works = info["level"], info["works"]
    text = "Nível **{}/{}** · Trabalhos feitos: **{}**\nEmprego atual: {}".format(
        lvl, economy.CLT_MAX, fmt_num(works), fmt_job(economy.best_job(lvl, works)))
    if lvl < economy.CLT_MAX:
        nxt = lvl + 1
        jobs = ", ".join("{} (exige {} trabalhos)".format(j.name, j.min_works) for j in economy.JOBS if j.level == nxt)
        text += "\n⬆️ Nível {} — **{}** pts: libera {}".format(nxt, fmt_num(economy.CLT_PRICES[nxt]), jobs)
    else:
        text += "\n✅ Nível máximo."
    return text + "\nVeja todos em `/empregos`."


def clt_result_embed(res):
    jobs = "\n".join("• {} — exige {} trabalhos".format(fmt_job(j), j.min_works) for j in res["jobs"])
    return discord.Embed(title="💼 CLT nível {}!".format(res["level"]), color=discord.Color.blue(),
                         description="Você pagou **{}** pts.\nEmpregos liberados:\n{}".format(
                             fmt_num(res["price"]), jobs))


def box_result_embed(box_key, res):
    box, t = catalog.BOXES[box_key], res["title"]
    desc = "{} **{}** — **{}**\n-# ⚡ {}".format(catalog.RARITY_EMOJI[t.rarity], catalog.RARITY_NAMES[t.rarity],
                                              t.name, t.desc)
    if res["duplicate"]:
        desc += "\nVocê já possui este título.\n♻️ Convertido em **{}** fragmentos.".format(res["fragments_gained"])
        if res.get("refund"):
            desc += "\n💸 **Gostos Parecidos:** {} pts reembolsados!".format(fmt_num(res["refund"]))
    else:
        desc += "\n✨ **Novo título!** Equipe pelo botão abaixo ou em `/titulos`."
    return discord.Embed(title="{} {} aberta!".format(box["emoji"], box["name"]), description=desc,
                         color=catalog.RARITY_COLORS[t.rarity])


class ShopView(discord.ui.View):
    def __init__(self, owner_id, guild_id, offer_title=None):
        super().__init__(timeout=300)
        self.owner_id, self.guild_id = owner_id, guild_id

        def add(label, emoji, style, row, callback, disabled=False):
            btn = discord.ui.Button(label=label, emoji=emoji, style=style, row=row, disabled=disabled)
            btn.callback = callback
            self.add_item(btn)

        for key, box in catalog.BOXES.items():
            add("{} · {} pts".format(box["name"], fmt_num(box_cost(guild_id, owner_id, key))), box["emoji"],
                discord.ButtonStyle.success, 0, self.make_callback(key, False))
        for key, box in catalog.BOXES.items():
            add("{} frag → {}".format(fmt_num(box["frags"]), box["name"]), "♻️",
                discord.ButtonStyle.secondary, 1, self.make_callback(key, True))
        lvl = economy.clt_info(db, guild_id, owner_id)["level"]
        if lvl < economy.CLT_MAX:
            add("Upgrade CLT nível {} · {} pts".format(lvl + 1, fmt_num(economy.CLT_PRICES[lvl + 1])), "💼",
                discord.ButtonStyle.primary, 2, self.clt_callback)
        else:
            add("CLT no nível máximo", "💼", discord.ButtonStyle.secondary, 2, self.clt_callback, disabled=True)
        card = economy.card_status(db, guild_id, owner_id)
        if card is None:
            add("Comprar cartão · {} pts".format(fmt_num(economy.CARD_PRICE)), "💳", discord.ButtonStyle.primary, 2,
                self.card_callback)
        elif card["next_level"] is not None:
            add("Cartão nível {} · {} pts".format(card["next_level"], fmt_num(card["next_cost"])), "💳",
                discord.ButtonStyle.primary, 2, self.card_callback)
        else:
            add("Cartão no nível máximo", "💳", discord.ButtonStyle.secondary, 2, self.card_callback, disabled=True)
        if offer_title:
            async def equip_cb(inter: discord.Interaction, t=offer_title):
                if t.id == catalog.WORLD_REQUIEM_ID:
                    return await ask_world_requiem(inter)
                res = progress.change_title(db, guild_id, owner_id, t.id)
                if not res["ok"]:
                    return await deny(inter, res["error"])
                await inter.response.edit_message(embed=shop_embed(guild_id, owner_id),
                                                  view=ShopView(owner_id, guild_id))
                await inter.followup.send("✅ Título equipado: **{}**{}".format(
                    title_label(t.id), power_text(guild_id, owner_id, t)), ephemeral=True)
            add("Equipar {}".format(offer_title.name), "🏷️", discord.ButtonStyle.primary, 3, equip_cb)

    async def clt_callback(self, inter: discord.Interaction):
        gid, uid = inter.guild_id, inter.user.id
        balance(gid, uid)
        res = economy.upgrade_clt(db, gid, uid)
        if not res["ok"]:
            return await deny(inter, res["error"])
        await inter.response.edit_message(embeds=[clt_result_embed(res), shop_embed(gid, uid)],
                                          view=ShopView(uid, gid))

    async def card_callback(self, inter: discord.Interaction):
        gid, uid = inter.guild_id, inter.user.id
        balance(gid, uid)
        has = economy.card_status(db, gid, uid) is not None
        res = economy.upgrade_card(db, gid, uid) if has else economy.buy_card(db, gid, uid)
        if not res["ok"]:
            return await deny(inter, res["error"])
        await inter.response.edit_message(embeds=[card_result_embed(res), shop_embed(gid, uid)],
                                          view=ShopView(uid, gid))

    async def interaction_check(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
            await deny(inter, "Essa loja é de outra pessoa. Use `/loja` para abrir a sua.")
            return False
        return True

    def make_callback(self, key, with_fragments):
        async def callback(inter: discord.Interaction):
            gid, uid = inter.guild_id, inter.user.id
            balance(gid, uid)
            res = (progress.exchange_fragments if with_fragments else progress.buy_box)(db, gid, uid, key)
            if not res["ok"]:
                return await deny(inter, res["error"])
            t = res["title"]
            await inter.response.edit_message(embeds=[box_result_embed(key, res), shop_embed(gid, uid)],
                                              view=ShopView(uid, gid, None if res["duplicate"] else t))
            if t.rarity >= 3:
                try:
                    await inter.channel.send("🎉 {} tirou {} na **{}**!".format(
                        inter.user.mention, title_label(t.id), catalog.BOXES[key]["name"]), allowed_mentions=NO_PING)
                except Exception as e:
                    print("brag erro:", repr(e))
        return callback


@cmd("loja", "Abre a loja: caixas de títulos, upgrade de CLT e cartão de transferência")
async def loja(inter: discord.Interaction):
    balance(inter.guild_id, inter.user.id)
    await inter.response.send_message(embed=shop_embed(inter.guild_id, inter.user.id),
                                      view=ShopView(inter.user.id, inter.guild_id), ephemeral=True)


def collect_interest(guild_id, user_id):
    balance(guild_id, user_id)
    gained = economy.accrue(db, guild_id, user_id)
    db.commit()
    return gained, (progress.on_earn(db, guild_id, user_id, gained) if gained else [])


def earned_unlocks(guild_id, user_id, amount):
    return guarded("on_earn", lambda: progress.on_earn(db, guild_id, user_id, amount), [])


def wait_text(left):
    return "⏳ Volte <t:{}:R>.".format(int(time.time() + left))


def interest_note(gained):
    return "\n🏦 Rendimento do banco: **{} pts**".format(fmt_signed(gained)) if gained else ""


def bank_embed(guild_id, user_id):
    wallet = balance(guild_id, user_id)
    bank = economy.bank_info(db, guild_id, user_id)["bank"]
    nxt = economy.next_interest_at(db, guild_id, user_id)
    rate, cap, risk = titles.bank_params(db, guild_id, user_id, economy.BANK_RATE, economy.BANK_CAP)
    e = discord.Embed(title="🏦 Banco", color=discord.Color.green())
    e.add_field(name="🏦 No banco", value="**{}** pts".format(fmt_num(bank)), inline=True)
    e.add_field(name="💰 Na carteira", value="**{}** pts".format(fmt_num(wallet)), inline=True)
    e.add_field(name="📈 Próximo rendimento",
                value="<t:{}:R>".format(int(nxt)) if nxt else "Deposite para começar", inline=True)
    e.set_footer(text="Rende {:g}% ao dia sobre os primeiros {} pts{}. O dinheiro precisa ficar no banco "
                      "por 24h para render. Use /depositar e /sacar.".format(
                          rate * 100, fmt_num(cap),
                          " (com {:g}% de chance de perder {:g}% do saldo)".format(risk[0] * 100, risk[1] * 100)
                          if risk else ""))
    return e


@cmd("banco", "Mostra seu banco, que rende juros todos os dias")
async def banco(inter: discord.Interaction):
    gained, unlocked = collect_interest(inter.guild_id, inter.user.id)
    await inter.response.send_message(
        content=(interest_note(gained).strip() + unlock_text(unlocked)).strip() or None,
        embed=bank_embed(inter.guild_id, inter.user.id), ephemeral=True)


async def move_money(inter, valor, deposit):
    gid, uid = inter.guild_id, inter.user.id
    gained, unlocked = collect_interest(gid, uid)
    amount = economy.parse_amount(valor, balance(gid, uid) if deposit else economy.bank_info(db, gid, uid)["bank"])
    if amount is None:
        return await deny(inter, "Valor inválido. Ex.: `1000`, `10k` ou `tudo`.")
    res = (economy.deposit if deposit else economy.withdraw)(db, gid, uid, amount)
    if not res["ok"]:
        return await deny(inter, res["error"])
    await deny(inter, "{} **{}** pts. Banco: **{}** · Carteira: **{}**{}".format(
        "🏦 Depositado" if deposit else "💸 Sacado", fmt_num(res["amount"]), fmt_num(res["bank"]),
        fmt_num(res["wallet"]), interest_note(gained)) + unlock_text(unlocked))


@cmd("depositar", "Guarda pontos no banco para render juros", valor="Quantidade (aceita 1000, 10k, 1,5k ou 'tudo')")
async def depositar(inter: discord.Interaction, valor: str):
    await move_money(inter, valor, True)


@cmd("sacar", "Retira pontos do banco para a carteira", valor="Quantidade (aceita 1000, 10k, 1,5k ou 'tudo')")
async def sacar(inter: discord.Interaction, valor: str):
    await move_money(inter, valor, False)


@cmd("trabalho",
     "Trabalhe e ganhe pts (1 vez por hora). Melhore com o CLT na /loja",
     emprego="Emprego (opcional; por padrão usa o melhor que você tem liberado)")
async def trabalho(inter: discord.Interaction, emprego: Optional[str] = None):
    gid, uid = inter.guild_id, inter.user.id
    balance(gid, uid)
    res = economy.work(db, gid, uid, job_key=emprego)
    if not res["ok"]:
        return await deny(inter, res.get("error") or wait_text(res["left"]))
    unlocked = earned_unlocks(gid, uid, res["amount"])
    extra = " · 🏷️ " + ", ".join(res["notes"]) if res.get("notes") else ""
    await inter.response.send_message(
        "💼 {} — **+{} pts**! Carteira: {}\n-# {} · trabalho nº {}{}".format(
            res["text"], fmt_num(res["amount"]), fmt_num(res["wallet"]), res["job"].name, fmt_num(res["works"]), extra)
        + unlock_text(unlocked), allowed_mentions=NO_PING)


@trabalho.autocomplete("emprego")
async def trabalho_autocomplete(inter: discord.Interaction, current: str):
    info = economy.clt_info(db, inter.guild_id, inter.user.id)
    cur = current.lower()
    jobs = sorted(economy.available_jobs(info["level"], info["works"]), key=economy.job_avg, reverse=True)
    return [app_commands.Choice(name="{} ({}–{} pts)".format(j.name, fmt_num(j.pay_min), fmt_num(j.pay_max)),
                                value=j.key) for j in jobs if cur in j.name.lower()][:25]


@cmd("empregos", "Lista os empregos, o que cada um paga e como liberar")
async def empregos(inter: discord.Interaction):
    balance(inter.guild_id, inter.user.id)
    info = economy.clt_info(db, inter.guild_id, inter.user.id)
    lvl, works = info["level"], info["works"]
    lines = []
    for j in economy.JOBS:
        if j.level > lvl:
            mark, req = "🔒", "CLT nível {} + {} trabalhos".format(j.level, j.min_works)
        elif works < j.min_works:
            mark, req = "⏳", "faltam {} trabalhos".format(j.min_works - works)
        else:
            mark, req = "✅", "liberado"
        lines.append("{} {} — {}\n-# {}".format(mark, fmt_job(j), req, "Nível {} · mín. {} trabalhos".format(
            j.level, j.min_works) if mark == "✅" else req))
    e = discord.Embed(title="💼 Empregos", color=discord.Color.blue(),
                      description="CLT nível **{}/{}** · Trabalhos feitos: **{}**\n\n{}".format(
                          lvl, economy.CLT_MAX, fmt_num(works), "\n".join(lines)))
    e.set_footer(text="Compre níveis de CLT na /loja. Use /trabalho para trabalhar.")
    await inter.response.send_message(embed=e, ephemeral=True)


@cmd("pai",
     "Quer ser o meu pai? Vire filho de alguém por 1 dia (1x por semana)",
     usuario="Quem será seu pai e pagará a pensão")
async def pai(inter: discord.Interaction, usuario: discord.Member):
    gid, uid = inter.guild_id, inter.user.id
    if usuario.bot:
        return await deny(inter, "Bots não têm carteira. 🤖")
    balance(gid, uid)
    balance(gid, usuario.id)
    res = titles.adopt(db, gid, uid, usuario.id)
    if not res["ok"]:
        return await deny(inter, res["error"])
    note = "" if res["amount"] >= res["want"] else " (a carteira dele(a) só tinha isso; o devido era {})".format(
        fmt_num(res["want"]))
    await inter.response.send_message(
        "👨‍👧 {} agora é filho(a) de {} por 1 dia! Pensão de 30% do patrimônio: **+{} pts**{}.".format(
            inter.user.mention, usuario.mention, fmt_num(res["amount"]), note), allowed_mentions=NO_PING)


def card_field(guild_id, user_id):
    st = economy.card_status(db, guild_id, user_id)
    if not st:
        return ("Necessário para **enviar e receber** pontos com `/transferir`.\n"
                "Compra: **{}** pts · nível 0 com limite de **{}** pts/dia (enviar e receber).".format(
                    fmt_num(economy.CARD_PRICE), fmt_num(economy.card_limit(0))))
    text = "Nível **{}/{}** · Limite: **{}** pts/dia\nHoje: enviado {} · recebido {}".format(
        st["level"], economy.CARD_MAX_LEVEL, fmt_num(st["limit"]), fmt_num(st["sent"]), fmt_num(st["recv"]))
    if st["next_level"] is None:
        return text + "\n✅ Nível máximo."
    return text + "\n⬆️ Nível {} — **{}** pts: limite de {} pts/dia".format(
        st["next_level"], fmt_num(st["next_cost"]), fmt_num(st["next_limit"]))


def card_result_embed(res):
    if res["level"] == 0:
        return discord.Embed(title="💳 Cartão comprado!", color=discord.Color.blurple(),
                             description="Você pagou **{}** pts. Já pode usar `/transferir` (limite de **{}** pts/dia, "
                                         "para enviar e para receber).".format(
                                 fmt_num(res["cost"]), fmt_num(economy.card_limit(0))))
    return discord.Embed(title="💳 Cartão nível {}!".format(res["level"]), color=discord.Color.blurple(),
                         description="Você pagou **{}** pts. Novo limite: **{}** pts/dia.".format(
                             fmt_num(res["cost"]), fmt_num(res["limit"])))


@cmd("transferir", "Transfere pontos para outro jogador (os dois precisam de cartão)",
     usuario="Quem vai receber", valor="Quantidade: 1000, 5k, 1,5m ou tudo (o máximo que o cartão permite)")
async def transferir(inter: discord.Interaction, usuario: discord.Member, valor: str):
    gid, uid = inter.guild_id, inter.user.id
    if usuario.bot:
        return await deny(inter, "Bots não têm carteira. 🤖")
    if usuario.id == uid:
        return await deny(inter, "Você não pode transferir para si mesmo.")
    wallet = balance(gid, uid)
    balance(gid, usuario.id)
    mine, theirs = economy.card_status(db, gid, uid), economy.card_status(db, gid, usuario.id)
    if not mine:
        return await deny(inter, "Você precisa de um cartão para transferir. Compre na `/loja`.")
    if not theirs:
        return await deny(inter, "{} ainda não tem cartão para receber.".format(usuario.mention))
    amount = economy.parse_amount(valor, min(wallet, mine["send_left"], theirs["recv_left"]))
    if amount is None or amount <= 0:
        return await deny(inter, "Valor inválido (ou sem saldo/limite hoje). Use um número, como `1000`, `5k`, "
                                 "`1,5m`, ou `tudo`.")
    res = economy.transfer(db, gid, uid, usuario.id, amount)
    if not res["ok"]:
        return await deny(inter, res["error"])
    await inter.response.send_message("💸 {} transferiu **{} pts** para {}.".format(
        inter.user.mention, fmt_num(amount), usuario.mention), allowed_mentions=NO_PING)
    await inter.followup.send("Saldo: **{}** pts · restam **{}** pts para enviar hoje.".format(
        fmt_num(balance(gid, uid)), fmt_num(res["send_left"])), ephemeral=True)


@cmd("semanal", "Resgata seu bônus semanal")
async def semanal(inter: discord.Interaction):
    gid, uid = inter.guild_id, inter.user.id
    balance(gid, uid)
    res = economy.weekly(db, gid, uid)
    if not res["ok"]:
        return await deny(inter, wait_text(res["left"]))
    unlocked = earned_unlocks(gid, uid, res["amount"])
    await inter.response.send_message(
        "🗓️ {} resgatou o bônus semanal: **+{} pts**! Carteira: {}".format(
            inter.user.mention, fmt_num(res["amount"]), fmt_num(balance(gid, uid))) + unlock_text(unlocked),
        allowed_mentions=NO_PING)


@cmd("crime", "50% de lucro, 50% de multa (2h). Valores escalam com o patrimônio, com teto")
async def crime(inter: discord.Interaction):
    gid, uid = inter.guild_id, inter.user.id
    balance(gid, uid)
    collect_interest(gid, uid)  # rende o banco antes, para a multa já considerar o saldo atualizado
    res = economy.crime(db, gid, uid)
    if not res["ok"]:
        if res.get("jailed"):
            return await deny(inter, "🔒 Você está **preso** e não pode cometer crimes. Liberdade <t:{}:R>.".format(
                int(time.time() + res["left"])))
        return await deny(inter, wait_text(res["left"]))
    tag = "\n-# 🏷️ " + ", ".join(res["notes"]) if res.get("notes") else ""
    if res["success"]:
        unlocked = earned_unlocks(gid, uid, res["delta"])
        msg = "🦹 {} — lucro de **+{} pts**! Carteira: {}".format(
            res["text"], fmt_num(res["delta"]), fmt_num(res["wallet"])) + tag + unlock_text(unlocked)
    else:
        msg = "🚔 {} — multa de **{} pts**. Carteira: {}".format(
            res["text"], fmt_num(-res["delta"]), fmt_num(res["wallet"])) + tag
        if res["from_bank"]:
            msg += "\n🏦 **{}** pts foram tirados do seu banco.".format(fmt_num(res["from_bank"]))
        if res.get("jailed"):
            msg += "\n🔒 Você foi **preso por 1 semana** e não pode usar `/crime` até <t:{}:R>.".format(
                int(res["jail_until"]))
    await inter.response.send_message(msg, allowed_mentions=NO_PING)


@cmd("slots",
     "Caça-níquel: aposte de 100 a 10.000 pts (a cada 10 min). Alguns títulos mudam isso",
     valor="Quantidade (aceita 500, 1k ou 'tudo', até o máximo permitido)")
async def slots(inter: discord.Interaction, valor: str):
    gid, uid = inter.guild_id, inter.user.id
    cap = titles.slot_mods(db, gid, uid, economy.SLOTS_CD, economy.SLOTS_MIN, economy.SLOTS_MAX).max_bet
    bet = economy.parse_amount(valor, min(balance(gid, uid), cap))
    if bet is None:
        return await deny(inter, "Valor inválido. Ex.: `500`, `1k` ou `tudo`.")
    res = economy.slots(db, gid, uid, bet)
    if not res["ok"]:
        return await deny(inter, res.get("error") or wait_text(res["left"]))
    if res["mult"] >= 3:
        outcome = "🎉 **JACKPOT!** {}x — ganhou **+{} pts**".format(res["mult"], fmt_num(res["net"]))
    elif res["net"] > 0:
        outcome = "✅ Par pago: **+{} pts**.".format(fmt_num(res["net"]))
    elif res["net"] == 0:
        outcome = "↩️ Aposta devolvida."
    else:
        outcome = "❌ Perdeu **{} pts**.".format(fmt_num(bet))
    if res.get("note"):
        outcome += "\n-# " + res["note"]
    unlocked = earned_unlocks(gid, uid, res["net"]) if res["net"] > 0 else []
    await inter.response.send_message(
        "🎰 **[ {} ]**\n{}\nCarteira: {}".format(" | ".join(res["reels"]), outcome, fmt_num(res["wallet"]))
        + unlock_text(unlocked), allowed_mentions=NO_PING)


async def edit_message(g, view=None):
    try:
        channel = bot.get_channel(g["channel_id"]) or await bot.fetch_channel(g["channel_id"])
        try:
            msg = await channel.fetch_message(g["message_id"])
            await msg.edit(embed=build_embed(g), view=view)
        except discord.NotFound:
            if g["status"] == "done":  # mensagem original apagada: publica o resultado de novo
                await channel.send(embed=build_embed(g))
    except Exception as e:
        print("edit_message erro:", e)


def compute_payouts_with_fixed_house(bets, outcome, house_odd=HOUSE_ODD):
    if not bets:
        return {}, "none", 0
    if outcome is None:
        return compute_payouts(bets, None)[0], "refund", 0
    pool = {"win": 0, "lose": 0}
    for _, side, amount in bets:
        pool[side] += amount
    if pool["win"] and pool["lose"]:
        return compute_payouts(bets, outcome)[0], "pool", 0
    side = "win" if pool["win"] else "lose"
    if outcome != side:
        return {}, "house", pool[side]
    payouts = {}
    for user, _, amount in bets:
        payouts[user] = payouts.get(user, 0) + int(amount * house_odd)
    return payouts, "house", pool[side] - sum(payouts.values())


def payout_summary(g, bets, payouts, mode, outcome, house_net=0):
    house_odd = house_odd_for(g)
    if g["test"]:
        return "🧪 Partida de teste: sem apostas e sem pagamentos."
    if not bets:
        return "Ninguém apostou nessa partida."
    if mode == "refund":
        return "↩️ Apostas reembolsadas porque a partida foi anulada."

    stakes = {u: a for u, _, a in bets}
    lines = "\n".join("<@{}> recebeu **{}** (lucro {:+d})".format(u, pay, pay - stakes[u])
                      for u, pay in sorted(payouts.items(), key=lambda kv: -kv[1])[:10])
    if mode == "house":
        if payouts:
            house_line = ("A casa perdeu **{} pts** neste mercado.".format(-house_net) if house_net < 0
                          else "A casa ficou com **{} pts** neste mercado.".format(house_net))
            return "🏦 **Casa — odd fixa {:.2f}x**\n{}\n\n{}".format(house_odd, lines, house_line)
        if house_net > 0:
            return "🏦 A casa ficou com **{} pts** porque o único lado apostado perdeu.".format(house_net)
        return "🏦 A casa não teve lucro neste mercado."

    lost = sum(a for _, s, a in bets if s != outcome)
    return "💰 **Pagamentos pelo pool**\n" + lines + "\n\nPerdedores deixaram **{} pts** no pool.".format(lost)


def title_summary(title_res):
    lines = []
    for uid, res in sorted(title_res.items(), key=lambda kv: -(kv[1].credit + kv[1].frags)):
        who = title_label(res.title.id) if res.title else "🏷️"
        for label, amt, unit in res.notes:
            if unit == "info":
                lines.append("<@{}> · {} — {}".format(uid, who, label))
            else:
                lines.append("<@{}> · {} — {}: **{} {}**".format(
                    uid, who, label, fmt_signed(amt), "pts" if unit == "pts" else "fragmentos ♻️"))
    if not lines:
        return ""
    shown = lines[:10] + (["… e mais {} efeito(s).".format(len(lines) - 10)] if len(lines) > 10 else [])
    return "🏷️ **Poderes de título**\n" + "\n".join(shown)


async def settle(g, match):
    fresh = db.execute("SELECT status FROM games WHERE gid=?", (g["gid"],)).fetchone()
    if not fresh or fresh["status"] == "done":
        return

    parts = match["info"]["participants"]
    me = next((p for p in parts if p["puuid"] == g["puuid"]), None)
    if any(p.get("gameEndedInEarlySurrender") for p in parts) or me is None:
        outcome, headline = None, "⚠️ Partida anulada (remake)."
    elif me["win"]:
        outcome, headline = "win", "✅ **" + g["riot_id"] + "** venceu!"
    else:
        outcome, headline = "lose", "❌ **" + g["riot_id"] + "** perdeu!"

    rows = db.execute("SELECT * FROM bets WHERE gid=?", (g["gid"],)).fetchall()
    bets = [(b["discord_id"], b["side"], b["amount"]) for b in rows]
    payouts, mode, house_net = compute_payouts_with_fixed_house(bets, outcome, house_odd_for(g))
    base_payouts = dict(payouts)
    real = outcome is not None and bets and not g["test"]

    title_res = {}
    if real:
        title_res = guarded("titles.settle_bets", lambda: titles.settle_bets(
            db, g, rows, base_payouts, outcome, mode, house_odd=house_odd_for(g),
            brothers=guarded("titles.brother_counts", lambda: titles.brother_counts(db, g["guild_id"], parts), {})), {})
        # Reencarnação: +10% por nível sobre o lucro (já com os bônus de título) de quem lucrou nesta partida
        stake_by = {}
        for b in rows:
            stake_by[b["discord_id"]] = stake_by.get(b["discord_id"], 0) + b["amount"]
        for uid, stake in stake_by.items():
            res0 = title_res.get(uid)
            profit = base_payouts.get(uid, 0) + (res0.credit if res0 else 0) - stake
            extra = titles.reinc_bonus(db, g["guild_id"], uid, profit)
            if extra:
                title_res.setdefault(uid, titles.Res(None)).give(extra, "🔁 Reencarnação")
        for uid, res in title_res.items():
            payouts[uid] = payouts.get(uid, 0) + res.credit

    for uid, amt in payouts.items():
        add_balance(g["guild_id"], uid, amt, commit=False)

    # Estatísticas/conquistas: nunca pode quebrar o pagamento (que ainda está na transação).
    unlocked = guarded("progress.record_settlement", lambda: progress.record_settlement(
        db, g["guild_id"], build_bet_results(g, bets, payouts, outcome,
                                             {uid for uid, res in title_res.items() if res.flip})), []) if real else []

    text = headline + "\n\n" + payout_summary(g, bets, base_payouts, mode, outcome, house_net)
    extra = title_summary(title_res)
    if extra:
        text += "\n\n" + extra
    db.execute("UPDATE games SET status='done', result=? WHERE gid=?", (text[:3900], g["gid"]))
    db.commit()

    await edit_message(get_game(g["gid"]))
    await announce_unlocks(g["channel_id"], unlocked)


async def cancel_game(g, reason):
    fresh = db.execute("SELECT status FROM games WHERE gid=?", (g["gid"],)).fetchone()
    if not fresh or fresh["status"] == "done":  # evita reembolso duplicado
        return False
    bets = [(b["discord_id"], b["side"], b["amount"]) for b in db.execute("SELECT * FROM bets WHERE gid=?", (g["gid"],))]
    for uid, amt in compute_payouts(bets, None)[0].items():
        add_balance(g["guild_id"], uid, amt, commit=False)
    db.execute("UPDATE games SET status='done', result=? WHERE gid=?", (reason, g["gid"]))
    db.commit()
    await edit_message(get_game(g["gid"]))
    return True


async def announce_game(a, pending):
    busy = pending.get((a["guild_id"], a["puuid"]))
    if busy:
        return "pulado: partida anterior ainda pendente (gid {} game_id {} status {})".format(
            busy["gid"], busy["game_id"], busy["status"])
    data = await riot.active_game(a["puuid"])
    await asyncio.sleep(0.2)
    if not data:
        return "fora de partida"
    qid = data.get("gameQueueConfigId")
    is_custom = data.get("gameType") == "CUSTOM_GAME" or qid == 0
    if is_custom and not ANNOUNCE_CUSTOM_TESTS:
        return "pulado: partida personalizada desativada"
    if not is_custom and qid not in ALLOWED_QUEUES:
        return "pulado: fila não permitida (queue id {})".format(qid)
    if db.execute("SELECT 1 FROM games WHERE guild_id=? AND game_id=?", (a["guild_id"], data["gameId"])).fetchone():
        return "pulado: partida {} já foi anunciada (outro jogador vinculado?)".format(data["gameId"])
    channel = bot.get_channel(a["channel_id"])
    if not channel:
        return "pulado: canal {} não encontrado (sem acesso/apagado?)".format(a["channel_id"])

    me = next((p for p in data["participants"] if p.get("puuid") == a["puuid"]), None)
    champ = champions.get(me["championId"], "Desconhecido") if me else "Desconhecido"
    queue = "Personalizada (teste, sem apostas)" if is_custom else queue_name(qid)
    players_json = json.dumps(await build_players_snapshot(data), ensure_ascii=False)

    cur = db.execute(
        "INSERT INTO games(guild_id, game_id, discord_id, puuid, riot_id, champion, queue, channel_id,"
        " status, close_at, test, players_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (a["guild_id"], data["gameId"], a["discord_id"], a["puuid"], a["riot_id"], champ, queue,
         a["channel_id"], "open", time.time() + BET_WINDOW, 1 if is_custom else 0, players_json))
    db.commit()
    gid = cur.lastrowid
    ping, mentions = None, NO_PING
    if not is_custom:
        cfg = get_cfg(a["guild_id"])
        role = channel.guild.get_role(cfg["role_id"]) if cfg and cfg["role_id"] else None
        if role:
            ping, mentions = role.mention, discord.AllowedMentions(roles=[role])
    msg = await channel.send(content=ping, embed=build_embed(get_game(gid)), view=BetView(gid),
                             allowed_mentions=mentions)
    db.execute("UPDATE games SET message_id=? WHERE gid=?", (msg.id, gid))
    db.commit()
    return "anunciada (gid {})".format(gid)


async def refresh_puuid(a):
    # o PUUID é criptografado por chave da Riot: se a chave mudou, o salvo dá HTTP 400; busca de novo pelo Riot ID
    name, tag = a["riot_id"].rsplit("#", 1)
    acc = await riot.puuid_by_riot_id(name.strip(), tag.strip())
    if not acc:
        return None
    new = acc["puuid"]
    db.execute("UPDATE accounts SET puuid=? WHERE guild_id=? AND discord_id=?",
               (new, a["guild_id"], a["discord_id"]))
    db.execute("UPDATE games SET puuid=? WHERE guild_id=? AND discord_id=? AND status IN ('open','closed')",
               (new, a["guild_id"], a["discord_id"]))
    db.commit()
    return new


@tasks.loop(seconds=POLL_LIVE)
async def poll_live():
    rows = db.execute("SELECT a.*, c.channel_id FROM accounts a JOIN config c USING(guild_id)").fetchall()
    dbg("ciclo: monitorando", len(rows), "conta(s):", ", ".join(r["riot_id"] for r in rows) or "nenhuma")
    pending = {(r["guild_id"], r["puuid"]): r for r in db.execute(
        "SELECT gid, game_id, status, guild_id, puuid FROM games WHERE status IN ('open','closed')")}
    for a in rows:
        try:
            dbg(a["riot_id"], await announce_game(a, pending))
        except RiotAuthError as e:
            warn_riot_auth(e)
            break
        except RiotError as e:
            if e.status == 400:
                try:
                    new = await refresh_puuid(a)
                    print("poll_live: PUUID renovado para", a["riot_id"], "->", bool(new))
                except Exception as e2:
                    print("poll_live: falha ao renovar PUUID:", a["riot_id"], repr(e2))
            else:
                print("poll_live erro:", a["riot_id"], repr(e))
        except Exception as e:
            print("poll_live erro:", a["riot_id"], repr(e))


@tasks.loop(seconds=POLL_RESULT)
async def poll_results():
    for g in db.execute("SELECT * FROM games WHERE status IN ('open','closed')").fetchall():
        try:
            if g["status"] == "open" and time.time() > g["close_at"]:
                db.execute("UPDATE games SET status='closed' WHERE gid=?", (g["gid"],))
                db.commit()
                g = get_game(g["gid"])
                await edit_message(g)
            match = await riot.match(g["game_id"])
            await asyncio.sleep(0.2)
            if match:
                await settle(g, match)
            elif time.time() > g["close_at"] + RESULT_TIMEOUT:
                await cancel_game(g, "⚠️ Resultado indisponível na API da Riot. Apostas reembolsadas.")
        except RiotAuthError as e:
            warn_riot_auth(e)
            break
        except Exception as e:
            print("poll_results erro:", repr(e))


@tasks.loop(hours=1)
async def month_loop():
    try:
        items = progress.finalize_months(db)
        db.commit()
    except Exception as e:
        print("month_loop erro:", repr(e))
        return
    for guild_id, user_id, ach in items:
        cfg = get_cfg(guild_id)
        if cfg:
            await announce_unlocks(cfg["channel_id"], [(user_id, ach)])


@tasks.loop(hours=1)
async def bank_loop():
    try:
        items = economy.accrue_all(db)
    except Exception as e:
        print("bank_loop erro:", repr(e))
        return
    for guild_id, user_id, gained in items:
        unlocked = earned_unlocks(guild_id, user_id, gained)
        cfg = get_cfg(guild_id)
        if cfg and unlocked:
            await announce_unlocks(cfg["channel_id"], [(user_id, a) for a in unlocked])


@tasks.loop(hours=1)
async def pec_loop():
    try:
        items = economy.pec_blusinhas(db)
    except Exception as e:
        print("pec_loop erro:", repr(e))
        return
    for guild_id, user_id, tax, worth, from_bank in items:
        cfg = get_cfg(guild_id)
        channel = bot.get_channel(cfg["channel_id"]) if cfg else None
        if not channel:
            continue
        e = discord.Embed(title="👚 PEC das blusinhas", color=discord.Color.red(),
                          description="<@{}> é o **top 1** do ranking e foi taxado em **{}%** do patrimônio.".format(
                              user_id, int(economy.PEC_RATE * 100)))
        e.add_field(name="Patrimônio antes", value="{} pts".format(fmt_num(worth)))
        e.add_field(name="Imposto cobrado", value="-{} pts".format(fmt_num(tax)))
        e.add_field(name="Patrimônio agora", value="{} pts".format(fmt_num(worth - tax)))
        if from_bank:
            e.set_footer(text="{} pts foram tirados do banco (a carteira não bastou).".format(fmt_num(from_bank)))
        try:
            await channel.send(content="<@{}>".format(user_id), embed=e,
                               allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)]))
        except Exception as ex:
            print("pec_loop envio erro:", repr(ex))


@poll_live.before_loop
@poll_results.before_loop
@month_loop.before_loop
@bank_loop.before_loop
@pec_loop.before_loop
async def _wait():
    await bot.wait_until_ready()


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
