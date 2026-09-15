#!/usr/bin/env python3
"""hivemind — quick CLI for the Banodoco knowledge corpus.

Commands:
  hivemind "QUERY" [--limit N]                    distillations-first corpus search
  hivemind search "QUERY" [flags]                 same as above
      [--resources] [--kind K] [--since D] [--limit N] [--offset N] [--json]
  hivemind probe "TERM" [flags]                   message counts per channel group
      [--since D] [--until D] [--channel C] [--json]
      (bare `probe --channel C` = total message count for that channel)
  hivemind trend "TERM" [flags]                   monthly counts, newest first (corpus-wide)
      [--months N] [--since D] [--until D] [--channel C] [--json]
  hivemind authors NAME... [--json]               message counts per author (case-sensitive!)
  hivemind top-authors [--limit N]                rank authors by message count
  hivemind recent [flags]                         recent messages (no --channel = ALL channels)
      [--channel C] [--author A] [--term T ...] [--since D] [--until D]
      [--limit N] [--full] [--count] [--no-order] [--order asc|desc]
      [--before-id ID] [--after-id ID] [--links] [--json]
      (paging: backward = --before-id, forward = --order asc + --after-id;
       unscoped asc scans on big channels time out — anchor with a date/keyset)
  hivemind around ID [--window M]                 conversation window around a message (±M minutes)
  hivemind day --channel C --date D               full chronological day in a channel
  hivemind profile AUTHOR [--channel C]           first/last/total/busiest month
  hivemind reactions ID [--json]                  who reacted to a message (emoji breakdown)
  hivemind media ID [--json]                      all media for a message (refresh-media-urls
                                                 edge function + CDN links in content)
  hivemind top-reacted [--channel C] [--limit N] [--since D] [--until D]
                                                 most-reacted messages (bucket-bisected fetch)
  hivemind get ID|URL [--json]                    fetch one message + permalink (accepts
                                                 discord.com/channels/{g}/{c}/{m} URLs)
  hivemind cites KIND ID [--resolve] [--json]     distillations citing an item
      KIND = message|resource|distillation; for KIND=distillation, lists the
      distillation's own cites (forward direction)

Notes: --since inclusive, --until exclusive. `trend` is corpus-wide; `probe` is
group-scoped (groups overlap — `resources` appears in 5 — and omit narrow
channels; use --channel). Do NOT pre-URL-encode terms. `recent` with no
--channel searches all channels. Offset paging without an order silently
repeats rows — always page by time buckets or keyset.

Full API reference: the /hivemind skill.
"""
import json
import io
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone

API = "https://ujlwuvkrxlvoswwkerdf.supabase.co/rest/v1"
KEY = "sb_publishable_O38oPBafrBoFrpi_rlWJvA_UJrulFsx"
URL_RE = re.compile(r"https?://[^\s\"'<>]+")

GROUPS = {
    "summaries": ["daily_summaries", "live_updates"],
    "wan": ["wan_chatter", "wan_comfyui", "wan_gens", "wan_resources", "resources"],
    "ltx": ["ltx_chatter", "ltx_resources", "ltx_gens", "ltx_training", "resources"],
    "comfy": ["comfyui", "wan_comfyui", "ltx_chatter", "resources"],
    "training": ["training_control_loras", "ltx_training", "wan_training", "comfyui"],
    "coding": ["vibecoding", "resources"],
    "minimax": ["minimax_h3_chatter", "minimax_h3_gens", "minimax_h3_training",
                "minimax_h3_resources", "minimax_music3", "resources"],
    "general": ["chatter", "nsfw", "general", "updates", "dev-chatter",
                "art_experiments", "editorial_decisions"],
}

# Narrow channels the groups omit (probe them with --channel). Snapshot 2026-08-18.
NARROW = ["ace-step", "acestep_resources", "hunyuanvideo", "qwen-image",
          "chroma", "flux", "flux3", "z-image", "magi", "seedance", "top_gens",
          "art_sharing", "introductions", "music", "off-topic", "res4lyf",
          "become-a-speaker", "welcome"]

VALUE_FLAGS = {"limit", "offset", "since", "until", "channel", "author", "term", "kind",
               "months", "before-id", "after-id", "window", "date", "order"}


