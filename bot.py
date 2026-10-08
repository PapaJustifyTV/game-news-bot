"""
Агрегатор ігрових новин для Telegram.

Джерела: RSS ігрових видань, X (Twitter), Steam (новини ігор і зміни дат виходу),
оголошення Discord-серверів. ШІ оцінює важливість кожної новини (критерії —
у importance.txt), відсіює мотлох, решту коротко переказує українською і
публікує з картинкою в чорновий Telegram-канал. Запуск — GitHub Actions.

Змінні середовища (у GitHub: Settings -> Secrets and variables -> Actions):
  TELEGRAM_BOT_TOKEN  токен бота від @BotFather
  TELEGRAM_CHAT_ID    ID чорнового каналу (наприклад -1001234567890)
  LLM_API_KEY         ключ ШІ-провайдера (для Gemini — з aistudio.google.com)
  LLM_PROVIDER        gemini (безкоштовно, за замовчуванням) | groq | openrouter | openai
  LLM_MODEL           необов'язково: конкретна модель
  X_API_KEY           необов'язково: ключ twitterapi.io, щоб читати X/Twitter
  DISCORD_BOT_TOKEN   необов'язково: токен свого Discord-бота, щоб читати оголошення
  MIN_SCORE           поріг важливості 0–10 для всіх джерел (5)
  MIN_SCORE_ARTICLE / MIN_SCORE_TWEET / MIN_SCORE_STEAM / MIN_SCORE_DISCORD
                      окремий поріг для джерела (якщо треба інший)
  MAX_LLM_CALLS       максимум запитів до ШІ за запуск (80) — бережемо безкоштовні ліміти
  MAX_POSTS_PER_RUN   скільки постів максимум за запуск (25)
  MAX_AGE_HOURS       новини старші за це не беремо (12)
  DRY_RUN=1           нічого не надсилати, лише друкувати в лог
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
FEEDS_FILE = BASE_DIR / "feeds.txt"
X_ACCOUNTS_FILE = BASE_DIR / "x_accounts.txt"
STEAM_FILE = BASE_DIR / "steam_apps.txt"
DISCORD_FILE = BASE_DIR / "discord_channels.txt"
IMPORTANCE_FILE = BASE_DIR / "importance.txt"
SEEN_FILE = BASE_DIR / "seen.json"
STATE_FILE = BASE_DIR / "state.json"

# ---------------------------------------------------------------- ШІ-провайдери
# Усі працюють через однаковий OpenAI-сумісний формат запиту.
PROVIDERS = {
    "gemini": {
        # Gemini викликаємо через власний REST Google: нові ключі формату «AQ.»
        # не працюють через OpenAI-сумісну адресу.
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "models": ["gemini-3.1-flash-lite", "gemini-2.5-flash"],
        "reasoning": "low",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile"],
        "reasoning": None,
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models": [],  # задайте LLM_MODEL, напр. назву моделі з позначкою :free на openrouter.ai
        "reasoning": None,
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "models": ["gpt-5.4-mini", "gpt-4o-mini"],
        "reasoning": "low",
    },
}

LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "gemini").strip().lower()
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
if LLM_PROVIDER not in PROVIDERS:
    sys.exit(f"Невідомий LLM_PROVIDER: {LLM_PROVIDER}. Варіанти: {', '.join(PROVIDERS)}")
PROVIDER = PROVIDERS[LLM_PROVIDER]
MODELS = ([os.getenv("LLM_MODEL")] if os.getenv("LLM_MODEL") else []) + PROVIDER["models"]
if not MODELS:
    sys.exit(f"Для {LLM_PROVIDER} треба задати змінну LLM_MODEL")

X_API_KEY = os.getenv("X_API_KEY", "")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
MIN_SCORE_DEFAULT = int(os.getenv("MIN_SCORE") or "5")
MIN_SCORE = {
    kind: int(os.getenv(f"MIN_SCORE_{kind.upper()}") or MIN_SCORE_DEFAULT)
    for kind in ("article", "tweet", "steam", "discord")
}
MAX_LLM_CALLS = int(os.getenv("MAX_LLM_CALLS") or "80")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
MAX_POSTS = int(os.getenv("MAX_POSTS_PER_RUN") or "25")
MAX_AGE = timedelta(hours=float(os.getenv("MAX_AGE_HOURS") or "12"))
DRY_RUN = os.getenv("DRY_RUN") == "1"
SEEN_LIMIT = 8000  # скільки посилань пам'ятати

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    )
}

SOURCE_HINTS = {
    "article": "англомовну статтю ігрового видання. Перекажи суть своїми словами, не копіюй речення дослівно.",
    "tweet": "допис з X (Twitter) від студії, видавця, розробника чи новинного акаунта.",
    "steam": "оголошення розробників зі Steam (новина гри, патч-нотатки або зміна дати виходу).",
    "discord": "оголошення з Discord-сервера гри чи студії.",
}


def build_prompt(kind):
    criteria = "\n".join(
        l for l in IMPORTANCE_FILE.read_text(encoding="utf-8").splitlines()
        if not l.lstrip().startswith("#")
    ).strip() if IMPORTANCE_FILE.exists() else "Оціни важливість новини від 0 до 10."
    return f"""Ти — редактор українського Telegram-каналу новин відеоігрової індустрії.
