import random
import re
import unicodedata
from collections import namedtuple

RARITY_NAMES = ["Comum", "Incomum", "Raro", "Épico", "Lendário", "Mítico", "Secreto"]
RARITY_EMOJI = ["⚪", "🟢", "🔵", "🟣", "🟠", "🔴", "⚫"]
RARITY_COLORS = [0x95A5A6, 0x2ECC71, 0x3498DB, 0x9B59B6, 0xE67E22, 0xE74C3C, 0x23272A]
N_RARITIES = len(RARITY_NAMES)

# Fragmentos recebidos quando o título sorteado já é seu.
FRAGMENT_VALUE = [10, 25, 60, 150, 400, 1000, 2500]

# weights: chance (em %) de cada raridade, na ordem de RARITY_NAMES.
BOXES = {
    "basica": {"name": "Caixa Básica", "emoji": "📦", "price": 5_000,
               "weights": [70, 25, 5, 0, 0, 0, 0], "frags": 100},
    "premium": {"name": "Caixa Premium", "emoji": "🎁", "price": 25_000,
                "weights": [40, 35, 20, 5, 0, 0, 0], "frags": 500},
    "lendaria": {"name": "Caixa Lendária", "emoji": "💎", "price": 100_000,
                 "weights": [0, 20.02, 40.04, 30.03, 9.01, 0.8, 0.1], "frags": 1500},
}

Title = namedtuple("Title", "id name rarity desc")

