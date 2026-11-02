# record_settlement, check_wealth e finalize_months não dão commit (rodam na transação do settle); as demais dão
import random
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import catalog
import titles

TZ = timezone(timedelta(hours=-3))   # dia/mês "de jogo" no horário de Brasília

ALLIN_MIN = 1_000            # All-In só vale com pelo menos isso apostado (evita farm com 1 moeda)
BIG_BET = 100_000
JACKPOT_PAYOUT = 100_000
MASTER_STROKE_MULT = 10.0
ZEBRA_MULT = 5.0
UNDERDOG_ODD = 2.0           # odd do lado > 2.0 = underdog; < 2.0 = favorito (só pool de 2 lados)
RECOVERY_AMOUNT = 100_000

# user: id do discord | stake: total apostado | payout: total creditado (0 se perdeu)
# won: acertou o lado | side_odd: odd do pool para o lado (None se a casa cobriu) | puuid: jogador
BetResult = namedtuple("BetResult", "user stake payout won side_odd puuid")

STAT_FIELDS = [
    "earned", "peak_balance", "total_bets", "wins", "losses",
    "streak", "best_streak", "lose_streak", "best_lose_streak",
    "profit", "profit_trough", "day_key", "day_profit", "best_day_profit",
    "month_key", "month_profit", "month_min",
    "fav_wins", "underdog_wins", "best_target_hits",
    "had_100k", "zero_flag", "boxes_opened", "fragments", "equipped_title", "mimic_title",
]


