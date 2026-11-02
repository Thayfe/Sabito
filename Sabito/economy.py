import itertools
import random
import time
from collections import Counter, namedtuple

import titles

BANK_RATE = 0.01          # 1% ao dia
BANK_CAP = 1_000_000      # só os primeiros 1.000.000 do banco rendem (rende no máx. 10.000/dia)
PERIOD = 24 * 3600        # um "dia" de rendimento = 24h desde o último rendimento
MAX_PERIODS = 30          # limite de dias acumulados de uma vez (bot ficou offline)

PEC_RATE = 0.05              # imposto diário sobre o patrimônio do top 1
PEC_MIN_WORTH = 1_000_000    # só cobra se o top 1 tiver MAIS que isso (carteira + banco)

WORK_CD, WORK_MIN, WORK_MAX = 3600, 500, 1500
WEEKLY_CD, WEEKLY_AMOUNT = 7 * 24 * 3600, 7_500
CRIME_CD, CRIME_CHANCE = 2 * 3600, 0.50
CRIME_WIN, CRIME_FINE = (2_000, 5_000), (1_000, 3_000)
# Escala com o patrimônio (carteira + banco): sorteia uma % e usa o maior entre o piso e a %, até o teto.
CRIME_WIN_PCT, CRIME_FINE_PCT = (0.02, 0.05), (0.015, 0.04)
CRIME_WIN_CAP, CRIME_FINE_CAP = 15_000, 10_000
SLOTS_MIN, SLOTS_MAX, SLOTS_CD = 100, 10_000, 600

BICO_TEXTS = [
    "Você trabalhou como **entregador de pizza** e driblou o trânsito da hora do almoço",
    "Você fez um **freela de design** e entregou três logos antes do prazo",
    "Você foi **caster de um campeonato amador** e narrou uma partida para 12 espectadores",
    "Você deu **coaching de Elo para um Ferro** e ele finalmente aprendeu a usar ward",
    "Você consertou o **PC de um vizinho** e o problema era só o cabo de energia solto",
    "Você vendeu **skins no mercado paralelo** sem levar ban",
    "Você foi **streamer por algumas horas** e a chat quase lotou (quase)",
    "Você ajudou na **cafeteria do Rift** durante um turno corrido",
    "Você montou **builds para iniciantes** e explicou por que não dá para comprar só dano",
    "Você fez **bico de moderador de servidor** e apagou uma briga de ping",
    "Você **lavou carros** o fim de semana inteiro",
    "Você **passeou com os cachorros** do prédio todo",
    "Você **vendeu brigadeiros** no intervalo da faculdade",
    "Você fez a **faxina de uma LAN house** e achou moedas atrás dos PCs",
    "Você **editou clipes de jogadas** para um streamer",
    "Você deu **aula particular de matemática** para o filho do vizinho",
    "Você **carregou caixas numa mudança** e ainda levou gorjeta",
    "Você **montou um PC gamer** para um amigo e ele pagou pelo trabalho",
]

# Preço para chegar a cada nível (o nível 0 é gratuito).
CLT_PRICES = {1: 5_000, 2: 15_000, 3: 30_000, 4: 75_000, 5: 150_000}
CLT_MAX = max(CLT_PRICES)