Тобі дають {SOURCE_HINTS[kind]}

КРОК 1. Оціни важливість (score, ціле число 0–10) за цими критеріями:
{criteria}

КРОК 2. Напиши українською:
- title: заголовок-суть до 100 символів, без клікбейту;
- summary: 2–4 речення, до 600 символів: що сталося, хто, коли, чому це важливо.
  Прибери хештеги, емодзі, згадки @, заклики «репостни/підпишись».
Якщо score нижче 3 — title і summary можна залишити порожніми.

Правила мови:
- Жива літературна українська, без калькування з англійської і без русизмів.
- Назви ігор, студій, платформ і серій — в оригіналі (Elden Ring, FromSoftware, PS5).
- Нічого не вигадуй, лише факти з наданого тексту.
- Дати та ціни — у звичному українському форматі.

Відповідай ЛИШЕ JSON-об'єктом: {{"score": 0, "title": "...", "summary": "..."}}"""


# ---------------------------------------------------------------- утиліти

def log(*args):
    print(*args, flush=True)


def read_list(path):
    """Рядки файлу без порожніх і коментарів. Формат: 'Назва | значення' або просто 'значення'."""
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name, value = [p.strip() for p in line.split("|", 1)]
        else:
            name, value = "", line
        result.append((name, value))
    return result


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")


def normalize_link(link):
    """Прибирає utm-мітки та якорі, щоб одна стаття не рахувалась двічі."""
    p = urlparse(link)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")


def strip_html(text):
    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


def entry_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


# ---------------------------------------------------------------- RSS: картинки і текст

def image_from_entry(entry):
    """Шукає картинку прямо в RSS: enclosure, media:content, media:thumbnail, <img>."""
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image") and enc.get("href"):
            return enc["href"]
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            url = m.get("url")
            if url and (m.get("medium") in (None, "image") or "image" in m.get("type", "")):
                return url
    for field in ("content", "summary"):
        raw = entry.get(field)
        if isinstance(raw, list):
            raw = " ".join(c.get("value", "") for c in raw)
        if raw:
            img = BeautifulSoup(raw, "html.parser").find("img")
            if img and img.get("src"):
                return img["src"]
    return None


