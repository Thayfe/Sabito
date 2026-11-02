# Quando o desequilíbrio é grande (a odd normal do underdog chega a TRIGGER_ODD),
# o favorito passa a pagar FAVORITE_ODD fixa, e o underdog mantém a odd normal
# limitada a UNDERDOG_MAX_ODD.
TRIGGER_ODD = 5.0
FAVORITE_ODD = 2.0
UNDERDOG_MAX_ODD = 10.0


def _raw_odds(pool_win, pool_lose):
    total = pool_win + pool_lose
    o_win = total / pool_win if pool_win > 0 else None
    o_lose = total / pool_lose if pool_lose > 0 else None
    return o_win, o_lose


def odds(pool_win, pool_lose):
    # (odd_vitoria, odd_derrota); None quando o lado não tem apostas
    o_win, o_lose = _raw_odds(pool_win, pool_lose)
    if o_win is None or o_lose is None:
        return o_win, o_lose

    if max(o_win, o_lose) >= TRIGGER_ODD:
        if pool_win >= pool_lose:
            return FAVORITE_ODD, min(o_lose, UNDERDOG_MAX_ODD)
        return min(o_win, UNDERDOG_MAX_ODD), FAVORITE_ODD
    return o_win, o_lose


def compute_payouts(bets, outcome):
    # bets: [(user_id, side, amount)] com side 'win'/'lose'; outcome None = partida anulada. Retorna (payouts, refunded)
    if not bets:
        return {}, False

    pool = {"win": 0, "lose": 0}
    for _, side, amount in bets:
        pool[side] += amount

    if outcome is None or pool["win"] == 0 or pool["lose"] == 0:
        refund = {}
        for user, _, amount in bets:
            refund[user] = refund.get(user, 0) + amount
        return refund, True

    total = pool["win"] + pool["lose"]
    winners = [(u, a) for u, s, a in bets if s == outcome]

    # Desequilíbrio grande: favorito a 2x e underdog pela odd normal (teto 10x); a casa cobre a diferença.
    o_win, o_lose = odds(pool["win"], pool["lose"])
    raw_win, raw_lose = _raw_odds(pool["win"], pool["lose"])
    if (o_win, o_lose) != (raw_win, raw_lose):
        fixed = o_win if outcome == "win" else o_lose
        payouts = {}
        for u, a in winners:
            payouts[u] = payouts.get(u, 0) + int(a * fixed)
        return payouts, False

    payouts = {}
    for u, a in winners:
        payouts[u] = payouts.get(u, 0) + a * total // pool[outcome]

    # Sobra do arredondamento vai para a maior aposta vencedora (conserva o total).
    leftover = total - sum(payouts.values())
    if leftover > 0:
        top_user = max(winners, key=lambda w: w[1])[0]
        payouts[top_user] += leftover
    return payouts, False