# (nome, descrição do poder). O ID vem do nome (não renomeie: quem já tem o título perderia o vínculo).
# Os poderes são implementados em titles.py; mantenha as descrições em sincronia com ele.
_TITLE_DEFS = {
    0: [
        ("Primeira Reencarnação", "Passivo (vale sem equipar, outro título pode ficar equipado): +10% em todo dinheiro ganho."),
        ("Apostador", "Ao vencer uma aposta, recebe +5% do que apostou de volta."),
        ("Sequencialista", "Resgatar o /daily com uma sequência de 2 ou mais concede +5% de moedas bônus, que escala 1% a cada dia de sequência até o máximo de 10%."),
        ("Espectador", "Recebe +1000 moedas se não tiver nenhuma aposta realizada no dia."),
        ("Medroso", "Ao vencer uma aposta em partida em um time favorito, recebe +5% de lucro."),
        ("Analista", "A cada 3 apostas ganhas, recebe +1000 moedas."),
        ("Solo", "Se vencer uma aposta em que só você apostou no time, recebe 7,5% de lucro."),
        ("Trabalhador", "Ao executar o comando /trabalho, possui 10% de chance de ganhar +25% de salário."),
        ("Viciado", "A cada 5 apostas realizadas, ganha um reembolso de até 1k do valor da 6ª aposta."),
        ("Riquinho", "Suas apostas contam com um bônus de 5% a mais na pool."),
        ("Competidor", "Ao vencer uma aposta onde os dois lados investiram dinheiro, recebe +500 moedas."),
        ("Bronze", "Ao vencer uma aposta em partida ranqueada sua, ganha +1000 moedas."),
        ("Mercador", "Comprar Caixas na /loja custa 5% a menos."),
    ],
    1: [
        ("Segunda Reencarnação", "Passivo (vale sem equipar, outro título pode ficar equipado): +20% em todo dinheiro ganho."),
        ("Sortudo", "No caça-níquel (/slots), possui +5% de chance de obter combinações pagadoras."),
        ("Azarado", "Ao perder uma aposta, possui 20% de chance de perder +20% do que apostou, mas caso vença tem 20% de chance de ganhar +20%."),
        ("Sonâmbulo", "Apostas e trabalhos feitos entre 23h e 04h rendem +15% de lucro/pagamento."),
        ("Banqueiro", "Rende 2% de juros no banco."),
        ("Corajoso", "Apostas acima de 10.000 moedas dão +10% de lucro ao vencer."),
        ("Ladrão", "Ao cometer um /crime com sucesso, obtém +15% de lucro extra, mas a multa também é +15%."),
        ("Tarado", "Acertar 3 apostas seguidas em partidas do mesmo jogador concede +3.500 moedas (não conta a si mesmo)."),
        ("Predador", "Pode anular a aposta de um adversário uma vez por dia. Botão ✨ Poder."),
        ("Colecionador de Títulos", "Títulos repetidos rendem +20% de fragmentos bônus."),
        ("Visionário", "Se vencer a 1ª aposta do dia, recebe +50% de lucro."),
        ("Além das Odds", "A odd da casa em partidas não ranqueadas são 2x e em ranqueadas 2,5x."),
    ],
    2: [
        ("Irmão", "Jogar uma partida com outro Irmão no mesmo time aumenta em 10% o lucro da sua aposta nela, +5% por cada irmão extra (máximo de 25%)."),
        ("Terceira Reencarnação", "Passivo (vale sem equipar, outro título pode ficar equipado): +30% em todo dinheiro ganho."),
        ("Fã de Gacha", "A cada 5 vitórias em apostas, recebe +60 fragmentos de caixa."),
        ("Ousado", "Apostas acima de 30.000 moedas dão +30% de lucro ao vencer."),
        ("Investidor de Alto Risco", "Cada rendimento tem juros de 5% do valor depositado, mas também existe uma chance de 5% de perder 10% do valor total."),
        ("Político", "Se o /crime falhar, a multa cobrada é reduzida em 25%."),
        ("Gostos Parecidos", "Ao abrir Caixas na /loja, caso venha título repetido uma vez por dia é reembolsado (não funciona para caixas premium)."),
        ("Escravo", "Realizou 18 trabalhos no mesmo dia? O último trabalho do dia paga +200% de salário."),
        ("Rei do crime", "Reduz o tempo de recarga do comando /crime de 2h para 1h e 45 minutos."),
        ("Hobby Caro", "No /slots, se tirar 2 símbolos iguais (par), ganha +50% do valor apostado em vez de apenas devolver a stake."),
        ("Carioca da Gema", "O sucesso no comando /crime possui 10% de chance de dobrar o valor roubado."),
        ("Talento Inato", "Enquanto usar esse título cada trabalho que realizar aumenta em +2 o contador para requisitos de CLT."),
        ("O Incerto", "Sua aposta é sempre aleatória, mas rende 60% mais."),
        ("O Contrário", "Vencer apostando no lado com menor quantidade de apostadores concede +30% de lucro."),
        ("O Imprudente", "Todo All-In coloca a odd em 3x para você (2 vezes por dia)."),
    ],
    3: [
        ("Quarta Reencarnação", "Passivo (vale sem equipar, outro título pode ficar equipado): +40% em todo dinheiro ganho."),
        ("Caçador de Odds", "Uma vez por dia consegue subir a odd para 5x, pode ser feito enquanto as apostas não foram encerradas. Botão ✨ Poder."),
        ("Sombra", "1x por dia, pode impedir todos os jogadores de apostar na sua partida menos você. Botão ✨ Poder."),
        ("Sem alma", "A cada dia de daily em sequência aumenta em 2% o valor ganho em até um máximo de 1000%."),
        ("Karl Marx", "Aumenta em +100% o ganho em todos os empregos e bicos do /trabalho."),
        ("Mal Perdedor", "Ao perder uma aposta, possui 15% de chance de não perder nada (stake 100% devolvida)."),
        ("Quero perder todo meu dinheiro, não aguento mais", "O tempo de espera do caça-níquel (/slots) é reduzido de 10 para 1 minuto, mas slots tem o mínimo de 1k."),
        ("Assassino em série", "A taxa de sucesso do comando /crime sobe de 50% para 90%, mas se for pego é incapaz de usar /crime por 1 semana enquanto está preso."),
        ("Vitorioso", "A cada sequência de 5 vitórias seguidas em apostas, recebe +10.000 moedas."),
    ],
    4: [
        ("Quinta Reencarnação", "Passivo (vale sem equipar, outro título pode ficar equipado): +50% em todo dinheiro ganho."),
        ("Manipulador", "1x a cada 24h, ao invés de perder uma aposta, você trata ela como vitória e lucra até 50% do valor apostado."),
        ("Lenda Viva", "A cada 5 vitórias em apostas, recebe +7.500 moedas e +100 fragmentos."),
        ("Imortal", "Imune aos poderes que outros jogadores usam contra você (Predador, Sombra, Odeio perder, Judas, /pai e World Requiem)."),
        ("Senhor do Destino", "1x por dia, torna a partida uma vitória para todos os jogadores envolvidos nela. Botão ✨ Poder."),
        ("Acionista", "O rendimento diário do /banco passa a calcular juros sobre até 15.000.000 de moedas."),
        ("Magnata", "Ao vencer uma aposta em uma partida que não é sua, possui 30% de chance de dobrar o lucro."),
        ("O Incontestável", "Ao atingir uma sequência de 10 vitórias seguidas em apostas, ganha +60.000 moedas."),
        ("Dono dos meios de produção", "Todos os trabalhos executados no /trabalho pagam sempre o valor máximo da faixa do emprego e recebe os benefícios do título \"Karl Marx\"."),
        ("Odeio perder", "Se estiver em uma aposta contra outros jogadores, caso perca pode fazer todos perderem uma vez por dia. Botão ✨ Poder (arme antes do fechamento)."),
    ],
    5: [
        ("Sexta Reencarnação", "Passivo (vale sem equipar, outro título pode ficar equipado): +60% em todo dinheiro ganho."),
        ("Deusa da Vitória", "No /slots, possui 1% de chance de tirar 7️⃣7️⃣7️⃣ (150x) e o limite de aposta sobe para 50.000."),
        ("Midas", "Ao vencer uma aposta, possui 50% de chance de dobrar o lucro total."),
        ("Hackeando o Sistema", "No slot você não perde mais dinheiro, apenas tem a aposta devolvida, contudo o cooldown aumenta para 30min."),
        ("Tive azar e peguei esse título mítico", "Todas as caixas de lootbox compradas na /loja recebem 50% de desconto."),
        ("Judas", "Uma vez por dia, caso vença uma aposta, rouba o lucro que todos os vencedores receberam e quem venceu junto a você não recebe nada."),
        ("13", "1x por dia, coloca a odd da sua aposta em 13x. Botão ✨ Poder."),
        ("Quer ser o meu pai?", "Uma vez por semana, use /pai para se tornar filho de alguém por 1 dia: ele deve te pagar uma pensão igual a 30% do SEU patrimônio."),
    ],
    6: [
        ("Sétima Reencarnação", "Passivo (vale sem equipar, outro título pode ficar equipado): +70% em todo dinheiro ganho. Bloqueia o World Requiem para você."),
        ("World Requiem", "Ao ser equipado (com confirmação), reseta o saldo e os títulos de todas as contas vinculadas do servidor (menos os Imortais), inclusive a sua, e você ganha a próxima Reencarnação (+10% de dinheiro ganho por nível). Bloqueado para quem tem a Sétima Reencarnação."),
        ("O Viajante", "Permite imitar a habilidade passiva ou ativa de qualquer um dos outros títulos (escolhido via /imitar). Trocar o título imitado tem recarga de 1 semana."),
    ],
}


