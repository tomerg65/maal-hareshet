# -*- coding: utf-8 -*-
"""Builds the volleyball app page from public Israeli volleyball sources.

Run:  python volleyball/build.py
Reads template.html, writes maal-hareshet.html (the page that gets published).

Sources: Israel Volleyball Association site (table, games, sets, squads, news),
the Sport5 volleyball section, and Google News headlines about Israeli volleyball.
Only headlines and links are taken from news sites, never article text or photos.
"""
import hashlib, html, json, os, re, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).parent
BASE = "https://www.iva.org.il"
LEAGUES = {
    "m": {"name": "גברים", "url": BASE + "/league/?LeagueId=eaOcdd&cYear=2027"},
    "w": {"name": "נשים", "url": BASE + "/league/?LeagueId=fePcdd&cYear=2027"},
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
SPORT5 = "https://www.sport5.co.il/liga.aspx?FolderID=1715"
TRANSFER = re.compile(r"חתם|חתמה|חתימ|החתי|הצטרפ|מצטרפ|הצטרף|רכש|עזב|עוזב|שוחרר|מונה ל|מונתה|מינוי|"
                      r"זר נוסף|זרה נוספת|זר חדש|האריכ|האריך|חוזה|מאמן חדש|מאמנת חדשה|עבר מ|עברה מ|פרש|פרשה|מעבר גדול|מתחזק")
OTHER_SPORT = re.compile(r"כדורסל|כדורגל|כדוריד|NBA|יורוליג|יורוקאפ|ליגת האלופות|טניס|שחייה|ג'ודו")
FOREIGN = re.compile(r"וייטנאם|Vietnam|\.vn")
VB_ONLY = ["כדורעף", "מטה אשר", "עיילבון", "המעפיל", "חוסאם נצרת", "ליגת אתנה", "בנות נצרת", "SVA"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "he"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return res.read().decode("utf-8", "replace")


def text(s):
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def rows(table_html):
    body = table_html.split("<tbody", 1)[-1]
    out = []
    for m in re.finditer(r"<tr([^>]*)>(.*?)</tr>", body, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)
        out.append((m.group(1), cells))
    return out


def parse_league(page):
    tables = re.findall(r"<table.*?</table>", page, re.S)
    standings = []
    for _, c in rows(tables[0]):
        if len(c) < 12:
            continue
        t = [text(x) for x in c]
        standings.append({
            "pos": int(t[0]), "team": t[1], "g": int(t[2]), "w": int(t[3]), "l": int(t[4]),
            "pf": int(t[5]), "pa": int(t[6]), "pr": t[7],
            "sf": int(t[8]), "sa": int(t[9]), "sr": t[10], "pts": int(t[11]),
        })
    games = []
    for attrs, c in rows(tables[1]):
        if len(c) < 6:
            continue
        rnd = re.search(r'data-round="(\d+)"', attrs)
        gid = re.search(r"GameId=(\w+)", c[0])
        score = re.search(r"(\d)\s*-\s*(\d)", text(c[0]))
        d = re.search(r"(\d{2})/(\d{2})/(\d{4}).*?(\d{1,2}:\d{2})", text(c[5]))
        if not d:
            continue
        g = {
            "r": int(rnd.group(1)) if rnd else 0,
            "at": f"{d.group(3)}-{d.group(2)}-{d.group(1)}T{d.group(4).zfill(5)}",
            "host": text(c[3]), "guest": text(c[2]), "venue": text(c[1]),
            "id": gid.group(1) if gid else None,
        }
        if score:  # the site lists the score as guest-host
            g["hs"], g["gs"] = int(score.group(2)), int(score.group(1))
        games.append(g)
    return standings, games


def parse_squads(page):
    """Squads and staff printed on a game page: {team: {"players": [...], "staff": [...]}}."""
    out = {}
    for m in re.finditer(r"<h3>(.*?)</h3>\s*<div class=\"players-container\">(.*?)</section>", page, re.S):
        team = text(m.group(1)).replace("סגל הקבוצה", "").strip()
        players = []
        for block in m.group(2).split('<div class="player">')[1:]:
            t = text(block)
            p = re.search(r"#\s*(\d*)\s*(.+)$", t)
            if not p or not p.group(2).strip():
                continue
            h = re.search(r"\|\s*([\d.]+)", t)
            height = None
            if h:
                v = float(h.group(1))
                v = v * 100 if v < 3 else v
                height = int(round(v)) if 150 <= v <= 225 else None
            players.append({"n": p.group(1), "name": p.group(2).strip(), "h": height})
        if players:
            players.sort(key=lambda x: int(x["n"]) if x["n"] else 99)
            out.setdefault(team, {})["players"] = players
    for m in re.finditer(r"<h3>(.*?)</h3>\s*<div class=\"team-main__body\">(.*?)</section>", page, re.S):
        team = text(m.group(1)).replace("צוות מקצועי", "").strip()
        staff = [{"name": text(a), "role": text(b)} for a, b in re.findall(
            r'team-main__item-title">(.*?)</div>\s*<div class="team-main__item-role">(.*?)</div>', m.group(2), re.S)]
        order = {"מאמן": 0, "מאמנת": 0, "עוזר מאמן": 1, "עוזרת מאמן": 1}
        staff.sort(key=lambda s: order.get(s["role"], 5))
        if staff:
            out.setdefault(team, {})["staff"] = staff
    return out


def fetch_game(game):
    """Adds set scores to a finished game and returns the squads listed on its page."""
    try:
        page = get(f"{BASE}/game-zone/?GameId={game['id']}")
    except Exception:
        return None
    live = re.search(r"dataproject\.com/Livescore_Adv\.aspx\?ID=(\d+)", page)
    if live:
        game["live"] = live.group(1)
    if "hs" not in game:
        return {}
    try:
        part = page.split("תוצאה לפי מערכות", 1)[1]
        table = re.search(r"<table.*?</table>", part, re.S).group(0)
        nums = []
        for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", table.split("<tbody", 1)[-1], re.S):
            cells = [text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)]
            vals = [int(x) for x in cells[1:-1] if x.isdigit()]
            if vals:
                nums.append(vals)
        if len(nums) >= 2:
            sets = [[a, b] for a, b in zip(nums[0], nums[1]) if a or b]
            won = sum(1 for a, b in sets if a > b)
            if sets and won == game["hs"] and len(sets) - won == game["gs"]:
                game["sets"] = sets
    except Exception:
        pass
    try:
        return parse_squads(page)
    except Exception:
        return {}


# ---------- news ----------
def norm(title):
    return re.sub(r"[^0-9A-Za-zא-ת]", "", title)


def iva_news():
    posts = json.loads(get(BASE + "/wp-json/wp/v2/posts?per_page=40&_fields=date,link,title"))
    return [{"title": html.unescape(p["title"]["rendered"]).strip(), "url": p["link"],
             "date": p["date"][:16], "src": "איגוד הכדורעף"} for p in posts]


def sport5_news():
    page = get(SPORT5)
    items, seen = [], set()
    for url, inner in re.findall(r'<a[^>]+href="(https://www\.sport5\.co\.il/articles\.aspx\?FolderID=1716&(?:amp;)?docID=\d+)"[^>]*>(.*?)</a>', page, re.S):
        url, title = html.unescape(url), text(inner)
        if url in seen or len(title) < 12:
            continue
        seen.add(url)
        items.append({"title": title, "url": url, "date": None, "src": "ספורט 5"})

    def stamp(it):
        try:
            m = re.search(r"(\d{2}):(\d{2})\s*-\s*(\d{2})\.(\d{2})\.(\d{2})", get(it["url"]))
            if m:
                it["date"] = f"20{m.group(5)}-{m.group(4)}-{m.group(3)}T{m.group(1)}:{m.group(2)}"
        except Exception:
            pass
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(stamp, items))
    return [i for i in items if i["date"]]


