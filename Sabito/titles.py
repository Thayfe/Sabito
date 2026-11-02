# settle_bets não dá commit (roda na transação do settle); on_place, use_*, annul, adopt e set_mimic dão commit
import random
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import catalog
import logic

TZ = timezone(timedelta(hours=-3))   # dia "de jogo" no horário de Brasília

MIN_FLAT_STAKE = 250     # aposta mínima para ganhar bônus de moedas fixas
ALLIN_MIN = 1_000
DAY, WEEK = 86_400, 7 * 86_400

BOX_DISCOUNT = {"Mercador": 0.05, "Tive azar e peguei esse título mítico": 0.50}
FRAG_BONUS = {"Colecionador de Títulos": 0.20}
ACTIVE = {"Caçador de Odds", "13", "Senhor do Destino", "Predador", "Sombra", "Odeio perder"}


def init(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS title_kv(guild_id INTEGER, discord_id INTEGER, k TEXT,
        val REAL DEFAULT 0, txt TEXT DEFAULT '', day TEXT DEFAULT '',
        PRIMARY KEY(guild_id, discord_id, k));
    """)
    # dados de cada aposta, gravados na hora em que ela é feita (bets já existe, criada no bot.py)
    for col, ddl in (("title", "TEXT DEFAULT ''"), ("day", "TEXT DEFAULT ''"), ("hour", "INTEGER DEFAULT 0"),
                     ("n_day", "INTEGER DEFAULT 0"), ("ordinal", "INTEGER DEFAULT 0"),
                     ("first_game", "INTEGER DEFAULT 0"), ("target_new", "INTEGER DEFAULT 0"),
                     ("allin", "INTEGER DEFAULT 0"), ("flag", "TEXT DEFAULT ''")):
        try:
            db.execute("ALTER TABLE bets ADD COLUMN {} {}".format(col, ddl))
        except Exception:
            pass
    try:
        db.execute("ALTER TABLE games ADD COLUMN shadow_by INTEGER DEFAULT 0")
    except Exception:
        pass
    db.commit()


def kv_get(db, g, u, k):
    r = db.execute("SELECT val, txt, day FROM title_kv WHERE guild_id=? AND discord_id=? AND k=?",
                   (g, u, k)).fetchone()
    return (r[0] or 0, r[1] or "", r[2] or "") if r else (0, "", "")


def kv_set(db, g, u, k, val=0, txt="", day=""):
    db.execute("INSERT INTO title_kv(guild_id, discord_id, k, val, txt, day) VALUES(?,?,?,?,?,?)"
               " ON CONFLICT(guild_id, discord_id, k) DO UPDATE SET"
               " val=excluded.val, txt=excluded.txt, day=excluded.day", (g, u, k, val, txt, day))


def day_val(db, g, u, k, day):
    v, _, d = kv_get(db, g, u, k)
    return v if d == day else 0


def day_add(db, g, u, k, day, delta):
    new = day_val(db, g, u, k, day) + delta
    kv_set(db, g, u, k, new, "", day)
    return new


def cd_left(db, g, u, k, seconds, now):
    last = kv_get(db, g, u, k)[0]
    return max(0, last + seconds - now) if last else 0


def cd_set(db, g, u, k, now):
    kv_set(db, g, u, k, now)


def game_day(ts=None):
    dt = datetime.now(TZ) if ts is None else datetime.fromtimestamp(ts, TZ)
    return dt.strftime("%Y-%m-%d")


def effective_title(db, g, u):
    r = db.execute("SELECT equipped_title, mimic_title FROM stats WHERE guild_id=? AND discord_id=?",
                   (g, u)).fetchone()
    if not r:
        return None
    tid = r[0] or ""
    if tid == catalog.MIMIC_ID:
        tid = r[1] or ""
    return catalog.TITLES_BY_ID.get(tid)


IMMUNE_TITLE = "Imortal"


def title_immune(t):
    return bool(t) and t.name == IMMUNE_TITLE


def is_immune(db, g, u):
    return title_immune(effective_title(db, g, u))


REINC_STEP = 0.10


def reinc_level(db, g, u):
    owned = {r[0] for r in db.execute("SELECT title_id FROM titles WHERE guild_id=? AND discord_id=?", (g, u))}
    return max((i + 1 for i, tid in enumerate(catalog.REINC_IDS) if tid in owned), default=0)


def reinc_bonus(db, g, u, gain):
    if gain <= 0:
        return 0
    return int(gain * REINC_STEP * reinc_level(db, g, u))


def set_mimic(db, g, u, title_id):
    t = catalog.TITLES_BY_ID.get(title_id)
    if not t or t.id in catalog.NOT_IMITABLE:
        return None
    has = db.execute("SELECT 1 FROM titles WHERE guild_id=? AND discord_id=? AND title_id=?",
                     (g, u, catalog.MIMIC_ID)).fetchone()
    if not has:
        return None
    db.execute("INSERT OR IGNORE INTO stats(guild_id, discord_id) VALUES(?,?)", (g, u))
    db.execute("UPDATE stats SET mimic_title=? WHERE guild_id=? AND discord_id=?", (t.id, g, u))
    db.commit()
    return t


def mimic_of(db, g, u):
    r = db.execute("SELECT mimic_title FROM stats WHERE guild_id=? AND discord_id=?", (g, u)).fetchone()
    return catalog.TITLES_BY_ID.get(r[0]) if r and r[0] else None


def _name(db, g, u):
    t = effective_title(db, g, u)
    return t.name if t else ""


def _bump_daily_streak(db, g, u, now):
    val, txt, _ = kv_get(db, g, u, "daily_streak")
    try:
        last = float(txt)
    except ValueError:
        last = 0
    streak = int(val) + 1 if last and now - last < 2 * DAY else 1
    kv_set(db, g, u, "daily_streak", streak, str(now))
    return streak


def bets_today(db, g, u, day):
    return db.execute("SELECT COUNT(*) FROM bets WHERE discord_id=? AND day=? AND gid IN"
                      " (SELECT gid FROM games WHERE guild_id=?)", (u, day, g)).fetchone()[0]


def daily_bonus(db, g, u, base, now=None):
    now = time.time() if now is None else now
    streak = _bump_daily_streak(db, g, u, now)
    t = effective_title(db, g, u)
    name = t.name if t else ""
    bonus = 0
    if name == "Sequencialista" and streak >= 2:
        bonus = int(base * min(0.10, 0.05 + 0.01 * (streak - 2)))
    elif name == "Espectador" and bets_today(db, g, u, game_day(now)) == 0:
        bonus = 1_000
    elif name == "Sem alma":
        bonus = int(base * min(10.0, 0.02 * streak))
    return bonus, t


def daily_streak(db, g, u):
    return int(kv_get(db, g, u, "daily_streak")[0])


def box_price(db, g, u, price):
    t = effective_title(db, g, u)
    return int(price * (1 - BOX_DISCOUNT.get(t.name, 0))) if t else price


def frag_value(db, g, u, base):
    t = effective_title(db, g, u)
    return int(base * (1 + FRAG_BONUS.get(t.name, 0))) if t else base


def box_refund(db, g, u, box_key, price, duplicate, now=None):
    if not duplicate or box_key != "basica" or _name(db, g, u) != "Gostos Parecidos":
        return 0
    day = game_day(now)
    if day_val(db, g, u, "d_gostos", day) > 0:
        return 0
    day_add(db, g, u, "d_gostos", day, 1)
    return price


def work_adjust(db, g, u, job, amount, now=None, rng=random):
    now = time.time() if now is None else now
    name = _name(db, g, u)
    n_today = day_add(db, g, u, "d_works", game_day(now), 1)     # conta para todos (Escravo)
    hour = datetime.fromtimestamp(now, TZ).hour
    base, inc, notes = amount, 1, []
    if name == "Dono dos meios de produção":
        base = job.pay_max
        amount = base * 2
        notes.append("valor máximo da faixa +100%")
    elif name == "Karl Marx":
        amount = base * 2
        notes.append("+100% de salário")
    elif name == "Trabalhador" and rng.random() < 0.10:
        amount = base + int(base * 0.25)
        notes.append("+25% de salário")
    elif name == "Sonâmbulo" and (hour >= 23 or hour < 4):
        amount = base + int(base * 0.15)
        notes.append("+15% (turno da madrugada)")
    elif name == "Escravo" and n_today == 18:
        amount = base * 3
        notes.append("18º trabalho do dia: +200%")
    elif name == "Talento Inato":
        inc = 2
        notes.append("conta +2 para o CLT")
    return int(amount), inc, notes


def crime_mods(db, g, u, base_chance, base_cd):
    t = _name(db, g, u)
    m = SimpleNamespace(name=t, chance=base_chance, cd=base_cd, win_mult=1.0, fine_mult=1.0,
                        double=0.0, fixed=None, jail=0)
    if t == "Ladrão":
        m.win_mult, m.fine_mult = 1.15, 1.15
    elif t == "Político":
        m.fine_mult = 0.75
    elif t == "Rei do crime":
        m.cd = 105 * 60
    elif t == "Carioca da Gema":
        m.double = 0.10
    elif t == "Assassino em série":
        m.chance, m.jail = 0.90, WEEK
    return m


def slot_mods(db, g, u, base_cd, base_min, base_max):
    t = _name(db, g, u)
    m = SimpleNamespace(name=t, cd=base_cd, min_bet=base_min, max_bet=base_max, luck=0.0, pair_mult=1.0,
                        no_loss=False, jackpot=0.0)
    if t == "Sortudo":
        m.luck = 0.05
    elif t == "Hobby Caro":
        m.pair_mult = 1.5
    elif t == "Quero perder todo meu dinheiro, não aguento mais":
        m.cd, m.min_bet = 60, 1_000
    elif t == "Hackeando o Sistema":
        m.no_loss, m.cd = True, 30 * 60
    elif t == "Deusa da Vitória":
        m.jackpot, m.max_bet = 0.01, 50_000
    return m


def bank_params(db, g, u, base_rate, base_cap):
    t = _name(db, g, u)
    if t == "Banqueiro":
        return 0.02, base_cap, None
    if t == "Investidor de Alto Risco":
        return 0.05, base_cap, (0.05, 0.10)
    if t == "Acionista":
        return base_rate, 15_000_000, None
    return base_rate, base_cap, None


def adopt(db, g, u, parent, now=None):
    now = time.time() if now is None else now
    if _name(db, g, u) != "Quer ser o meu pai?":
        return {"ok": False, "error": "Seu título equipado não é **Quer ser o meu pai?**."}
    if parent == u:
        return {"ok": False, "error": "Você não pode ser filho de si mesmo."}
    if is_immune(db, g, parent):
        return {"ok": False, "error": "Esse jogador é **Imortal**: imune a esse poder."}
    left = cd_left(db, g, u, "cd_pai", WEEK, now)
    if left > 0:
        return {"ok": False, "left": left, "error": "O poder recarrega <t:{}:R>.".format(int(now + left))}
    worth = db.execute("SELECT balance + COALESCE(bank, 0) FROM users WHERE guild_id=? AND discord_id=?",
                       (g, u)).fetchone()
    wallet = db.execute("SELECT balance FROM users WHERE guild_id=? AND discord_id=?", (g, parent)).fetchone()
    want = int((worth[0] if worth else 0) * 0.30)
    pay = min(want, max(0, wallet[0] if wallet else 0))
    if pay <= 0:
        return {"ok": False, "error": "Esse pai não tem moedas na carteira para pagar a pensão "
                                      "(o poder **não** foi gasto)."}
    db.execute("UPDATE users SET balance = balance - ? WHERE guild_id=? AND discord_id=?", (pay, g, parent))
    db.execute("UPDATE users SET balance = balance + ? WHERE guild_id=? AND discord_id=?", (pay, g, u))
    cd_set(db, g, u, "cd_pai", now)
    db.commit()
    return {"ok": True, "amount": pay, "want": want}


def is_underdog(less_money):
    return bool(less_money)


def is_favorite(more_money):
    return bool(more_money)


class Res:
    def __init__(self, title):
        self.title = title
        self.coins = 0      # bônus (lucro extra / moedas fixas)
        self.back = 0       # devolução em derrota
        self.frags = 0
        self.penalty = 0    # moedas tiradas (Azarado, Odeio perder, Judas)
        self.flip = False   # a derrota virou vitória (Manipulador, Senhor do Destino)
        self.notes = []     # (texto, quantia, unidade) — unidade: "pts", "frag" ou "info"

    @property
    def credit(self):
        return self.coins + self.back - self.penalty

    def give(self, n, label):
        n = int(n)
        if n > 0:
            self.coins += n
            self.notes.append((label, n, "pts"))

    def refund(self, n, label):
        n = int(n)
        if n > 0:
            self.back += n
            self.notes.append((label, n, "pts"))

    def frag(self, n, label):
        n = int(n)
        if n > 0:
            self.frags += n
            self.notes.append((label, n, "frag"))

    def penalize(self, n, label):
        n = int(n)
        if n > 0:
            self.penalty += n
            self.notes.append((label, -n, "pts"))

    def info(self, label):
        self.notes.append((label, 0, "info"))

    def clamp_penalty(self, available):
        excess = self.penalty - max(0, available)
        if excess <= 0:
            return
        self.penalty -= excess
        for i in range(len(self.notes) - 1, -1, -1):
            label, amt, unit = self.notes[i]
            if unit == "pts" and amt < 0 and excess > 0:
                cut = min(excess, -amt)
                self.notes[i] = (label, amt + cut, unit)
                excess -= cut
        self.notes = [n for n in self.notes if not (n[2] == "pts" and n[1] == 0)]


def _pct(p):
    return "{:g}".format(round(p * 100, 2))


def _profit(c):
    return max(0, c.payout - c.stake)


def win_profit_pct(p, cond=None, why=""):
    label = "+{}% de lucro".format(_pct(p)) + (" ({})".format(why) if why else "")

    def h(c, r):
        if c.won and (cond is None or cond(c)):
            r.give(_profit(c) * p, label)
    return h


def win_stake_pct(p, why=""):
    label = "+{}% da stake".format(_pct(p)) + (" ({})".format(why) if why else "")

    def h(c, r):
        if c.won:
            r.give(c.stake * p, label)
    return h


def win_flat(n, cond, why):
    def h(c, r):
        if c.won and c.stake >= MIN_FLAT_STAKE and cond(c):
            r.give(n, why)
    return h


def lose_back(p, chance=1.0, cond=None, why=""):
    label = "{}% da stake devolvidos".format(_pct(p)) + (" ({})".format(why) if why else "")

    def h(c, r):
        if not c.won and (cond is None or cond(c)) and (chance >= 1 or random.random() < chance):
            r.refund(c.stake * p, label)
    return h


def lose_cooldown(p, seconds, why, key):
    def h(c, r):
        if c.won or c.stake < MIN_FLAT_STAKE:
            return
        if cd_left(c.db, c.g, c.u, key, seconds, c.now) > 0:
            return
        cd_set(c.db, c.g, c.u, key, c.now)
        r.refund(c.stake * p, why)
    return h


def win_chance_double(chance, why, cap=None, cond=None):
    def h(c, r):
        if c.won and (cond is None or cond(c)) and random.random() < chance:
            r.give(min(_profit(c), cap) if cap else _profit(c), why)
    return h


def _big(p, minimum):
    return win_profit_pct(p, lambda c: c.stake > minimum)


def h_viciado(c, r):
    if c.ordinal and c.ordinal % 6 == 0:
        r.refund(min(c.stake, 1_000), "reembolso da 6ª aposta")


def h_riquinho(c, r):
    if c.won and c.mode == "pool":
        r.give(c.payout * 0.05, "+5% na pool")


def h_azarado(c, r):
    if c.won:
        if random.random() < 0.20:
            r.give(_profit(c) * 0.20, "azar virou sorte: +20% de lucro")
    elif random.random() < 0.20:
        r.penalize(c.stake * 0.20, "azar: -20% da stake")


IMPRUDENTE_DAILY = 2


def h_imprudente(c, r):
    if not (c.won and c.allin) or day_val(c.db, c.g, c.u, "d_imprud", c.day) >= IMPRUDENTE_DAILY:
        return
    bonus = max(0, c.stake * 3 - c.payout)   # odd fixa em 3x: completa até stake*3 (nunca reduz)
    if bonus > 0:                            # só gasta um dos usos do dia se o poder realmente rendeu
        n = day_add(c.db, c.g, c.u, "d_imprud", c.day, 1)
        r.give(bonus, "All-In: odd em 3x ({}/{} do dia)".format(n, IMPRUDENTE_DAILY))


def h_gacha(c, r):
    if c.won and c.stake >= MIN_FLAT_STAKE and c.wins_after % 5 == 0:
        r.frag(60, "5 vitórias")


def h_odd5(c, r):
    if c.won and c.flag == "odd5":
        r.give(max(0, c.stake * 5 - c.payout), "odd subiu para 5x")


def h_treze(c, r):
    if c.won and c.flag == "odd13":
        r.give(max(0, c.stake * 13 - c.payout), "odd subiu para 13x")


BROTHER_BASE, BROTHER_EXTRA, BROTHER_MAX = 0.10, 0.05, 0.25


def brother_pct(n):
    if n <= 0:
        return 0.0
    return min(BROTHER_MAX, BROTHER_BASE + BROTHER_EXTRA * (n - 1))


def h_irmao(c, r):
    pct = brother_pct(c.brothers)
    if c.won and pct:
        r.give(_profit(c) * pct, "Irmãos: +{:g}% de lucro ({} irmão(s) no time)".format(round(pct * 100, 2), c.brothers))


def brother_counts(db, g, participants):
    team = {p.get("puuid"): p.get("teamId") for p in participants if p.get("puuid")}
    if not team:
        return {}
    played = {}
    for r in db.execute("SELECT discord_id, puuid FROM accounts WHERE guild_id=?", (g,)).fetchall():
        if r["puuid"] in team:
            played.setdefault(r["discord_id"], team[r["puuid"]])
    brothers = {u for u in played if _name(db, g, u) == "Irmão"}
    return {u: sum(1 for v in brothers if v != u and played[v] == tm) for u, tm in played.items()}


def h_lenda(c, r):
    if c.won and c.stake >= MIN_FLAT_STAKE and c.wins_after % 5 == 0:
        r.give(7_500, "5 vitórias")
        r.frag(100, "5 vitórias")


def h_alem_odds(c, r):
    if c.won and c.mode == "house":
        mult = 2.5 if c.ranked else 2.0
        r.give(max(0, c.stake * mult - c.payout), "odd da casa {}x".format("2,5" if c.ranked else "2"))


def h_manipulador(c, r):
    if c.won or c.stake < MIN_FLAT_STAKE or cd_left(c.db, c.g, c.u, "cd_manip", DAY, c.now) > 0:
        return
    cd_set(c.db, c.g, c.u, "cd_manip", c.now)
    r.refund(c.stake * 1.5, "derrota virou vitória (+50% da stake)")
    r.flip = True


EFFECTS = {
    "Apostador": win_stake_pct(0.05),
    "Sequencialista": None,    # /daily
    "Espectador": None,        # /daily
    "Medroso": win_profit_pct(0.05, lambda c: c.favorite, "time favorito"),
    "Analista": win_flat(1_000, lambda c: c.wins_after % 3 == 0, "3 vitórias"),
    "Solo": win_profit_pct(0.075, lambda c: c.solo, "só você apostou no time"),
    "Trabalhador": None,       # /trabalho
    "Viciado": h_viciado,
    "Riquinho": h_riquinho,
    "Competidor": win_flat(500, lambda c: c.two_sided, "venceu contra outros apostadores"),
    "Bronze": win_flat(1_000, lambda c: c.ranked and c.own, "vitória em partida ranqueada sua"),
    "Mercador": None,          # /loja
    "Sortudo": None,           # /slots
    "Azarado": h_azarado,
    "Sonâmbulo": win_profit_pct(0.15, lambda c: c.hour >= 23 or c.hour < 4, "aposta de madrugada"),
    "Banqueiro": None,         # banco
    "Corajoso": _big(0.10, 10_000),
    "Ladrão": None,            # /crime
    "Caçador de Odds": h_odd5,
    "Tarado": win_flat(3_500, lambda c: c.spec_n == 3 and not c.own, "3 acertos seguidos no mesmo jogador"),
    "Predador": None,          # botão
    "Colecionador de Títulos": None,   # /loja
    "Visionário": win_profit_pct(0.50, lambda c: c.n_day == 1, "1ª do dia"),
    "Fã de Gacha": h_gacha,
    "Ousado": _big(0.30, 30_000),
    "Investidor de Alto Risco": None,  # banco
    "Político": None,          # /crime
    "Gostos Parecidos": None,  # /loja
    "Escravo": None,           # /trabalho
    "Rei do crime": None,      # /crime
    "Hobby Caro": None,        # /slots
    "Carioca da Gema": None,   # /crime
    "Talento Inato": None,     # /trabalho
    "O Incerto": win_profit_pct(0.60, None, "aposta aleatória"),
    "O Contrário": win_profit_pct(0.30, lambda c: c.fewer_bettors, "lado com menos apostadores"),
    "O Imprudente": h_imprudente,
    "Sombra": None,            # botão
    "Sem alma": None,          # /daily
    "Karl Marx": None,         # /trabalho
    "Mal Perdedor": lose_back(1.0, 0.15, why="nada perdido"),
    "Quero perder todo meu dinheiro, não aguento mais": None,   # /slots
    "Manipulador": h_manipulador,
    "Assassino em série": None,   # /crime
    "Vitorioso": win_flat(10_000, lambda c: c.wins_after_streak and c.wins_after_streak % 5 == 0,
                          "5 vitórias seguidas"),
    "Lenda Viva": h_lenda,
    "World Requiem": None,     # ativado por confirmação no bot (progress.world_requiem)
    "Primeira Reencarnação": None, "Segunda Reencarnação": None, "Terceira Reencarnação": None,
    "Quarta Reencarnação": None, "Quinta Reencarnação": None, "Sexta Reencarnação": None,
    "Sétima Reencarnação": None,   # passivas: +10% de dinheiro ganho por nível (reinc_bonus)
    "Irmão": h_irmao,
    "Imortal": None,           # imunidade (Predador/Sombra/Odeio perder/Judas//pai)
    "Senhor do Destino": None, # botão (flag "destino", resolvido em settle_bets)
    "Acionista": None,         # banco
    "Magnata": win_chance_double(0.30, "lucro dobrado", cond=lambda c: not c.own),
    "O Incontestável": win_flat(60_000, lambda c: c.wins_after_streak == 10, "10 vitórias seguidas"),
    "Dono dos meios de produção": None,   # /trabalho
    "Odeio perder": None,      # tratado depois do loop (flag "hate")
    "Além das Odds": h_alem_odds,
    "Deusa da Vitória": None,  # /slots
    "Midas": win_chance_double(0.50, "lucro dobrado"),
    "Hackeando o Sistema": None,   # /slots
    "Tive azar e peguei esse título mítico": None,   # /loja
    "Judas": None,             # tratado depois do loop
    "13": h_treze,
    "Quer ser o meu pai?": None,   # /pai
    "O Viajante": None,        # imita outro título (resolvido em effective_title)
}
assert set(EFFECTS) == {t.name for t in catalog.TITLES}, "título sem poder (ou poder sem título)"


def on_place(db, g, gid, u, is_new, balance_after, game_total, now=None):
    now_dt = now or datetime.now(TZ)
    allin = 1 if (balance_after == 0 and game_total >= ALLIN_MIN) else 0
    t = effective_title(db, g, u)
    if not is_new:
        if allin:
            db.execute("UPDATE bets SET allin=1 WHERE gid=? AND discord_id=?", (gid, u))
            db.commit()
        return t

    day = now_dt.strftime("%Y-%m-%d")
    # minha linha ainda está com day='', então o COUNT só pega as apostas anteriores do dia
    n_day = bets_today(db, g, u, day) + 1
    first_game = db.execute("SELECT COUNT(*) FROM bets WHERE gid=? AND discord_id!=?",
                            (gid, u)).fetchone()[0] == 0
    target_new = db.execute(
        "SELECT 1 FROM bets b JOIN games gm ON gm.gid=b.gid WHERE b.discord_id=? AND gm.guild_id=?"
        " AND gm.puuid=(SELECT puuid FROM games WHERE gid=?) AND b.gid!=? LIMIT 1",
        (u, g, gid, gid)).fetchone() is None
    r = db.execute("SELECT total_bets FROM stats WHERE guild_id=? AND discord_id=?", (g, u)).fetchone()
    db.execute("UPDATE bets SET title=?, day=?, hour=?, n_day=?, ordinal=?, first_game=?, target_new=?,"
               " allin=? WHERE gid=? AND discord_id=?",
               (t.id if t else "", day, now_dt.hour, n_day, r[0] if r else 0, int(first_game),
                int(target_new), allin, gid, u))
    db.commit()
    return t


def settle_bets(db, game, bets, payouts, outcome, mode, now=None, house_odd=2.0, brothers=None):
    if outcome is None or mode not in ("pool", "house"):
        return {}
    now_ts = time.time() if now is None else now
    g = game["guild_id"]
    pool, cnt = {"win": 0, "lose": 0}, {"win": 0, "lose": 0}
    for b in bets:
        pool[b["side"]] += b["amount"]
        cnt[b["side"]] += 1
    two = pool["win"] > 0 and pool["lose"] > 0
    total = pool["win"] + pool["lose"]
    o_win, o_lose = logic.odds(pool["win"], pool["lose"])
    ranked = (game["queue"] or "").startswith("Ranqueada")
    today = game_day(now_ts)
    info, judas, hate, destiny, ctxs = {}, [], [], [], {}
    brothers = brothers or {}

    for b in bets:
        u, side, stake = b["discord_id"], b["side"], b["amount"]
        other = "lose" if side == "win" else "win"
        won = side == outcome
        payout = payouts.get(u, 0)
        less = two and pool[side] < pool[other]
        more = two and pool[side] > pool[other]
        t = catalog.TITLES_BY_ID.get(b["title"] or "")
        flag = b["flag"] or ""
        day = b["day"] or today
        pre = db.execute("SELECT streak, lose_streak, wins FROM stats WHERE guild_id=? AND discord_id=?",
                         (g, u)).fetchone()
        streak, lose_streak, wins = (pre[0], pre[1], pre[2]) if pre else (0, 0, 0)
        own = game["discord_id"] == u
        sv, stxt, _ = kv_get(db, g, u, "spec")
        av, atxt, _ = kv_get(db, g, u, "alt")
        spec_n = 0 if (own or not won) else (sv + 1 if stxt == game["puuid"] else 1)
        c = SimpleNamespace(
            db=db, g=g, u=u, now=now_ts, day=day, won=won, stake=stake, payout=payout, side=side,
            mode=mode, flag=flag, own=own, odd=((o_win if side == "win" else o_lose) if two else house_odd),
            mult=(payout / stake) if won and stake else 0, ranked=ranked, two_sided=two,
            less_money=less, fewer_bettors=two and cnt[side] < cnt[other], solo=cnt[side] == 1,
            underdog=is_underdog(less),
            favorite=is_favorite(more),
            n_day=b["n_day"] or 0, hour=b["hour"] or 0, ordinal=b["ordinal"] or 0, brothers=brothers.get(u, 0),
            first_game=bool(b["first_game"]), target_new=bool(b["target_new"]), allin=bool(b["allin"]),
            streak=streak, lose_streak=lose_streak, wins_after=wins + (1 if won else 0),
            wins_after_streak=(streak + 1) if won else 0,
            day_wins=day_val(db, g, u, "d_wins", day), day_losses=day_val(db, g, u, "d_losses", day),
            spec_n=spec_n,
            alt_n=((av + 1 if av > 0 and atxt != side else 1) if won else 0),
        )
        ctxs[u] = c
        r = Res(t)
        fn = EFFECTS.get(t.name) if t else None
        if fn:
            fn(c, r)
        if t and t.name == "Judas" and won:
            judas.append((u, c, r))
        if flag == "hate" and not won and two:
            hate.append((u, c, r))
        if flag == "destino" and t and t.name == "Senhor do Destino":
            destiny.append((u, c, r))
        info[u] = SimpleNamespace(won=won, payout=payout, stake=stake, res=r, title=t)

        # Contadores: atualizados para TODOS (com ou sem título), para continuarem certos se trocar.
        day_add(db, g, u, "d_wins" if won else "d_losses", day, 1)
        if not own:
            kv_set(db, g, u, "spec", 0 if c.spec_n >= 3 else c.spec_n, game["puuid"] or "")
        kv_set(db, g, u, "alt", 0 if c.alt_n >= 3 else c.alt_n, side)

    def strip(v, label, amount):
        vi = info[v]
        vr = vi.res
        vr.coins = vr.back = 0
        vr.notes = [n for n in vr.notes if n[2] == "frag"]
        vr.penalize(amount, label)

    # Senhor do Destino: a partida vira vitória para todos. Quem perdeu recebe stake x odd do seu lado
    # (a casa cobre a diferença) e conta como vitória. Quem já ganhou (ou teve a stake devolvida) fica como está.
    if destiny:
        flipped = 0
        for v, i in info.items():
            if i.won or i.payout > 0 or i.stake <= 0:
                continue
            gain = int(i.stake * ctxs[v].odd)
            vr = i.res
            vr.coins = vr.back = vr.penalty = 0
            vr.notes = [n for n in vr.notes if n[2] == "frag"]
            vr.refund(gain, "Senhor do Destino: derrota virou vitória ({:.2f}x)".format(ctxs[v].odd))
            vr.flip = True
            i.won, i.payout = True, gain
            flipped += 1
        for u, c, r in destiny:
            r.info("virou a partida: {} derrota(s) viraram vitória".format(flipped))

    for u, c, r in hate:
        if day_val(db, g, u, "d_hate", c.day) > 0:
            continue
        victims = [v for v, i in info.items()
                   if v != u and i.won and i.payout > 0 and not title_immune(i.title)]
        if not victims:
            continue
        day_add(db, g, u, "d_hate", c.day, 1)
        r.info("derrubou {} vencedor(es)".format(len(victims)))
        for v in victims:
            strip(v, "perdeu por causa do Odeio perder", info[v].payout)

    for u, c, r in judas:
        if day_val(db, g, u, "d_judas", c.day) > 0:
            continue
        victims = [v for v, i in info.items()
                   if v != u and i.won and i.payout > i.stake and not i.res.penalty and not title_immune(i.title)]
        stolen = sum(info[v].payout - info[v].stake for v in victims)
        if stolen <= 0:
            continue
        day_add(db, g, u, "d_judas", c.day, 1)
        r.give(stolen, "lucro roubado de {} vencedor(es)".format(len(victims)))
        for v in victims:
            strip(v, "lucro roubado pelo Judas", info[v].payout - info[v].stake)

    out = {}
    for u, i in info.items():
        r = i.res
        if r.penalty:
            w = db.execute("SELECT balance FROM users WHERE guild_id=? AND discord_id=?", (g, u)).fetchone()
            r.clamp_penalty(payouts.get(u, 0) + (w[0] if w else 0) + r.coins + r.back)
        if r.coins or r.back or r.frags or r.penalty or r.flip or r.notes:
            out[u] = r
        if r.frags:
            db.execute("INSERT OR IGNORE INTO stats(guild_id, discord_id) VALUES(?,?)", (g, u))
            db.execute("UPDATE stats SET fragments = fragments + ? WHERE guild_id=? AND discord_id=?",
                       (r.frags, g, u))
    return out


def _closed(game, now):
    if game["status"] != "open" or now > game["close_at"]:
        return {"ok": False, "error": "As apostas desta partida já foram encerradas."}
    return None


def _need_title(db, g, u, name):
    t = effective_title(db, g, u)
    if not t or t.name != name:
        return {"ok": False, "error": "Seu título equipado não é **{}**.".format(name)}
    return None


def _own_bet(db, game, u, t):
    bet = db.execute("SELECT title, flag, side, amount FROM bets WHERE gid=? AND discord_id=?",
                     (game["gid"], u)).fetchone()
    if not bet:
        return None, {"ok": False, "error": "Você ainda não apostou nesta partida."}
    if bet["title"] != t.id:
        return None, {"ok": False, "error": "Essa aposta foi feita com outro título. O poder só vale para "
                                            "apostas feitas com ele equipado."}
    return bet, None


def _daily_gate(db, g, u, key, now, label):
    day = game_day(now)
    if day_val(db, g, u, key, day) > 0:
        return {"ok": False, "error": "{} já foi usado hoje. Recarrega à meia-noite (horário de Brasília).".format(label)}
    return None


def use_odds(db, g, u, game, now=None):
    now = time.time() if now is None else now
    err = _need_title(db, g, u, "Caçador de Odds") or _closed(game, now)
    if err:
        return err
    bet, err = _own_bet(db, game, u, effective_title(db, g, u))
    if err:
        return err
    if bet["flag"] == "odd5":
        return {"ok": False, "error": "A odd desta aposta já está em **5x**."}
    err = _daily_gate(db, g, u, "d_odds", now, "O Caçador de Odds")
    if err:
        return err
    day_add(db, g, u, "d_odds", game_day(now), 1)
    db.execute("UPDATE bets SET flag='odd5' WHERE gid=? AND discord_id=?", (game["gid"], u))
    db.commit()
    return {"ok": True, "stake": bet["amount"]}


def use_odd13(db, g, u, game, now=None):
    now = time.time() if now is None else now
    err = _need_title(db, g, u, "13") or _closed(game, now)
    if err:
        return err
    bet, err = _own_bet(db, game, u, effective_title(db, g, u))
    if err:
        return err
    if bet["flag"] == "odd13":
        return {"ok": False, "error": "A odd desta aposta já está em **13x**."}
    err = _daily_gate(db, g, u, "d_odd13", now, "O poder do 13")
    if err:
        return err
    day_add(db, g, u, "d_odd13", game_day(now), 1)
    db.execute("UPDATE bets SET flag='odd13' WHERE gid=? AND discord_id=?", (game["gid"], u))
    db.commit()
    return {"ok": True, "stake": bet["amount"]}


def use_destino(db, g, u, game, now=None):
    now = time.time() if now is None else now
    err = _need_title(db, g, u, "Senhor do Destino") or _closed(game, now)
    if err:
        return err
    bet, err = _own_bet(db, game, u, effective_title(db, g, u))
    if err:
        return err
    if bet["flag"] == "destino":
        return {"ok": False, "error": "O destino já está selado nesta partida."}
    err = _daily_gate(db, g, u, "d_destino", now, "O Senhor do Destino")
    if err:
        return err
    day_add(db, g, u, "d_destino", game_day(now), 1)
    db.execute("UPDATE bets SET flag='destino' WHERE gid=? AND discord_id=?", (game["gid"], u))
    db.commit()
    return {"ok": True}


def use_hate(db, g, u, game, now=None):
    now = time.time() if now is None else now
    err = _need_title(db, g, u, "Odeio perder") or _closed(game, now)
    if err:
        return err
    bet, err = _own_bet(db, game, u, effective_title(db, g, u))
    if err:
        return err
    if bet["flag"] == "hate":
        return {"ok": False, "error": "O poder já está armado nesta aposta."}
    err = _daily_gate(db, g, u, "d_hate", now, "O Odeio perder")
    if err:
        return err
    db.execute("UPDATE bets SET flag='hate' WHERE gid=? AND discord_id=?", (game["gid"], u))
    db.commit()
    return {"ok": True}


def use_shadow(db, g, u, game, now=None):
    now = time.time() if now is None else now
    err = _need_title(db, g, u, "Sombra") or _closed(game, now)
    if err:
        return err
    if game["discord_id"] != u:
        return {"ok": False, "error": "A Sombra só funciona na **sua** partida."}
    if game["shadow_by"]:
        return {"ok": False, "error": "A partida já está fechada para os outros apostadores."}
    err = _daily_gate(db, g, u, "d_shadow", now, "A Sombra")
    if err:
        return err
    day_add(db, g, u, "d_shadow", game_day(now), 1)
    db.execute("UPDATE games SET shadow_by=? WHERE gid=?", (u, game["gid"]))
    db.commit()
    return {"ok": True}


def annul_targets(db, g, u, game, now=None):
    now = time.time() if now is None else now
    err = _need_title(db, g, u, "Predador") or _closed(game, now)
    if err:
        return err
    bet, err = _own_bet(db, game, u, effective_title(db, g, u))
    if err:
        return err
    err = _daily_gate(db, g, u, "d_annul", now, "O Predador")
    if err:
        return err
    rows = db.execute("SELECT discord_id, amount, title FROM bets WHERE gid=? AND side!=? ORDER BY amount DESC",
                      (game["gid"], bet["side"])).fetchall()
    if not rows:
        return {"ok": False, "error": "Ainda não há adversários (apostas no lado oposto) para anular."}
    targets = [(r[0], r[1]) for r in rows if not title_immune(catalog.TITLES_BY_ID.get(r[2] or ""))]
    if not targets:
        return {"ok": False, "error": "Seus adversários são **Imortais**: imunes ao Predador."}
    return {"ok": True, "targets": targets}


def use_annul(db, g, u, game, target, now=None):
    now = time.time() if now is None else now
    pre = annul_targets(db, g, u, game, now)
    if not pre["ok"]:
        return pre
    row = next((x for x in pre["targets"] if x[0] == target), None)
    if not row:
        return {"ok": False, "error": "Esse jogador não está mais no lado oposto desta partida."}
    amount = row[1]
    db.execute("DELETE FROM bets WHERE gid=? AND discord_id=?", (game["gid"], target))
    db.execute("UPDATE users SET balance = balance + ? WHERE guild_id=? AND discord_id=?", (amount, g, target))
    db.execute("UPDATE stats SET total_bets = MAX(total_bets - 1, 0) WHERE guild_id=? AND discord_id=?",
               (g, target))
    day_add(db, g, u, "d_annul", game_day(now), 1)
    db.commit()
    return {"ok": True, "amount": amount, "target": target}


# nome do título -> (tipo, chave no title_kv, usos por dia | segundos de recarga)
#   "day": limite por dia de jogo (zera à meia-noite de Brasília); "cd": recarga corrida desde o último uso.
POWER_LIMITS = {
    "Caçador de Odds": ("day", "d_odds", 1),
    "13": ("day", "d_odd13", 1),
    "Predador": ("day", "d_annul", 1),
    "Sombra": ("day", "d_shadow", 1),
    "Odeio perder": ("day", "d_hate", 1),
    "Judas": ("day", "d_judas", 1),
    "O Imprudente": ("day", "d_imprud", IMPRUDENTE_DAILY),
    "Gostos Parecidos": ("day", "d_gostos", 1),
    "Manipulador": ("cd", "cd_manip", DAY),
    "Senhor do Destino": ("day", "d_destino", 1),
    "Quer ser o meu pai?": ("cd", "cd_pai", WEEK),
}


def next_midnight(now):
    dt = datetime.fromtimestamp(now, TZ) + timedelta(days=1)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def power_status(db, g, u, now=None):
    now = time.time() if now is None else now
    t = effective_title(db, g, u)
    spec = POWER_LIMITS.get(t.name) if t else None
    if not spec:
        return None
    kind, key, lim = spec
    if kind == "day":
        used = day_val(db, g, u, key, game_day(now))
        if used >= lim:
            return {"title": t, "ready": False, "at": next_midnight(now), "left": 0, "max": lim}
        return {"title": t, "ready": True, "at": 0, "left": lim - used, "max": lim}
    left = cd_left(db, g, u, key, lim, now)
    if left > 0:
        return {"title": t, "ready": False, "at": now + left, "left": 0, "max": 1}
    return {"title": t, "ready": True, "at": 0, "left": 1, "max": 1}
