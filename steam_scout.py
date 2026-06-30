#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场监测员 v1 —— SteamSpy 热门游戏 + 可移植性打分
==================================================
每天拉 SteamSpy 近两周最热的游戏，按"做成手机版的可移植性"打分，
输出一张排好序的候选表（Markdown + JSON）。

用法:
    python steam_scout.py                # 拉真实数据，输出今日候选表
    python steam_scout.py --top 20       # 指定输出候选数量（默认 15）
    python steam_scout.py --pool 40      # 参与精评的热门池大小（默认 35）
    python steam_scout.py --no-enrich    # 不逐个补全标签（更快，但打分更糙）
    python steam_scout.py --demo         # 用内置样例数据跑一遍（不联网，验证脚本）

依赖:
    pip install requests
"""

import argparse
import datetime as dt
import json
import sys
import time

try:
    import requests
except ImportError:
    requests = None  # --demo 模式下不需要

# ============================================================
# 可调参数区（James 直接改这里就能调打分口味）
# ============================================================

# 四个维度的权重（不必加起来等于 1，脚本会自动归一化）
WEIGHTS = {
    "heat":        0.35,   # 热度：越热越好
    "portability": 0.35,   # 可移植性：越适合搬手机越好
    "art":         0.15,   # 美术成本：越省美术越好
    "legal":       0.15,   # 法律安全：越不容易踩 IP 越好
}

# 可移植性：这些标签代表"适合搬手机"（单手 / 回合 / 休闲 / 益智）
PORTABLE_TAGS = {
    "casual", "puzzle", "turn-based", "card game", "deckbuilding",
    "idle", "clicker", "match 3", "hypercasual", "board game",
    "strategy", "roguelike", "roguelite", "tower defense",
    "point & click", "indie", "minimalist", "singleplayer",
    "2d platformer", "auto battler", "word game", "simulation",
}
# 这些标签代表"不适合搬手机"（需要键鼠精准 / 重 3D / 高操作）
UNPORTABLE_TAGS = {
    "fps", "first-person", "shooter", "action", "open world",
    "souls-like", "racing", "flight", "rts", "moba",
    "fighting", "3d platformer", "vr", "horror", "stealth",
    "twin stick shooter", "hack and slash", "bullet hell",
}

# 美术成本：便宜的（加分）
CHEAP_ART_TAGS = {
    "pixel graphics", "2d", "minimalist", "hand-drawn",
    "retro", "cute", "colorful", "top-down",
}
# 美术成本：贵的（减分）
EXPENSIVE_ART_TAGS = {
    "3d", "realistic", "photorealistic", "atmospheric",
    "great soundtrack", "cinematic", "gore",
}

# 法律风险提示：命中这些大厂/强 IP 关键词时，降低"法律安全分"
IP_RISK_PUBLISHERS = {
    "valve", "nintendo", "sega", "capcom", "square enix",
    "bandai namco", "ubisoft", "electronic arts", "activision",
    "take-two", "rockstar", "bethesda", "disney", "warner",
}

# ============================================================
# 数据获取
# ============================================================

STEAMSPY = "https://steamspy.com/api.php"
HEADERS = {"User-Agent": "market-scout/1.0"}


def fetch_top_2weeks():
    """拉近两周最热的 100 款游戏。返回 [game_dict, ...]，按热度降序。"""
    r = requests.get(STEAMSPY, params={"request": "top100in2weeks"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()  # 形如 { "appid": {游戏字段...}, ... }
    games = list(data.values())
    # SteamSpy 已按近两周玩家数排序，但保险起见用 average_2weeks 再排一次
    games.sort(key=lambda g: _num(g.get("average_2weeks")), reverse=True)
    return games


def fetch_appdetails(appid):
    """补全单款游戏的详细字段（含完整 tags）。"""
    r = requests.get(STEAMSPY, params={"request": "appdetails", "appid": appid},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


# ============================================================
# 打分
# ============================================================

def _num(x):
    """把 SteamSpy 里可能是字符串/None 的数字安全转成 float。"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _tag_set(game):
    """把游戏的 tags + genre 统一成一个小写标签集合。"""
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
    """tags 与参考集合的命中个数。"""
    return len(tags & ref)


def heat_score(game, rank, total):
    """热度分 0-100：榜单位置越靠前越高，再叠加近两周活跃。"""
    pos = 100.0 * (1 - rank / max(total, 1))          # 排名分
    active = min(_num(game.get("average_2weeks")) / 6.0, 100.0)  # 近两周人均分钟→粗略活跃
    return round(0.7 * pos + 0.3 * active, 1)


def portability_score(tags):
    base = 50.0
    base += 12 * _overlap(tags, PORTABLE_TAGS)
    base -= 15 * _overlap(tags, UNPORTABLE_TAGS)
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
    for name in IP_RISK_PUBLISHERS:
        if name in pub or name in dev:
            base -= 35
            break
    return round(max(0.0, min(100.0, base)), 1)