def google_news(query, people):
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=he&gl=IL&ceid=IL:he"
    out = []
    for t, link, d, src in re.findall(
            r"<item><title>(.*?)</title><link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>.*?<source[^>]*>(.*?)</source>",
            get(url), re.S):
        title, src = html.unescape(t), html.unescape(src)
        if title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        title = re.sub(r"\s+", " ", title).strip()
        if FOREIGN.search(title + src) or OTHER_SPORT.search(title):
            continue
        if not (any(k in title for k in VB_ONLY) or any(p in title for p in people)):
            continue  # keep only headlines that are verifiably about volleyball
        try:
            when = parsedate_to_datetime(d).astimezone().strftime("%Y-%m-%dT%H:%M")
        except Exception:
            continue
        out.append({"title": title, "url": html.unescape(link), "date": when,
                    "src": {"Sport5": "ספורט 5"}.get(src, src)})
    return out


def team_aliases(leagues):
    """City-style short names used to spot teams in headlines."""
    drop = ["הפועל", "מכבי", "מ.ס.", "מ.כ.", "עירוני", "SVA", "XT", "yullia", '"גליל"', "יואב", "יעדים",
            "מוסינזון", "אריות", "חוסאם", "בנות"]
    out = []
    for key, lg in leagues.items():
        for row in lg["standings"]:
            name = row["team"]
            short = name
            for w in drop:
                short = short.replace(w, " ")
            short = re.sub(r"\s+", " ", short.split("/")[0]).strip()
            alts = {short, short.replace("קרית", "קריית")}
            if "תל אביב" in short:
                alts |= {'ת"א', "ת״א"}
            out.append((key, name, [a for a in alts if len(a) >= 3]))
    return out