def get(table, params, prefer=None, retries=4):
    url = f"{API}/{table}?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"apikey": KEY}
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, dict(r.headers), json.load(r)
        except urllib.error.HTTPError as e:
            code, h = e.code, dict(e.headers)
            body = e.read().decode("utf-8", "replace")[:300]
            if code >= 500 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return code, h, body
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return 0, {}, str(e)[:300]


def total(headers):
    """Total row count from the Content-Range header (count=exact probes)."""
    cr = headers.get("Content-Range") or headers.get("content-range")
    if not cr:
        return None
    try:
        return int(cr.split("/")[1])
    except (IndexError, ValueError):
        return None


def flags(argv):
    """Pull --flag value pairs out of argv; booleans are True; repeats append."""
    out = {}
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            out[k] = v
        elif a.startswith("--"):
            k = a[2:]
            if k in VALUE_FLAGS and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                v = argv[i + 1]
                if k in out and isinstance(out[k], list):
                    out[k].append(v)
                elif k in out:
                    out[k] = [out[k], v]
                else:
                    out[k] = v
                i += 1
            else:
                out[k] = True
        else:
            rest.append(a)
        i += 1
    return out, rest


def _count_or_error(label, status, headers, body, js=False):
    if status not in (200, 206):
        err = f"ERROR {status} {body}"
        print(json.dumps({label: err}) if js else f"{label}: {err}")
        return 1
    n = total(headers)
    if n is None:
        err = "ERROR missing Content-Range header"
        print(json.dumps({label: err}) if js else f"{label}: {err}")
        return 1
    print(json.dumps({label: n}) if js else f"{label}: {n}")
    return 0


def _parse_date(s):
    """Accept ISO date, today, yesterday, or Nd/Nw/Nm (days/weeks/months ago)."""
    s = s.strip().lower()
    if s == "today":
        return date.today().isoformat()
    if s == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()
    if re.fullmatch(r"\d+[dwmy]", s):
        n = int(s[:-1])
        unit = s[-1]
        d = date.today()
        if unit == "d":
            d -= timedelta(days=n)
        elif unit == "w":
            d -= timedelta(weeks=n)
        elif unit == "m":
            d = (d.replace(day=1) - timedelta(days=1)).replace(day=1)
            for _ in range(n - 1):
                d = (d.replace(day=1) - timedelta(days=1)).replace(day=1)
        return d.isoformat()
    return s


def date_filters(fl):
    """created_at.gte/lt params from --since/--until (accepts date words)."""
    vals = []
    if fl.get("since"):
        vals.append(f"gte.{_parse_date(fl['since'])}")
    if fl.get("until"):
        vals.append(f"lt.{_parse_date(fl['until'])}")
    return {"created_at": vals} if vals else {}


def _terms(fl):
    t = fl.get("term")
    if isinstance(t, list):
        return t
    return [t] if t else []


def _resolve_id(iid):
    """Accept a bare snowflake or discord.com/channels/{g}/{c}/{m} URL."""
    if "discord.com/channels/" in iid:
        for p in reversed([p for p in iid.split("/") if p]):
            if p.isdigit():
                return p
    return iid