def score_game(game, rank, total):
    tags = _tag_set(game)
    subs = {
        "heat":        heat_score(game, rank, total),
        "portability": portability_score(tags),
        "art":         art_score(tags),
        "legal":       legal_score(game),
    }
    wsum = sum(WEIGHTS.values())
    composite = sum(subs[k] * WEIGHTS[k] for k in WEIGHTS) / wsum
    total10 = round(composite / 10.0, 1)   # 归一到 0-10，对齐看板"候选评分"
    return {
        "appid": game.get("appid"),
        "name": game.get("name", "?"),
        "score": total10,
        "subs": subs,
        "tags_preview": ", ".join(sorted(list(tags))[:6]),
        "publisher": game.get("publisher", ""),
        "url": f"https://store.steampowered.com/app/{game.get('appid')}/",
    }


# ============================================================
# 输出
# ============================================================

def to_markdown(scored, top_n):
    today = dt.date.today().isoformat()
    lines = [
        f"# 今日候选表 · {today}",
        "",
        f"> 数据源 SteamSpy 近两周最热 · 已按可移植性打分 · 取 Top {top_n}",
        "> 「移动版」一栏需人工二次确认（SteamSpy 不提供），上架前去 App Store / Play 搜一下。",
        "",
        "| # | 名称 | 评分 | 热度 | 可移植 | 美术 | 法律 | 标签摘要 | 移动版 | Steam |",
        "|---|------|------|------|--------|------|------|----------|--------|-------|",
    ]
    for i, s in enumerate(scored[:top_n], 1):
        sub = s["subs"]
        lines.append(
            f"| {i} | {s['name']} | **{s['score']}** | {sub['heat']:.0f} | "
            f"{sub['portability']:.0f} | {sub['art']:.0f} | {sub['legal']:.0f} | "
            f"{s['tags_preview']} | 待确认 | [↗]({s['url']}) |"
        )
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15, help="输出候选数量")
    ap.add_argument("--pool", type=int, default=35, help="参与精评的热门池大小")
    ap.add_argument("--no-enrich", action="store_true", help="不逐个补全标签")
    ap.add_argument("--demo", action="store_true", help="用样例数据离线跑")
    args = ap.parse_args()

    if args.demo:
        games = DEMO_GAMES
    else:
        if requests is None:
            sys.exit("缺少 requests，请先 pip install requests（或用 --demo）")
        print("拉取 SteamSpy 近两周最热…", file=sys.stderr)
        games = fetch_top_2weeks()

    # 先按热度取热门池，只精评这批，省请求
    pool = games[: args.pool]

    # 补全标签（默认开，礼貌限速 1.1s/次）
    if not args.demo and not args.no_enrich:
        print(f"补全 {len(pool)} 款的标签（约 {len(pool)} 秒）…", file=sys.stderr)
        for g in pool:
            if not g.get("tags"):
                try:
                    detail = fetch_appdetails(g.get("appid"))
                    g["tags"] = detail.get("tags")
                    g["genre"] = detail.get("genre") or g.get("genre")
                    g["publisher"] = detail.get("publisher") or g.get("publisher")
                    g["developer"] = detail.get("developer") or g.get("developer")
                except Exception:
                    pass
                time.sleep(1.1)

    total = len(pool)
    scored = [score_game(g, rank, total) for rank, g in enumerate(pool)]
    scored.sort(key=lambda s: s["score"], reverse=True)

    today = dt.date.today().isoformat()
    md = to_markdown(scored, args.top)

    md_path = f"候选表_{today}.md"
    json_path = f"候选_{today}.json"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(scored[: args.top], f, ensure_ascii=False, indent=2)

    print(md)
    print(f"\n已保存: {md_path} / {json_path}", file=sys.stderr)


# ============================================================
# 离线样例数据（--demo 用，验证脚本逻辑）
# ============================================================

DEMO_GAMES = [
    {"appid": 1001, "name": "ColorRush（变色躲障碍·示例）",
     "average_2weeks": 320, "genre": "Casual, Indie",
     "tags": {"Casual": 900, "Puzzle": 700, "Pixel Graphics": 400, "2D": 500},
     "publisher": "Tiny Indie", "developer": "Tiny Indie"},
    {"appid": 1002, "name": "MegaShooter 9（重 3D 射击·示例）",
     "average_2weeks": 410, "genre": "Action, FPS",
     "tags": {"FPS": 1200, "Shooter": 900, "3D": 800, "Realistic": 600},
     "publisher": "Valve", "developer": "Valve"},
    {"appid": 1003, "name": "Deck Tactics（回合卡牌·示例）",
     "average_2weeks": 260, "genre": "Strategy, Indie",
     "tags": {"Turn-Based": 800, "Card Game": 850, "Deckbuilding": 700,
              "Roguelike": 500, "2D": 400},
     "publisher": "Indie Co", "developer": "Indie Co"},
    {"appid": 1004, "name": "Idle Empire（放置经营·示例）",
     "average_2weeks": 180, "genre": "Casual, Simulation",
     "tags": {"Idle": 900, "Clicker": 700, "Casual": 600, "Minimalist": 300},
     "publisher": "Solo Dev", "developer": "Solo Dev"},
    {"appid": 1005, "name": "RaceKing 3D（竞速·示例）",
     "average_2weeks": 300, "genre": "Racing, Sports",
     "tags": {"Racing": 1000, "3D": 700, "Atmospheric": 400},
     "publisher": "Indie Co", "developer": "Indie Co"},
]

if __name__ == "__main__":
    main()
