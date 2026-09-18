import random, math, statistics as st
random.seed(7)

# ---- inverse normal CDF (Acklam) ----
def inv_norm(p):
    a=[-3.969683028665376e+01,2.209460984245205e+02,-2.759285104469687e+02,1.383577518672690e+02,-3.066479806614716e+01,2.506628277459239e+00]
    b=[-5.447609879822406e+01,1.615858368580409e+02,-1.556989798598866e+02,6.680131188771972e+01,-1.328068155288572e+01]
    c=[-7.784894002430293e-03,-3.223964580411365e-01,-2.400758277161838e+00,-2.549732539343734e+00,4.374664141464968e+00,2.938163982698783e+00]
    d=[7.784695709041462e-03,3.224671290700398e-01,2.445134137142996e+00,3.754408661907416e+00]
    pl,ph=0.02425,1-0.02425
    if p<pl:
        q=math.sqrt(-2*math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5])/((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p>ph:
        q=math.sqrt(-2*math.log(1-p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5])/((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q=p-0.5; r=q*q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q/(((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)

# ---- sport peer groups: (shape, scale) of per-game fantasy points ----
# NFL RB: very skewed, boom/bust. NBA G: fat, stable. MLB bat: spiky, low mean.
SPORTS={'NFL_RB':(1.6,8.0),'NBA_G':(9.0,3.6),'MLB_BAT':(1.2,7.0)}
N_PLAYERS, N_GAMES = 220, 60

def sample_pool(shape, scale):
    """Each player has a latent talent multiplier; games drawn around it."""
    pool={}
    for i in range(N_PLAYERS):
        talent = random.lognormvariate(0, 0.32)     # player-to-player skill spread
        pool[i] = [random.gammavariate(shape, scale)*talent for _ in range(N_GAMES)]
    return pool

def quantile(sorted_xs, q):
    idx = q*(len(sorted_xs)-1); lo=int(idx); hi=min(lo+1,len(sorted_xs)-1)
    return sorted_xs[lo] + (idx-lo)*(sorted_xs[hi]-sorted_xs[lo])

class PeerGroup:
    """Stage 1-2: empirical distribution used for rank-normalization."""
    def __init__(self, pool):
        self.all = sorted(x for games in pool.values() for x in games)
        self.n = len(self.all)
        self.q90 = quantile(self.all, 0.90)
        self.q99 = quantile(self.all, 0.99)
    def pct(self, fp):
        lo, hi = 0, self.n
        while lo < hi:                      # bisect for rank
            mid=(lo+hi)//2
            if self.all[mid] < fp: lo=mid+1
            else: hi=mid
        return lo/self.n

def game_score(fp, pg, tail_w=0.75, k=12.0):
    """Stages 2-4: rank -> z, plus capped tail-magnitude bonus, onto a 0-100 scale."""
    p = min(max(pg.pct(fp), 0.001), 0.999)
    z = inv_norm(p)
    e = max(0.0, (fp - pg.q90) / (pg.q99 - pg.q90)) if pg.q99 > pg.q90 else 0.0
    z += tail_w * min(e, 1.0)
    return max(0.0, min(100.0, 50 + k*z))

# ============================================================================
#  CROSS-SPORT SCORING ENGINE
#  Stage 1  peer group   : (sport, position, trailing 365d) empirical FP distro
#  Stage 2  rank-normalize: percentile -> z via inverse normal  [distribution-free]
#  Stage 3  tail bonus    : capped credit for blow-up games, scaled to peer tail
#  Stage 4  common scale  : GameScore = 50 + 12z, clamped 0-100
# ============================================================================
TAIL_WEIGHT, SCALE = 0.75, 12.0
REPLACEMENT_SCORE  = 15.0      # awarded on DNP with no bench sub

# Per-sport tuning. K = games each card must play for a challenge to resolve.
# VOLATILITY = tactic-card swing, tuned so every sport lands in the same
# competitive band despite differing box-score signal-to-noise.
SPORT_CONFIG = {
    'NFL_RB':  dict(K=1, volatility=0.10),
    'NBA_G':   dict(K=1, volatility=0.40),
    'MLB_BAT': dict(K=2, volatility=0.18),
}
RARITY_FLOOR = {'common':0,'uncommon':20,'rare':28,'elite':34,'signature':38}
# NOTE: floors >38 measured pay-to-win (57.6% win rate). 38 is the hard ceiling.

def card_score(fp_games, peer, volatility=0.0, floor=0, tactic_hit=None):
    """Resolve one card. Tactic multipliers MUST be mean-neutral (E=1.0),
       or volatility silently becomes raw power."""
    gs = [game_score(fp, peer, TAIL_WEIGHT, SCALE) for fp in fp_games] or [REPLACEMENT_SCORE]
    s = max(sum(gs)/len(gs), floor)
    if tactic_hit is not None and volatility:
        s *= (1 + volatility) if tactic_hit else (1 - volatility)
    return s

if __name__ == '__main__':
    pools  = {s: sample_pool(*p) for s, p in SPORTS.items()}
    groups = {s: PeerGroup(pools[s]) for s in SPORTS}
    print(f"{'peer group':<10}{'medianFP':>10}{'p50':>7}{'p90':>7}{'p99':>7}")
    for s in SPORTS:
        fps = [fp for g in pools[s].values() for fp in g]
        v = sorted(game_score(fp, groups[s]) for fp in fps)
        print(f"{s:<10}{quantile(sorted(fps),.5):>10.1f}"
              f"{quantile(v,.5):>7.1f}{quantile(v,.9):>7.1f}{quantile(v,.99):>7.1f}")