def cmd_search(q, fl):
    """Run the maintained raw-table search and retain the legacy display."""
    limit = int(fl.get("limit", 20))
    argv = ["--query", q, "--limit", str(limit)]
    if fl.get("since"):
        argv += ["--since", _parse_date(str(fl["since"]))]
    if fl.get("until"):
        argv += ["--until", _parse_date(str(fl["until"]))]
    if fl.get("channel"):
        argv += ["--channel", str(fl["channel"])]
    if fl.get("author"):
        argv += ["--author", str(fl["author"])]
    if fl.get("kind"):
        argv += ["--kinds", str(fl["kind"])]
    elif fl.get("resources"):
        argv += ["--kinds", "resource"]
    if fl.get("offset"):
        argv += ["--offset", str(fl["offset"])]
    out = io.StringIO()
    err = io.StringIO()
    try:
        from .executors.search.run import main as search_main
    except ImportError:
        from executors.search.run import main as search_main
    with redirect_stdout(out), redirect_stderr(err):
        rc = search_main(argv)
    raw = out.getvalue().strip()
    # The canonical executor keeps paging/degradation diagnostics on stderr;
    # preserve that channel for callers even though we adapt stdout for the
    # legacy human display.
    if err.getvalue():
        sys.stderr.write(err.getvalue())
    if fl.get("json"):
        print(raw)
        return rc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        if raw:
            print(raw)
        return rc or 1
    if payload.get("error"):
        print(f"ERROR {payload['error']}")
        return rc or 1
    rows = payload.get("results", [])
    for r in rows:
        kind = r.get("kind")
        if kind == "distillation":
            st = (r.get("metadata") or {}).get("status", "?")
            kind = f"distillation({st})"
        title = r.get("title") or ""
        body = (r.get("body") or "").replace("\n", " ")
        print(f"[{kind}] {title or body[:80]}")
        print(f"  {r.get('author') or '-'} @ {r.get('context') or '-'}  "
              f"{(r.get('created_at') or '')[:10]}  "
              f"id={r.get('item_id')}  src={r.get('source') or '-'}")
        if body:
            print(f"  {body[:220]}")
        if r.get("url"):
            print(f"  {r['url']}")
        print()
    total_count = payload.get("total")
    if total_count is not None:
        shown = len(rows)
        page = payload.get("page", 1)
        pages = payload.get("pages", 1)
        print(f"Showing {shown} of {total_count} results (page {page} of {pages})")
    return 0


def cmd_probe(term, fl):
    rc = 0
    if fl.get("channel"):
        params = {"select": "message_id", "channel_name": f"eq.{fl['channel']}",
                  "limit": 0}
        if term:
            params["content"] = f"ilike.*{term}*"
        params.update(date_filters(fl))
        s, h, b = get("message_feed", params, prefer="count=exact")
        return _count_or_error(fl["channel"], s, h, b, js=bool(fl.get("json")))
    if not term:
        print("probe needs a TERM or --channel C")
        return 2
    for group, channels in GROUPS.items():
        s, h, b = get("message_feed",
                      {"select": "message_id",
                       "channel_name": f"in.({','.join(channels)})",
                       "content": f"ilike.*{term}*", "limit": 0, **date_filters(fl)},
                      prefer="count=exact")
        rc |= _count_or_error(group, s, h, b, js=bool(fl.get("json")))
    return rc


def cmd_trend(term, fl):
    js = bool(fl.get("json"))
    rc = 0
    end = date.fromisoformat(fl["until"]) if fl.get("until") else date.today()
    if fl.get("since"):
        start = date.fromisoformat(fl["since"])
        months = max(1, min(((end.year - start.year) * 12 + end.month - start.month), 36))
    else:
        months = int(fl.get("months", 6))
        months = max(1, min(months, 36))
        start = end.replace(day=1)
    seen = 0
    cur_end = end
    while seen < months:
        cur_start = (cur_end.replace(day=1) - timedelta(days=1)).replace(day=1)
        label = cur_start.strftime("%Y-%m")
        params = {"select": "message_id", "content": f"ilike.*{term}*",
                  "created_at": [f"gte.{cur_start.isoformat()}",
                                 f"lt.{cur_end.isoformat()}"],
                  "limit": 0}
        if fl.get("channel"):
            params["channel_name"] = f"eq.{fl['channel']}"
        s, h, b = get("message_feed", params, prefer="count=exact")
        rc |= _count_or_error(label, s, h, b, js=js)
        cur_end = cur_start
        seen += 1
    return rc


def cmd_authors(names, fl):
    js = bool(fl.get("json"))
    rc = 0
    for name in names:
        s, h, b = get("message_feed",
                      {"select": "message_id",
                       "author_name": f"eq.{urllib.parse.quote(name, safe='')}",
                       "limit": 0},
                      prefer="count=exact")
        rc |= _count_or_error(name, s, h, b, js=js)
    return rc