# level: nível de CLT necessário | min_works: total de vezes que o usuário já trabalhou (/trabalho)
Job = namedtuple("Job", "key name level min_works pay_min pay_max texts")
JOBS = [
    Job("bico", "Bicos", 0, 0, WORK_MIN, WORK_MAX, BICO_TEXTS),
    Job("barista", "Barista da cafeteria do Rift", 1, 10, 1_000, 2_000,
        [
        "Você foi **barista na cafeteria do Rift** e serviu muito café com leite",
        "Você abriu a cafeteria do Rift como **barista** e fechou com o caixa cheio",
        "Você fez um **latte art do Teemo** e o cliente postou na rede social",
        "Você preparou **cafés para um time inteiro** antes da final",
        "Você inventou o **frappé 'Poro Gelado'** e esgotou o estoque",
        "Você enfrentou a **fila da hora do rush** sem errar um único pedido",
        "Você atendeu um cliente que pediu 'só um cafezinho' e acabou com **doze pedidos extras**",
        "Você fechou o turno com as **gorjetas de uma mesa de jogadores** felizes",
    ]),
    Job("moderador", "Moderador de comunidade", 1, 25, 1_200, 2_500,
        [
        "Você trabalhou como **moderador de uma comunidade grande** e baniu uns trolls",
        "Você passou o turno **moderando o servidor** sem deixar passar spam",
        "Você **apagou uma treta de 300 mensagens** antes que virasse caso de polícia",
        "Você **organizou o evento semanal** da comunidade e ninguém reclamou do horário",
        "Você **configurou o AutoMod** e o spam de 'vendo conta' desapareceu",
        "Você **mediou a discussão sobre o melhor jungler** e saiu como herói",
        "Você **atendeu denúncias** até de madrugada",
        "Você **fixou as regras** pela décima vez e alguém ainda perguntou onde elas estavam",
    ]),
    Job("caster", "Caster de campeonato", 2, 50, 2_000, 3_500,
        [
        "Você foi **caster de um campeonato regional** e narrou uma virada épica",
        "Você **narrou uma final de campeonato** com a voz quase indo embora",
        "Você soltou um **'ACABOU!' perfeito** na hora em que o Nexus caiu",
        "Você fez a **análise ao vivo do draft** e acertou a composição vencedora",
        "Você **improvisou 40 minutos de cobertura** quando o servidor caiu",
        "Você **entrevistou o MVP** da série e arrancou a melhor frase do dia",
        "Você **narrou um pentakill** e a cabine inteira vibrou",
        "Você **segurou a transmissão** durante um pause técnico de 20 minutos",
    ]),
    Job("coach", "Coach de Elo", 2, 100, 2_500, 4_500,
        [
        "Você deu **coaching de Elo** e tirou um jogador do Ferro",
        "Você fez **análise de replay** para um cliente e cobrou caro",
        "Você **corrigiu o posicionamento** de um atirador que morria no minuto 3",
        "Você montou um **plano de wards** que mudou o jogo de um cliente",
        "Você **ensinou last hit** por duas horas e viu o CS subir",
        "Você **revisou 5 derrotas seguidas** e achou o mesmo erro em todas",
        "Você fez uma **sessão de VOD review** com um time inteiro",
        "Você **treinou o mental** de um cliente que tiltava a cada first blood",
    ]),
    Job("streamer", "Streamer profissional", 3, 150, 3_500, 6_000,
        [
        "Você fez uma **live com a chat lotada** e faturou em subs e doações",
        "Você **streamou por horas** e fechou patrocínio no meio da live",
        "Você **venceu uma partida na frente de 5 mil pessoas** e as doações choveram",
        "Você **reagiu ao próprio erro** e o clipe viralizou",
        "Você fez uma **maratona de 12 horas** e o chat te manteve acordado",
        "Você **sorteou uma skin** na live e dobrou a audiência",
        "Você **recebeu uma raid** de um streamer gigante e ganhou 500 seguidores",
        "Você jogou uma **partida com o chat escolhendo as builds** e foi puro caos lucrativo",
    ]),
    Job("analista", "Analista de time", 3, 250, 4_500, 7_500,
        [
        "Você trabalhou como **analista de um time semi-profissional** e montou o scouting",
        "Você **preparou o draft** de um time e acertou todas as bans",
        "Você **destrinchou 30 partidas** do próximo adversário e achou o ponto fraco",
        "Você entregou um **relatório de objetivos** que mudou a estratégia do time",
        "Você **montou o dashboard de estatísticas** e o técnico finalmente leu",
        "Você **previu a rota do jungle** inimigo e o time garantiu o dragão",
        "Você **cruzou dados de 200 jogos** e encontrou o padrão do mid rival",
        "Você apresentou um **plano de bans** que desmontou o campeão favorito deles",
    ]),
    Job("tecnico", "Técnico de time profissional", 4, 400, 7_000, 11_000,
        [
        "Você foi **técnico de um time profissional** e ganhou o bootcamp",
        "Você **liderou a equipe técnica** em uma série decisiva",
        "Você **trocou a composição no intervalo** da série e virou o jogo",
        "Você **segurou o vestiário** depois de uma derrota por 3 a 0 e preparou a revanche",
        "Você **escalou o substituto certo** e ele foi o MVP da série",
        "Você **desenhou a estratégia de early game** que quebrou o time rival",
        "Você **organizou o bootcamp** do time num centro de treino de primeira",
        "Você **pediu tempo técnico** no momento exato e salvou a série",
    ]),
    Job("diretor", "Diretor de e-sports", 5, 600, 10_000, 16_000,
        [
        "Você **fechou um contrato milionário** como diretor de e-sports",
        "Você **administrou uma organização inteira** e fechou o trimestre no verde",
        "Você **negociou a renovação** do melhor jogador do elenco por um preço justo",
        "Você **fechou patrocínio com uma marca global** para o time principal",
        "Você **inaugurou o centro de treinamento** da organização",
        "Você **contratou a nova promessa** do cenário antes dos concorrentes",
        "Você **apresentou o relatório anual** e os investidores sorriram",
        "Você **sediou um torneio internacional** e vendeu todos os ingressos",
    ]),
]
JOBS_BY_KEY = {j.key: j for j in JOBS}
assert len(JOBS_BY_KEY) == len(JOBS), "chaves de emprego duplicadas"


