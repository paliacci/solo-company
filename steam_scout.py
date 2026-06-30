#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场监测员 v2 —— SteamSpy 目标品类捞取 + 可移植性打分
======================================================
v1 的教训：用"近两周最热"当鱼塘，捞上来全是 AAA/网游（搬不动 / 早有手机版）。
v2 改两点：
  1. 换鱼塘：按"目标品类标签"（休闲/益智/回合/卡牌…）去捞，直接在对的池子里找。
  2. 加硬门槛：MMO/大逃杀/巨型3D/玩家数过千万的，直接淘汰或重罚。

用法:
    python steam_scout.py                 # 真实数据，输出今日候选表
    python steam_scout.py --top 20        # 输出数量（默认 15）
    python steam_scout.py --demo          # 离线样例，验证脚本逻辑
    python steam_scout.py --also-trending # 额外并入"近两周热门"里的小游戏

依赖: pip install requests
"""

import argparse
import datetime as dt
import json
import re
import sys
import time

try:
    import requests
except ImportError:
    requests = None

# ============================================================
# 可调参数区
# ============================================================

# 鱼塘：去这些品类标签里捞候选（SteamSpy tag 端点）
TARGET_TAGS = [
    "Casual", "Puzzle", "Turn-Based Strategy", "Card Game",
    "Idler", "Roguelike Deckbuilder", "Match 3", "Word Game",
    "Tower Defense", "Clicker", "Board Game", "Hidden Object",
]

# 玩家数"甜区"（取 owners 区间中点）：太小没搜索需求，太大=AAA/多半已有手机版
OWNERS_MIN = 50_000           # 低于此：太冷门，无流量价值
OWNERS_SWEET = 3_000_000      # 甜区上沿：超过开始扣分
OWNERS_HARD_CAP = 12_000_000  # 超过这个直接淘汰（AAA 体量）

# 直接淘汰的标签（根本无法单人 clone）
DISQUALIFY_TAGS = {
    "mmorpg", "mmo", "massively multiplayer", "battle royale",
    "moba", "vr", "vr only",
}

# 可移植性：加分（适合搬手机）
PORTABLE_TAGS = {
    "casual", "puzzle", "turn-based", "turn-based strategy", "card game",
    "deckbuilding", "idle", "clicker", "match 3", "hypercasual",
    "board game", "tower defense", "point & click", "minimalist",
    "word game", "hidden object", "2d", "auto battler",
}
# 可移植性：重扣（需键鼠精准 / 重 3D / 大体量）
UNPORTABLE_TAGS = {
    "fps", "first-person", "shooter", "3d", "open world", "souls-like",
    "racing", "flight", "rts", "fighting", "3d platformer", "horror",
    "stealth", "hack and slash", "bullet hell", "action rpg",
    "story rich", "open world survival craft",
}

CHEAP_ART_TAGS = {"pixel graphics", "2d", "minimalist", "hand-drawn",
                  "retro", "cute", "colorful", "top-down"}
EXPENSIVE_ART_TAGS = {"3d", "realistic", "photorealistic", "atmospheric",
                      "cinematic", "gore", "great soundtrack"}

IP_RISK_PUBLISHERS = {
    "valve", "nintendo", "sega", "capcom", "square enix", "bandai namco",
    "ubisoft", "electronic arts", "activision", "take-two", "rockstar",
    "bethesda", "disney", "warner", "larian", "krafton", "game science",
}

WEIGHTS = {"demand": 0.30, "portability": 0.45, "art": 0.12, "legal": 0.13}

# ============================================================
STEAMSPY = "https://steamspy.com/api.php"
HEADERS = {"User-Agent": "market-scout/2.0"}


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def owners_midpoint(s):
    """把 '1,000,000 .. 2,000,000' 解析成中点数字。"""
    if not s:
        return 0.0
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", str(s))]
    if not nums:
        return 0.0
    return sum(nums) / len(nums)


def fetch_tag(tag):
    r = requests.get(STEAMSPY, params={"request": "tag", "tag": tag},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return list(r.json().values())


def fetch_top_2weeks():
    r = requests.get(STEAMSPY, params={"request": "top100in2weeks"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return list(r.json().values())


def fetch_appdetails(appid):
    r = requests.get(STEAMSPY, params={"request": "appdetails", "appid": appid},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def build_pool(also_trending):
    """从目标品类标签捞候选，去重。可选并入近两周热门。"""
    seen, pool = set(), []
    for tag in TARGET_TAGS:
        try:
            for g in fetch_tag(tag):
                aid = g.get("appid")
                if aid and aid not in seen:
                    seen.add(aid); pool.append(g)
        except Exception as e:
            print(f"  标签 {tag} 拉取失败：{e}", file=sys.stderr)
        time.sleep(1.1)
    if also_trending:
        try:
            for g in fetch_top_2weeks():
                aid = g.get("appid")
                if aid and aid not in seen:
                    seen.add(aid); pool.append(g)
        except Exception:
            pass
    return pool


def _tag_set(game):
    tags = set()
    raw = game.get("tags")
    if isinstance(raw, dict):
        tags |= {t.lower() for t in raw.keys()}
    elif isinstance(raw, list):
        tags |= {str(t).lower() for t in raw}
    genre = game.get("genre") or ""
    tags |= {g.strip().lower() for g in genre.split(",") if g.strip()}
    return tags


def _overlap(tags, ref):
    return len(tags & ref)


def is_disqualified(game, tags):
    """硬淘汰：MMO/大逃杀/VR，或玩家数超 AAA 上限。"""
    if tags & DISQUALIFY_TAGS:
        return True
    if owners_midpoint(game.get("owners")) > OWNERS_HARD_CAP:
        return True
    return False


def demand_score(game):
    """需求分 0-100：玩家数落在'甜区'最高，太小或太大都降。"""
    owners = owners_midpoint(game.get("owners"))
    if owners <= 0:
        return 20.0
    if owners < OWNERS_MIN:
        return round(100.0 * owners / OWNERS_MIN * 0.5, 1)   # 太冷门，最多 50
    if owners <= OWNERS_SWEET:
        return 100.0                                          # 甜区
    span = OWNERS_HARD_CAP - OWNERS_SWEET                     # 超甜区线性衰减
    over = min(owners - OWNERS_SWEET, span)
    return round(100.0 - 70.0 * over / span, 1)


def portability_score(tags):
    base = 50.0
    base += 12 * _overlap(tags, PORTABLE_TAGS)
    base -= 18 * _overlap(tags, UNPORTABLE_TAGS)
    return round(max(0.0, min(100.0, base)), 1)


def art_score(tags):
    base = 50.0
    base += 12 * _overlap(tags, CHEAP_ART_TAGS)
    base -= 12 * _overlap(tags, EXPENSIVE_ART_TAGS)
    return round(max(0.0, min(100.0, base)), 1)


def legal_score(game):
    base = 75.0
    pub = (game.get("publisher") or "").lower()
    dev = (game.get("developer") or "").lower()
    if any(n in pub or n in dev for n in IP_RISK_PUBLISHERS):
        base -= 35
    return round(max(0.0, min(100.0, base)), 1)


def score_game(game):
    tags = _tag_set(game)
    subs = {
        "demand": demand_score(game),
        "portability": portability_score(tags),
        "art": art_score(tags),
        "legal": legal_score(game),
    }
    wsum = sum(WEIGHTS.values())
    composite = sum(subs[k] * WEIGHTS[k] for k in WEIGHTS) / wsum
    return {
        "appid": game.get("appid"),
        "name": game.get("name", "?"),
        "score": round(composite / 10.0, 1),
        "subs": subs,
        "owners": game.get("owners", ""),
        "tags_preview": ", ".join(sorted(list(tags))[:6]),
        "publisher": game.get("publisher", ""),
        "url": f"https://store.steampowered.com/app/{game.get('appid')}/",
    }


def to_markdown(scored, top_n):
    today = dt.date.today().isoformat()
    lines = [
        f"# 今日候选表 · {today}",
        "",
        f"> 鱼塘=目标品类标签 · 已淘汰 AAA/网游 · 取 Top {top_n}",
        "> 「移动版」需人工二次确认（SteamSpy 不提供），上架前去 App Store / Play 搜原名。",
        "",
        "| # | 名称 | 评分 | 需求 | 可移植 | 美术 | 法律 | 标签摘要 | 移动版 | Steam |",
        "|---|------|------|------|--------|------|------|----------|--------|-------|",
    ]
    for i, s in enumerate(scored[:top_n], 1):
        b = s["subs"]
        lines.append(
            f"| {i} | {s['name']} | **{s['score']}** | {b['demand']:.0f} | "
            f"{b['portability']:.0f} | {b['art']:.0f} | {b['legal']:.0f} | "
            f"{s['tags_preview']} | 待确认 | [↗]({s['url']}) |"
        )
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--also-trending", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo:
        pool = DEMO_GAMES
    else:
        if requests is None:
            sys.exit("缺少 requests，请先 pip install requests（或用 --demo）")
        print("按目标品类标签捞候选…", file=sys.stderr)
        pool = build_pool(args.also_trending)
        # 补全缺标签前，先按玩家数粗排，只给前 ENRICH_CAP 个补全，省请求
        pool.sort(key=lambda g: owners_midpoint(g.get("owners")), reverse=True)
        ENRICH_CAP, enriched = 60, 0
        for g in pool:                       # 补全缺标签的（限速）
            if enriched >= ENRICH_CAP:
                break
            if not g.get("tags"):
                enriched += 1
                try:
                    d = fetch_appdetails(g.get("appid"))
                    g["tags"] = d.get("tags"); g["genre"] = d.get("genre")
                    g["owners"] = d.get("owners") or g.get("owners")
                    g["publisher"] = d.get("publisher"); g["developer"] = d.get("developer")
                except Exception:
                    pass
                time.sleep(1.1)

    survivors = [g for g in pool if not is_disqualified(g, _tag_set(g))]
    scored = sorted((score_game(g) for g in survivors),
                    key=lambda s: s["score"], reverse=True)

    today = dt.date.today().isoformat()
    md = to_markdown(scored, args.top)
    with open(f"候选表_{today}.md", "w", encoding="utf-8") as f:
        f.write(md)
    with open(f"候选_{today}.json", "w", encoding="utf-8") as f:
        json.dump(scored[:args.top], f, ensure_ascii=False, indent=2)
    print(md)
    print(f"\n池子 {len(pool)} → 淘汰 {len(pool)-len(survivors)} → 候选 "
          f"{min(args.top, len(scored))}", file=sys.stderr)


# ============================================================
# 离线样例（--demo）：混入 AAA 验证淘汰/降权是否生效
# ============================================================
DEMO_GAMES = [
    {"appid": 1, "name": "ColorRush（休闲益智·示例）", "owners": "200,000 .. 500,000",
     "genre": "Casual", "tags": {"Casual": 9, "Puzzle": 7, "Pixel Graphics": 4, "2D": 5},
     "publisher": "Tiny", "developer": "Tiny"},
    {"appid": 2, "name": "Baldur's Gate 3（AAA RPG·应被淘汰）",
     "owners": "20,000,000 .. 50,000,000", "genre": "RPG",
     "tags": {"Turn-Based": 8, "RPG": 9, "3D": 8, "Story Rich": 7, "Open World": 6},
     "publisher": "Larian", "developer": "Larian"},
    {"appid": 3, "name": "DeckLoop（回合卡牌·示例）", "owners": "300,000 .. 800,000",
     "genre": "Strategy", "tags": {"Turn-Based": 8, "Card Game": 9, "Deckbuilding": 7, "2D": 4},
     "publisher": "Indie", "developer": "Indie"},
    {"appid": 4, "name": "PUBG（大逃杀·应被淘汰）", "owners": "50,000,000 .. 100,000,000",
     "genre": "Action", "tags": {"Battle Royale": 9, "Shooter": 8, "3D": 7},
     "publisher": "KRAFTON", "developer": "KRAFTON"},
    {"appid": 5, "name": "IdleTown（放置·示例）", "owners": "100,000 .. 200,000",
     "genre": "Casual", "tags": {"Idle": 9, "Clicker": 7, "Casual": 6, "Minimalist": 3},
     "publisher": "Solo", "developer": "Solo"},
    {"appid": 6, "name": "TinyTowerDefense（塔防·示例）", "owners": "400,000 .. 900,000",
     "genre": "Strategy", "tags": {"Tower Defense": 9, "Strategy": 6, "2D": 5, "Cute": 4},
     "publisher": "Indie", "developer": "Indie"},
]

if __name__ == "__main__":
    main()