# Títulos que mudaram de raridade mantêm o ID antigo (o ID leva a raridade de quando foi criado),
# senão quem já tem o título perderia o vínculo. O prefixo do ID não é a raridade atual desses dois.
_ID_OVERRIDE = {
    "Manipulador": "3:manipulador",        # hoje Lendário
    "Além das Odds": "4:alem_das_odds",    # hoje Incomum
    "Caçador de Odds": "1:cacador_de_odds",  # hoje Épico
}


def _slug(text):
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s or "misterio"


TITLES = []
for _r, _defs in _TITLE_DEFS.items():
    for _n, _d in _defs:
        TITLES.append(Title(_ID_OVERRIDE.get(_n, "{}:{}".format(_r, _slug(_n))), _n, _r, _d))
TITLES_BY_ID = {t.id: t for t in TITLES}
assert len(TITLES_BY_ID) == len(TITLES), "IDs de título duplicados"
MIMIC_ID = next(t.id for t in TITLES if t.name == "O Viajante")   # o único título que imita os outros
TITLES_BY_RARITY = {r: [t for t in TITLES if t.rarity == r] for r in range(N_RARITIES)}

# Reencarnações: passivas, uma por nível (Primeira..Sétima), nunca caem em caixas e não se equipam.
REINC_NAMES = ["Primeira Reencarnação", "Segunda Reencarnação", "Terceira Reencarnação", "Quarta Reencarnação",
               "Quinta Reencarnação", "Sexta Reencarnação", "Sétima Reencarnação"]