def job_avg(job):
    return (job.pay_min + job.pay_max) / 2


def available_jobs(level, works):
    return [j for j in JOBS if j.level <= level and works >= j.min_works]


def best_job(level, works):
    return max(available_jobs(level, works), key=job_avg)


CRIME_WIN_TEXTS = [
    "Você **hackeou a loja de skins** sem ninguém perceber",
    "Você **vendeu uma conta 'Smurf'** para um desavisado",
    "Você **trapaceou num torneio de bairro** e saiu com o prêmio",
    "Você **revendeu RP falso** no Discord",
    "Você **falsificou ingressos** de uma final e esgotou o lote",
    "Você **aplicou o golpe do boost de Elo** e sumiu antes da entrega",
    "Você **vazou o draft do rival** e vendeu a informação",
    "Você **fraudou um sorteio de skins** a seu favor",
    "Você **combinou o resultado de um amistoso** e apostou na zebra",
    "Você **revendeu convites de um beta fechado** por três vezes o preço",
    "Você **invadiu o servidor de treino** de um time e vendeu os replays",
    "Você vendeu **'coaching' de um Challenger falso**",
]
CRIME_FAIL_TEXTS = [
    "Você foi **pego tentando hackear a loja de skins**",
    "Você foi **denunciado pelo comprador da conta Smurf**",
    "Você foi **descoberto trapaceando no torneio**",
    "Você viu o **golpe do RP falso dar errado**",
    "Você foi **flagrado falsificando ingressos** na entrada do estádio",
    "Você foi **entregue pelo cliente do boost de Elo**",
    "Você foi **desmascarado pelo time rival**, que descobriu o vazamento do draft",
    "Você viu o **sorteio fraudado virar print** na internet",
    "Você foi **descoberto combinando o resultado** do amistoso",
    "Você foi **pego revendendo convites** do beta",
    "Você foi **rastreado pelo time invadido**, que achou seu IP",
    "Você foi **desmascarado ao vivo** vendendo o 'coaching' do Challenger falso",
]

SLOT_SYMBOLS = ["🍒", "🍋", "🔔", "⭐", "💎", "7️⃣"]
SLOT_WEIGHTS = [30, 25, 20, 13, 8, 4]
SLOT_TRIPLE = {"🍒": 5, "🍋": 8, "🔔": 12, "⭐": 25, "💎": 60, "7️⃣": 150}  # 3 iguais; 2 iguais devolve a aposta


def _p_lose():
    tot = sum(SLOT_WEIGHTS)
    p = 0.0
    for a, b, c in itertools.product(range(len(SLOT_WEIGHTS)), repeat=3):
        if len({a, b, c}) == 3:
            p += SLOT_WEIGHTS[a] * SLOT_WEIGHTS[b] * SLOT_WEIGHTS[c] / tot ** 3
    return p


SLOT_P_LOSE = _p_lose()


