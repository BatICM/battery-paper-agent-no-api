#!/usr/bin/env python3
"""Battery Paper Agent - No API Version.

每天检索电池管理相关论文，使用规则打分生成中文HTML日报，支持邮件推送和GitHub Pages归档。
不调用 OpenAI/ChatGPT API，因此不会产生API token费用。
"""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
OUTPUT_DIR = ROOT / "outputs"


@dataclass
class Paper:
    title: str
    authors: List[str] = field(default_factory=list)
    abstract: str = ""
    venue: str = ""
    date: str = ""
    doi: str = ""
    url: str = ""
    source: str = ""
    score: float = 0.0
    grade: str = "C"
    matched_keywords: List[str] = field(default_factory=list)
    excluded_keywords: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)


def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["contact_email"] = os.getenv("CONTACT_EMAIL") or cfg.get("contact_email") or "your_email@example.com"
    return cfg


def http_get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return json.loads(data)
    except Exception as exc:
        print(f"[WARN] JSON request failed: {exc}\n  URL: {url}", file=sys.stderr)
        return None


def http_get_text(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Optional[str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"[WARN] Text request failed: {exc}\n  URL: {url}", file=sys.stderr)
        return None


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(x) for x in value if x)
    value = re.sub(r"<[^>]+>", " ", str(value))
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_title(title: str) -> str:
    title = clean_text(title).lower()
    title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def parse_date(value: str) -> Optional[dt.date]:
    if not value:
        return None
    value = value.strip()
    for fmt in ["%Y-%m-%d", "%Y-%m", "%Y"]:
        try:
            parsed = dt.datetime.strptime(value[: len(fmt)], fmt).date()
            return parsed
        except Exception:
            pass
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except Exception:
        return None


def date_range(days: int) -> Tuple[str, str]:
    today = dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=days)
    return start.isoformat(), today.isoformat()