def build_news(leagues, people):
    items = []
    for fn in (iva_news, sport5_news):
        try:
            items += fn()
        except Exception as e:
            print("news source failed:", fn.__name__, e)
    verbs = "(חתם OR חתמה OR החתימה OR הצטרף OR מצטרף OR מצטרפת OR עבר OR רכש OR עזב OR שוחרר OR מונה OR מינוי)"
    for q in ["intitle:כדורעף when:240d", "intitle:כדורעף " + verbs + " when:300d",
              "כדורעף " + verbs + " site:sport5.co.il when:300d", "כדורעף site:one.co.il when:240d",
              "כדורעף site:sport5.co.il when:120d", '"ליגת אתנה" OR "מטה אשר" כדורעף when:120d']:
        try:
            items += google_news(q, people)
        except Exception as e:
            print("news query failed:", q[:30], e)
    cutoff = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
    aliases = team_aliases(leagues)
    seen, out = set(), []
    for it in sorted(items, key=lambda x: (x["src"] != "איגוד הכדורעף", x["url"].startswith("https://news.google"))):
        key = norm(it["title"])[:40]
        if key in seen or it["date"] < cutoff:
            continue
        seen.add(key)
        t = it["title"]
        if TRANSFER.search(t):
            it["cat"] = "transfer"
        elif re.search(r"נבחרת|אליפות אירופה|מוקדמות|חופים", t):
            it["cat"] = "national"
        elif re.search(r"גביע האתגר|באירופה|אירופי|צ'אלנג'", t):
            it["cat"] = "europe"
        else:
            it["cat"] = "league"
        women = bool(re.search(r"נשים|אתנה|ניצחה|הצהובות|בנות", t))
        teams = []
        for key2, name, alts in aliases:
            if any(a in t for a in alts):
                teams.append([key2, name])
        both = {k for k, _ in teams}
        if len(both) == 2 and re.search(r"נשים|אתנה|גברים", t):
            keep = "w" if re.search(r"נשים|אתנה", t) else "m"
            teams = [x for x in teams if x[0] == keep]
        it["teams"] = teams[:6]
        it["w"] = women
        out.append(it)
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:70]


SITE_HEAD = ('<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
             '<meta name="description" content="ליגת העל בכדורעף, גברים ונשים: תוצאות, טבלה, לוח משחקים, קבוצות, חדשות ורכש.">'
             '<meta name="theme-color" content="#0a2d75"><meta property="og:title" content="מעל הרשת ע״י אלי">'
             '<meta property="og:description" content="ליגת העל בכדורעף: תוצאות, טבלה, משחקים, קבוצות וחדשות.">'
             '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E'
             '%3Ccircle cx=%2716%27 cy=%2716%27 r=%2715%27 fill=%27%23f4b400%27 stroke=%27%230a2d75%27 stroke-width=%272%27/%3E%3C/svg%3E">'
             '<style>:root{color-scheme:light}body{margin:0;font:14px system-ui}img{max-width:100%}'
             '[hidden]{display:none!important}</style>')
CI = bool(os.environ.get("GITHUB_ACTIONS"))
CACHE = HERE / "cache" / "data.json" if CI else HERE / "data.json"
MAX_PAGES = 14          # game pages fetched per run, to stay gentle with the federation site
NEWS_EVERY_MIN = 45


