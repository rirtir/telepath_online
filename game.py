"""
以心伝心 -TELEPATH- のゲームロジック（サーバー内・純粋ロジック）

ルール概要:
- 複数プレイヤーが 1 体のキャラクターを共有して操作する協力ゲーム。
- 各プレイヤーには順番（スロット）が割り振られ、難易度に応じた手数だけ操作を選ぶ。
- 全員が「自分の操作列」をブラインドで提出し、全員そろったらスロット順に一括実行する。
- 全操作を実行し終えた時点でキャラがゴール上にいればクリア。
- 失敗しても max_chances 回まで挑戦でき、失敗のたびに全員の選択が公開される（読み合い）。
- 壁・画面外への移動は「空振り（その場に留まる）」になる。

実力要素（運ゲー化を防ぐ仕組み）:
- 封印   : ふつう以上では各プレイヤーに「使えない方向」が 1 つ公開で割り振られる。
           誰がどの方向を担当できるかが公開情報になり、読みが推理に変わる。
- ピン    : 選択中に盤面へ最大 MAX_PINS 個のピンを置いて全員に共有できる
           （チャット無しの限定シグナル）。
- ジェム  : 寄り道の位置に置かれたサブ目標。クリア時に全回収＋初回成功で★3。
- ステージ: クリアするたびに次のマップへ進み、合計★を積み上げるラン形式。

絶対条件:
- マップ生成時に BFS で最短距離を求め、最短距離 <= 合計操作回数(total_ops) を満たす
  マス目のみをゴール候補にする（＝必ず理論上クリア可能なマップになる）。
- 封印・ジェムは「スロット実行順・封印込みで、全ジェムを回収してゴールに到達できる」
  ことを状態探索（feasible_all）で確認してから採用する（＝★3 は常に実力で取れる）。

拡張ポイント（未実装のアイデア）:
- 破壊可能な壁 / 攻撃操作 / 倒すべき敵。導入時は feasible_all の状態に
  「壁・敵の残り状態」を加えて到達可能性を保証する必要がある。
"""

import random
import uuid
from collections import deque

# --- 操作の定義 --------------------------------------------------------------
OP_UP, OP_DOWN, OP_LEFT, OP_RIGHT, OP_STAY = "U", "D", "L", "R", "S"
OPS = [OP_UP, OP_DOWN, OP_LEFT, OP_RIGHT, OP_STAY]
DELTA = {
    OP_UP:    (0, -1),
    OP_DOWN:  (0, 1),
    OP_LEFT:  (-1, 0),
    OP_RIGHT: (1, 0),
    OP_STAY:  (0, 0),
}

# 1 人が同時に置けるピンの数
MAX_PINS = 2

# --- 難易度設定 --------------------------------------------------------------
# ops_per_player: 1 人あたりの操作回数（難易度で増える）
# walls:          配置する壁の数（多いほど迂回の読み合いが増える）
# chances:        挑戦できる回数（ステージごとにリセット）
# max_slack:      「最短距離」と「合計手数」の差の最大値。
#                 大きいほど “誰かがとどまる必要” が生まれて難しくなる。
# gems:           配置するジェム（サブ目標）の数
# seals:          封印（使えない方向の公開割り振り）を行うか
DIFFICULTIES = {
    "easy":   {"w": 6,  "h": 6,  "ops_per_player": 1, "walls": 5,  "chances": 3, "max_slack": 1, "gems": 1, "seals": False},
    "normal": {"w": 8,  "h": 8,  "ops_per_player": 2, "walls": 12, "chances": 3, "max_slack": 2, "gems": 2, "seals": True},
    "hard":   {"w": 10, "h": 10, "ops_per_player": 3, "walls": 22, "chances": 3, "max_slack": 3, "gems": 3, "seals": True},
}


def _bfs(grid, start, w, h):
    """start から各マスへの最短距離（4 近傍・壁は通れない）を返す。"""
    dist = {start: 0}
    dq = deque([start])
    while dq:
        x, y = dq.popleft()
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and grid[ny][nx] == 0 and (nx, ny) not in dist:
                dist[(nx, ny)] = dist[(x, y)] + 1
                dq.append((nx, ny))
    return dist