REINC_IDS = [next(t.id for t in TITLES if t.name == n) for n in REINC_NAMES]    # índice = nível - 1
PASSIVE_IDS = set(REINC_IDS)
WORLD_REQUIEM_ID = next(t.id for t in TITLES if t.name == "World Requiem")
# O Viajante também não imita nenhum título Secreto.
NOT_IMITABLE = PASSIVE_IDS | {WORLD_REQUIEM_ID, MIMIC_ID} | {t.id for t in TITLES if t.rarity == N_RARITIES - 1}
DROP_BY_RARITY = {r: [t for t in v if t.id not in PASSIVE_IDS] for r, v in TITLES_BY_RARITY.items()}


def roll_box(box_key, rng=random):
    weights = BOXES[box_key]["weights"]
    rarity = rng.choices(range(N_RARITIES), weights=weights)[0]
    return rng.choice(DROP_BY_RARITY[rarity])


# stat/need: conquistas "declarativas" (liberam quando stats[stat] >= need).
# Sem stat: liberadas por eventos específicos em progress.py.
Ach = namedtuple("Ach", "key cat name desc rarity stat need")

CATEGORIES = [
    ("wealth", "💰", "Patrimônio"),
    ("bets", "🎯", "Apostas"),
    ("profit", "📈", "Lucro"),
    ("streak", "🔥", "Sequências"),
    ("lol", "⚔️", "LoL"),
]


def _a(key, cat, name, desc, rarity, stat=None, need=None):
    return Ach(key, cat, name, desc, rarity, stat, need)