def cmd_top_authors(fl):
    limit = int(fl.get("limit", 5))
    limit = max(1, min(limit, 10))
    # Sample both ends of each window; monthly sub-windows for recent history
    # (newest-only sampling misses a contributor's year-long activity spread).
    windows = [("2024-01-01", "2025-01-01")]
    cur = date(2025, 1, 1)
    while cur < date.today():
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        windows.append((cur.isoformat(), nxt.isoformat()))
        cur = nxt
    tally = {}
    for lo, hi in windows:
        for order in ("created_at.desc", "created_at.asc"):
            params = {"select": "author_name", "order": order,
                      "created_at": [f"gte.{lo}", f"lt.{hi}"], "limit": 300}
            s, _, rows = get("message_feed", params)
            if not isinstance(rows, list):
                print(rows)
                return 1
            for r in rows:
                a = r.get("author_name")
                if a:
                    tally[a] = tally.get(a, 0) + 1
    cands = [a for a, _ in sorted(tally.items(), key=lambda kv: -kv[1])][:limit * 3]
    ranked = []
    for a in cands:
        s, h, b = get("message_feed",
                      {"select": "message_id",
                       "author_name": f"eq.{urllib.parse.quote(a, safe='')}",
                       "limit": 0},
                      prefer="count=exact")
        n = total(h) if s in (200, 206) else 0
        ranked.append((a, n, s, b))
    ranked.sort(key=lambda t: -t[1])
    for a, n, s, b in ranked[:limit]:
        if s not in (200, 206):
            print(f"{a}: ERROR {s} {b}")
        else:
            print(f"{a}: {n}")
    return 0


def cmd_recent(fl):
    limit = int(fl.get("limit", 10))
    terms = _terms(fl)
    params = {"select": "message_id,content,author_name,channel_name,created_at",
              "limit": limit}
    order = fl.get("order")
    if terms or fl.get("no-order"):
        if terms:
            params["content"] = [f"ilike.*{t}*" for t in terms]
    else:
        # Snowflakes are time-ordered, so message_id ordering IS chronology
        # and walks the PK index (created_at ordering forces a full sort and
        # 57014s on big channels). Keyset flags fix the direction.
        if fl.get("after-id"):
            params["order"] = "message_id.asc"
        elif fl.get("before-id"):
            params["order"] = "message_id.desc"
        else:
            params["order"] = "message_id.desc" if order != "asc" else "message_id.asc"
    if fl.get("channel"):
        params["channel_name"] = f"eq.{fl['channel']}"
    if fl.get("author"):
        params["author_name"] = f"eq.{urllib.parse.quote(fl['author'], safe='')}"
    if fl.get("before-id"):
        params["message_id"] = f"lt.{fl['before-id']}"
    if fl.get("after-id"):
        if "message_id" in params:
            params["message_id"] = [params["message_id"], f"gt.{fl['after-id']}"]
        else:
            params["message_id"] = f"gt.{fl['after-id']}"
    params.update(date_filters(fl))
    js = bool(fl.get("json"))
    if fl.get("count"):
        params["limit"] = 0
        s, h, b = get("message_feed", params, prefer="count=exact")
        return _count_or_error("matches", s, h, b, js=js)
    _, _, rows = get("message_feed", params)
    if not isinstance(rows, list):
        print(rows)
        return 1
    if terms and not fl.get("no-order"):
        rows.sort(key=lambda r: r.get("created_at") or "",
                  reverse=(order != "asc"))
    if terms and not js:
        s, h, b = get("message_feed",
                      {k: v for k, v in params.items() if k != "order"},
                      prefer="count=exact")
        n = total(h) if s in (200, 206) else None
        if n is not None and n > len(rows):
            print(f"({n} matches; showing {len(rows)} — use --limit / --before-id to page)")
    full = bool(fl.get("full"))
    links = bool(fl.get("links"))
    for r in rows:
        content = (r.get("content") or "").replace("\n", " ")
        if not full:
            content = content[:220] + ("…" if len(content) > 220 else "")
        if js:
            out = dict(r)
            if links:
                out["links"] = URL_RE.findall(r.get("content") or "")
            print(json.dumps(out))
        else:
            print(f"[{r.get('channel_name')}] {r.get('author_name')} "
                  f"{(r.get('created_at') or '')[:16]}  id={r.get('message_id')}")
            print(f"  {content}")
            if links:
                for u in URL_RE.findall(r.get("content") or ""):
                    print(f"  🔗 {u}")
    return 0