def fetch_page(url, want_text=True):
    """Завантажує сторінку: повертає (текст, og:image). Помилки не фатальні."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log(f"   ! не вдалось відкрити сторінку: {e}")
        return "", None

    soup = BeautifulSoup(r.text, "html.parser")
    og = None
    for attr in ({"property": "og:image"}, {"name": "twitter:image"}):
        tag = soup.find("meta", attrs=attr)
        if tag and tag.get("content"):
            og = urljoin(url, tag["content"])
            break
    if not want_text:
        return "", og

    for bad in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        bad.decompose()
    container = soup.find("article") or soup.body or soup
    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    text = "\n".join(p for p in paragraphs if len(p) > 40)
    return text[:6000], og


def collect_rss(seen_set, remember):
    now = datetime.now(timezone.utc)
    items = []
    for name, url in read_list(FEEDS_FILE):
        source = name or urlparse(url).netloc.replace("www.", "")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            feed = feedparser.parse(resp.content)
        except Exception as e:
            log(f"! {source}: не вдалось отримати RSS ({e})")
            continue
        if not feed.entries:
            log(f"! {source}: стрічка порожня або недоступна (HTTP {resp.status_code})")
            continue
        for entry in feed.entries:
            link = entry.get("link")
            if not link:
                continue
            key = normalize_link(link)
            if key in seen_set:
                continue
            published = entry_date(entry)
            if published and now - published > MAX_AGE:
                remember(key)  # стара новина — запам'ятовуємо і пропускаємо
                continue
            items.append({
                "kind": "article",
                "key": key,
                "link": link,
                "source": source,
                "title": strip_html(entry.get("title", "")),
                "text": strip_html(entry.get("summary", "")),
                "image": image_from_entry(entry),
                "date": published or now,
            })
    return items


# ---------------------------------------------------------------- X (Twitter) через twitterapi.io

def tweet_date(value):
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
    return datetime.now(timezone.utc)


def tweet_image(tw):
    """Фото з твіта (для відео — його обкладинка); якщо нема — картинка з посилання у твіті."""
    for holder in (tw.get("extendedEntities"), tw.get("entities"), tw):
        for m in (holder or {}).get("media", []) or []:
            url = m.get("media_url_https") or m.get("media_url") or m.get("url")
            if url and "pbs.twimg.com" in url:
                return url
    for u in ((tw.get("entities") or {}).get("urls") or []):
        expanded = u.get("expanded_url") or ""
        host = urlparse(expanded).netloc
        if expanded and not any(h in host for h in ("x.com", "twitter.com", "t.co")):
            _, og = fetch_page(expanded, want_text=False)
            if og:
                return og
    return None


def collect_tweets(seen_set, state):
    accounts = [h.lstrip("@") for _, h in read_list(X_ACCOUNTS_FILE)]
    if not accounts:
        return []
    if not X_API_KEY:
        log("i x_accounts.txt заповнено, але X_API_KEY не задано — X пропускаю")
        return []

    now = datetime.now(timezone.utc)
    # Беремо твіти з моменту минулої перевірки (з запасом 10 хв) — так платимо лише за нові
    since = state.get("x_since") or int((now - timedelta(hours=1)).timestamp())
    since = min(since - 600, int(now.timestamp()) - 600)

    items, ok = [], True
    for i in range(0, len(accounts), 12):  # по 12 акаунтів в одному запиті
        group = accounts[i:i + 12]
        query = "(" + " OR ".join(f"from:{a}" for a in group) + f") since_time:{since} -filter:replies"
        cursor = ""
        for _ in range(5):  # максимум 5 сторінок на групу
            try:
                r = requests.get(
                    "https://api.twitterapi.io/twitter/tweet/advanced_search",
                    headers={"X-API-Key": X_API_KEY},
                    params={"query": query, "queryType": "Latest", "cursor": cursor},
                    timeout=40,
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                log(f"! X: помилка запиту ({e})")
                ok = False
                break
            for tw in data.get("tweets", []) or []:
                if tw.get("retweeted_tweet") or tw.get("isReply"):
                    continue  # ретвіти й відповіді пропускаємо
                author = tw.get("author") or {}
                handle = author.get("userName", "")
                link = tw.get("url") or f"https://x.com/{handle}/status/{tw.get('id')}"
                key = f"x:{tw.get('id')}"
                if key in seen_set:
                    continue
                text = tw.get("text", "")
                text = re.sub(r"https://t\.co/\S+", "", text).strip()
                if len(text) < 15:
                    continue  # порожні твіти (лише картинка/посилання) пропускаємо
                items.append({
                    "kind": "tweet",
                    "key": key,
                    "link": link,
                    "source": f"{author.get('name') or handle} (@{handle}) у X",
                    "title": text[:80],
                    "text": text,
                    "tweet": tw,
                    "image": None,  # визначимо перед публікацією
                    "date": tweet_date(tw.get("createdAt")),
                })
            if not data.get("has_next_page") or not data.get("next_cursor"):
                break
            cursor = data["next_cursor"]
        time.sleep(1)

    if ok:
        state["x_since"] = int(now.timestamp())
    log(f"X: нових твітів {len(items)}")
    return items

# ---------------------------------------------------------------- Steam

STEAM_CLAN_IMG = "https://clan.akamai.steamstatic.com/images/"


def steam_text_and_image(contents):
    """Steam-новини написані BBCode: витягуємо першу картинку і чистий текст."""
    contents = (contents or "").replace("{STEAM_CLAN_IMAGE}", STEAM_CLAN_IMG.rstrip("/"))
    image = None
    m = re.search(r'\[img(?:\s+src="([^"]+)")?\]([^\[]*)\[/img\]', contents)
    if m:
        image = (m.group(1) or m.group(2) or "").strip() or None
    if not image:
        m = re.search(r'<img[^>]+src="([^"]+)"', contents)
        image = m.group(1) if m else None
    text = re.sub(r"\[img[^\]]*\].*?\[/img\]", " ", contents, flags=re.S)
    text = re.sub(r"\[/?[a-zA-Z0-9*]+(?:=[^\]]*)?[^\]]*\]", " ", text)  # решта BBCode-тегів
    text = strip_html(text)
    return re.sub(r"\s+", " ", text).strip(), image


def steam_details(appid):
    try:
        r = requests.get(
            "https://store.steampowered.com/api/appdetails",
            params={"appids": appid, "cc": "us", "l": "english"},
            headers=HEADERS, timeout=30,
        )
        entry = r.json().get(str(appid)) or {}
        return entry.get("data") if entry.get("success") else None
    except Exception as e:
        log(f"! Steam {appid}: не вдалось отримати дані гри ({e})")
        return None


def collect_steam(seen_set, remember, state):
    apps = read_list(STEAM_FILE)
    if not apps:
        return []
    now = datetime.now(timezone.utc)
    dates = state.setdefault("steam_release", {})
    items = []
    for name, appid in apps:
        appid = appid.strip()
        details = steam_details(appid) or {}
        game = name or details.get("name") or f"Steam {appid}"
        header = details.get("header_image")

        # 1) Зміна дати виходу (те, що зазвичай ловлять на SteamDB)
        rd = details.get("release_date") or {}
        if rd:
            current = f"{rd.get('date') or '—'}|{'soon' if rd.get('coming_soon') else 'out'}"
            previous = dates.get(appid)
            dates[appid] = current
            if previous and previous != current:
                old_date, old_flag = previous.split("|", 1)
                new_date, new_flag = current.split("|", 1)
                if old_flag == "soon" and new_flag == "out":
                    change = f"Гра {game} щойно вийшла в Steam (дата на сторінці: {new_date})."
                else:
                    change = f"У Steam змінилася дата виходу гри {game}: було «{old_date}», стало «{new_date}»."
                items.append({
                    "kind": "steam", "key": f"steamdate:{appid}:{current}",
                    "link": f"https://store.steampowered.com/app/{appid}/",
                    "source": f"Steam · {game}", "title": change, "text": change,
                    "image": header, "date": now,
                })

        # 2) Офіційні оголошення розробників
        try:
            r = requests.get(
                "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                params={"appid": appid, "count": 5, "maxlength": 0,
                        "feeds": "steam_community_announcements"},
                headers=HEADERS, timeout=30,
            )
            news = (r.json().get("appnews") or {}).get("newsitems") or []
        except Exception as e:
            log(f"! Steam {game}: не вдалось отримати новини ({e})")
            continue
        for n in news:
            key = f"steam:{n.get('gid')}"
            if key in seen_set:
                continue
            published = datetime.fromtimestamp(n.get("date", 0), tz=timezone.utc)
            if now - published > MAX_AGE:
                remember(key)
                continue
            text, image = steam_text_and_image(n.get("contents"))
            items.append({
                "kind": "steam", "key": key,
                "link": n.get("url") or f"https://store.steampowered.com/news/app/{appid}",
                "source": f"Steam · {game}", "title": n.get("title", ""),
                "text": text, "image": image or header, "date": published,
            })
        time.sleep(1)  # Steam не любить часті запити
    log(f"Steam: нових подій {len(items)}")
    return items


# ---------------------------------------------------------------- Discord

def discord_api(path, **params):
    r = requests.get(
        f"https://discord.com/api/v10{path}",
        headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
        params=params, timeout=30,
    )
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
        return discord_api(path, **params)
    r.raise_for_status()
    return r.json()


def clean_discord(text):
    def ts(m):
        return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    text = re.sub(r"<t:(\d+)(?::[a-zA-Z])?>", ts, text or "")   # таймстемпи Discord -> дата
    text = re.sub(r"<a?:\w+:\d+>", "", text)                      # кастомні емодзі
    text = re.sub(r"<[@#][!&]?\d+>", "", text)                    # згадки ролей/людей/каналів
    text = re.sub(r"@(everyone|here)", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def collect_discord(seen_set, state):
    channels = read_list(DISCORD_FILE)
    if not channels:
        return []
    if not DISCORD_TOKEN:
        log("i discord_channels.txt заповнено, але DISCORD_BOT_TOKEN не задано — Discord пропускаю")
        return []
    last = state.setdefault("discord_last", {})
    guilds = state.setdefault("discord_guild", {})
    items = []
    for name, channel_id in channels:
        channel_id = channel_id.strip()
        try:
            if channel_id not in guilds:
                guilds[channel_id] = discord_api(f"/channels/{channel_id}").get("guild_id", "")
            params = {"limit": 50}
            if last.get(channel_id):
                params["after"] = last[channel_id]
            else:
                params["limit"] = 5  # перший запуск каналу — лише кілька останніх
            messages = discord_api(f"/channels/{channel_id}/messages", **params)
        except Exception as e:
            log(f"! Discord {name or channel_id}: помилка ({e})")
            continue
        for msg in sorted(messages, key=lambda m: int(m["id"])):
            last[channel_id] = msg["id"]
            key = f"discord:{msg['id']}"
            if key in seen_set:
                continue
            parts = [msg.get("content", "")]
            image = None
            for emb in msg.get("embeds", []) or []:
                parts += [emb.get("title", ""), emb.get("description", "")]
                image = image or (emb.get("image") or {}).get("url") or (emb.get("thumbnail") or {}).get("url")
            for att in msg.get("attachments", []) or []:
                if (att.get("content_type") or "").startswith("image") and not image:
                    image = att.get("url")
            text = clean_discord("\n".join(p for p in parts if p))
            if len(text) < 20:
                continue
            ref = msg.get("message_reference") or {}
            if ref.get("guild_id") and ref.get("message_id"):  # переслане з офіційного сервера
                link = f"https://discord.com/channels/{ref['guild_id']}/{ref['channel_id']}/{ref['message_id']}"
            else:
                link = f"https://discord.com/channels/{guilds.get(channel_id)}/{channel_id}/{msg['id']}"
            author = (msg.get("author") or {}).get("username") or name or "Discord"
            items.append({
                "kind": "discord", "key": key, "link": link,
                "source": f"Discord · {author}", "title": text[:80], "text": text,
                "image": image, "date": tweet_date(msg.get("timestamp")),
            })
    log(f"Discord: нових оголошень {len(items)}")
    return items


# ---------------------------------------------------------------- ШІ

def post_with_retry(url, headers, body):
    for attempt in range(4):
        r = requests.post(url, headers=headers, json=body, timeout=90)
        if r.status_code in (429, 503) and attempt < 3:  # ліміт безкоштовного тарифу / перевантаження
            wait = 20 * (attempt + 1)
            log(f"   ! ліміт запитів ШІ, чекаю {wait} с")
            time.sleep(wait)
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"{LLM_PROVIDER} {r.status_code}: {r.text[:300]}")
    return r.json()


def call_llm(model, system_prompt, user_text, use_json=True):
    if LLM_PROVIDER == "gemini":
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        }
        if use_json:
            body["generationConfig"] = {"responseMimeType": "application/json"}
        data = post_with_retry(
            PROVIDER["url"].format(model=model),
            {"x-goog-api-key": LLM_API_KEY, "Content-Type": "application/json"},
            body,
        )
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        content = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    else:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
        }
        if use_json:
            body["response_format"] = {"type": "json_object"}
        if PROVIDER["reasoning"] and (LLM_PROVIDER != "openai" or model.startswith(("gpt-5", "o"))):
            body["reasoning_effort"] = PROVIDER["reasoning"]
        data = post_with_retry(PROVIDER["url"], {"Authorization": f"Bearer {LLM_API_KEY}"}, body)
        content = data["choices"][0]["message"]["content"] or ""

    match = re.search(r"\{.*\}", content, re.S)  # на випадок ```json ... ``` навколо
    data = json.loads(match.group(0) if match else content)
    try:
        score = max(0, min(10, int(float(data.get("score", 0)))))
    except (TypeError, ValueError):
        score = 0
    return score, (data.get("title") or "").strip(), (data.get("summary") or "").strip()


def summarize(kind, title, text, source):
    prompt = build_prompt(kind)
    user_text = f"Джерело: {source}\nЗаголовок: {title}\n\nТекст:\n{text[:6000]}"

    last_error = None
    while MODELS:
        model = MODELS[0]
        try:
            try:
                return call_llm(model, prompt, user_text)
            except RuntimeError as e:
                if any(w in str(e).lower() for w in ("response_format", "responsemimetype", "json")):
                    return call_llm(model, prompt, user_text, use_json=False)
                raise
        except RuntimeError as e:
            last_error = e
            msg = str(e).lower()
            # модель не існує / недоступна — перемикаємось на наступну до кінця запуску
            if len(MODELS) > 1 and ("model" in msg or " 404" in msg):
                log(f"   ! модель {model} недоступна, перемикаюсь на {MODELS[1]}")
                MODELS.pop(0)
                continue
            raise
    raise last_error


# ---------------------------------------------------------------- Telegram

def tg(method, **kwargs):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}", timeout=60, **kwargs
    )
    data = r.json()
    if not data.get("ok"):
        # Telegram просить зачекати — чекаємо і пробуємо ще раз
        retry = data.get("parameters", {}).get("retry_after")
        if retry:
            time.sleep(retry + 1)
            return tg(method, **kwargs)
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data


def build_caption(title, summary, link, source, score=None):
    tail = f"\nВажливість: {score}/10" if score is not None else ""
    def make(text):
        return (
            f"<b>{html.escape(title)}</b>\n\n"
            f"{html.escape(text)}\n\n"
            f'Джерело: <a href="{html.escape(link)}">{html.escape(source)}</a>{tail}'
        )
    caption = make(summary)
    if len(caption) <= 1024:  # ліміт підпису до фото в Telegram
        return caption
    budget = 1024 - len(make("")) - 1
    cut = ""
    for ch in summary:
        if len(html.escape(cut + ch)) > budget:
            break
        cut += ch
    return make(cut.rstrip() + "…")


def post(caption, image_url):
    if DRY_RUN:
        log("----- DRY RUN -----\n" + caption + f"\n[картинка: {image_url}]\n")
        return
    base = {"chat_id": TG_CHAT, "caption": caption, "parse_mode": "HTML"}
    if image_url:
        # 1) просимо Telegram самому завантажити картинку за посиланням
        try:
            return tg("sendPhoto", data={**base, "photo": image_url})
        except Exception as e:
            log(f"   ! sendPhoto за URL не вдався ({e}), завантажую сам")
        # 2) завантажуємо картинку самі й відправляємо файлом
        try:
            img = requests.get(image_url, headers=HEADERS, timeout=30)
            img.raise_for_status()
            return tg("sendPhoto", data=base, files={"photo": ("image.jpg", img.content)})
        except Exception as e:
            log(f"   ! не вдалось відправити картинку ({e}), шлю текстом")
    # 3) без картинки — звичайне повідомлення з прев'ю посилання
    return tg("sendMessage", data={"chat_id": TG_CHAT, "text": caption, "parse_mode": "HTML"})


def post_score(sent, score):
    """Оцінка окремим повідомленням-відповіддю під постом: сам пост лишається чистим для пересилання."""
    if DRY_RUN or not sent:
        return
    msg_id = (sent.get("result") or {}).get("message_id")
    try:
        tg("sendMessage", data={
            "chat_id": TG_CHAT,
            "text": f"↑ Важливість: {score}/10",
            "reply_to_message_id": msg_id,
            "disable_notification": "true",
        })
    except Exception as e:
        log(f"   ! не вдалось надіслати оцінку ({e})")


# ---------------------------------------------------------------- головний цикл

def main():
    missing = [n for n, v in [("LLM_API_KEY", LLM_API_KEY),
                              ("TELEGRAM_BOT_TOKEN", TG_TOKEN),
                              ("TELEGRAM_CHAT_ID", TG_CHAT)] if not v]
    if missing and not DRY_RUN:
        sys.exit("Не задані змінні: " + ", ".join(missing))
    log(f"ШІ: {LLM_PROVIDER}, модель {MODELS[0]}")

    seen = load_json(SEEN_FILE, [])
    state = load_json(STATE_FILE, {})
    seen_set = set(seen)
    first_run = not seen

    def remember(key):
        if key not in seen_set:
            seen_set.add(key)
            seen.append(key)

    def save():
        save_json(SEEN_FILE, seen[-SEEN_LIMIT:])
        save_json(STATE_FILE, state)

    items = (collect_rss(seen_set, remember) + collect_tweets(seen_set, state)
             + collect_steam(seen_set, remember, state) + collect_discord(seen_set, state))
    items.sort(key=lambda i: i["date"])  # старіші першими — стрічка йде по порядку
    log(f"Знайдено нових новин: {len(items)}")

    if first_run:
        # Перший запуск: не заливаємо весь архів — беремо лише кілька найсвіжіших
        for it in items[:-5]:
            remember(it["key"])
        items = items[-5:]
        log("Перший запуск: обробляю лише 5 найсвіжіших, решту позначено прочитаними")

    posted = skipped = llm_calls = 0
    for it in items:
        if posted >= MAX_POSTS or llm_calls >= MAX_LLM_CALLS:
            log("Досягнуто ліміту за запуск, решта — наступного разу")
            break

        if it["kind"] == "article":
            page_text, og_image = fetch_page(it["link"])
            text = page_text if len(page_text) > len(it["text"]) else it["text"]
            image = og_image or it["image"]
        else:
            text = it["text"]
            image = tweet_image(it["tweet"]) if it["kind"] == "tweet" else it["image"]

        try:
            if not LLM_API_KEY and DRY_RUN:
                score, title_uk, summary_uk = 10, it["title"], text[:300]
            else:
                llm_calls += 1
                score, title_uk, summary_uk = summarize(it["kind"], it["title"], text or it["title"], it["source"])
            threshold = MIN_SCORE[it["kind"]]
            if score < threshold or not title_uk:
                skipped += 1
                log(f"   - [{score}/10] пропущено: [{it['source']}] {it['title'][:90]}")
            else:
                log(f"-> [{score}/10] [{it['source']}] {title_uk}")
                sent = post(build_caption(title_uk, summary_uk, it["link"], it["source"]), image)
                post_score(sent, score)
                posted += 1
                if not DRY_RUN:
                    time.sleep(3)  # щоб не впертись у ліміти Telegram
        except Exception as e:
            log(f"   ! помилка: {e} — [{it['source']}] {it['title'][:90]}")
        remember(it["key"])  # позначаємо навіть при помилці, щоб не зациклитись
        save()
        if not DRY_RUN:
            time.sleep(1.5)  # пауза для безкоштовного тарифу ШІ

    save()
    log(f"Готово. Опубліковано: {posted}, відсіяно як неважливе: {skipped}")


if __name__ == "__main__":
    main()