def generate_map(cfg, total_ops):
    """
    条件を満たすマップ(grid), スタート, ゴール, 最短距離 を返す。

    ゴールは BFS 最短距離が [total_ops - max_slack, total_ops] に収まるマスから選ぶ。
    これにより「最短距離 <= 合計手数」を必ず満たしつつ、あまり手が多すぎない
    （＝ちょうど良い難易度の）マップになる。壁で迂回が必要なマスを優先する。
    """
    w, h = cfg["w"], cfg["h"]
    walls = cfg["walls"]
    lo = max(1, total_ops - cfg["max_slack"])
    hi = total_ops

    for _ in range(1200):
        grid = [[0] * w for _ in range(h)]
        cells = [(x, y) for y in range(h) for x in range(w)]
        random.shuffle(cells)
        for (x, y) in cells[:walls]:
            grid[y][x] = 1

        open_cells = [(x, y) for y in range(h) for x in range(w) if grid[y][x] == 0]
        if len(open_cells) < 2:
            continue

        start = random.choice(open_cells)
        dist = _bfs(grid, start, w, h)
        cands = [c for c, d in dist.items() if lo <= d <= hi and c != start]
        if not cands:
            continue

        # 迂回（BFS距離 > マンハッタン距離）が大きく、かつゴールが遠いものを優先。
        def score(c):
            manhattan = abs(c[0] - start[0]) + abs(c[1] - start[1])
            detour = dist[c] - manhattan
            return (detour, dist[c])

        cands.sort(key=score, reverse=True)
        top = cands[:max(1, len(cands) // 3)]
        goal = random.choice(top)
        return grid, list(start), list(goal), dist[goal]

    # フォールバック: 壁なしで確実にクリア可能なマップを作る
    grid = [[0] * w for _ in range(h)]
    gx = min(total_ops, w - 1)
    gy = min(total_ops - gx, h - 1)
    if [gx, gy] == [0, 0]:
        gx = min(1, w - 1)
    return grid, [0, 0], [gx, gy], gx + gy


def execute(grid, start, ops_ordered, w, h):
    """
    操作列（実行順）を順番に適用し、
    trace（各手のあとの座標・先頭は開始位置）と steps（各手の詳細）を返す。
    壁・画面外は空振り（blocked=True でその場に留まる）。
    """
    x, y = start
    trace = [[x, y]]
    steps = []
    for op in ops_ordered:
        blocked = False
        if op == OP_STAY:
            nx, ny = x, y
        else:
            dx, dy = DELTA[op]
            nx, ny = x + dx, y + dy
            if not (0 <= nx < w and 0 <= ny < h) or grid[ny][nx] == 1:
                nx, ny = x, y
                blocked = True
        x, y = nx, ny
        steps.append({"op": op, "to": [x, y], "blocked": blocked})
        trace.append([x, y])
    return trace, steps


def feasible_all(grid, start, goal, w, h, owners, seals, gems):
    """
    「スロット実行順（owners）・封印（seals）のもとで、全ジェム（gems）を回収して
    最終的にゴール上に居られる操作列が存在するか」を状態探索で判定する。

    状態 = (位置, 回収済みジェムの bitmask)。各手番で、その手の担当者が使える操作
    （封印以外）だけ遷移させる。状態数は マス数 × 2^ジェム数 × 手数 で高々数万。
    """
    gem_idx = {tuple(g): i for i, g in enumerate(gems)}
    full = (1 << len(gems)) - 1

    def collect(pos, mask):
        i = gem_idx.get(pos)
        return mask | (1 << i) if i is not None else mask

    start_t = tuple(start)
    cur = {(start_t, collect(start_t, 0))}
    for slot in owners:
        sealed = seals.get(slot)
        nxt = set()
        for (x, y), mask in cur:
            for op in OPS:
                if op == sealed:
                    continue
                dx, dy = DELTA[op]
                nx, ny = x + dx, y + dy
                if op == OP_STAY or not (0 <= nx < w and 0 <= ny < h) or grid[ny][nx] == 1:
                    nx, ny = x, y
                nxt.add(((nx, ny), collect((nx, ny), mask)))
        cur = nxt
    goal_t = tuple(goal)
    return any(pos == goal_t and mask == full for pos, mask in cur)


class Game:
    """1 ルーム分のゲーム状態を持つ。"""

    def __init__(self):
        self.reset_to_lobby("easy")

    # --- 状態遷移 -----------------------------------------------------------
    def reset_to_lobby(self, difficulty="easy"):
        self.started = False
        self.phase = "lobby"           # lobby / selecting / result / won / lost
        self.difficulty = difficulty if difficulty in DIFFICULTIES else "easy"
        self.num_players = 0
        self.ops_per_player = 0
        self.total_ops = 0
        self.max_chances = 0
        self.chances = 0
        self.w = 0
        self.h = 0
        self.grid = []
        self.start_pos = [0, 0]
        self.goal = [0, 0]
        self.shortest = 0
        self.attempt = 0
        self.submissions = {}          # slot(int) -> list[op] または None
        self.last_attempt = None       # 直近に実行した挑戦の結果（公開用）
        self.game_id = ""              # ステージ開始ごとに変わる一意ID（クライアントのリセット判定用）
        self.owners = []               # 実行順に並べた「各手の担当スロット」
        self.seals = {}                # slot -> 封印された方向（無い slot はキー無し）
        self.gems = []                 # [[x,y], ...] サブ目標
        self.pins = {}                 # slot -> [[x,y], ...] 作戦ピン
        self.stage = 0                 # 現在のステージ番号（1 始まり）
        self.total_stars = 0           # このランで獲得した★の合計
        self.order = []                # 実行順（order[k] = k番目に動くプレイヤーの slot）
        self.order_mode = "random"     # "random"=階ごとにシャッフル / "fixed"=スロット順
        self.pins_enabled = False      # ピン機能（強力なのでデフォルトOFF・ロビーで変更可）

    def set_options(self, order_mode=None, pins_enabled=None):
        """ロビーで変更できるオプション（ゲーム中は変更不可）。"""
        if self.started:
            return
        if order_mode in ("fixed", "random"):
            self.order_mode = order_mode
        if isinstance(pins_enabled, bool):
            self.pins_enabled = pins_enabled

    def set_difficulty(self, difficulty):
        if not self.started and difficulty in DIFFICULTIES:
            self.difficulty = difficulty

    def start(self, num_players):
        cfg = DIFFICULTIES[self.difficulty]
        self.num_players = num_players
        self.ops_per_player = cfg["ops_per_player"]
        self.total_ops = num_players * self.ops_per_player
        self.max_chances = cfg["chances"]
        self.stage = 1
        self.total_stars = 0
        self.started = True
        self._setup_stage(cfg)

    def next_stage(self):
        """クリア後に次のステージへ（参加者・難易度・合計★は維持、チャンスはリセット）。"""
        if self.phase != "won":
            return
        self.stage += 1
        self._setup_stage(DIFFICULTIES[self.difficulty])

    def _setup_stage(self, cfg):
        """新しいマップ・封印・ジェム・実行順を生成してステージを始める。"""
        self.chances = cfg["chances"]
        self.w, self.h = cfg["w"], cfg["h"]
        self.grid, self.start_pos, self.goal, self.shortest = generate_map(cfg, self.total_ops)
        # 実行順: 固定ならスロット順、ランダムなら階ごとにシャッフル（役割の固定化を防ぐ）
        self.order = list(range(self.num_players))
        if self.order_mode == "random":
            random.shuffle(self.order)
        self.owners = [s for s in self.order for _ in range(self.ops_per_player)]
        self.seals = self._gen_seals(cfg)
        self.gems = self._gen_gems(cfg)
        self.attempt = 1
        self.submissions = {i: None for i in range(self.num_players)}
        self.pins = {i: [] for i in range(self.num_players)}
        self.last_attempt = None
        self.game_id = uuid.uuid4().hex[:8]
        self.phase = "selecting"

    def _gen_seals(self, cfg):
        """各プレイヤーに封印方向を割り振る。ゴール到達が不可能になる組合せは避ける。"""
        if not cfg.get("seals") or self.num_players <= 0:
            return {}
        dirs = [OP_UP, OP_DOWN, OP_LEFT, OP_RIGHT]
        for _ in range(80):
            seals = {s: random.choice(dirs) for s in range(self.num_players)}
            if feasible_all(self.grid, self.start_pos, self.goal, self.w, self.h,
                            self.owners, seals, []):
                return seals
        return {}  # フォールバック: 封印なし

    def _gen_gems(self, cfg):
        """
        ジェムを配置する。候補は「スタートからの距離 + ゴールまでの距離 <= 合計手数」
        のマス（＝理論上は寄り道して回収できる）。さらに封印込みで
        「全ジェム回収 + ゴール到達」が可能な組合せだけを採用する。
        """
        count = cfg.get("gems", 0)
        if count <= 0:
            return []
        ds = _bfs(self.grid, tuple(self.start_pos), self.w, self.h)
        dg = _bfs(self.grid, tuple(self.goal), self.w, self.h)
        cand = []
        for (x, y), d1 in ds.items():
            if [x, y] == self.start_pos or [x, y] == self.goal:
                continue
            d2 = dg.get((x, y))
            if d2 is not None and d1 + d2 <= self.total_ops:
                cand.append([x, y])
        if not cand:
            return []
        # 寄り道が必要なマス（ds+dg が大きい）を優先しつつランダム性を残す
        cand.sort(key=lambda c: ds[tuple(c)] + dg[tuple(c)], reverse=True)

        k = min(count, len(cand))
        while k > 0:
            pool = cand[:max(k * 3, k)]
            for _ in range(40):
                gems = random.sample(pool, k)
                if feasible_all(self.grid, self.start_pos, self.goal, self.w, self.h,
                                self.owners, self.seals, gems):
                    return gems
            k -= 1  # k 個では両立できない → 数を減らして再挑戦
        return []

    # --- プレイヤー入力 ------------------------------------------------------
    def submit(self, slot, ops):
        """操作列を提出。成功したら True。封印された方向を含む提出は拒否する。"""
        if self.phase != "selecting" or slot not in self.submissions:
            return False
        if not isinstance(ops, list) or len(ops) != self.ops_per_player:
            return False
        if any(o not in OPS for o in ops):
            return False
        sealed = self.seals.get(slot)
        if sealed is not None and sealed in ops:
            return False
        self.submissions[slot] = list(ops)
        return True

    def unsubmit(self, slot):
        if self.phase == "selecting" and slot in self.submissions:
            self.submissions[slot] = None

    def place_pin(self, slot, x, y):
        """作戦ピンを置く/外す（トグル）。上限を超えたら古いものから消える。"""
        if not self.pins_enabled:
            return
        if self.phase != "selecting" or slot not in self.pins:
            return
        if not (0 <= x < self.w and 0 <= y < self.h) or self.grid[y][x] == 1:
            return
        pins = self.pins[slot]
        if [x, y] in pins:
            pins.remove([x, y])
            return
        pins.append([x, y])
        if len(pins) > MAX_PINS:
            pins.pop(0)

    def all_submitted(self):
        return (
            self.started
            and self.phase == "selecting"
            and all(v is not None for v in self.submissions.values())
        )

    def resolve(self):
        """全員の操作を実行し、勝敗を判定する。"""
        if self.phase != "selecting":
            return
        ordered, owners = [], []
        for slot in self.order:
            ops = self.submissions[slot] or [OP_STAY] * self.ops_per_player
            for op in ops:
                ordered.append(op)
                owners.append(slot)

        trace, steps = execute(self.grid, self.start_pos, ordered, self.w, self.h)
        for i, s in enumerate(steps):
            s["owner"] = owners[i]
        reached = (trace[-1] == self.goal)

        # ジェム回収判定（通過したマスにあるジェムは回収）
        visited = set(map(tuple, trace))
        collected = [tuple(g) in visited for g in self.gems]

        # ★ 評価: クリア +1 / 全ジェム回収 +1 / 1回目の挑戦でクリア +1
        stars = 0
        if reached:
            stars = 1
            if self.gems and all(collected):
                stars += 1
            if self.attempt == 1:
                stars += 1
            self.total_stars += stars

        self.last_attempt = {
            "attempt": self.attempt,
            "ops_by_slot": {str(s): self.submissions[s] for s in range(self.num_players)},
            "trace": trace,
            "steps": steps,
            "reached": reached,
            "gems": collected,
            "stars": stars,
        }

        if reached:
            self.phase = "won"
        else:
            self.chances -= 1
            self.phase = "lost" if self.chances <= 0 else "result"

    def next_attempt(self):
        if self.phase != "result":
            return
        self.attempt += 1
        self.submissions = {i: None for i in range(self.num_players)}
        self.pins = {i: [] for i in range(self.num_players)}
        self.phase = "selecting"

    # --- クライアントへ送る状態 ---------------------------------------------
    def snapshot(self):
        """全員に配る公開情報。選択中は各自の操作内容は含めない（submitted のみ）。"""
        return {
            "started": self.started,
            "phase": self.phase,
            "game_id": self.game_id,
            "difficulty": self.difficulty,
            "num_players": self.num_players,
            "ops_per_player": self.ops_per_player,
            "total_ops": self.total_ops,
            "chances": self.chances,
            "max_chances": self.max_chances,
            "w": self.w,
            "h": self.h,
            "grid": self.grid,
            "start": self.start_pos,
            "goal": self.goal,
            "shortest": self.shortest,
            "attempt": self.attempt,
            "submitted": [self.submissions.get(i) is not None for i in range(self.num_players)],
            "last_attempt": self.last_attempt,
            "seals": [self.seals.get(i) for i in range(self.num_players)],
            "gems": self.gems,
            "pins": [self.pins.get(i, []) for i in range(self.num_players)],
            "max_pins": MAX_PINS,
            "stage": self.stage,
            "total_stars": self.total_stars,
            "order": self.order,
            "order_mode": self.order_mode,
            "pins_enabled": self.pins_enabled,
        }
