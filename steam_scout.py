#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场监测员 v3 —— 资料卡生成器（脚本取证，LLM 判案）
======================================================
分工：
  · 脚本（本文件）= 只取客观事实：Steam 官方分类/简介/平台 + 玩家数 + 查 App Store 有无手机版。
  · LLM（在 Cowork 里）= 读资料卡，判断"可移植性 / 法律风险"并打分。
  脚本绝不打可移植性/法律分——那是 LLM 的活。

数据源：
  · SteamSpy   → 发现候选（按目标品类标签）+ 玩家数
  · Steam 官方 → appdetails 拿真实分类、简介、平台、价格、厂商
  · iTunes API → 查 App Store 是否已有同名 iOS 版（你先打 iOS，正好对口）

用法:
    python steam_scout.py            # 真实数据，输出资料卡
    python steam_scout.py --cap 30   # 最多评估多少款（默认 40）
    python steam_scout.py --demo     # 离线样例，验证脚本逻辑

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
# 可调参数
# ============================================================
TARGET_TAGS = [
    "Casual", "Puzzle", "Turn-Based Strategy", "Card Game",
    "Idler", "Roguelike Deckbuilder", "Match 3", "Word Game",
    "Tower Defense", "Clicker", "Board Game", "Hidden Object",
]
OWNERS_MIN = 50_000           # 低于此太冷门
OWNERS_HARD_CAP = 12_000_000  # 高于此=AAA 体量，淘汰
POOL_CAP_DEFAULT = 40         # 最多对多少款做"取证"（控请求量）

STEAMSPY = "https://steamspy.com/api.php"
STEAM_APP = "https://store.steampowered.com/api/appdetails"
ITUNES = "https://itunes.apple.com/search"
HEADERS = {"User-Agent": "market-scout/3.0"}


def owners_midpoint(s):
    if not s:
        return 0.0
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", str(s))]
    return sum(nums) / len(nums) if nums else 0.0