def init(db):
    for col, ddl in (("bank", "INTEGER DEFAULT 0"), ("bank_min", "INTEGER DEFAULT 0"),
                     ("bank_stamp", "REAL DEFAULT 0"),
                     ("clt_level", "INTEGER DEFAULT 0"), ("work_count", "INTEGER DEFAULT 0")):
        try:
            db.execute("ALTER TABLE users ADD COLUMN {} {}".format(col, ddl))
        except Exception:
            pass
    db.execute("CREATE TABLE IF NOT EXISTS cooldowns(guild_id INTEGER, discord_id INTEGER, kind TEXT, ts REAL,"
               " PRIMARY KEY(guild_id, discord_id, kind))")
    db.execute("CREATE TABLE IF NOT EXISTS pec_log(guild_id INTEGER PRIMARY KEY, day TEXT NOT NULL DEFAULT '')")
    db.execute("CREATE TABLE IF NOT EXISTS cards(guild_id INTEGER, discord_id INTEGER, level INTEGER NOT NULL DEFAULT 0,"
               " sent_day TEXT NOT NULL DEFAULT '', sent INTEGER NOT NULL DEFAULT 0,"
               " recv_day TEXT NOT NULL DEFAULT '', recv INTEGER NOT NULL DEFAULT 0,"
               " PRIMARY KEY(guild_id, discord_id))")
    db.commit()


def parse_amount(text, maximum):
    t = (text or "").strip().lower().replace(" ", "")
    if t in ("tudo", "all", "max", "todo", "toda"):
        return maximum
    mult = 1
    if t.endswith("k"):
        mult, t = 1_000, t[:-1]
    elif t.endswith("m"):
        mult, t = 1_000_000, t[:-1]
    try:
        return int(round(float(t.replace(".", "").replace(",", ".")) * mult))
    except ValueError:
        return None


CARD_PRICE = 10_000          # preço do cartão (nível 0) e valor base do custo de cada nível
CARD_STEP = 10_000           # cada nível: +10k de limite diário e +10k no custo da evolução
CARD_BASE_LIMIT = 10_000     # limite diário de envio e de recebimento no nível 0
CARD_MAX_LEVEL = 10


def card_limit(level):
    return CARD_BASE_LIMIT + CARD_STEP * level


def card_upgrade_cost(level):
    return CARD_PRICE + CARD_STEP * level


def card_status(db, g, u, now=None):
    now = time.time() if now is None else now
    r = db.execute("SELECT level, sent_day, sent, recv_day, recv FROM cards WHERE guild_id=? AND discord_id=?",
                   (g, u)).fetchone()
    if not r:
        return None
    day = titles.game_day(now)
    level, limit = r[0], card_limit(r[0])
    sent = r[2] if r[1] == day else 0
    recv = r[4] if r[3] == day else 0
    nxt = level + 1 if level < CARD_MAX_LEVEL else None
    return {"level": level, "limit": limit, "sent": sent, "recv": recv,
            "send_left": max(0, limit - sent), "recv_left": max(0, limit - recv),
            "next_level": nxt, "next_cost": card_upgrade_cost(nxt) if nxt else None,
            "next_limit": card_limit(nxt) if nxt else None}


def buy_card(db, g, u):
    if card_status(db, g, u):
        return {"ok": False, "error": "Você já tem um cartão. Evolua-o na `/loja`."}
    if _wallet(db, g, u) < CARD_PRICE:
        return {"ok": False, "error": "Saldo insuficiente: o cartão custa **{}** pts.".format(CARD_PRICE)}
    _pay(db, g, u, -CARD_PRICE)
    db.execute("INSERT INTO cards(guild_id, discord_id) VALUES(?,?)", (g, u))
    db.commit()
    return {"ok": True, "level": 0, "cost": CARD_PRICE}


def upgrade_card(db, g, u):
    st = card_status(db, g, u)
    if not st:
        return {"ok": False, "error": "Você ainda não tem um cartão."}
    if st["next_level"] is None:
        return {"ok": False, "error": "Seu cartão já está no nível máximo (**{}**).".format(CARD_MAX_LEVEL)}
    cost = st["next_cost"]
    if _wallet(db, g, u) < cost:
        return {"ok": False, "error": "Saldo insuficiente: evoluir para o nível **{}** custa **{}** pts.".format(
            st["next_level"], cost)}
    _pay(db, g, u, -cost)
    db.execute("UPDATE cards SET level=level+1 WHERE guild_id=? AND discord_id=?", (g, u))
    db.commit()
    return {"ok": True, "level": st["next_level"], "cost": cost, "limit": st["next_limit"]}