def fetch_arxiv(cfg: Dict[str, Any], days: int) -> List[Paper]:
    if not cfg.get("sources", {}).get("arxiv", True):
        return []
    max_results = int(cfg["run"].get("max_raw_results_per_query", 25))
    include_queries = cfg.get("queries", {}).get("include", [])
    headers = {"User-Agent": f"battery-paper-agent/1.0 ({cfg['contact_email']})"}
    papers: List[Paper] = []

    # arXiv日期过滤不如OpenAlex方便，因此先按相关性抓近期排序，再由本地日期过滤。
    # 用少量高召回查询，避免过长URL。
    arxiv_queries = [
        'all:"battery management" OR all:"state of health" OR all:"remaining useful life"',
        'all:"lithium-ion battery" AND (all:"fault diagnosis" OR all:"thermal runaway" OR all:"digital twin")',
        'all:"battery" AND (all:"state of charge" OR all:"prognostics" OR all:"uncertainty")',
    ]
    start_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days)
    for q in arxiv_queries:
        params = {
            "search_query": q,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        text = http_get_text(url, headers=headers)
        time.sleep(1.2)  # arXiv礼貌访问
        if not text:
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            print(f"[WARN] arXiv XML parse failed: {exc}", file=sys.stderr)
            continue
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        for entry in root.findall("atom:entry", ns):
            title = clean_text(entry.findtext("atom:title", default="", namespaces=ns))
            abstract = clean_text(entry.findtext("atom:summary", default="", namespaces=ns))
            published = entry.findtext("atom:published", default="", namespaces=ns)
            pdate = parse_date(published)
            if pdate and pdate < start_date:
                continue
            authors = [clean_text(a.findtext("atom:name", default="", namespaces=ns)) for a in entry.findall("atom:author", ns)]
            link = ""
            for lnk in entry.findall("atom:link", ns):
                if lnk.attrib.get("rel") == "alternate":
                    link = lnk.attrib.get("href", "")
                    break
            papers.append(Paper(
                title=title,
                authors=[a for a in authors if a],
                abstract=abstract,
                venue="arXiv",
                date=(pdate.isoformat() if pdate else clean_text(published)[:10]),
                url=link,
                source="arXiv",
            ))
    return papers


def fetch_openalex(cfg: Dict[str, Any], days: int) -> List[Paper]:
    if not cfg.get("sources", {}).get("openalex", True):
        return []
    start, end = date_range(days)
    max_results = int(cfg["run"].get("max_raw_results_per_query", 25))
    contact = cfg["contact_email"]
    headers = {"User-Agent": f"battery-paper-agent/1.0 ({contact})"}
    papers: List[Paper] = []
    for query in cfg.get("queries", {}).get("include", []):
        params = {
            "search": query,
            "filter": f"from_publication_date:{start},to_publication_date:{end},type:article",
            "per-page": max_results,
            "sort": "publication_date:desc",
            "mailto": contact,
        }
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        data = http_get_json(url, headers=headers)
        time.sleep(0.2)
        if not data:
            continue
        for item in data.get("results", []):
            title = clean_text(item.get("title"))
            if not title:
                continue
            abstract = inverted_index_to_text(item.get("abstract_inverted_index"))
            authors = []
            for au in item.get("authorships", [])[:12]:
                name = clean_text((au.get("author") or {}).get("display_name"))
                if name:
                    authors.append(name)
            host = item.get("primary_location", {}) or {}
            source = host.get("source") or {}
            venue = clean_text(source.get("display_name") or item.get("host_venue", {}).get("display_name"))
            doi = clean_text(item.get("doi") or "").replace("https://doi.org/", "")
            url = clean_text(item.get("doi") or host.get("landing_page_url") or item.get("id"))
            papers.append(Paper(
                title=title,
                authors=authors,
                abstract=abstract,
                venue=venue or "OpenAlex",
                date=clean_text(item.get("publication_date")),
                doi=doi,
                url=url,
                source="OpenAlex",
            ))
    return papers


def inverted_index_to_text(inv: Any) -> str:
    if not isinstance(inv, dict):
        return ""
    words: List[Tuple[int, str]] = []
    for word, positions in inv.items():
        if isinstance(positions, list):
            for pos in positions:
                try:
                    words.append((int(pos), word))
                except Exception:
                    pass
    words.sort(key=lambda x: x[0])
    return clean_text(" ".join(w for _, w in words))


def fetch_crossref(cfg: Dict[str, Any], days: int) -> List[Paper]:
    if not cfg.get("sources", {}).get("crossref", True):
        return []
    start, end = date_range(days)
    max_results = int(cfg["run"].get("max_raw_results_per_query", 25))
    contact = cfg["contact_email"]
    headers = {"User-Agent": f"battery-paper-agent/1.0 (mailto:{contact})"}
    papers: List[Paper] = []
    for query in cfg.get("queries", {}).get("include", []):
        params = {
            "query.bibliographic": query,
            "filter": f"from-pub-date:{start},until-pub-date:{end},type:journal-article",
            "rows": max_results,
            "sort": "published",
            "order": "desc",
            "mailto": contact,
        }
        url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
        data = http_get_json(url, headers=headers)
        time.sleep(0.2)
        if not data:
            continue
        for item in (data.get("message") or {}).get("items", []):
            title = clean_text(item.get("title", [""]))
            if not title:
                continue
            abstract = clean_text(item.get("abstract", ""))
            authors = []
            for au in item.get("author", [])[:12]:
                name = clean_text(" ".join([au.get("given", ""), au.get("family", "")]))
                if name:
                    authors.append(name)
            venue = clean_text(item.get("container-title", [""]))
            doi = clean_text(item.get("DOI"))
            url = clean_text(item.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
            published = crossref_date(item)
            papers.append(Paper(
                title=title,
                authors=authors,
                abstract=abstract,
                venue=venue or "Crossref",
                date=published,
                doi=doi,
                url=url,
                source="Crossref",
            ))
    return papers


def crossref_date(item: Dict[str, Any]) -> str:
    for key in ["published-online", "published-print", "published", "issued", "created"]:
        parts = (((item.get(key) or {}).get("date-parts") or [[None]])[0])
        if parts and parts[0]:
            y = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 and parts[1] else 1
            d = int(parts[2]) if len(parts) > 2 and parts[2] else 1
            return dt.date(y, m, d).isoformat()
    return ""


def dedupe_papers(papers: Sequence[Paper]) -> List[Paper]:
    seen: Dict[str, Paper] = {}
    for p in papers:
        doi_key = p.doi.lower().strip()
        title_key = normalize_title(p.title)
        key = doi_key or title_key
        if not key:
            continue
        if key in seen:
            old = seen[key]
            # 合并更完整的字段。
            old.abstract = old.abstract or p.abstract
            old.venue = old.venue if old.venue and old.venue.lower() not in {"openalex", "crossref"} else p.venue
            old.url = old.url or p.url
            old.doi = old.doi or p.doi
            old.source = old.source + "+" + p.source if p.source not in old.source else old.source
            if len(p.authors) > len(old.authors):
                old.authors = p.authors
        else:
            seen[key] = p
    return list(seen.values())


def score_paper(p: Paper, cfg: Dict[str, Any]) -> Paper:
    text = f"{p.title} {p.abstract} {p.venue}".lower()
    score = 0.0
    reasons: List[str] = []
    matched: List[str] = []
    excluded: List[str] = []

    for kw, wt in cfg.get("scoring_keywords", {}).items():
        kw_lower = kw.lower()
        if kw_lower in text:
            score += float(wt)
            matched.append(kw)

    for kw in cfg.get("queries", {}).get("exclude", []):
        kw_lower = kw.lower()
        if kw_lower in text:
            excluded.append(kw)
            score -= 22

    # 标题中出现核心词更重要。
    title_text = p.title.lower()
    for core in ["state of health", "remaining useful life", "battery management", "thermal runaway", "fault diagnosis", "state of charge", "battery pack", "digital twin"]:
        if core in title_text:
            score += 10
            reasons.append(f"标题命中核心方向：{core}")

    # 期刊/来源加权。
    venue_norm = normalize_title(p.venue)
    whitelist = cfg.get("journal_whitelist", [])
    for journal in whitelist:
        j_norm = normalize_title(journal)
        if j_norm and (j_norm in venue_norm or venue_norm in j_norm):
            score += 25
            reasons.append(f"来源属于重点期刊：{journal}")
            break
    if p.source.lower() == "arxiv":
        score += 6
        reasons.append("arXiv新近预印本")

    # 日期加权。
    pdate = parse_date(p.date)
    if pdate:
        age = (dt.datetime.now(dt.timezone.utc).date() - pdate).days
        if age <= 2:
            score += 10
            reasons.append("最近2天内上线/发表")
        elif age <= 7:
            score += 5
            reasons.append("最近7天内上线/发表")

    # 摘要存在加少量分。
    if p.abstract and len(p.abstract) > 200:
        score += 3

    # 如果明显是纯材料方向且没有BMS核心词，重罚。
    has_bms_core = any(k in text for k in ["state of health", "remaining useful life", "state of charge", "battery management", "fault diagnosis", "thermal runaway", "prognostics", "digital twin", "battery pack", "field data", "early warning"])
    if excluded and not has_bms_core:
        score -= 30
        reasons.append("疑似纯材料/化学论文，且缺少BMS核心词")

    p.score = round(max(score, 0.0), 1)
    p.matched_keywords = sorted(set(matched), key=lambda x: x.lower())
    p.excluded_keywords = sorted(set(excluded), key=lambda x: x.lower())
    p.reasons = reasons
    if p.score >= 80:
        p.grade = "A"
    elif p.score >= 48:
        p.grade = "B"
    else:
        p.grade = "C"
    return p


def detect_topic(p: Paper) -> str:
    text = f"{p.title} {p.abstract}".lower()
    topics = []
    mapping = [
        ("SOH/RUL", ["state of health", "soh", "remaining useful life", "rul", "prognostics"]),
        ("SOC估计", ["state of charge", "soc"]),
        ("故障诊断", ["fault diagnosis", "fault detection", "anomaly detection"]),
        ("安全预警", ["thermal runaway", "safety warning", "early warning", "overheating"]),
        ("Pack一致性", ["battery pack", "pack inconsistency", "cell inconsistency", "inter-cell"]),
        ("现场数据/车队", ["field data", "fleet", "real-world", "electric vehicle"]),
        ("数字孪生/物理约束", ["digital twin", "physics-informed", "physics based", "model-based"]),
        ("储能系统", ["energy storage system", "bess", "grid"]),
        ("不确定性", ["uncertainty", "probabilistic", "confidence interval"]),
        ("迁移学习", ["transfer learning", "domain adaptation", "generalization"]),
    ]
    for name, kws in mapping:
        if any(kw in text for kw in kws):
            topics.append(name)
    return "、".join(topics[:3]) if topics else "电池健康管理相关"


def templated_summary(p: Paper) -> str:
    topic = detect_topic(p)
    if "SOH/RUL" in topic:
        return "围绕电池健康状态或剩余寿命预测，适合跟踪其特征构造、泛化验证和工程部署价值。"
    if "安全预警" in topic:
        return "面向电池安全与热失控早期识别，适合关注传感信号、预警阈值和可部署性。"
    if "SOC估计" in topic:
        return "聚焦SOC估计或SOC不确定性，对储能/车辆BMS状态估计与调度有参考价值。"
    if "Pack一致性" in topic:
        return "关注电池包或单体差异问题，可为pack-level健康评估与均衡策略提供参考。"
    if "数字孪生/物理约束" in topic:
        return "涉及物理模型、数字孪生或物理约束学习，可用于提升BMS模型可解释性和泛化能力。"
    return "与电池健康管理、诊断或运行优化相关，建议结合摘要进一步判断是否精读。"


def contribution_points(p: Paper) -> List[str]:
    topic = detect_topic(p)
    points = []
    if "SOH/RUL" in topic:
        points.append("提取或利用退化相关特征，用于SOH/RUL预测。")
        points.append("可重点检查数据集规模、跨工况验证和误差指标。")
    if "安全预警" in topic:
        points.append("围绕热失控、异常升温或安全风险进行早期识别。")
        points.append("可关注传感信号是否可在实际BMS/储能系统中部署。")
    if "SOC估计" in topic:
        points.append("涉及SOC估计、不确定性或调度约束，适合连接BMS与EMS。")
    if "Pack一致性" in topic:
        points.append("关注cell-to-pack差异、单体不一致性或电池包层级管理。")
    if "现场数据/车队" in topic:
        points.append("涉及真实运行或车队数据，具有较强工程参考价值。")
    if "迁移学习" in topic or "数字孪生/物理约束" in topic:
        points.append("可为跨电池、跨工况、跨场景泛化提供方法参考。")
    if not points:
        points.append("从标题和摘要看与电池健康管理相关，需进一步阅读全文确认贡献边界。")
    return points[:4]


def group_and_select(papers: List[Paper], cfg: Dict[str, Any]) -> List[Paper]:
    papers = [score_paper(p, cfg) for p in papers]
    include_c = bool(cfg.get("run", {}).get("include_c_level", False))
    papers = [p for p in papers if include_c or p.grade in {"A", "B"}]
    papers.sort(key=lambda p: (p.score, p.date), reverse=True)
    return papers[: int(cfg["run"].get("max_final_papers", 12))]


def html_escape(s: str) -> str:
    return html.escape(s or "")


def render_report(papers: List[Paper], cfg: Dict[str, Any], lookback_days: int) -> str:
    today = dt.datetime.now().strftime("%Y-%m-%d")
    a_count = sum(1 for p in papers if p.grade == "A")
    b_count = sum(1 for p in papers if p.grade == "B")
    c_count = sum(1 for p in papers if p.grade == "C")

    paper_cards = []
    for idx, p in enumerate(papers, 1):
        author_text = ", ".join(p.authors[:6]) + (" et al." if len(p.authors) > 6 else "")
        link = p.url or (f"https://doi.org/{p.doi}" if p.doi else "")
        doi_html = f"<a href=\"https://doi.org/{html_escape(p.doi)}\">{html_escape(p.doi)}</a>" if p.doi else "—"
        tag_class = f"tag-{p.grade.lower()}"
        points = "\n".join(f"<li>{html_escape(x)}</li>" for x in contribution_points(p))
        reasons = p.reasons or ["根据关键词、来源、日期和摘要相关性自动评分。"]
        reason_html = "；".join(html_escape(r) for r in reasons[:4])
        kw_html = ", ".join(html_escape(k) for k in p.matched_keywords[:10]) or "—"
        abstract_short = html_escape(p.abstract[:650] + ("..." if len(p.abstract) > 650 else ""))
        paper_cards.append(f"""
        <div class="paper">
          <h3>{idx}. {html_escape(p.title)}</h3>
          <p><span class="{tag_class}">{p.grade}级</span> <span class="score">规则评分：{p.score}</span></p>
          <p class="meta"><b>方向：</b>{html_escape(detect_topic(p))}</p>
          <p class="meta"><b>作者：</b>{html_escape(author_text) or "—"}</p>
          <p class="meta"><b>来源：</b>{html_escape(p.venue or p.source)} ｜ <b>日期：</b>{html_escape(p.date)} ｜ <b>数据源：</b>{html_escape(p.source)}</p>
          <p class="meta"><b>DOI：</b>{doi_html} ｜ <b>链接：</b>{f'<a href="{html_escape(link)}">Open</a>' if link else '—'}</p>
          <p><b>一句话总结：</b>{html_escape(templated_summary(p))}</p>
          <p><b>规则提取的核心贡献：</b></p>
          <ul>{points}</ul>
          <p><b>与BMS相关性：</b>命中关键词：{kw_html}</p>
          <p><b>推荐理由：</b>{reason_html}</p>
          <details><summary>查看摘要</summary><p>{abstract_short or '暂无摘要。'}</p></details>
        </div>
        """)

    rows = []
    for p in papers:
        link = p.url or (f"https://doi.org/{p.doi}" if p.doi else "")
        rows.append(f"""
        <tr>
          <td>{html_escape(p.grade)}</td>
          <td>{html_escape(p.title)}</td>
          <td>{html_escape(p.venue or p.source)}</td>
          <td>{html_escape(detect_topic(p))}</td>
          <td>{p.score}</td>
          <td>{f'<a href="{html_escape(link)}">Open</a>' if link else '—'}</td>
        </tr>
        """)

    trend = build_trend_text(papers)
    no_result_note = "" if papers else "<p class='warning'>今日未筛选出A/B级论文。建议放宽关键词或开启C级输出。</p>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html_escape(cfg.get('site', {}).get('title', 'BMS论文日报'))}｜{today}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", Arial, sans-serif; line-height: 1.65; color: #1f2933; max-width: 1080px; margin: 0 auto; padding: 28px; background: #f7f9fb; }}
  h1 {{ color: #0b3d5c; border-bottom: 3px solid #0b6fa4; padding-bottom: 8px; }}
  h2 {{ margin-top: 30px; color: #0b3d5c; }}
  .summary {{ background: #e8f4fb; border-left: 5px solid #0b6fa4; padding: 14px 18px; border-radius: 8px; }}
  .paper {{ background: #fff; border: 1px solid #d9e2ec; border-radius: 10px; padding: 18px 20px; margin: 18px 0; box-shadow: 0 2px 6px rgba(0,0,0,0.04); }}
  .tag-a, .tag-b, .tag-c {{ display: inline-block; color: white; padding: 3px 10px; border-radius: 13px; font-size: 13px; font-weight: 700; }}
  .tag-a {{ background: #c62828; }} .tag-b {{ background: #ef6c00; }} .tag-c {{ background: #607d8b; }}
  .score {{ color: #52616b; margin-left: 8px; font-size: 13px; }}
  .meta {{ color: #52616b; font-size: 14px; }}
  table {{ width: 100%; border-collapse: collapse; background: white; margin-top: 12px; }}
  th, td {{ border: 1px solid #d9e2ec; padding: 9px; text-align: left; vertical-align: top; }}
  th {{ background: #edf4f7; }}
  details {{ background: #f8fafc; padding: 8px 10px; border-radius: 6px; }}
  .warning {{ color: #b45309; font-weight: 700; }}
  .footer {{ margin-top: 30px; color: #64748b; font-size: 13px; }}
</style>
</head>
<body>
<h1>{html_escape(cfg.get('site', {}).get('title', 'BMS论文日报'))}</h1>
<div class="summary">
  <p><b>日期：</b>{today}</p>
  <p><b>检索范围：</b>最近 {lookback_days} 天。优先输出A/B级结果；本版本完全基于规则打分，不调用OpenAI/ChatGPT API。</p>
  <p><b>今日保留：</b>{len(papers)} 篇；A级 {a_count} 篇，B级 {b_count} 篇，C级 {c_count} 篇。</p>
  <p><b>数据源：</b>arXiv、OpenAlex、Crossref。筛选依据为关键词、期刊白名单、日期、摘要和排除词。</p>
</div>
{no_result_note}
<h2>一、今日推荐论文</h2>
{''.join(paper_cards) if paper_cards else '<p>暂无推荐论文。</p>'}
<h2>二、快速浏览表</h2>
<table>
  <tr><th>等级</th><th>论文</th><th>来源</th><th>方向</th><th>评分</th><th>链接</th></tr>
  {''.join(rows)}
</table>
<h2>三、今日趋势判断（规则生成）</h2>
<p>{html_escape(trend)}</p>
<h2>四、使用提醒</h2>
<ul>
  <li>无API版本不会产生ChatGPT/OpenAI token费用，但中文凝练为模板化表达。</li>
  <li>若要提高筛选精度，可在config.yaml中增减关键词、期刊白名单和排除词。</li>
  <li>若某天结果过少，脚本会自动从lookback_days放宽到fallback_lookback_days。</li>
</ul>
<p class="footer">Generated by battery-paper-agent-no-api.</p>
</body>
</html>
"""


def build_trend_text(papers: List[Paper]) -> str:
    if not papers:
        return "今日未形成明显趋势。可适当放宽时间窗口或增加关键词。"
    topic_counts: Dict[str, int] = {}
    for p in papers:
        for t in detect_topic(p).split("、"):
            if t:
                topic_counts[t] = topic_counts.get(t, 0) + 1
    ranked = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)
    top = "、".join(f"{k}({v})" for k, v in ranked[:5])
    return f"今日结果主要集中在：{top}。建议优先关注A级论文，并将SOH/RUL、故障诊断、安全预警、Pack一致性与真实运行数据相关条目纳入课题组文献库。"


def save_outputs(report_html: str, papers: List[Paper]) -> Tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now().strftime("%Y-%m-%d")
    html_path = OUTPUT_DIR / f"{today}.html"
    json_path = OUTPUT_DIR / f"{today}.json"
    html_path.write_text(report_html, encoding="utf-8")
    json_path.write_text(json.dumps([asdict(p) for p in papers], ensure_ascii=False, indent=2), encoding="utf-8")
    update_index()
    return html_path, json_path


def update_index() -> None:
    files = sorted(OUTPUT_DIR.glob("20*.html"), reverse=True)
    rows = []
    for f in files[:120]:
        date = f.stem
        rows.append(f"<li><a href=\"{html_escape(f.name)}\">{html_escape(date)} BMS论文日报</a></li>")
    index = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>BMS论文日报归档</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif;max-width:900px;margin:0 auto;padding:32px;line-height:1.7}} h1{{color:#0b3d5c}} li{{margin:8px 0}}</style>
</head><body>
<h1>BMS论文日报归档</h1>
<p>本页面由 GitHub Actions 自动更新。无API版本仅使用开放元数据接口和规则打分。</p>
<ul>{''.join(rows)}</ul>
</body></html>"""
    (OUTPUT_DIR / "index.html").write_text(index, encoding="utf-8")


def send_email_if_configured(cfg: Dict[str, Any], html_body: str, html_path: Path) -> None:
    email_cfg = cfg.get("email", {})
    if not email_cfg.get("enabled", True):
        print("[INFO] Email disabled in config.yaml")
        return

    def env(name_key: str) -> str:
        return os.getenv(email_cfg.get(name_key, ""), "")

    smtp_host = env("smtp_host_env")
    smtp_port = int(env("smtp_port_env") or "465")
    smtp_user = env("smtp_user_env")
    smtp_pass = env("smtp_pass_env")
    email_from = env("email_from_env") or smtp_user
    email_to = env("email_to_env")

    missing = [name for name, value in {
        "SMTP_HOST": smtp_host,
        "SMTP_USER": smtp_user,
        "SMTP_PASS": smtp_pass,
        "EMAIL_FROM/SMTP_USER": email_from,
        "EMAIL_TO": email_to,
    }.items() if not value]
    if missing:
        print(f"[INFO] Email skipped. Missing env/secrets: {', '.join(missing)}")
        return

    recipients = [x.strip() for x in email_to.split(",") if x.strip()]
    subject = f"BMS论文日报｜{dt.datetime.now().strftime('%Y-%m-%d')}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = ", ".join(recipients)
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.attach(MIMEText("请使用支持HTML的邮件客户端查看BMS论文日报。", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=45) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(email_from, recipients, msg.as_string())
    print(f"[INFO] Email sent to {len(recipients)} recipient(s).")


def collect_papers(cfg: Dict[str, Any], days: int) -> List[Paper]:
    papers: List[Paper] = []
    print(f"[INFO] Fetching papers from the last {days} day(s)...")
    papers.extend(fetch_arxiv(cfg, days))
    papers.extend(fetch_openalex(cfg, days))
    papers.extend(fetch_crossref(cfg, days))
    print(f"[INFO] Raw papers: {len(papers)}")
    papers = dedupe_papers(papers)
    print(f"[INFO] Deduped papers: {len(papers)}")
    return papers


def main() -> None:
    cfg = load_config()
    lookback = int(cfg["run"].get("lookback_days", 2))
    fallback = int(cfg["run"].get("fallback_lookback_days", 7))
    min_results = int(cfg["run"].get("min_results_before_fallback", 6))

    raw = collect_papers(cfg, lookback)
    selected = group_and_select(raw, cfg)
    actual_days = lookback
    if len(selected) < min_results and fallback > lookback:
        print(f"[INFO] Only {len(selected)} selected. Falling back to {fallback} days.")
        raw = collect_papers(cfg, fallback)
        selected = group_and_select(raw, cfg)
        actual_days = fallback

    report = render_report(selected, cfg, actual_days)
    html_path, json_path = save_outputs(report, selected)
    print(f"[INFO] Report saved: {html_path}")
    print(f"[INFO] Metadata saved: {json_path}")
    send_email_if_configured(cfg, report, html_path)


if __name__ == "__main__":
    main()
