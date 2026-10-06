#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
早报 → Telegraph → Telegram 频道，全自动发布（GitHub Actions 版）。

部署：
  1. 新建仓库，放入本目录所有文件
  2. 仓库 Settings → Secrets and variables → Actions，新建：
       BOT_TOKEN（必填，@BotFather 获取，Bot 须为频道管理员）
       CHANNEL（必填，频道 chat_id 或 @用户名）
       TELEGRAPH_TOKEN（必填，Telegraph 账号 token，自行保管）
  3. Actions 页手动 Run 一次验证，之后每小时自动跑

抓取 UA 用 bingbot（实测可绕过 zaobao.com 对非中国 IP 的地理跳转，
站点匹配小写 "bingbot/" 子串）。

流程：
  各栏目列表页 → 合并去重（URL 主键，不分板块） → 逐篇抓全文/AI摘要/关键词 →
  发布 Telegraph（链接按原文 URL 定制）→ Bot 推送到频道 → 写库

去重与容错：
  - 已发送记录存 sqlite（sent.db），url 主键；发送成功才标记
  - workflow 跑完后把 sent.db git push 回仓库，实现跨运行持久化
  - Telegraph 发布成功但频道推送失败时，下次复用已发布的链接，
    不会重复建 Telegraph 页面
  - 单篇文章失败不影响其他篇；每次运行有补发上限，避免狂轰频道
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "sent.db"
LOG_FILE = BASE_DIR / "xiawucha.log"
CONFIG_FILE = BASE_DIR / "config.json"

# ============================ 配置文件 ============================
# 非密钥配置在脚本同目录的 config.json 里改。
# token/频道 ID 走环境变量（GitHub Secrets），不在配置文件或仓库里放密钥：
#   BOT_TOKEN        必填（@BotFather 获取；Bot 须为目标频道管理员）
#   TELEGRAPH_TOKEN  必填（Telegraph 账号 token，自行保管好）
#   CHANNEL          必填（频道 chat_id 或 @用户名，如 @xwucha）
# 首次运行会自动生成 config.json 模板，直接可用。
DEFAULT_CONFIG = {
    # 抓取 UA：bingbot 可绕过 zaobao.com 对非中国 IP 的地理跳转
    # （实测：站点匹配小写 "bingbot/" 子串，勿改大小写）
    "user_agent": ("Mozilla/5.0 (compatible; bingbot/2.0; "
                   "+http://www.bing.com/bingbot.htm"),
    # 图片代理：早报图床有 Referer 防盗链，直接嵌图会裂图
    "img_proxy": "https://images.weserv.nl/?url={url}",
    # 栏目：name 仅用于日志，list_url 为栏目列表页
    "sources": [
        {"name": "下午察",
         "list_url": "https://www.zaobao.com/keywords/xia-wu-cha"},
        {"name": "中国早点",
         "list_url": "https://www.zaobao.com/forum/zaodian"},
    ],
    # 超过多少小时的文章不再补发（0 = 不限制）
    "max_age_hours": 72,
    # 每次运行最多处理几篇（跨栏目合并后的全局上限）
    "max_per_run": 10,
    # 每处理一篇文章后的间隔（秒），别太小以免被限流
    "sleep_between_articles": 5,
    # 去重记录保留天数
    "db_keep_days": 30,
}


def load_config():
    """读 config.json；不存在则生成模板并退出让用户填写。
    缺失的键用默认值补齐，保证向后兼容。"""
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"已生成默认配置文件：{CONFIG_FILE}")
        print("token 与频道请用环境变量（GitHub Secrets）配置后重新运行。")
        sys.exit(2)
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"配置文件读取失败 {CONFIG_FILE}：{e}")
        sys.exit(2)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


_cfg = load_config()
# token 与频道走环境变量（GitHub Secrets），不在 config.json 里放密钥
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TELEGRAPH_TOKEN = os.environ.get("TELEGRAPH_TOKEN", "").strip()
CHANNEL = os.environ.get("CHANNEL", "").strip()
IMG_PROXY = _cfg["img_proxy"]
SOURCES = _cfg["sources"]
MAX_AGE_HOURS = _cfg["max_age_hours"]
MAX_PER_RUN = _cfg["max_per_run"]
SLEEP_BETWEEN_ARTICLES = _cfg["sleep_between_articles"]
DB_KEEP_DAYS = _cfg["db_keep_days"]
FETCH_UA = _cfg["user_agent"] or DEFAULT_CONFIG["user_agent"]
TELEGRAPH_API = "https://api.telegra.ph"

