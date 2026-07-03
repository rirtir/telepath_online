"""
以心伝心 -TELEPATH- サーバー（FastAPI + WebSocket）

- 単一ルーム型（ito_online と同じ方針）。
- クライアントは ?uid=<id> で接続し、再接続時は同じ uid で状態を引き継ぐ。
- 状態は「全状態同期（full-state sync）」方式。変化があるたびに各クライアントへ
  STATE メッセージ（本人向けに一部個別化）を配信し、クライアントはそれを描画する。

ローカル実行:
    pip install -r requirements.txt
    python server.py          （http://localhost:10000 を開く。複数タブ＝複数人）

Render へのデプロイ:
    Build Command : pip install -r requirements.txt
    Start Command : uvicorn server:app --host 0.0.0.0 --port $PORT
"""

import os
import json
import uuid
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from game import Game, DIFFICULTIES, OP_STAY

app = FastAPI()

# players: uid -> { "ws": WebSocket|None, "connected": bool, "slot_idx": int|None, "name": str }
app.state.players = {}
app.state.slots = []          # スロットを占有している uid の順序付きリスト
app.state.game = Game()
app.state.lock = asyncio.Lock()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MIN_PLAYERS = 2
MAX_PLAYERS = 6


# --- ヘルパー ----------------------------------------------------------------
def slot_name(uid, idx):
    p = app.state.players.get(uid, {})
    name = (p.get("name") or "").strip()
    return name if name else f"{idx + 1}P"


def lobby_slots_info():
    info = []
    for idx, uid in enumerate(app.state.slots):
        p = app.state.players.get(uid, {})
        info.append({
            "slot": idx,
            "name": slot_name(uid, idx),
            "connected": bool(p.get("connected")),
        })
    return info


def build_state_for(uid):
    """uid 向けの STATE メッセージを組み立てる（本人の操作内容だけ個別に含める）。"""
    p = app.state.players.get(uid, {})
    g = app.state.game
    my_slot = p.get("slot_idx")
    my_ops = None
    if my_slot is not None and g.started:
        my_ops = g.submissions.get(my_slot)

    return {
        "type": "STATE",
        "you": {
            "uid": uid,
            "slot": my_slot,
            "name": slot_name(uid, my_slot) if my_slot is not None else (p.get("name") or ""),
            "is_host": my_slot == 0,
            "is_spectator": my_slot is None,
            "my_ops": my_ops,
        },
        "lobby": {
            "slots": lobby_slots_info(),
            "count": len(app.state.slots),
            "min_players": MIN_PLAYERS,
            "max_players": MAX_PLAYERS,
            "can_start": my_slot == 0 and len(app.state.slots) >= MIN_PLAYERS and not g.started,
        },
        "game": g.snapshot(),
        "difficulties": list(DIFFICULTIES.keys()),
    }


async def send_safe(ws, msg):
    try:
        await ws.send_text(json.dumps(msg))
    except Exception:
        pass


async def broadcast_state():
    for uid, p in list(app.state.players.items()):
        ws = p.get("ws")
        if ws is not None and p.get("connected"):
            await send_safe(ws, build_state_for(uid))


def reindex_slots():
    for idx, uid in enumerate(app.state.slots):
        p = app.state.players.get(uid)
        if p is not None:
            p["slot_idx"] = idx


def reset_game_keep_slots():
    """ロビーへ戻す（参加者・難易度・オプションは維持）。"""
    g = app.state.game
    ng = Game()
    ng.difficulty = g.difficulty
    ng.order_mode = g.order_mode
    ng.pins_enabled = g.pins_enabled
    app.state.game = ng


def all_slots_disconnected():
    """スロットを占有している全員が切断中なら True（スロットが無い場合は False）。"""
    if not app.state.slots:
        return False
    return all(not app.state.players.get(uid, {}).get("connected") for uid in app.state.slots)


def reset_room():
    """部屋を完全にロビーへ戻す（スロット解放・切断済みプレイヤーの掃除）。設定は維持。"""
    for uid in list(app.state.slots):
        p = app.state.players.get(uid)
        if p is not None:
            p["slot_idx"] = None
    app.state.slots = []
    # 切断済みで残っているプレイヤー情報を掃除
    for uid in list(app.state.players.keys()):
        if not app.state.players[uid].get("connected"):
            del app.state.players[uid]
    reset_game_keep_slots()


def slot_uid(slot):
    """スロット番号 -> uid（範囲外なら None）。ゲーム中スロットは固定なので安定。"""
    return app.state.slots[slot] if 0 <= slot < len(app.state.slots) else None


def try_resolve():
    """
    接続中のプレイヤー全員が提出済みなら解決する。
    未提出でも切断中のプレイヤーは「とどまる」で自動的に埋め、進行が止まらないようにする。
    （＝1人が落ちても他の人が待たされ続けない。複数挑戦にも対応）
    """
    g = app.state.game
    if g.phase != "selecting":
        return
    # 接続中で未提出の人が一人でもいれば、まだ待つ
    for slot in range(g.num_players):
        if g.submissions.get(slot) is None:
            uid = slot_uid(slot)
            if uid is not None and app.state.players.get(uid, {}).get("connected"):
                return
    # 残る未提出は切断者のみ → Stay で埋めて解決
    for slot in range(g.num_players):
        if g.submissions.get(slot) is None:
            g.submit(slot, [OP_STAY] * g.ops_per_player)
    if g.all_submitted():
        g.resolve()