def cmd_around(iid, fl):
    iid = _resolve_id(iid)
    _, _, rows = get("message_feed",
                     {"select": "message_id,content,author_name,channel_name,"
                                "channel_id,created_at",
                      "message_id": f"eq.{iid}"})
    if not isinstance(rows, list):
        print(rows)
        return 1
    if not rows:
        print(f"message {iid}: not found")
        return 1
    anchor = rows[0]
    ts = datetime.fromisoformat(anchor["created_at"])
    win = int(fl.get("window", 30))
    lo, hi = ts - timedelta(minutes=win), ts + timedelta(minutes=win)
    cid = anchor["channel_id"]
    params = {"select": "message_id,content,author_name,channel_name,created_at",
              "channel_id": f"eq.{cid}",
              "created_at": [f"gte.{lo.isoformat()}", f"lt.{hi.isoformat()}"],
              "order": "created_at.asc,message_id.asc"}
    msgs = []
    offset = 0
    while True:
        s, _, page = get("message_feed", {**params, "limit": 1000, "offset": offset})
        if not isinstance(page, list):
            print(page)
            return 1
        msgs += page
        if len(page) < 1000:
            break
        offset += 1000
    s, h, _ = get("message_feed",
                  {"select": "message_id", "channel_id": f"eq.{cid}",
                   "created_at": [f"gte.{lo.isoformat()}", f"lt.{hi.isoformat()}"],
                   "limit": 0}, prefer="count=exact")
    n = total(h) if s in (200, 206) else None
    print(f"conversation around {iid} ({anchor.get('channel_name')}, "
          f"±{win}min of {anchor.get('created_at')})")
    if n is not None:
        print(f"({n} messages in window{'; may be partial (opted-out authors excluded)' if n else ''})")
    print("(threads are flattened in this archive — blank lines separate author "
          "turns; adjacency is a proxy, not real reply edges)")
    prev_author = None
    for m in msgs:
        author = m.get("author_name")
        if prev_author is not None and author != prev_author:
            print()
        prev_author = author
        mark = " >>" if str(m.get("message_id")) == str(iid) else "   "
        print(f"{mark} [{m.get('created_at') or ''}][{author}] "
              f"id={m.get('message_id')}")
        print(f"      {(m.get('content') or '').replace(chr(10), ' ')[:200]}")
    return 0


def cmd_day(fl):
    channel = fl.get("channel")
    day = _parse_date(fl.get("date", "today")) if fl.get("date") else date.today().isoformat()
    if not channel:
        print("day needs --channel C [--date YYYY-MM-DD]")
        return 2
    d = date.fromisoformat(day)
    nxt = d + timedelta(days=1)
    params = {"select": "message_id,content,author_name,channel_name,created_at",
              "channel_name": f"eq.{channel}",
              "created_at": [f"gte.{d.isoformat()}", f"lt.{nxt.isoformat()}"],
              "order": "created_at.asc,message_id.asc"}
    s, h, _ = get("message_feed", {**params, "limit": 0}, prefer="count=exact")
    n = total(h) if s in (200, 206) else None
    rows = []
    offset = 0
    while True:
        s, _, page = get("message_feed", {**params, "limit": 1000, "offset": offset})
        if not isinstance(page, list):
            print(page)
            return 1
        rows += page
        if len(page) < 1000:
            break
        offset += 1000
    print(f"{channel} on {day}: {n or len(rows)} messages")
    for m in rows:
        print(f"[{m.get('created_at') or ''}][{m.get('author_name')}] id={m.get('message_id')}")
        print(f"  {(m.get('content') or '').replace(chr(10), ' ')[:220]}")
    return 0