try:
    import requests
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:
    sys.exit("缺少依赖，请先执行：pip install requests beautifulsoup4")

SKIP_CLASS_KEYWORDS = ("bff-google-ad", "bff-recommend-article",
                       "brightcove", "google-ad",
                       "further-reading", "read-on-app-cover",
                       "article-pic-title")
BLOCK_TAG_MAP = {"h1": "h3", "h2": "h3", "h3": "h4", "h4": "h4",
                 "blockquote": "blockquote", "p": "p",
                 "ul": "ul", "ol": "ol", "li": "li",
                 "pre": "pre", "hr": "hr", "aside": "aside"}
INLINE_TAG_MAP = {"a": "a", "b": "b", "strong": "strong",
                  "i": "i", "em": "em", "code": "code",
                  "s": "s", "u": "u", "br": "br"}


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ])


# ---------------- HTTP ----------------
def decode_html(resp):
    """正确解码 HTML（早报服务器常不声明 charset，直接用 resp.text 会乱码）。"""
    raw = resp.content
    m = re.search(r"charset=[\"']?([\w-]+)",
                  resp.headers.get("Content-Type", ""), re.I)
    header_enc = m.group(1) if m else None
    meta_enc = None
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:8192], re.I)
    if m:
        try:
            meta_enc = m.group(1).decode("ascii")
        except UnicodeDecodeError:
            pass
    candidates = []
    if header_enc and header_enc.lower() not in ("iso-8859-1", "latin-1"):
        candidates.append(header_enc)
    if meta_enc:
        candidates.append(meta_enc)
    if header_enc:
        candidates.append(header_enc)
    candidates += ["utf-8", "gb18030"]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def http_get(url, timeout=30, retries=3):
    """返回 (final_url, html_text)，带重试。"""
    last = None
    for i in range(1, retries + 1):
        try:
            r = requests.get(url, headers={"User-Agent": FETCH_UA}, timeout=timeout)
            r.raise_for_status()
            return r.url, decode_html(r)
        except requests.RequestException as e:
            last = e
            logging.warning(f"抓取失败（{i}/{retries}）：{url}：{e}")
            time.sleep(2 * i)
    raise RuntimeError(f"抓取多次失败：{url}：{last}")