ACHIEVEMENTS = [
    _a("first_salary", "wealth", "Primeiro Salário", "Ganhe suas primeiras 1.000 moedas", 0, "earned", 1_000),
    _a("good_start", "wealth", "Começando Bem", "Acumule 10.000 moedas", 0, "peak_balance", 10_000),
    _a("five_digits", "wealth", "Cinco Dígitos", "Acumule 50.000 moedas", 1, "peak_balance", 50_000),
    _a("from_zero", "wealth", "Do Zero", "Chegar a 0 moedas perdendo uma aposta", 1),
    _a("magnate", "wealth", "Magnata", "Acumule 100.000 moedas", 2, "peak_balance", 100_000),
    _a("lost_fortune", "wealth", "Fortuna Perdida", "Ter 100.000 moedas e cair para menos de 1.000", 2),
    _a("millionaire", "wealth", "Milionário", "Acumule 1.000.000 moedas", 3, "peak_balance", 1_000_000),
    _a("phoenix", "wealth", "Fênix", "Chegar a 0 e depois acumular 100.000", 3),
    _a("billionaire", "wealth", "Bilionário", "Acumule 1.000.000.000 moedas", 5, "peak_balance", 1_000_000_000),
    _a("first_bet", "bets", "Primeira Aposta", "Fazer sua primeira aposta", 0, "total_bets", 1),
    _a("bets_50", "bets", "Apostador Frequente", "Fazer 50 apostas", 1, "total_bets", 50),
    _a("hot_hand", "bets", "Mão Quente", "Acertar 5 apostas consecutivas", 1, "best_streak", 5),
    _a("bad_luck", "bets", "Má Sorte", "Errar 10 apostas consecutivas", 1, "best_lose_streak", 10),
    _a("all_in", "bets", "All-In", "Apostar todo o seu saldo (mín. 1.000) em uma partida", 2),
    _a("fearless", "bets", "Sem Medo", "Fazer uma aposta acima de 100.000", 2),
    _a("bets_500", "bets", "Veterano", "Fazer 500 apostas", 3, "total_bets", 500),
    _a("unstoppable", "bets", "Imparável", "Acertar 10 apostas consecutivas", 3, "best_streak", 10),
    _a("jackpot", "bets", "Jackpot", "Ganhar uma aposta que pague mais de 100.000", 3),
    _a("bets_1000", "bets", "Viciado em Apostas", "Fazer 1.000 apostas", 4, "total_bets", 1_000),
    _a("master_stroke", "bets", "Golpe de Mestre", "Ganhar uma aposta com odd de 10x ou mais", 4),
    _a("absolute_precision", "bets", "Precisão Absoluta", "Acertar 20 apostas consecutivas", 5, "best_streak", 20),
    _a("first_profit", "profit", "Primeiro Lucro", "Terminar uma aposta com lucro", 0, "wins", 1),
    _a("plus_10k", "profit", "+10K", "Obter 10.000 de lucro acumulado", 1, "profit", 10_000),
    _a("plus_100k", "profit", "+100K", "Obter 100.000 de lucro acumulado", 2, "profit", 100_000),
    _a("lucky_day", "profit", "Dia de Sorte", "Lucrar 50.000 em um único dia", 2, "best_day_profit", 50_000),
    _a("survivor", "profit", "Sobrevivente", "Terminar o mês com lucro após ter passado pelo prejuízo", 2),
    _a("recovery", "profit", "Recuperação", "Recuperar 100.000 após estar no prejuízo", 3),
    _a("investor", "profit", "Investidor", "Obter 1.000.000 de lucro acumulado", 4, "profit", 1_000_000),
    _a("historic_day", "profit", "Dia Histórico", "Lucrar 500.000 em um único dia", 4, "best_day_profit", 500_000),
    _a("streak_2", "streak", "2 em 2", "2 vitórias consecutivas", 0, "best_streak", 2),
    _a("streak_3", "streak", "Hat-trick", "3 vitórias consecutivas", 1, "best_streak", 3),
    _a("streak_5", "streak", "Em Chamas", "5 vitórias consecutivas", 2, "best_streak", 5),
    _a("streak_10", "streak", "Não Erra", "10 vitórias consecutivas", 3, "best_streak", 10),
    _a("streak_15", "streak", "Máquina", "15 vitórias consecutivas", 4, "best_streak", 15),
    _a("streak_25", "streak", "Lenda", "25 vitórias consecutivas", 4, "best_streak", 25),
    _a("streak_50", "streak", "Imortal", "50 vitórias consecutivas", 5, "best_streak", 50),
    _a("rift_connoisseur", "lol", "Conhecedor do Rift", "Apostar em 10 partidas", 0, "total_bets", 10),
    _a("analyst", "lol", "Analista", "Acertar 10 vencedores", 1, "wins", 10),
    _a("meta_reader", "lol", "Leitor de Meta", "Acertar 10 partidas de um mesmo jogador", 2, "best_target_hits", 10),
    _a("anti_underdog", "lol", "Anti-Underdog", "Acertar 10 apostas em favoritos", 2, "fav_wins", 10),
    _a("zebra_hunter", "lol", "Caçador de Zebras", "Acertar 10 apostas em underdogs", 3, "underdog_wins", 10),
    _a("zebra", "lol", "Zebra", "Ganhar uma aposta em um underdog com odd de 5x ou mais", 3),
    _a("oracle", "lol", "Oracle", "Acertar 50 vencedores", 3, "wins", 50),
    _a("prophet", "lol", "Profeta", "Acertar 100 vencedores", 4, "wins", 100),
]
ACH_BY_KEY = {a.key: a for a in ACHIEVEMENTS}
assert len(ACH_BY_KEY) == len(ACHIEVEMENTS), "chaves de conquista duplicadas"


def rarest_by_category(unlocked_keys):
    out = {}
    for idx, a in enumerate(ACHIEVEMENTS):
        if a.key not in unlocked_keys:
            continue
        cur = out.get(a.cat)
        if cur is None or (a.rarity, idx) > (cur[0].rarity, cur[1]):
            out[a.cat] = (a, idx)
    return {cat: v[0] for cat, v in out.items()}