def init(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS stats(
        guild_id INTEGER, discord_id INTEGER,
        earned INTEGER DEFAULT 0, peak_balance INTEGER DEFAULT 0,
        total_bets INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
        streak INTEGER DEFAULT 0, best_streak INTEGER DEFAULT 0,
        lose_streak INTEGER DEFAULT 0, best_lose_streak INTEGER DEFAULT 0,
        profit INTEGER DEFAULT 0, profit_trough INTEGER DEFAULT 0,
        day_key TEXT DEFAULT '', day_profit INTEGER DEFAULT 0, best_day_profit INTEGER DEFAULT 0,
        month_key TEXT DEFAULT '', month_profit INTEGER DEFAULT 0, month_min INTEGER DEFAULT 0,
        fav_wins INTEGER DEFAULT 0, underdog_wins INTEGER DEFAULT 0, best_target_hits INTEGER DEFAULT 0,
        had_100k INTEGER DEFAULT 0, zero_flag INTEGER DEFAULT 0,
        boxes_opened INTEGER DEFAULT 0, fragments INTEGER DEFAULT 0, equipped_title TEXT DEFAULT '',
        mimic_title TEXT DEFAULT '',
        PRIMARY KEY(guild_id, discord_id));
    CREATE TABLE IF NOT EXISTS achievements(guild_id INTEGER, discord_id INTEGER, key TEXT, unlocked_at REAL,
        PRIMARY KEY(guild_id, discord_id, key));
    CREATE TABLE IF NOT EXISTS titles(guild_id INTEGER, discord_id INTEGER, title_id TEXT, obtained_at REAL,
        PRIMARY KEY(guild_id, discord_id, title_id));
    CREATE TABLE IF NOT EXISTS target_hits(guild_id INTEGER, discord_id INTEGER, puuid TEXT, hits INTEGER,
        PRIMARY KEY(guild_id, discord_id, puuid));
    """)
    try:
        db.execute("ALTER TABLE stats ADD COLUMN mimic_title TEXT DEFAULT ''")
    except Exception:
        pass
    db.commit()


def _row(db, sql, params=()):
    cur = db.execute(sql, params)
    r = cur.fetchone()
    if r is None:
        return None
    return dict(zip([d[0] for d in cur.description], tuple(r)))


def _bal(db, g, u):
    r = db.execute("SELECT balance FROM users WHERE guild_id=? AND discord_id=?", (g, u)).fetchone()
    return r[0] if r else 0


def _net_worth(db, g, u):
    r = db.execute("SELECT balance + COALESCE(bank, 0) FROM users WHERE guild_id=? AND discord_id=?",
                   (g, u)).fetchone()
    return r[0] if r else 0


def get_stats(db, g, u):
    db.execute("INSERT OR IGNORE INTO stats(guild_id, discord_id) VALUES(?,?)", (g, u))
    return _row(db, "SELECT * FROM stats WHERE guild_id=? AND discord_id=?", (g, u))


def _save(db, g, u, s):
    sets = ", ".join(f + "=?" for f in STAT_FIELDS)
    db.execute("UPDATE stats SET " + sets + " WHERE guild_id=? AND discord_id=?",
               [s[f] for f in STAT_FIELDS] + [g, u])


def _grant(db, g, u, key):
    cur = db.execute("INSERT OR IGNORE INTO achievements VALUES(?,?,?,?)", (g, u, key, time.time()))
    return catalog.ACH_BY_KEY[key] if cur.rowcount else None


def _eval_stats(db, g, u, s):
    out = []
    for a in catalog.ACHIEVEMENTS:
        if a.stat and s[a.stat] >= a.need:
            got = _grant(db, g, u, a.key)
            if got:
                out.append(got)
    return out


def _keys(items):
    return [x for x in items if x]


def unlocked_keys(db, g, u):
    return {r[0] for r in db.execute("SELECT key FROM achievements WHERE guild_id=? AND discord_id=?", (g, u))}


def on_bet_placed(db, g, u, game_total, balance_after, is_new):
    s = get_stats(db, g, u)
    out = []
    if is_new:
        s["total_bets"] += 1
        _save(db, g, u, s)
    if balance_after == 0 and game_total >= ALLIN_MIN:
        out.append(_grant(db, g, u, "all_in"))
    if game_total > BIG_BET:
        out.append(_grant(db, g, u, "fearless"))
    out = _keys(out) + _eval_stats(db, g, u, s)
    db.commit()
    return out


def on_earn(db, g, u, amount):
    s = get_stats(db, g, u)
    if amount > 0:
        s["earned"] += amount
        _save(db, g, u, s)
    out = check_wealth(db, g, u, "earn")
    out += _eval_stats(db, g, u, get_stats(db, g, u))
    db.commit()
    return out


on_daily = on_earn


def check_wealth(db, g, u, source, lost=False):
    bal = _net_worth(db, g, u)
    s = get_stats(db, g, u)
    out = []
    s["peak_balance"] = max(s["peak_balance"], bal)
    if bal >= 100_000:
        s["had_100k"] = 1
    if source == "settle" and lost:
        if bal == 0:
            s["zero_flag"] = 1
            out.append(_grant(db, g, u, "from_zero"))
        if s["had_100k"] and bal < 1_000:
            out.append(_grant(db, g, u, "lost_fortune"))
    if s["zero_flag"] and bal >= 100_000:
        out.append(_grant(db, g, u, "phoenix"))
    _save(db, g, u, s)
    return _keys(out) + _eval_stats(db, g, u, s)


def record_settlement(db, g, results, now=None):
    now = now or datetime.now(TZ)
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    unlocked = []

    for r in results:
        u = r.user
        s = get_stats(db, g, u)
        got = []
        net = r.payout - r.stake

        if s["month_key"] != month:
            if s["month_key"] and s["month_profit"] > 0 and s["month_min"] < 0:
                got.append(_grant(db, g, u, "survivor"))
            s["month_key"], s["month_profit"], s["month_min"] = month, 0, 0
        if s["day_key"] != day:
            s["day_key"], s["day_profit"] = day, 0

        s["profit"] += net
        s["profit_trough"] = min(s["profit_trough"], s["profit"])
        s["day_profit"] += net
        s["best_day_profit"] = max(s["best_day_profit"], s["day_profit"])
        s["month_profit"] += net
        s["month_min"] = min(s["month_min"], s["month_profit"])
        if s["profit_trough"] < 0 and s["profit"] - s["profit_trough"] >= RECOVERY_AMOUNT:
            got.append(_grant(db, g, u, "recovery"))

        if r.won:
            mult = r.payout / r.stake
            s["wins"] += 1
            s["earned"] += max(net, 0)
            s["streak"] += 1
            s["best_streak"] = max(s["best_streak"], s["streak"])
            s["lose_streak"] = 0
            if r.payout > JACKPOT_PAYOUT:
                got.append(_grant(db, g, u, "jackpot"))
            if mult >= MASTER_STROKE_MULT:
                got.append(_grant(db, g, u, "master_stroke"))
            if r.side_odd is not None:
                if r.side_odd > UNDERDOG_ODD:
                    s["underdog_wins"] += 1
                    if mult >= ZEBRA_MULT:
                        got.append(_grant(db, g, u, "zebra"))
                elif r.side_odd < UNDERDOG_ODD:
                    s["fav_wins"] += 1
            if r.puuid:
                db.execute("INSERT INTO target_hits VALUES(?,?,?,1) ON CONFLICT(guild_id, discord_id, puuid)"
                           " DO UPDATE SET hits = hits + 1", (g, u, r.puuid))
                hits = db.execute("SELECT hits FROM target_hits WHERE guild_id=? AND discord_id=? AND puuid=?",
                                  (g, u, r.puuid)).fetchone()[0]
                s["best_target_hits"] = max(s["best_target_hits"], hits)
        else:
            s["losses"] += 1
            s["lose_streak"] += 1
            s["best_lose_streak"] = max(s["best_lose_streak"], s["lose_streak"])
            s["streak"] = 0

        _save(db, g, u, s)
        got = _keys(got) + _eval_stats(db, g, u, s)
        got += check_wealth(db, g, u, "settle", lost=net < 0)
        unlocked += [(u, a) for a in got]
    return unlocked


def finalize_months(db, now=None):
    now = now or datetime.now(TZ)
    month = now.strftime("%Y-%m")
    out = []
    rows = db.execute("SELECT guild_id, discord_id, month_profit, month_min FROM stats"
                      " WHERE month_key != '' AND month_key != ?", (month,)).fetchall()
    for g, u, mp, mm in rows:
        if mp > 0 and mm < 0:
            a = _grant(db, g, u, "survivor")
            if a:
                out.append((g, u, a))
        db.execute("UPDATE stats SET month_key=?, month_profit=0, month_min=0 WHERE guild_id=? AND discord_id=?",
                   (month, g, u))
    return out


def owned_titles(db, g, u):
    return {r[0] for r in db.execute("SELECT title_id FROM titles WHERE guild_id=? AND discord_id=?", (g, u))}


def _open_box(db, g, u, box_key, rng):
    t = catalog.roll_box(box_key, rng)
    if titles.reinc_level(db, g, u) >= len(catalog.REINC_IDS):     # Sétima Reencarnação bloqueia o World Requiem
        for _ in range(100):
            if t.id != catalog.WORLD_REQUIEM_ID:
                break
            t = catalog.roll_box(box_key, rng)
        else:
            t = rng.choice(catalog.DROP_BY_RARITY[0])
    s = get_stats(db, g, u)
    has = db.execute("SELECT 1 FROM titles WHERE guild_id=? AND discord_id=? AND title_id=?",
                     (g, u, t.id)).fetchone()
    gained = 0
    if has:
        gained = titles.frag_value(db, g, u, catalog.FRAGMENT_VALUE[t.rarity])
        s["fragments"] += gained
    else:
        db.execute("INSERT INTO titles VALUES(?,?,?,?)", (g, u, t.id, time.time()))
    s["boxes_opened"] += 1
    _save(db, g, u, s)
    return {"ok": True, "title": t, "duplicate": bool(has), "fragments_gained": gained}


def buy_box(db, g, u, box_key, rng=random):
    box = catalog.BOXES[box_key]
    price = titles.box_price(db, g, u, box["price"])
    if _bal(db, g, u) < price:
        return {"ok": False, "error": "Saldo insuficiente: a {} custa **{:,}** pts.".format(
            box["name"], price).replace(",", ".")}
    db.execute("UPDATE users SET balance = balance - ? WHERE guild_id=? AND discord_id=?",
               (price, g, u))
    res = _open_box(db, g, u, box_key, rng)
    res["price"] = price
    res["refund"] = titles.box_refund(db, g, u, box_key, price, res["duplicate"])
    if res["refund"]:
        db.execute("UPDATE users SET balance = balance + ? WHERE guild_id=? AND discord_id=?",
                   (res["refund"], g, u))
    db.commit()
    return res


def exchange_fragments(db, g, u, box_key, rng=random):
    box = catalog.BOXES[box_key]
    s = get_stats(db, g, u)
    if s["fragments"] < box["frags"]:
        return {"ok": False, "error": "Fragmentos insuficientes: a {} custa **{}** (você tem {}).".format(
            box["name"], box["frags"], s["fragments"])}
    s["fragments"] -= box["frags"]
    _save(db, g, u, s)
    res = _open_box(db, g, u, box_key, rng)
    db.commit()
    return res


EQUIP_CD = 24 * 3600     # trocar (ou tirar) o título equipado só a cada 24h


def change_title(db, g, u, title_id, now=None):
    now = time.time() if now is None else now
    title_id = title_id or ""
    if title_id and title_id not in owned_titles(db, g, u):
        return {"ok": False, "error": "Você não possui esse título."}
    if title_id in catalog.PASSIVE_IDS:
        return {"ok": False, "error": "🔁 Reencarnações são passivas: valem sempre, sem precisar equipar."}
    if title_id == catalog.WORLD_REQUIEM_ID:
        return {"ok": False, "error": "O World Requiem só é ativado pela confirmação do `/equipar`."}
    if get_stats(db, g, u)["equipped_title"] == title_id:
        return {"ok": True, "changed": False}
    left = titles.cd_left(db, g, u, "cd_equip", EQUIP_CD, now)
    if left > 0:
        return {"ok": False, "left": left,
                "error": "⏳ Você só pode trocar de título a cada 24h. Próxima troca <t:{}:R>.".format(
                    int(now + left))}
    equip(db, g, u, title_id)
    titles.cd_set(db, g, u, "cd_equip", now)
    db.commit()
    return {"ok": True, "changed": True}


MIMIC_CD = 7 * 24 * 3600     # recarga para o O Viajante trocar o título imitado


def change_mimic(db, g, u, target_id, now=None):
    now = time.time() if now is None else now
    cur = titles.mimic_of(db, g, u)
    if cur and cur.id == target_id:
        return {"ok": True, "same": True}
    if cur:
        left = titles.cd_left(db, g, u, "cd_imitar", MIMIC_CD, now)
        if left > 0:
            return {"ok": False, "left": left,
                    "error": "⏳ O Viajante só pode trocar o título imitado a cada 7 dias. "
                             "Próxima troca <t:{}:R>.".format(int(now + left))}
    if not titles.set_mimic(db, g, u, target_id):
        return {"ok": False, "error": "Você precisa ter o título **O Viajante** para imitar."}
    titles.cd_set(db, g, u, "cd_imitar", now)
    db.commit()
    return {"ok": True, "next": now + MIMIC_CD}


def world_requiem_targets(db, g, u):
    uids = {r[0] for r in db.execute("SELECT discord_id FROM accounts WHERE guild_id=?", (g,))}
    uids.add(u)
    immune = sorted(v for v in uids if v != u and titles.is_immune(db, g, v))
    return sorted(uids - set(immune)), immune


def world_requiem(db, g, u, start_balance=1000, now=None):
    now = time.time() if now is None else now
    if catalog.WORLD_REQUIEM_ID not in owned_titles(db, g, u):
        return {"ok": False, "error": "Você não possui o World Requiem."}
    level = titles.reinc_level(db, g, u)
    if level >= len(catalog.REINC_IDS):
        return {"ok": False, "error": "Você já tem a **Sétima Reencarnação**: o World Requiem está bloqueado para você."}
    new = catalog.TITLES_BY_ID[catalog.REINC_IDS[level]]
    uids, immune = world_requiem_targets(db, g, u)
    keep = list(catalog.REINC_IDS)
    marks = ",".join("?" * len(keep))
    try:
        for v in uids:
            db.execute("INSERT OR IGNORE INTO users(guild_id, discord_id, balance) VALUES(?,?,?)",
                       (g, v, start_balance))
            db.execute("UPDATE users SET balance=?, bank=0, bank_min=0, bank_stamp=0"
                       " WHERE guild_id=? AND discord_id=?", (start_balance, g, v))
            db.execute("INSERT OR IGNORE INTO stats(guild_id, discord_id) VALUES(?,?)", (g, v))
            db.execute("UPDATE stats SET equipped_title='', mimic_title='' WHERE guild_id=? AND discord_id=?", (g, v))
            if v == u:
                db.execute("DELETE FROM titles WHERE guild_id=? AND discord_id=?", (g, v))
            else:
                db.execute("DELETE FROM titles WHERE guild_id=? AND discord_id=? AND title_id NOT IN (%s)" % marks,
                           (g, v, *keep))
        db.execute("INSERT INTO titles VALUES(?,?,?,?)", (g, u, new.id, now))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"ok": True, "title": new, "level": level + 1, "accounts": len(uids), "immune": len(immune)}


def find_owned_title(db, g, u, text):
    text = (text or "").strip().lower()
    for tid in owned_titles(db, g, u):
        t = catalog.TITLES_BY_ID.get(tid)
        if t and (tid == text or t.name.lower() == text):
            return t
    return None


def equip(db, g, u, title_id):
    if title_id and title_id not in owned_titles(db, g, u):
        return False
    s = get_stats(db, g, u)
    s["equipped_title"] = title_id or ""
    _save(db, g, u, s)
    db.commit()
    return True


# Reembolso em moedas por raridade ANTIGA (índice 0..5 = Comum..Mítico) de cada título que deixou de existir.
REFUND_COINS = [3_500, 10_000, 50_000, 250_000, 550_000, 5_000_000]
LEGACY_MIMIC = "5:misterio"          # antigo "???" -> vira "O Viajante" (mesmo poder), sem reembolso
MIGRATION_NAME = "titles_v2"


def migrate_titles(db):
    db.execute("CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY, ts REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS titles_removed(guild_id INTEGER, discord_id INTEGER, title_id TEXT,"
               " refund INTEGER, ts REAL)")
    if db.execute("SELECT 1 FROM migrations WHERE name=?", (MIGRATION_NAME,)).fetchone():
        return {}
    now = time.time()
    summary = {}
    rows = db.execute("SELECT guild_id, discord_id, title_id, obtained_at FROM titles").fetchall()
    for r in rows:
        g, u, tid, got = r[0], r[1], r[2], r[3]
        if tid in catalog.TITLES_BY_ID:
            continue
        db.execute("DELETE FROM titles WHERE guild_id=? AND discord_id=? AND title_id=?", (g, u, tid))
        if tid == LEGACY_MIMIC:
            db.execute("INSERT OR IGNORE INTO titles VALUES(?,?,?,?)", (g, u, catalog.MIMIC_ID, got))
            db.execute("UPDATE stats SET equipped_title=? WHERE guild_id=? AND discord_id=? AND equipped_title=?",
                       (catalog.MIMIC_ID, g, u, tid))
            continue
        try:
            rarity = min(int(tid.split(":")[0]), len(REFUND_COINS) - 1)
        except ValueError:
            rarity = 0
        refund = REFUND_COINS[rarity]
        db.execute("INSERT OR IGNORE INTO users(guild_id, discord_id, balance) VALUES(?,?,0)", (g, u))
        db.execute("UPDATE users SET balance = balance + ? WHERE guild_id=? AND discord_id=?", (refund, g, u))
        db.execute("INSERT INTO titles_removed VALUES(?,?,?,?,?)", (g, u, tid, refund, now))
        n, coins = summary.get((g, u), (0, 0))
        summary[(g, u)] = (n + 1, coins + refund)
    valid = tuple(catalog.TITLES_BY_ID)
    marks = ",".join("?" * len(valid))
    db.execute("UPDATE stats SET equipped_title='' WHERE equipped_title!='' AND equipped_title NOT IN (%s)" % marks, valid)
    db.execute("UPDATE stats SET mimic_title='' WHERE mimic_title!='' AND mimic_title NOT IN (%s)" % marks, valid)
    db.execute("UPDATE stats SET equipped_title='' WHERE equipped_title!='' AND NOT EXISTS ("
               " SELECT 1 FROM titles t WHERE t.guild_id=stats.guild_id AND t.discord_id=stats.discord_id"
               " AND t.title_id=stats.equipped_title)")
    db.execute("INSERT INTO migrations VALUES(?,?)", (MIGRATION_NAME, now))
    db.commit()
    return summary