def transfer(db, g, src, dst, amount, now=None):
    now = time.time() if now is None else now
    if src == dst:
        return {"ok": False, "error": "Você não pode transferir para si mesmo."}
    if amount <= 0:
        return {"ok": False, "error": "Informe um valor positivo."}
    cs, cd = card_status(db, g, src, now), card_status(db, g, dst, now)
    if not cs:
        return {"ok": False, "error": "Você precisa de um cartão para transferir. Compre na `/loja`."}
    if not cd:
        return {"ok": False, "error": "O destinatário ainda não tem cartão para receber."}
    if _wallet(db, g, src) < amount:
        return {"ok": False, "error": "Saldo insuficiente."}
    if amount > cs["send_left"]:
        return {"ok": False, "error": "Limite diário de envio do seu cartão: restam **{}** pts hoje.".format(cs["send_left"])}
    if amount > cd["recv_left"]:
        return {"ok": False, "error": "O destinatário só pode receber mais **{}** pts hoje (limite do cartão dele).".format(
            cd["recv_left"])}
    day = titles.game_day(now)
    _pay(db, g, src, -amount)
    _pay(db, g, dst, amount)
    db.execute("UPDATE cards SET sent_day=?, sent=? WHERE guild_id=? AND discord_id=?",
               (day, cs["sent"] + amount, g, src))
    db.execute("UPDATE cards SET recv_day=?, recv=? WHERE guild_id=? AND discord_id=?",
               (day, cd["recv"] + amount, g, dst))
    db.commit()
    return {"ok": True, "amount": amount, "send_left": cs["send_left"] - amount, "recv_left": cd["recv_left"] - amount}


def cooldown_left(db, g, u, kind, seconds, now=None):
    now = time.time() if now is None else now
    r = db.execute("SELECT ts FROM cooldowns WHERE guild_id=? AND discord_id=? AND kind=?", (g, u, kind)).fetchone()
    return max(0, (r[0] if r else 0) + seconds - now)


def _set_cd(db, g, u, kind, now):
    db.execute("INSERT INTO cooldowns VALUES(?,?,?,?) ON CONFLICT(guild_id, discord_id, kind)"
               " DO UPDATE SET ts=excluded.ts", (g, u, kind, now))


def _wallet(db, g, u):
    r = db.execute("SELECT balance FROM users WHERE guild_id=? AND discord_id=?", (g, u)).fetchone()
    return r[0] if r else 0


def _pay(db, g, u, delta):
    db.execute("UPDATE users SET balance = balance + ? WHERE guild_id=? AND discord_id=?", (delta, g, u))


def bank_info(db, g, u):
    r = db.execute("SELECT bank, bank_stamp FROM users WHERE guild_id=? AND discord_id=?", (g, u)).fetchone()
    return {"bank": r[0] or 0, "stamp": r[1] or 0} if r else {"bank": 0, "stamp": 0}