# ---------- 发现候选 ----------
def fetch_tag(tag):
    r = requests.get(STEAMSPY, params={"request": "tag", "tag": tag},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return list(r.json().values())


def discover_pool():
    seen, pool = set(), []
    for tag in TARGET_TAGS:
        try:
            for g in fetch_tag(tag):
                aid = g.get("appid")
                if aid and aid not in seen:
                    seen.add(aid)
                    pool.append({"appid": aid, "name": g.get("name", ""),
                                 "owners": g.get("owners", "")})
        except Exception as e:
            print(f"  标签 {tag} 失败：{e}", file=sys.stderr)
        time.sleep(1.1)
    return pool


# ---------- Steam 官方取证 ----------
def fetch_steam_details(appid):
    r = requests.get(STEAM_APP, params={"appids": appid, "l": "english"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    node = r.json().get(str(appid), {})
    if not node.get("success"):
        return None
    d = node.get("data", {})
    if d.get("type") != "game":          # 过滤掉 DLC / 软件 / 壁纸引擎之类
        return None
    plat = d.get("platforms", {})
    return {
        "name": d.get("name"),
        "short_description": d.get("short_description", ""),
        "is_free": d.get("is_free", False),
        "price": (d.get("price_overview", {}) or {}).get("final_formatted", "Free" if d.get("is_free") else ""),
        "genres": [g["description"] for g in d.get("genres", [])],
        "categories": [c["description"] for c in d.get("categories", [])],
        "platforms": [k for k in ("windows", "mac", "linux") if plat.get(k)],
        "developer": (d.get("developers") or [""])[0],
        "publisher": (d.get("publishers") or [""])[0],
    }


# ---------- 查 App Store 有无手机版 ----------
def check_ios(name):
    try:
        r = requests.get(ITUNES, params={"term": name, "entity": "software",
                                         "country": "us", "limit": 3},
                         headers=HEADERS, timeout=20)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception:
        return {"checked": False, "found": False, "match": ""}
    name_l = name.lower().strip()
    for app in results:
        track = (app.get("trackName") or "").lower()
        # 名字高度相似才算命中（避免同名误判）
        if name_l in track or track in name_l:
            return {"checked": True, "found": True,
                    "match": app.get("trackName", ""), "seller": app.get("sellerName", "")}
    return {"checked": True, "found": False, "match": ""}


def build_dossiers(cap):
    print("SteamSpy 发现候选…", file=sys.stderr)
    pool = discover_pool()
    # 客观淘汰：玩家数太小 / 太大（AAA），再按玩家数排序取前 cap 个做取证
    pool = [g for g in pool
            if OWNERS_MIN <= owners_midpoint(g["owners"]) <= OWNERS_HARD_CAP]
    pool.sort(key=lambda g: owners_midpoint(g["owners"]), reverse=True)
    pool = pool[:cap]
    print(f"取证 {len(pool)} 款（Steam 官方 + iOS 查询）…", file=sys.stderr)

    dossiers = []
    for g in pool:
        det = None
        try:
            det = fetch_steam_details(g["appid"])
        except Exception:
            pass
        time.sleep(0.4)
        if not det:                      # 拿不到官方数据 / 非 game，跳过
            continue
        ios = check_ios(det["name"])
        time.sleep(0.4)
        dossiers.append({
            "appid": g["appid"],
            "name": det["name"],
            "url": f"https://store.steampowered.com/app/{g['appid']}/",
            "owners": g["owners"],
            "price": det["price"],
            "is_free": det["is_free"],
            "genres": det["genres"],
            "categories": det["categories"],
            "platforms": det["platforms"],
            "developer": det["developer"],
            "publisher": det["publisher"],
            "short_description": det["short_description"],
            "ios": ios,   # {checked, found, match, seller}
        })
    return dossiers


def to_markdown(dossiers):
    today = dt.date.today().isoformat()
    lines = [
        f"# 资料卡 · {today}",
        "",
        f"> 脚本只取客观事实，可移植性/法律由 LLM 判。共 {len(dossiers)} 款。",
        "> iOS 列：✅=App Store 已有同名版（多半淘汰）｜—=未发现｜?=没查到",
        "",
        "| 名称 | 玩家数 | 价格 | 分类 | iOS | Steam |",
        "|------|--------|------|------|-----|-------|",
    ]
    for d in dossiers:
        ios = d["ios"]
        ios_cell = "✅已有" if ios.get("found") else ("—" if ios.get("checked") else "?")
        genres = "/".join(d["genres"][:3])
        lines.append(
            f"| {d['name']} | {d['owners']} | {d['price'] or '-'} | "
            f"{genres} | {ios_cell} | [↗]({d['url']}) |"
        )
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=POOL_CAP_DEFAULT)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo:
        dossiers = DEMO_DOSSIERS
    else:
        if requests is None:
            sys.exit("缺少 requests，请先 pip install requests（或用 --demo）")
        dossiers = build_dossiers(args.cap)

    today = dt.date.today().isoformat()
    with open(f"资料卡_{today}.json", "w", encoding="utf-8") as f:
        json.dump(dossiers, f, ensure_ascii=False, indent=2)
    with open(f"资料卡_{today}.md", "w", encoding="utf-8") as f:
        f.write(to_markdown(dossiers))
    print(to_markdown(dossiers))
    print(f"\n已生成 {len(dossiers)} 张资料卡 → 资料卡_{today}.json", file=sys.stderr)


# ============================================================
# 离线样例（--demo）
# ============================================================
DEMO_DOSSIERS = [
    {"appid": 646570, "name": "Slay the Spire",
     "url": "https://store.steampowered.com/app/646570/",
     "owners": "5,000,000 .. 10,000,000", "price": "$24.99", "is_free": False,
     "genres": ["Indie", "Strategy"], "categories": ["Single-player"],
     "platforms": ["windows", "mac", "linux"], "developer": "Mega Crit Games",
     "publisher": "Mega Crit Games",
     "short_description": "A roguelike deckbuilding card game.",
     "ios": {"checked": True, "found": True, "match": "Slay the Spire", "seller": "Humble"}},
    {"appid": 999001, "name": "TinyDeck Roguelike",
     "url": "https://store.steampowered.com/app/999001/",
     "owners": "200,000 .. 500,000", "price": "$4.99", "is_free": False,
     "genres": ["Indie", "Strategy"], "categories": ["Single-player"],
     "platforms": ["windows"], "developer": "Solo Dev", "publisher": "Solo Dev",
     "short_description": "A small turn-based deckbuilder with cute pixel art.",
     "ios": {"checked": True, "found": False, "match": ""}},
    {"appid": 999002, "name": "GunBlast Arena",
     "url": "https://store.steampowered.com/app/999002/",
     "owners": "300,000 .. 800,000", "price": "$9.99", "is_free": False,
     "genres": ["Action"], "categories": ["Multi-player", "PvP"],
     "platforms": ["windows"], "developer": "Indie", "publisher": "Indie",
     "short_description": "Fast-paced first-person multiplayer shooter.",
     "ios": {"checked": True, "found": False, "match": ""}},
]

if __name__ == "__main__":
    main()