def cmd_profile(author, fl):
    channel = fl.get("channel")
    base = {"author_name": f"eq.{urllib.parse.quote(author, safe='')}"}
    if channel:
        base["channel_name"] = f"eq.{channel}"
    s, h, _ = get("message_feed", {**base, "select": "message_id", "limit": 0},
                  prefer="count=exact")
    n = total(h) if s in (200, 206) else None
    s, _, first = get("message_feed",
                      {**base, "select": "message_id,content,created_at,channel_name",
                       "order": "created_at.asc", "limit": 1})
    s, _, last = get("message_feed",
                     {**base, "select": "message_id,content,created_at,channel_name",
                      "order": "created_at.desc", "limit": 1})
    where = f" in {channel}" if channel else ""
    print(f"{author}{where}: total = {n if n is not None else '?'}")
    if isinstance(first, list) and first:
        f = first[0]
        print(f"  first: {f.get('created_at')}  id={f.get('message_id')}  "
              f"{(f.get('content') or '').replace(chr(10), ' ')[:80]}")
    if isinstance(last, list) and last:
        l = last[0]
        print(f"  last:  {l.get('created_at')}  id={l.get('message_id')}  "
              f"{(l.get('content') or '').replace(chr(10), ' ')[:80]}")
    # busiest month: probe from first to last, cap 36 months
    if isinstance(first, list) and first:
        lo = datetime.fromisoformat(first[0]["created_at"]).date().replace(day=1)
        hi = (datetime.fromisoformat(last[0]["created_at"]).date().replace(day=1)
              if isinstance(last, list) and last else date.today())
        best, bestn, months = None, -1, []
        cur = lo
        while cur <= hi and len(months) < 36:
            nxt = (cur + timedelta(days=32)).replace(day=1)
            s, h, _ = get("message_feed",
                          {**base, "select": "message_id",
                           "created_at": [f"gte.{cur.isoformat()}", f"lt.{nxt.isoformat()}"],
                           "limit": 0}, prefer="count=exact")
            c = total(h) if s in (200, 206) else 0
            months.append((cur.strftime("%Y-%m"), c))
            if c > bestn:
                best, bestn = cur.strftime("%Y-%m"), c
            cur = nxt
        print(f"  busiest month: {best} ({bestn})")
        print("  monthly: " + ", ".join(f"{m}={c}" for m, c in months))
    return 0


def cmd_reactions(iid, fl):
    iid = _resolve_id(iid)
    _, _, rows = get("message_feed",
                     {"select": "reactions,content,author_name,channel_name",
                      "message_id": f"eq.{iid}"})
    if not isinstance(rows, list):
        print(rows)
        return 1
    if not rows:
        print(f"message {iid}: not found")
        return 1
    r = rows[0]
    if fl.get("json"):
        print(json.dumps(r))
        return 0
    rx = r.get("reactions") or []
    if not rx:
        print(f"message {iid}: no reactions stored")
        return 0
    by_emoji = {}
    for x in rx:
        by_emoji.setdefault(x.get("emoji"), []).append(x.get("reactor"))
    total_react = len(rx)
    uniq = len({x.get("reactor") for x in rx if x.get("reactor")})
    print(f"message {iid} ({r.get('author_name')} @ {r.get('channel_name')}): "
          f"{total_react} reactions, {uniq} unique reactors")
    for emoji, names in sorted(by_emoji.items(), key=lambda kv: -len(kv[1])):
        print(f"  {emoji} ×{len(names)}: {', '.join(n or '?' for n in names)}")
    return 0


CHANNELS = sorted({c for chs in GROUPS.values() for c in chs} | set(NARROW))


def _fetch_reacted(lo, hi, channel, budget):
    """Fetch all messages with non-null reactions in [lo, hi) for ONE channel,
    bucket-bisected. Corpus-wide reactions scans time out; channel-scoped
    scans are fast. Offset paging without order silently repeats rows, so we
    bisect by time instead."""
    if budget[0] <= 0:
        return []
    budget[0] -= 1
    params = {"select": "message_id,content,author_name,channel_name,channel_id,created_at,reactions",
              "reactions": "not.is.null",
              "channel_name": f"eq.{channel}",
              "created_at": [f"gte.{lo.isoformat()}", f"lt.{hi.isoformat()}"]}
    s, h, _ = get("message_feed", {**params, "limit": 0}, prefer="count=exact")
    n = total(h) if s in (200, 206) else None
    if not n:
        return []
    if n <= 1000:
        s, _, rows = get("message_feed", {**params, "order": "message_id.asc",
                                          "limit": 1000})
        return rows if isinstance(rows, list) else []
    if hi - lo <= timedelta(days=1):
        s, _, rows = get("message_feed", {**params, "order": "message_id.asc",
                                          "limit": 1000})
        rows = rows if isinstance(rows, list) else []
        if rows:
            print(f"  (note: {n} reacted msgs in a single day >1000; partial)", file=sys.stderr)
        return rows
    mid = lo + (hi - lo) / 2
    return _fetch_reacted(lo, mid, channel, budget) + _fetch_reacted(mid, hi, channel, budget)