# ---------------- sqlite 去重 ----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_items (
            url TEXT PRIMARY KEY,
            title TEXT,
            telegraph_url TEXT,
            timestamp REAL NOT NULL
        )
    """)
    # Telegraph 已发布但频道未推送成功的中间态，下次复用链接避免重复建页
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telegraph_pages (
            url TEXT PRIMARY KEY,
            telegraph_url TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def is_sent(conn, url):
    cur = conn.execute("SELECT 1 FROM sent_items WHERE url = ?", (url,))
    return cur.fetchone() is not None


def get_pending_telegraph(conn, url):
    cur = conn.execute(
        "SELECT telegraph_url FROM telegraph_pages WHERE url = ?", (url,))
    row = cur.fetchone()
    return row[0] if row else None


def save_pending_telegraph(conn, url, telegraph_url):
    conn.execute(
        "INSERT OR REPLACE INTO telegraph_pages (url, telegraph_url, timestamp)"
        " VALUES (?, ?, ?)", (url, telegraph_url, time.time()))
    conn.commit()


def mark_sent(conn, url, title, telegraph_url):
    conn.execute(
        "INSERT OR IGNORE INTO sent_items (url, title, telegraph_url, timestamp)"
        " VALUES (?, ?, ?, ?)", (url, title, telegraph_url, time.time()))
    conn.commit()


def cleanup_db(conn, days=DB_KEEP_DAYS):
    cutoff = time.time() - days * 86400
    for table in ("sent_items", "telegraph_pages"):
        cur = conn.execute(
            f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
        if cur.rowcount:
            logging.info(f"清理 {table} 中 {cur.rowcount} 条旧记录")
    conn.commit()


# ---------------- 列表页解析 ----------------
def norm_url(href, base):
    u = urljoin(base, href)
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")


def parse_card_date(article_tag):
    """从卡片里的 <span>10月2日</span> 解析发布日期，失败返回 None。"""
    span = article_tag.find("span", string=re.compile(r"\d+\s*月\s*\d+\s*日"))
    if not span:
        return None
    m = re.search(r"(\d+)\s*月\s*(\d+)\s*日", span.get_text())
    if not m:
        return None
    now = datetime.now()
    try:
        d = datetime(now.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    if d > now:  # 如 12月31日 而现在是1月初，算去年
        d = datetime(now.year - 1, int(m.group(1)), int(m.group(2)))
    return d


def parse_date_from_url(url):
    """从 story20261002-xxxx 这样的 URL 中提取日期，失败返回 None。

    有些列表页（如中国早点）卡片上不带日期，用 URL 里的日期兜底，
    时效过滤才能正常工作。
    """
    m = re.search(r"story(\d{4})(\d{2})(\d{2})", url)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def extract_list(html, base_url):
    """提取列表页文章 [(url, title, pub_date)]，新→旧排序。

    定位策略（由稳到弱）：
      1. a.article-link[href]：语义化类名，最稳定
      2. 兜底：全文正则 href="...story123..."，不依赖任何结构
    """
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select("a.article-link[href]")
    story_anchors = [a for a in anchors if "story" in a.get("href", "")]
    if story_anchors:
        logging.info("列表定位：a.article-link（语义化类名）")
    else:
        logging.warning("未找到 a.article-link，降级用正则全页提取")
        story_anchors = []
        for m in re.finditer(r'href="([^"]*?story\d+[^"]*)"', html):
            tag = soup.new_tag("a", href=m.group(1))
            story_anchors.append(tag)

    items = {}
    for a in story_anchors:
        url = norm_url(a.get("href", "").split("?")[0], base_url)
        if "/story" not in url or url in items:
            continue
        title = (a.get("title") or "").strip() or a.get_text(strip=True)[:80]
        pub_date = None
        card = a.find_parent("article")
        if card is not None:
            pub_date = parse_card_date(card)
        if pub_date is None:
            pub_date = parse_date_from_url(url)
        items[url] = (title, pub_date)
    result = [(u, t, d) for u, (t, d) in items.items()]
    logging.info(f"列表页共发现 {len(result)} 篇文章")
    return result


# ---------------- 文章解析 ----------------
def _inside_skipped(el, body):
    for p in el.parents:
        if p is body:
            break
        if isinstance(p, Tag):
            if p.name == "astro-island":
                return True
            cls = " ".join(p.get("class", []))
            if any(k in cls for k in SKIP_CLASS_KEYWORDS):
                return True
            if p.name == "figure":
                return True
    return False


def inline_children(el):
    out = []
    for child in el.children:
        if isinstance(child, NavigableString):
            if str(child):
                out.append(str(child))
        elif isinstance(child, Tag):
            if child.name in ("script", "style"):
                continue
            mapped = INLINE_TAG_MAP.get(child.name)
            if mapped == "br":
                out.append({"tag": "br"})
            elif mapped == "a":
                href = child.get("href", "")
                kids = inline_children(child)
                if kids:
                    node = {"tag": "a", "children": kids}
                    if href.startswith("http"):
                        node["attrs"] = {"href": href}
                    out.append(node)
            elif mapped:
                kids = inline_children(child)
                if kids:
                    out.append({"tag": mapped, "children": kids})
            else:
                text = child.get_text()
                if text:
                    out.append(text)
    merged = []
    for item in out:
        if isinstance(item, str) and not item.strip():
            continue
        if isinstance(item, str) and merged and isinstance(merged[-1], str):
            merged[-1] += item
        else:
            merged.append(item)
    return merged


def figure_node_from_img(img):
    if img is None:
        return None
    src = (img.get("src") or "").strip()
    if not src.startswith("http"):
        return None
    caption = (img.get("title") or img.get("alt") or "").strip()
    children = [{"tag": "img", "attrs": {"src": src}}]
    if caption:
        children.append({"tag": "figcaption", "children": [caption]})
    return {"tag": "figure", "children": children}


def _parse_body(body):
    """解析 zaobao.com 正文容器 article#article-body。

    图片在 div.inline-image-container（图注取 img title），其后紧跟的
    div.article-pic-title 是重复图注，已在 SKIP 列表中跳过。
    """
    nodes = []
    for el in body.find_all(["p", "div", "h1", "h2", "h3", "h4",
                             "blockquote", "ul", "ol", "pre", "hr"],
                            recursive=True):
        if _inside_skipped(el, body):
            continue
        if el.name == "div" and "inline-image-container" in el.get("class", []):
            node = figure_node_from_img(el.find("img"))
            if node:
                nodes.append(node)
        elif el.name in BLOCK_TAG_MAP:
            kids = inline_children(el)
            text_only = "".join(k for k in kids if isinstance(k, str)).strip()
            if kids and text_only:
                nodes.append({"tag": BLOCK_TAG_MAP[el.name],
                              "children": kids})
    return [n for n in nodes if n.get("children")]


def extract_lead_images(soup, existing_srcs):
    """提取正文容器外的图库头图（img.gallery-image）。

    existing_srcs: 正文内已提取图片的 src（去掉 query），用于去重。
    返回 figure 节点列表，按页面顺序，调用方放在正文内容最前面。
    """
    nodes = []
    seen = set(existing_srcs)
    for img in soup.select("img.gallery-image"):
        src = (img.get("src") or "").strip()
        if not src.startswith("http"):
            continue
        key = src.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        node = figure_node_from_img(img)
        if node:
            nodes.append(node)
    return nodes


def parse_article(html, fallback_title=""):
    """返回 (title, author, content, ai_summary, keywords)。

    ai_summary 可能为空字符串；keywords 为去重后的关键词列表。
    """
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = re.sub(r"\s*\|\s*联合早报.*$", "", title) or fallback_title

    author = "联合早报"
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            a = (item or {}).get("author")
            if isinstance(a, dict) and a.get("name"):
                author = a["name"]
                break
        if author != "联合早报":
            break

    body = soup.select_one("article#article-body, .article-body")
    if body is None:
        raise RuntimeError("未找到正文容器 article#article-body，页面结构可能已变化")
    content = _parse_body(body)
    # 正文容器外的图库头图（img.gallery-image）：中国早点这类文章只有头图、
    # 正文内无内嵌图，不补这一步 Telegraph 页面就没有图片。
    # 与正文内已提取的图片按 src（去 query）去重，避免下午察这类文章重复。
    existing = set()
    for n in content:
        for c in n.get("children", []):
            if isinstance(c, dict) and c.get("tag") == "img":
                src = (c.get("attrs", {}).get("src") or "").split("?")[0]
                if src:
                    existing.add(src)
    content = extract_lead_images(soup, existing) + content
    n_text = sum(len("".join(c for c in n.get("children", [])
                             if isinstance(c, str))) for n in content)
    if n_text < 200:
        raise RuntimeError(f"正文过短（{n_text} 字），疑似付费墙截断或结构变化")
    return (title, author, content,
            extract_ai_summary(soup), extract_keywords(soup))


def extract_keywords(soup):
    """提取关键词用于频道索引。优先 related-words 区块，兜底 meta keywords。

    返回去重后的关键词列表（保持原顺序）。
    """
    kws = []
    block = soup.select_one('[data-testid="related-words"]')
    if block:
        for a in block.select("a.keyword-tag_item"):
            t = a.get_text(strip=True)
            if t and t not in kws:
                kws.append(t)
    if not kws:
        meta = soup.find("meta", attrs={"name": "keywords"})
        if meta and meta.get("content"):
            for k in meta["content"].split(","):
                k = k.strip()
                if k and k not in kws:
                    kws.append(k)
    return kws


def extract_ai_summary(soup):
    """提取 <zb-ai-summary> 中的 AI 摘要；没有则返回空字符串。"""
    el = soup.find("zb-ai-summary")
    if el is None:
        return ""
    text = el.get_text(separator="\n", strip=True)
    # 规范化：每行去首尾空，丢掉空行；条目之间空一行，排版更疏朗
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n\n".join(lines)


# ---------------- Telegraph ----------------
def api_post(method, data, timeout=90, retries=3):
    last = None
    for i in range(1, retries + 1):
        try:
            r = requests.post(f"{TELEGRAPH_API}/{method}", data=data,
                              timeout=timeout)
            p = r.json()
            if not p.get("ok"):
                raise RuntimeError(p.get("description", p))
            return p["result"]
        except (requests.RequestException, RuntimeError, ValueError) as e:
            last = e
            logging.warning(f"Telegraph API {method} 第{i}次失败：{e}，重试…")
            time.sleep(2 * i)
    raise RuntimeError(f"Telegraph API {method} 多次失败：{last}")


def get_telegraph_token():
    # token 只从 Secrets 取，不在仓库里存文件、也不自动创建
    if not TELEGRAPH_TOKEN:
        logging.error("环境变量 TELEGRAPH_TOKEN 未设置（仓库 Secrets 里新建 TELEGRAPH_TOKEN），退出")
        sys.exit(2)
    return TELEGRAPH_TOKEN


def derive_slug_title(source_url):
    """从原文 URL 派生建页临时标题，用于定制 path。

    /news/china/story20260930-9762332 → 建页 path 为
    china-story20260930-9762332-10-03（日期后缀 Telegraph 自动加）。
    """
    parts = [p for p in urlparse(source_url).path.strip("/").split("/") if p]
    if not parts:
        return None
    segs = parts[-2:] if len(parts) >= 2 else parts
    slug = re.sub(r"[^A-Za-z0-9]+", "-", "-".join(segs)).strip("-").lower()
    return slug or None


def rewrite_image_proxy(content, template):
    if "{url}" not in template:
        raise RuntimeError("IMG_PROXY 模板必须包含 {url} 占位符")
    for node in content:
        if node.get("tag") != "figure":
            continue
        for child in node.get("children", []):
            if isinstance(child, dict) and child.get("tag") == "img":
                src = (child.get("attrs") or {}).get("src", "")
                if src:
                    child["attrs"]["src"] = template.replace(
                        "{url}", quote(src, safe=""))
    return content


def publish_telegraph(token, title, author, source_url, content):
    base = {"access_token": token,
            "author_name": author[:128],
            "author_url": source_url,
            "content": json.dumps(content, ensure_ascii=False),
            "return_content": "false"}
    slug_title = derive_slug_title(source_url)
    if slug_title:
        r = api_post("createPage", {**base, "title": slug_title})
        path = r["path"]
        # path 创建后不可变，改回真实标题
        r = api_post("editPage", {**base, "path": path,
                                  "title": title[:256] or "无标题"})
    else:
        r = api_post("createPage", {**base,
                                    "title": title[:256] or "无标题"})
    return "https://telegra.ph/" + r["path"]


# ---------------- Telegram 推送（HTML 模式） ----------------
def build_channel_message(title, page_url, source_url, ai_summary="",
                          keywords=None):
    """频道消息格式（HTML parse_mode）：

    <b>标题</b>(telegraph链接) | <b>原文</b>(原文链接)

    AI摘要（可选）

    #关键词1 #关键词2（可选，方便频道内索引）
    """
    title_link = f"<a href='{page_url}'><b>{escape(title)}</b></a>"
    origin_link = f"<a href='{source_url}'><b>原文</b></a>"
    text = f"{title_link} | {origin_link}"
    if ai_summary:
        text += "\n\n" + escape(ai_summary)
    tags = ["#" + escape(k) for k in dict.fromkeys(
        k.strip() for k in (keywords or []) if k.strip())]
    if tags:
        text += "\n\n" + " ".join(tags)
    return text


def send_to_channel(chat_id, title, page_url, source_url, ai_summary="",
                    keywords=None):
    text = build_channel_message(title, page_url, source_url,
                                 ai_summary, keywords)
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": chat_id, "text": text,
              "parse_mode": "HTML",
              "disable_web_page_preview": "false"},
        timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"推送频道失败：{data}")
    logging.info(f"已推送到频道 {chat_id}")


# ---------------- 主流程 ----------------
def process_article(conn, token, src, url, list_title=""):
    final_url, html = http_get(url)
    if "zaobao.com" in urlparse(url).netloc \
            and "zaobao.com.sg" in urlparse(final_url).netloc:
        raise RuntimeError("被跳转到 zaobao.com.sg（疑似非中国 IP），跳过")
    title, author, content, ai_summary, keywords = parse_article(
        html, fallback_title=list_title)
    content = rewrite_image_proxy(content, IMG_PROXY)

    telegraph_url = get_pending_telegraph(conn, url)
    if telegraph_url:
        logging.info(f"复用已发布的 Telegraph 链接：{telegraph_url}")
    else:
        logging.info(f"正在发布 Telegraph：{title[:40]}")
        telegraph_url = publish_telegraph(token, title, author,
                                          final_url, content)
        save_pending_telegraph(conn, url, telegraph_url)
        logging.info(f"Telegraph 发布成功：{telegraph_url}")

    send_to_channel(CHANNEL, title, telegraph_url, url,
                    ai_summary, keywords)
    mark_sent(conn, url, title, telegraph_url)
    logging.info(f"完成：{title[:40]}"
                 + ("（含 AI 摘要" if ai_summary else "（无摘要")
                 + (f"，{len(keywords)} 个关键词）" if keywords else "）"))


def collect_new_items(conn, sources):
    """从各栏目收集未发送的新文章：跨栏目合并去重，按发布时间旧→新排序。

    去重不分板块——同一篇文章可能出现在多个栏目列表，URL 主键全局去重，
    简单且容错。
    """
    merged = {}
    for src in sources:
        try:
            _, html = http_get(src["list_url"])
            items = extract_list(html, src["list_url"])
        except Exception:  # noqa: BLE001
            logging.exception(f"栏目「{src['name']}」列表抓取失败，已跳过")
            continue
        for url, title, pub_date in items:
            if url not in merged:
                merged[url] = (src, url, title, pub_date)
    dated = sorted((v for v in merged.values() if v[3] is not None),
                   key=lambda v: v[3])
    undated = [v for v in merged.values() if v[3] is None]
    result = [v for v in dated + undated if not is_sent(conn, v[1])]
    logging.info(f"各栏目合并去重后待处理 {len(result)} 篇")
    return result


def main():
    ap = argparse.ArgumentParser(description="早报 → Telegraph → 频道（GitHub Actions 版）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出待发送文章，不发布、不写库")
    ap.add_argument("--source", help="只跑指定栏目（name 字段）")
    args = ap.parse_args()

    setup_logging()
    if not BOT_TOKEN:
        logging.error("环境变量 BOT_TOKEN 未设置（仓库 Secrets 里新建 BOT_TOKEN），退出")
        sys.exit(2)
    sources = [s for s in SOURCES
               if not args.source or s["name"] == args.source]
    if not sources:
        logging.error(f"未找到栏目：{args.source}")
        sys.exit(2)
    if not CHANNEL:
        logging.error("环境变量 CHANNEL 未设置（仓库 Secrets 里新建 CHANNEL，填频道 chat_id 或 @用户名），退出")
        sys.exit(2)

    token = get_telegraph_token()
    conn = init_db()
    try:
        items = collect_new_items(conn, sources)

        now = datetime.now()
        fresh = []
        skipped_old = 0
        for src, url, title, pub_date in items:
            if (MAX_AGE_HOURS > 0 and pub_date is not None
                    and (now - pub_date).total_seconds()
                    > MAX_AGE_HOURS * 3600):
                skipped_old += 1
                continue
            fresh.append((src, url, title, pub_date))
        if skipped_old:
            logging.info(f"跳过 {skipped_old} 篇超过 {MAX_AGE_HOURS} 小时的旧文")
        batch = fresh[:MAX_PER_RUN]
        if len(fresh) > len(batch):
            logging.info(f"待处理 {len(fresh)} 篇，本次上限 {len(batch)} 篇，"
                         "其余下次补发")

        if args.dry_run:
            for src, url, title, pub_date in batch:
                d = pub_date.strftime("%m-%d") if pub_date else "未知日期"
                logging.info(f"[dry-run][{src['name']}] 待发送：{d} "
                             f"{title[:40]} {url}")
        else:
            for src, url, title, pub_date in batch:
                try:
                    process_article(conn, token, src, url, list_title=title)
                except Exception as e:  # noqa: BLE001
                    logging.error(f"处理失败，已跳过：{url}：{e}")
                time.sleep(SLEEP_BETWEEN_ARTICLES)
        if not args.dry_run:
            cleanup_db(conn)
    finally:
        conn.commit()
        conn.close()
    logging.info("本轮运行结束")


if __name__ == "__main__":
    main()