async def schedule_disconnect_cleanup(uid, grace=10):
    """
    切断から grace 秒たっても復帰しなければ後片付けする。
    この猶予により、リロード（＝すぐ同じ uid で再接続）では席や部屋を失わない。
    - ロビー中 : スロットを解放し、プレイヤー情報を掃除。
    - ゲーム中 : 全員切断なら部屋をリセット。そうでなければ席は保持（復帰可能）しつつ、
                この人待ちで止まっていれば解決する。
    """
    await asyncio.sleep(grace)
    async with app.state.lock:
        p = app.state.players.get(uid)
        if p is None or p.get("connected"):
            return  # 既に復帰済み or 消滅済み → 何もしない
        g = app.state.game
        if not g.started:
            if p.get("slot_idx") is not None:
                try:
                    app.state.slots.remove(uid)
                except ValueError:
                    pass
                p["slot_idx"] = None
                reindex_slots()
            app.state.players.pop(uid, None)
        else:
            if all_slots_disconnected():
                reset_room()
            else:
                try_resolve()
        await broadcast_state()


# --- WebSocket ---------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    query_uid = websocket.query_params.get("uid")

    async with app.state.lock:
        if query_uid and query_uid in app.state.players:
            # 既存プレイヤーの再接続
            uid = query_uid
            p = app.state.players[uid]
            p["ws"] = websocket
            p["connected"] = True
        else:
            # 新規（クライアント指定の uid が未知でもそれを採用）
            uid = query_uid or str(uuid.uuid4())
            app.state.players[uid] = {
                "ws": websocket, "connected": True, "slot_idx": None, "name": "",
            }
        # 全参加者が切断状態のまま新規/観戦者が来たら、放置された部屋をロビーへ戻す。
        # （スロット保持中の本人が再接続した場合は connected=True になるためリセットされない）
        if app.state.game.started and all_slots_disconnected():
            reset_room()
        await send_safe(websocket, {"type": "ASSIGN_ID", "user_id": uid})
        await broadcast_state()

    try:
        while True:
            data = await websocket.receive_text()  # 切断で WebSocketDisconnect
            try:
                msg = json.loads(data)
            except Exception:
                continue
            mtype = msg.get("type")

            if mtype == "PING":
                await send_safe(websocket, {"type": "PONG"})
                continue

            try:
                async with app.state.lock:
                    p = app.state.players.get(uid)
                    if p is None:
                        continue
                    g = app.state.game

                    if mtype == "SET_NAME":
                        p["name"] = str(msg.get("name", ""))[:16]

                    elif mtype == "ENTER_GAME":
                        if not g.started and p["slot_idx"] is None and len(app.state.slots) < MAX_PLAYERS:
                            app.state.slots.append(uid)
                            p["slot_idx"] = len(app.state.slots) - 1

                    elif mtype == "LEAVE_GAME":
                        if not g.started and p["slot_idx"] is not None:
                            try:
                                app.state.slots.remove(uid)
                            except ValueError:
                                pass
                            p["slot_idx"] = None
                            reindex_slots()

                    elif mtype == "SET_DIFFICULTY":
                        if p["slot_idx"] == 0 and not g.started:
                            g.set_difficulty(str(msg.get("difficulty", "")))

                    elif mtype == "SET_OPTIONS":
                        if p["slot_idx"] == 0 and not g.started:
                            g.set_options(msg.get("order_mode"), msg.get("pins_enabled"))

                    elif mtype == "START":
                        if p["slot_idx"] == 0 and not g.started and len(app.state.slots) >= MIN_PLAYERS:
                            g.start(len(app.state.slots))

                    elif mtype == "SUBMIT_OPS":
                        slot = p["slot_idx"]
                        if slot is not None and g.submit(slot, msg.get("ops")):
                            try_resolve()

                    elif mtype == "UNSUBMIT":
                        slot = p["slot_idx"]
                        if slot is not None:
                            g.unsubmit(slot)

                    elif mtype == "PLACE_PIN":
                        slot = p["slot_idx"]
                        if slot is not None:
                            try:
                                g.place_pin(slot, int(msg.get("x")), int(msg.get("y")))
                            except (TypeError, ValueError):
                                pass

                    elif mtype == "NEXT_ATTEMPT":
                        g.next_attempt()

                    elif mtype == "NEXT_STAGE":
                        if p["slot_idx"] == 0 and g.phase == "won":
                            g.next_stage()

                    elif mtype == "PLAY_AGAIN":
                        if p["slot_idx"] == 0 and g.phase in ("won", "lost"):
                            reset_game_keep_slots()

                    elif mtype == "ABORT_GAME":
                        # ゲーム中に参加者の誰でも中断してロビーへ戻せる（全員に反映）
                        if g.started and p["slot_idx"] is not None:
                            reset_game_keep_slots()

                    await broadcast_state()
            except Exception:
                # ハンドラ内の想定外エラーでは接続を切らずに継続する
                pass

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        # 切断時の後片付け。ただし「自分が今もこのプレイヤーの現行接続」の場合のみ。
        # リロード等で新しい接続が既に ws を差し替えているなら、古い接続の切断は無視する
        # （＝幽霊切断で connected を落として画面を固めてしまうのを防ぐ）。
        async with app.state.lock:
            p = app.state.players.get(uid)
            if p is not None and p.get("ws") is websocket:
                p["connected"] = False
                p["ws"] = None
                # この人待ちで止まらないよう、必要なら解決を試みる
                try_resolve()
                # 席の解放・部屋リセットは猶予後に判定（リロードで席を失わないように）
                asyncio.create_task(schedule_disconnect_cleanup(uid))
                await broadcast_state()


# --- 静的ファイル配信 --------------------------------------------------------
@app.get("/")
async def root():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True), name="static")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