def cmd_top_reacted(fl):
    limit = int(fl.get("limit", 10))
    limit = max(1, min(limit, 25))
    lo = (datetime.fromisoformat(fl["since"]).astimezone(timezone.utc)
          if fl.get("since") else datetime.now(timezone.utc) - timedelta(days=180))
    hi = (datetime.fromisoformat(fl["until"]).astimezone(timezone.utc)
          if fl.get("until") else datetime.now(timezone.utc))
    channels = [fl["channel"]] if fl.get("channel") else CHANNELS
    print(f"fetching reacted messages {lo.date()}..{hi.date()} across "
          f"{len(channels)} channels …", file=sys.stderr)
    rows = []
    budget = [300]
    for ch in channels:
        rows += _fetch_reacted(lo, hi, ch, budget)
        if budget[0] <= 0:
            print("  (request budget exhausted; results partial)", file=sys.stderr)
            break
    if not rows:
        print("no reacted messages found")
        return 0
    seen = {}
    for r in rows:
        if r.get("message_id") not in seen:
            seen[r["message_id"]] = r
    ranked = []
    for r in seen.values():
        rx = r.get("reactions") or []
        reactors = {x.get("reactor") for x in rx if x.get("reactor")}
        ranked.append((len(reactors), len(rx), r))
    ranked.sort(key=lambda t: (-t[0], -t[1]))
    print(f"top {min(limit, len(ranked))} most-reacted (ranked by distinct reactors):")
    for i, (nr, nt, r) in enumerate(ranked[:limit], 1):
        url = (f"https://discord.com/channels/1076117621407223829/"
               f"{r.get('channel_id')}/{r.get('message_id')}")
        print(f"{i:>2}. {nr} reactors / {nt} reactions | {r.get('author_name')} @ "
              f"{r.get('channel_name')} {(r.get('created_at') or '')[:10]} "
              f"id={r.get('message_id')}")
        print(f"     {url}")
        print(f"     {(r.get('content') or '').replace(chr(10), ' ')[:120]}")
    return 0


def cmd_get(iid, fl):
    iid = _resolve_id(iid)
    _, _, rows = get("message_feed",
                     {"select": "message_id,content,author_name,channel_name,"
                                "channel_id,guild_id,created_at,reactions",
                      "message_id": f"eq.{iid}"})
    if not isinstance(rows, list):
        print(rows)
        return 1
    if not rows:
        print(f"message {iid}: not found")
        return 1
    r = rows[0]
    if fl.get("json"):
        print(json.dumps(r))
        return 0
    url = (f"https://discord.com/channels/{r.get('guild_id')}/"
           f"{r.get('channel_id')}/{r.get('message_id')}")
    print(f"author:   {r.get('author_name')}")
    print(f"channel:  {r.get('channel_name')}  ({r.get('channel_id')})")
    print(f"date:     {r.get('created_at')}")
    print(f"url:      {url}")
    rx = r.get("reactions") or []
    if rx:
        print(f"reactions: {len(rx)} total / "
              f"{len({x.get('reactor') for x in rx if x.get('reactor')})} unique")
    print(f"content:  {(r.get('content') or '').replace(chr(10), ' ')}")
    return 0