def accrue(db, g, u, now=None):
    # não dá commit (quem chama faz)
    now = time.time() if now is None else now
    r = db.execute("SELECT bank, bank_min, bank_stamp FROM users WHERE guild_id=? AND discord_id=?",
                   (g, u)).fetchone()
    if not r:
        return 0
    bank, bmin, stamp = r[0] or 0, r[1] or 0, r[2] or 0
    if bank <= 0 or stamp <= 0:
        db.execute("UPDATE users SET bank_min=?, bank_stamp=? WHERE guild_id=? AND discord_id=?",
                   (bank if stamp <= 0 else 0, now, g, u))
        return 0
    periods = int((now - stamp) // PERIOD)
    if periods <= 0:
        return 0
    rate, cap, risk = titles.bank_params(db, g, u, BANK_RATE, BANK_CAP)

    def _risk(v):
        if risk and random.random() < risk[0]:
            return v - int(v * risk[1])
        return v

    base = min(bmin, bank, cap)
    new = _risk(bank + int(base * rate))
    for _ in range(min(periods, MAX_PERIODS) - 1):
        new = _risk(new + int(min(new, cap) * rate))
    new += titles.reinc_bonus(db, g, u, new - bank)
    new_stamp = now if periods > MAX_PERIODS else stamp + periods * PERIOD
    db.execute("UPDATE users SET bank=?, bank_min=?, bank_stamp=? WHERE guild_id=? AND discord_id=?",
               (new, new, new_stamp, g, u))
    return new - bank


def accrue_all(db, now=None):
    now = time.time() if now is None else now
    rows = db.execute("SELECT guild_id, discord_id FROM users WHERE bank > 0 AND bank_stamp > 0"
                      " AND bank_stamp + ? <= ?", (PERIOD, now)).fetchall()
    out = []
    for g, u in rows:
        gained = accrue(db, g, u, now)
        if gained:
            out.append((g, u, gained))
    db.commit()
    return out


def pec_blusinhas(db, now=None):
    now = time.time() if now is None else now
    today = titles.game_day(now)
    out = []
    for (g,) in db.execute("SELECT DISTINCT guild_id FROM users").fetchall():
        done = db.execute("SELECT day FROM pec_log WHERE guild_id=?", (g,)).fetchone()
        if done and done[0] == today:
            continue
        top = db.execute("SELECT discord_id, balance + COALESCE(bank, 0) AS worth FROM users WHERE guild_id=?"
                         " ORDER BY worth DESC, discord_id LIMIT 1", (g,)).fetchone()
        db.execute("INSERT INTO pec_log(guild_id, day) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET day=excluded.day",
                   (g, today))
        if not top or top[1] <= PEC_MIN_WORTH:
            continue
        u, worth = top[0], top[1]
        tax = int(worth * PEC_RATE)
        charged, from_bank = _fine(db, g, u, tax, now)
        if charged:
            out.append((g, u, charged, worth, from_bank))
    db.commit()
    return out


def deposit(db, g, u, amount, now=None):
    now = time.time() if now is None else now
    interest = accrue(db, g, u, now)
    wallet = _wallet(db, g, u)
    if amount <= 0:
        db.commit()
        return {"ok": False, "error": "Informe um valor positivo.", "interest": interest}
    if amount > wallet:
        db.commit()
        return {"ok": False, "error": "Você só tem **{}** pts na carteira.".format(wallet), "interest": interest}
    bank = bank_info(db, g, u)["bank"]
    if bank <= 0:  # banco vazio: o relógio do rendimento começa agora, com este depósito como base
        db.execute("UPDATE users SET bank_min=?, bank_stamp=? WHERE guild_id=? AND discord_id=?",
                   (amount, now, g, u))
    _pay(db, g, u, -amount)
    db.execute("UPDATE users SET bank = bank + ? WHERE guild_id=? AND discord_id=?", (amount, g, u))
    db.commit()
    return {"ok": True, "amount": amount, "bank": bank + amount, "wallet": wallet - amount, "interest": interest}


def withdraw(db, g, u, amount, now=None):
    now = time.time() if now is None else now
    interest = accrue(db, g, u, now)
    bank = bank_info(db, g, u)["bank"]
    if amount <= 0:
        db.commit()
        return {"ok": False, "error": "Informe um valor positivo.", "interest": interest}
    if amount > bank:
        db.commit()
        return {"ok": False, "error": "Você só tem **{}** pts no banco.".format(bank), "interest": interest}
    db.execute("UPDATE users SET bank = bank - ?, bank_min = MIN(bank_min, bank - ?)"
               " WHERE guild_id=? AND discord_id=?", (amount, amount, g, u))
    _pay(db, g, u, amount)
    db.commit()
    return {"ok": True, "amount": amount, "bank": bank - amount, "wallet": _wallet(db, g, u),
            "interest": interest}


def next_interest_at(db, g, u):
    info = bank_info(db, g, u)
    return info["stamp"] + PERIOD if info["bank"] > 0 and info["stamp"] > 0 else None


def clt_info(db, g, u):
    r = db.execute("SELECT clt_level, work_count FROM users WHERE guild_id=? AND discord_id=?", (g, u)).fetchone()
    return {"level": (r[0] or 0) if r else 0, "works": (r[1] or 0) if r else 0}


def upgrade_clt(db, g, u):
    info = clt_info(db, g, u)
    if info["level"] >= CLT_MAX:
        return {"ok": False, "error": "Você já está no **nível máximo** de CLT."}
    nxt = info["level"] + 1
    price = CLT_PRICES[nxt]
    if _wallet(db, g, u) < price:
        return {"ok": False, "error": "Saldo insuficiente: o CLT nível {} custa **{:,}** pts.".format(
            nxt, price).replace(",", ".")}
    db.execute("UPDATE users SET balance = balance - ?, clt_level = ? WHERE guild_id=? AND discord_id=?",
               (price, nxt, g, u))
    db.commit()
    return {"ok": True, "level": nxt, "price": price, "wallet": _wallet(db, g, u),
            "jobs": [j for j in JOBS if j.level == nxt], "works": info["works"]}


def work(db, g, u, job_key=None, rng=random, now=None):
    now = time.time() if now is None else now
    info = clt_info(db, g, u)
    if job_key:
        job = JOBS_BY_KEY.get(job_key)
        if job is None:
            return {"ok": False, "error": "Emprego não encontrado. Veja a lista em `/empregos`."}
        if job.level > info["level"]:
            return {"ok": False, "error": "**{}** exige **CLT nível {}** (você é nível {}). Compre na `/loja`.".format(
                job.name, job.level, info["level"])}
        if info["works"] < job.min_works:
            return {"ok": False, "error": "**{}** exige **{}** trabalhos feitos (você tem {}).".format(
                job.name, job.min_works, info["works"])}
    else:
        job = best_job(info["level"], info["works"])
    left = cooldown_left(db, g, u, "work", WORK_CD, now)
    if left > 0:
        return {"ok": False, "left": left}
    amount, inc, notes = titles.work_adjust(db, g, u, job, rng.randint(job.pay_min, job.pay_max), now, rng)
    extra = titles.reinc_bonus(db, g, u, amount)
    if extra:
        amount += extra
        notes = list(notes) + ["🔁 Reencarnação +{}".format(extra)]
    db.execute("UPDATE users SET balance = balance + ?, work_count = COALESCE(work_count, 0) + ?"
               " WHERE guild_id=? AND discord_id=?", (amount, inc, g, u))
    _set_cd(db, g, u, "work", now)
    db.commit()
    return {"ok": True, "amount": amount, "text": rng.choice(job.texts), "job": job, "notes": notes,
            "works": info["works"] + inc, "wallet": _wallet(db, g, u)}


def weekly(db, g, u, now=None):
    now = time.time() if now is None else now
    left = cooldown_left(db, g, u, "weekly", WEEKLY_CD, now)
    if left > 0:
        return {"ok": False, "left": left}
    amount = WEEKLY_AMOUNT + titles.reinc_bonus(db, g, u, WEEKLY_AMOUNT)
    _pay(db, g, u, amount)
    _set_cd(db, g, u, "weekly", now)
    db.commit()
    return {"ok": True, "amount": amount, "wallet": _wallet(db, g, u)}


def _fine(db, g, u, amount, now):
    wallet = _wallet(db, g, u)
    from_wallet = min(wallet, amount)
    if from_wallet:
        _pay(db, g, u, -from_wallet)
    from_bank = 0
    rest = amount - from_wallet
    if rest > 0:
        accrue(db, g, u, now)  # mantém bank_min coerente antes de mexer no saldo do banco
        from_bank = min(rest, bank_info(db, g, u)["bank"])
        if from_bank:
            db.execute("UPDATE users SET bank = bank - ?, bank_min = MIN(bank_min, bank - ?)"
                       " WHERE guild_id=? AND discord_id=?", (from_bank, from_bank, g, u))
    return from_wallet + from_bank, from_bank


def _scaled(rng, base, pct, cap, worth):
    return min(cap, max(rng.randint(*base), int(worth * rng.uniform(*pct))))


def crime(db, g, u, rng=random, now=None):
    now = time.time() if now is None else now
    jail = cooldown_left(db, g, u, "jail", 0, now)   # preso: "ts" guarda o dia em que sai
    if jail > 0:
        return {"ok": False, "left": jail, "jailed": True}
    m = titles.crime_mods(db, g, u, CRIME_CHANCE, CRIME_CD)
    left = cooldown_left(db, g, u, "crime", CRIME_CD, now)
    if left > 0:
        return {"ok": False, "left": left}
    from_bank = 0
    jailed = False
    notes = []
    accrue(db, g, u, now)
    worth = _wallet(db, g, u) + bank_info(db, g, u)["bank"]
    if rng.random() < m.chance:
        delta = _scaled(rng, CRIME_WIN, CRIME_WIN_PCT, CRIME_WIN_CAP, worth)
        if m.win_mult != 1:
            delta = int(delta * m.win_mult)
            notes.append("+15% de lucro")
        if m.fixed:
            delta = m.fixed
            notes.append("lucro fixo")
        if m.double and rng.random() < m.double:
            delta *= 2
            notes.append("valor dobrado")
        extra = titles.reinc_bonus(db, g, u, delta)
        if extra:
            delta += extra
            notes.append("🔁 Reencarnação +{}".format(extra))
        text, success = rng.choice(CRIME_WIN_TEXTS), True
        _pay(db, g, u, delta)
    else:
        fine = _scaled(rng, CRIME_FINE, CRIME_FINE_PCT, CRIME_FINE_CAP, worth)
        if m.fine_mult != 1:
            fine = int(fine * m.fine_mult)
            notes.append("multa {:+d}%".format(round((m.fine_mult - 1) * 100)))
        charged, from_bank = _fine(db, g, u, fine, now)
        delta, text, success = -charged, rng.choice(CRIME_FAIL_TEXTS), False
        if m.jail:
            _set_cd(db, g, u, "jail", now + m.jail)
            jailed = True
    # a recarga fica "dentro" do tempo do título (Rei do crime): guarda o ts já deslocado
    _set_cd(db, g, u, "crime", now + m.cd - CRIME_CD)
    db.commit()
    return {"ok": True, "success": success, "delta": delta, "from_bank": from_bank, "text": text,
            "wallet": _wallet(db, g, u), "notes": notes, "jailed": jailed, "jail_until": now + m.jail}


def slots(db, g, u, bet, rng=random, now=None):
    now = time.time() if now is None else now
    m = titles.slot_mods(db, g, u, SLOTS_CD, SLOTS_MIN, SLOTS_MAX)
    if bet < m.min_bet or bet > m.max_bet:
        return {"ok": False, "error": "A aposta no caça-níquel vai de **{}** a **{}** pts.".format(
            m.min_bet, m.max_bet)}
    if _wallet(db, g, u) < bet:
        return {"ok": False, "error": "Saldo insuficiente na carteira."}
    left = cooldown_left(db, g, u, "slots", SLOTS_CD, now)
    if left > 0:
        return {"ok": False, "left": left}
    jackpot_sym = SLOT_SYMBOLS[-1]
    note = ""
    if m.jackpot and rng.random() < m.jackpot:
        reels = [jackpot_sym] * 3
        note = "🌟 Deusa da Vitória: 7️⃣7️⃣7️⃣ garantido!"
    else:
        reels = rng.choices(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, k=3)
        while m.jackpot and reels == [jackpot_sym] * 3:
            reels = rng.choices(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, k=3)
    top, n = Counter(reels).most_common(1)[0]
    mult = SLOT_TRIPLE[top] if n == 3 else (1 if n == 2 else 0)
    if mult == 0 and m.luck and rng.random() < m.luck / SLOT_P_LOSE:
        reels = [reels[0], reels[0], reels[2]]
        mult, n, note = 1, 2, "🍀 Sortudo: a rodada virou um par!"
    if n == 2 and m.pair_mult != 1:
        mult, note = m.pair_mult, "💸 Hobby Caro: par paga +50%!"
    if mult == 0 and m.no_loss:
        mult, note = 1, "💻 Hackeando o Sistema: aposta devolvida."
    payout = int(bet * mult)
    extra = titles.reinc_bonus(db, g, u, payout - bet)
    if extra:
        payout += extra
        note = (note + " " if note else "") + "🔁 Reencarnação +{}".format(extra)
    _pay(db, g, u, payout - bet)
    # recarga "dentro" do tempo do título: guarda o ts deslocado (não dá para trocar de título e furar o tempo)
    _set_cd(db, g, u, "slots", now + m.cd - SLOTS_CD)
    db.commit()
    return {"ok": True, "reels": reels, "mult": mult, "payout": payout, "net": payout - bet, "note": note,
            "wallet": _wallet(db, g, u)}