def render(payload):
    tpl = (HERE / "template.html").read_text(encoding="utf-8")
    # The page published as a Claude artifact (no polling there).
    (HERE / "maal-hareshet.html").write_text(tpl.replace("/*__DATA__*/null", payload), encoding="utf-8")
    # The public site: full document, data inlined for the first paint, then polled from data.json.
    head, body = tpl.split('<div dir="rtl" lang="he" id="app">', 1)
    body = '<div dir="rtl" lang="he" id="app">' + body
    body = body.replace("/*__DATA__*/null", payload).replace("/*__LIVE__*/false", "true")
    site = HERE / "docs"
    site.mkdir(exist_ok=True)
    (site / "index.html").write_text(SITE_HEAD + head + "</head><body>" + body + "</body></html>", encoding="utf-8")
    (site / "data.json").write_text(payload, encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")


def load_cache():
    for p in (CACHE, HERE / "data.json"):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def main():
    old = load_cache()
    if "--offline" in sys.argv:  # re-render from the saved data, no network
        render(json.dumps(old, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
        print("rendered from saved data")
        return
    now = datetime.now(timezone.utc)
    data = {"updated": old.get("updated", ""), "season": "2026/27", "leagues": {},
            "news": old.get("news", []), "news_at": old.get("news_at", "")}
    today = datetime.now().strftime("%Y-%m-%d")
    soon = (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d")
    budget, fresh, people = MAX_PAGES, 0, set()
    for key, lg in LEAGUES.items():
        oldlg = old.get("leagues", {}).get(key) or {}
        try:
            standings, games = parse_league(get(lg["url"]))
            if len(standings) < 6 or len(games) < 20:
                raise ValueError(f"parsed too little ({len(standings)} teams, {len(games)} games)")
        except Exception as e:
            print("league", key, "not refreshed:", e)
            if not oldlg:
                sys.exit("no saved data to fall back on")
            data["leagues"][key] = oldlg
            continue
        fresh += 1
        prev = {g["id"]: g for g in oldlg.get("games", []) if g.get("id")}
        squads = dict(oldlg.get("squads", {}))
        for g in games:
            p = prev.get(g["id"]) or {}
            for f in ("live", "lc", "nf"):
                if f in p:
                    g[f] = p[f]
            if "sets" in p and p.get("hs") == g.get("hs") and p.get("gs") == g.get("gs"):
                g["sets"] = p["sets"]
        games_by_time = sorted(games, key=lambda g: g["at"])
        todo = [g for g in games_by_time if g["id"] and "hs" in g and "sets" not in g and not g.get("nf")]
        todo += [g for g in games_by_time if g["id"] and "hs" not in g and "live" not in g
                 and today <= g["at"][:10] <= soon and g.get("lc") != today]
        names = {r["team"] for r in standings}
        for g in todo:
            if budget <= 0:
                break
            budget -= 1
            time.sleep(0.5)
            sq = fetch_game(g)
            if sq is None:  # page did not load; try again on a later run
                continue
            if "hs" in g:
                g["nf"] = 1
                for team, v in sq.items():
                    if team in names:
                        squads.setdefault(team, {}).update(v)
            else:
                g["lc"] = today
        data["leagues"][key] = {"name": lg["name"], "url": lg["url"], "standings": standings,
                                "games": games, "squads": squads}
    for lg in data["leagues"].values():
        for v in lg.get("squads", {}).values():
            for p in v.get("players", []) + v.get("staff", []):
                if re.fullmatch(r"[א-ת'\"׳״\- ]+", p["name"]) and len(p["name"].split()) >= 2 and len(p["name"]) >= 8:
                    people.add(p["name"])
    stale = True
    try:
        stale = (now - datetime.fromisoformat(data["news_at"])).total_seconds() > NEWS_EVERY_MIN * 60
    except Exception:
        pass
    if stale or "--news" in sys.argv:
        news = build_news(data["leagues"], people)
        if len(news) >= 5:
            data["news"], data["news_at"] = news, now.isoformat(timespec="seconds")
    if fresh:
        data["updated"] = now.isoformat(timespec="seconds")
    data["checked"] = now.isoformat(timespec="seconds")
    body = json.dumps([data["leagues"], data["news"]], ensure_ascii=False, sort_keys=True)
    data["rev"] = hashlib.md5(body.encode("utf-8")).hexdigest()[:12]

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    render(payload)
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(payload, encoding="utf-8")
    for key, lg in data["leagues"].items():
        done = [g for g in lg["games"] if "hs" in g]
        print(key, len(lg["standings"]), "teams,", len(done), "finished,", sum(1 for g in done if "sets" in g),
              "with sets,", sum(1 for g in lg["games"] if "live" in g), "live links,", len(lg["squads"]), "squads")
    print("leagues refreshed:", fresh, "/ 2 | game pages fetched:", MAX_PAGES - budget,
          "| news:", len(data["news"]), "| rev", data["rev"], "| updated", data["updated"])


if __name__ == "__main__":
    main()