def cmd_cites(kind, iid, fl):
    iid = _resolve_id(iid)
    if kind == "distillation":
        params = {"select": "*", "distillation_id": f"eq.{iid}"}
    else:
        params = {"select": "*", "item_kind": f"eq.{kind}", "item_id": f"eq.{iid}"}
    _, _, rows = get("distillation_cites", params)
    if not isinstance(rows, list):
        print(rows)
        return 1
    if fl.get("json"):
        print(json.dumps(rows))
        return 0
    if not rows:
        print(f"no cites for {kind} {iid}")
        return 0
    print(f"{len(rows)} cite(s) for {kind} {iid}:")
    for r in rows:
        print(f"  distillation_id={r.get('distillation_id')} "
              f"({r.get('item_kind')} {r.get('item_id')})")
    if fl.get("resolve") and kind != "distillation":
        ids = [str(r["item_id"]) for r in rows]
        _, _, msgs = get("message_feed",
                         {"select": "message_id,content,author_name,channel_name",
                          "message_id": f"in.({','.join(ids)})"})
        if isinstance(msgs, list):
            for m in msgs:
                print(f"    msg {m.get('message_id')} | {m.get('author_name')} @ "
                      f"{m.get('channel_name')}: "
                      f"{(m.get('content') or '').replace(chr(10), ' ')[:120]}")
    return 0


def cmd_media(iid, fl):
    iid = _resolve_id(iid)
    _, _, rows = get("message_feed",
                     {"select": "content", "message_id": f"eq.{iid}"})
    content = rows[0].get("content") if isinstance(rows, list) and rows else ""
    content_links = [u for u in URL_RE.findall(content or "")
                     if "cdn.discordapp" in u or "media.discordapp" in u]
    req = urllib.request.Request(
        "https://ujlwuvkrxlvoswwkerdf.supabase.co/functions/v1/refresh-media-urls",
        data=json.dumps({"message_id": iid}).encode(),
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"refresh-media-urls: ERROR {e.code} {e.read().decode('utf-8', 'replace')[:300]}")
        return 1
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        print(f"refresh-media-urls: ERROR {e}")
        return 1
    if fl.get("json"):
        out = {"message_id": iid, "attachments": data.get("attachments", []),
               "content_cdn_links": content_links}
        print(json.dumps(out))
        return 0
    atts = data.get("attachments", [])
    print(f"message {iid}: {len(atts)} attachment(s) (refreshed via Discord API)")
    for a in atts:
        print(f"  {a.get('filename')}")
        print(f"    url:  {a.get('url')}")
        if a.get("proxy_url"):
            print(f"    proxy: {a.get('proxy_url')}")
    if content_links:
        print(f"  + {len(content_links)} CDN link(s) found in message content:")
        for u in content_links:
            print(f"    {u}")
    if not atts and not content_links:
        print("  no attachments (message may have none, or the edge function found none)")
    return 0


def usage():
    print(__doc__.strip())
    return 0


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return usage()
    fl, rest = flags(argv)
    cmd = rest[0] if rest else argv[0]
    if cmd in ("-h", "--help", "help"):
        return usage()
    if cmd == "search":
        if len(rest) < 2:
            return usage()
        return cmd_search(rest[1], fl)
    if cmd == "probe":
        return cmd_probe(rest[1] if len(rest) > 1 else "", fl)
    if cmd == "trend":
        if len(rest) < 2:
            return usage()
        return cmd_trend(rest[1], fl)
    if cmd == "authors":
        if len(rest) < 2:
            return usage()
        return cmd_authors(rest[1:], fl)
    if cmd == "top-authors":
        return cmd_top_authors(fl)
    if cmd == "recent":
        return cmd_recent(fl)
    if cmd == "around":
        if len(rest) < 2:
            return usage()
        return cmd_around(rest[1], fl)
    if cmd == "day":
        return cmd_day(fl)
    if cmd == "profile":
        if len(rest) < 2:
            return usage()
        return cmd_profile(rest[1], fl)
    if cmd == "reactions":
        if len(rest) < 2:
            return usage()
        return cmd_reactions(rest[1], fl)
    if cmd == "media":
        if len(rest) < 2:
            return usage()
        return cmd_media(rest[1], fl)
    if cmd == "top-reacted":
        return cmd_top_reacted(fl)
    if cmd == "get":
        if len(rest) < 2:
            return usage()
        return cmd_get(rest[1], fl)
    if cmd == "cites":
        if len(rest) < 3:
            return usage()
        return cmd_cites(rest[1], rest[2], fl)
    return cmd_search(cmd, fl)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
