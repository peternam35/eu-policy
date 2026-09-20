#!/usr/bin/env python3
"""
Weekly EU-policy data check.

Reads the P / EN_P data blocks out of the web file, asks Claude (with web
search) to verify each bill's status against current sources, and — only
if something material actually changed — writes back updated blocks to
both the web and mobile files, plus a changelog file the PR description
is built from.

Exits 0 with no file changes if nothing needs updating (workflow then
skips the PR step). This script never pushes or opens the PR itself —
that's handled by the GitHub Action step after it runs.

--- Filenames ---
WEB_FILE / MOBILE_FILE default to the names actually served by GitHub
Pages (confirmed from the deployed QR codes: the web version is served
at the repo root as `index.html`, the mobile version at `/mobile.html`).
Earlier drafts of this script pointed at "eu-policy-unified.html" /
"eu-policy-mobile.html", which do not exist in the deployed repo and
would make `read(WEB_FILE)` raise FileNotFoundError immediately. Override
via env vars if your repo really does use different names.

--- Known limitation ---
This script re-verifies both the EU tracker (P / EN_P, 11 bills) and the
G20 tab's country data (`countries`, 21 members incl. EU). The EU entry
inside the G20 tab is derived live from P/EN_P at render time (via the
page's BI() helper) and is intentionally left untouched in the
`countries` block — do not add a `laws` array to it. Each of the other
20 countries carries its own `ck` (last-checked) date so freshness can be
tracked the same way individual bills are.

--- EU / G20 as-of dates are independent ---
The EU tracker and the G20 tab carry their OWN "as of" dates and only move
when their own block actually changes (see apply_all below) -- they used to
be bumped together any time anything changed, which made the G20 tab look
freshly verified even on weeks where only EU bills were checked.

--- Archive snapshots ---
Right before the EU tracker's P/EN_P block is overwritten, the PRE-change
state is preserved to archive/data/<old-as-of-date>.json (the same shape
the in-page "지난 기록 보기" viewer reads) and archive/manifest.json is
updated; snapshots older than ARCHIVE_RETENTION_DAYS are pruned so the
archive doesn't grow forever. This only triggers off EU tracker changes
(new_p), since that's the only data the viewer currently displays.
"""
import datetime
import json
import os
import re
import sys

import anthropic

WEB_FILE = os.environ.get("EU_POLICY_WEB_FILE", "index.html")
MOBILE_FILE = os.environ.get("EU_POLICY_MOBILE_FILE", "mobile.html")
CHANGELOG_FILE = "weekly_changelog.md"

ARCHIVE_DATA_DIR = "archive/data"
ARCHIVE_MANIFEST = "archive/manifest.json"
ARCHIVE_RETENTION_DAYS = 90

MODEL = "claude-sonnet-5"

P_START = "const P=["
P_END_MARK = "\n];"
ENP_START = "const EN_P={"
ENP_END_MARK = "\n};"
COUNTRIES_START = "const countries=["
COUNTRIES_END_MARK = "\n  ];"

# Every pattern below is (prefix, OLD_VALUE, suffix) so the same object
# serves both extraction (group 2) and in-place substitution (\g<1> + new
# value + \g<3>) without touching the surrounding markup.

# --- EU-scoped markers: only touched when the P/EN_P block changes ---
EU_ASOF_KO_PATTERN = re.compile(
    r'(<b>)(\d{4}년 \d{1,2}월 \d{1,2}일)(</b> 현재 기준 \(EU 트랙 · G20 탭은 )'
)
EU_ASOF_EN_PATTERN = re.compile(
    r'(<b>)([A-Za-z]+ \d{1,2}, \d{4})(</b> \(EU track · G20 tab uses a separate reference date, )'
)
HMETA_DATE_PATTERN = re.compile(
    r'(<span class="v mono" style="color:var\(--ink-strong\);font-weight:700">)(\d{4}-\d{2}-\d{2})(</span>)'
)
# Per-section "provenance" footers (org-chart / detail / review panels) that
# each carry their own hardcoded EU "as of" literal — same underlying date,
# repeated three times in the markup rather than computed once.
PROV_MONO_PATTERN = re.compile(
    r'(<span class="sep">·</span><span class="mono">)(\d{4}-\d{2}-\d{2})(</span>)'
)
EU_AS_OF_JS_PATTERN = re.compile(r"(const EU_AS_OF=')(\d{4}-\d{2}-\d{2})(';)")

# --- G20-scoped markers: only touched when the countries block changes ---
G20_MENTION_KO_PATTERN = re.compile(r'(G20 탭은 )(\d{4}-\d{2}-\d{2})( 별도 기준\)</span>)')
G20_MENTION_EN_PATTERN = re.compile(r'(separate reference date, )(\d{4}-\d{2}-\d{2})(\)</span>)')
G20_NOTE_KO_PATTERN = re.compile(r'(기준일 )(\d{4}-\d{2}-\d{2})( · )')
G20_NOTE_EN_PATTERN = re.compile(r'(As of )(\d{4}-\d{2}-\d{2})( · )')
G20_ASOF_JS_PATTERN = re.compile(r"(const AS_OF=')(\d{4}-\d{2}-\d{2})(';)")


def extract_block(text, start_marker, end_marker):
    i = text.index(start_marker)
    j = text.index(end_marker, i) + len(end_marker)
    return text[i:j], i, j


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, content):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0)


def current_eu_asof(text):
    """The EU tracker's current as-of date (ISO), read from the hmeta tag."""
    m = HMETA_DATE_PATTERN.search(text)
    return m.group(2) if m else None


def make_snapshot(pre_change_web_text, date_label):
    """Preserve the EU tracker's pre-change state under
    archive/data/<date_label>.json — same shape the in-page "지난 기록 보기"
    viewer reads (raw JS source text, evaluated client-side with
    new Function(...), since P/EN_P/countries are JS object literals, not
    strict JSON)."""
    p_block, _, _ = extract_block(pre_change_web_text, P_START, P_END_MARK)
    enp_block, _, _ = extract_block(pre_change_web_text, ENP_START, ENP_END_MARK)
    countries_block, _, _ = extract_block(pre_change_web_text, COUNTRIES_START, COUNTRIES_END_MARK)
    snapshot = {
        "date": date_label,
        "P_js": p_block,
        "EN_P_js": enp_block,
        "countries_js": countries_block,
    }
    write_json(os.path.join(ARCHIVE_DATA_DIR, f"{date_label}.json"), snapshot)


def update_archive_manifest(new_date, today):
    """Add new_date to the manifest and prune anything older than
    ARCHIVE_RETENTION_DAYS (both the manifest entry and its json file).
    Returns the list of pruned dates (for logging)."""
    manifest = read_json(ARCHIVE_MANIFEST, {"dates": []})
    dates = set(manifest.get("dates", []))
    dates.add(new_date)

    cutoff = today - datetime.timedelta(days=ARCHIVE_RETENTION_DAYS)
    kept, pruned = set(), set()
    for d in dates:
        try:
            d_date = datetime.date.fromisoformat(d)
        except ValueError:
            kept.add(d)  # unparseable — keep rather than silently discard
            continue
        (kept if d_date >= cutoff else pruned).add(d)

    for d in pruned:
        p = os.path.join(ARCHIVE_DATA_DIR, f"{d}.json")
        if os.path.exists(p):
            os.remove(p)

    manifest["dates"] = sorted(kept)
    write_json(ARCHIVE_MANIFEST, manifest)
    return sorted(pruned)


def check_eu_bills(client, today_str, p_block, enp_block):
    """Verify the 11 EU bills (P / EN_P). Returns (new_p, new_enp, changelog)
    or (None, None, None) if nothing needs to change."""
    system_prompt = f"""You maintain the data layer of an EU-policy tracking dashboard (11 bills,
Korean + English versions kept in sync). Today's date is {today_str}.

You will be given two JS data blocks: `P` (Korean bill records) and `EN_P` (their English mirror,
same array order, same ids). Each bill record has fields like pos, tension, sum, route[], refs[], dir, ck.

Your job: web-search for each bill's CURRENT real-world status (EUR-Lex, the Commission's press
corner, the European Parliament's legislative observatory, Council press releases). Compare against
what the blocks currently say.

Always explicitly check these three watch items every run, even if nothing else changed, since
they are known to be in flux (bill ids in parentheses):
- CBAM downstream scope-expansion trilogue (EU-TAXUD-0002) — has a start date been announced, or
  has a provisional/final agreement been reached?
- Whether stranded wire / wire products (e.g. CN heading 7312) are confirmed in or out of the final
  CBAM downstream product list (EU-TAXUD-0002) — only mark this settled once you find it in an
  adopted/agreed legal text, not a negotiating position.
- Whether the revised ESRS delegated acts, C(2026) 5010 and C(2026) 5011 (EU-FISMA-0007), have been
  published in the Official Journal yet, and if so, the entry-into-force date.

Rules:
- Only change a field if you have found a genuine, source-backed update since the `ck` date recorded
  for that bill. Do not paraphrase-for-the-sake-of-it — untouched bills must come back byte-identical.
- When you do update a bill, update its `ck` field to {today_str}, and if the direction classification
  (`dir`) genuinely changed character, explain why in the changelog rather than flipping it quietly.
- Keep exact JS object-literal syntax valid — this is parsed as JavaScript, not JSON. Preserve quoting
  style, trailing commas, and field order exactly as in the input for any bill you don't touch.
- Do not invent URLs. Only add a `u:` field to a refs[] entry if you can name the exact source you
  found it at.
- If, after checking, nothing needs to change, output exactly the single line: NO_CHANGES

If there ARE changes, output in exactly this format (no other commentary):

===P===
<full replacement for the P array, starting with "const P=[" and ending with "];">
===EN_P===
<full replacement for the EN_P object, starting with "const EN_P={{" and ending with "}};">
===CHANGELOG===
<a short bullet list, in Korean, of what changed and why — one bullet per bill touched, each bullet
citing the source you found>
"""

    user_prompt = f"""Here is the current P block:

{p_block}

Here is the current EN_P block:

{enp_block}

Check each bill against current sources and respond per the rules above."""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system_prompt,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user_prompt}],
    )
    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    full_text = "\n".join(text_parts).strip()

    if full_text == "NO_CHANGES" or "NO_CHANGES" in full_text[:40]:
        print("EU bills: no changes found this week.")
        return None, None, None

    try:
        new_p = full_text.split("===P===", 1)[1].split("===EN_P===", 1)[0].strip()
        new_enp = full_text.split("===EN_P===", 1)[1].split("===CHANGELOG===", 1)[0].strip()
        changelog = full_text.split("===CHANGELOG===", 1)[1].strip()
    except IndexError:
        print("EU bills: could not parse model response — skipping this section.", file=sys.stderr)
        print(full_text[:2000], file=sys.stderr)
        return None, None, None

    if not new_p.startswith("const P=[") or not new_enp.startswith("const EN_P={"):
        print("EU bills: model output failed a basic sanity check — skipping this section.", file=sys.stderr)
        return None, None, None

    return new_p, new_enp, changelog


def check_g20_countries(client, today_str, countries_block):
    """Verify the 20 non-EU G20 members' law lists (`countries`). Returns
    (new_countries, changelog) or (None, None) if nothing needs to change."""
    system_prompt = f"""You maintain the "G20법규" (G20 regulations) tab of an ESG-compliance
dashboard. Today's date is {today_str}.

You will be given the `countries` JS array (21 members: the 19 G20 countries, the EU, and the
African Union). Each entry has fields c, n/n_en (name), f (flag emoji), r (region), m (maturity
0-5), a/a_en (regulator), t/t_en (corporate action item), and — for every entry except the one
with `c:'EU'` — a `ck` (last-checked date) and a `laws` array of
{{n, n_en, s, s_en, note, note_en}} objects (law name / status badge / one-line note, Korean and
English).

The `c:'EU'` entry has `dynamic:'eu'` and no `laws`/`ck` fields on purpose — it is rendered live
from this dashboard's separate EU bill tracker elsewhere on the page. Leave that one object
completely untouched, byte-identical, including its position in the array.

Your job: for each of the other 20 entries, web-search their current real regulatory status
(official regulator sites, government gazettes, reputable law-firm client alerts) and compare
against what's recorded.

Rules:
- Only touch a country if you find a genuine, source-backed change since its `ck` date — a new law,
  a status change (e.g. voluntary → mandatory, a threshold change, a court ruling, a rescission), or
  a materially wrong/outdated note. Untouched countries must come back byte-identical, including the
  `c:'EU'` entry.
- When you touch a country, update its `ck` field to {today_str}. You may add, edit, or remove
  individual entries inside that country's `laws` array as needed — do not fabricate a law that
  doesn't exist, and do not invent specific dates or thresholds you can't source.
- If a country's overall regulatory maturity has clearly shifted (e.g. a voluntary regime became
  mandatory), you may update `m` (0-5: 0=확인 중/verifying, 1=초기 단계/early stage,
  2=정책·가이드라인/policy & guidelines, 3=부분 의무화/partial mandate, 4=주요 항목
  의무화/key items mandated, 5=통합 책임체계/integrated framework) — explain why in the changelog.
- Keep exact JS object-literal syntax valid — this is parsed as JavaScript, not JSON. Preserve
  quoting style, field order, and array order for any country you don't touch.
- If, after checking, nothing needs to change, output exactly the single line: NO_CHANGES

If there ARE changes, output in exactly this format (no other commentary):

===COUNTRIES===
<full replacement for the countries array, starting with "const countries=[" and ending with "];">
===CHANGELOG===
<a short bullet list, in Korean, of what changed and why — one bullet per country touched, each
bullet citing the source you found>
"""

    user_prompt = f"""Here is the current countries block:

{countries_block}

Check each non-EU country against current sources and respond per the rules above."""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system_prompt,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user_prompt}],
    )
    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    full_text = "\n".join(text_parts).strip()

    if full_text == "NO_CHANGES" or "NO_CHANGES" in full_text[:40]:
        print("G20 countries: no changes found this week.")
        return None, None

    try:
        new_countries = full_text.split("===COUNTRIES===", 1)[1].split("===CHANGELOG===", 1)[0].strip()
        changelog = full_text.split("===CHANGELOG===", 1)[1].strip()
    except IndexError:
        print("G20 countries: could not parse model response — skipping this section.", file=sys.stderr)
        print(full_text[:2000], file=sys.stderr)
        return None, None

    if not new_countries.startswith("const countries=["):
        print("G20 countries: model output failed a basic sanity check — skipping this section.", file=sys.stderr)
        return None, None
    if "dynamic:'eu'" not in new_countries:
        print("G20 countries: EU placeholder entry went missing from the model output — skipping this section.", file=sys.stderr)
        return None, None

    return new_countries, changelog


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    today = datetime.date.today()
    today_str = today.isoformat()
    today_ko = f"{today.year}년 {today.month}월 {today.day}일"
    today_en = today.strftime("%B %-d, %Y") if os.name != "nt" else today.strftime("%B %d, %Y")

    web = read(WEB_FILE)
    p_block, _, _ = extract_block(web, P_START, P_END_MARK)
    enp_block, _, _ = extract_block(web, ENP_START, ENP_END_MARK)
    countries_block, _, _ = extract_block(web, COUNTRIES_START, COUNTRIES_END_MARK)

    client = anthropic.Anthropic(api_key=api_key)

    new_p, new_enp, eu_changelog = check_eu_bills(client, today_str, p_block, enp_block)
    new_countries, g20_changelog = check_g20_countries(client, today_str, countries_block)

    if new_p is None and new_countries is None:
        print("No changes found this week.")
        sys.exit(0)

    # Preserve the EU tracker's pre-change state before it gets overwritten.
    # Only the EU tracker is snapshotted -- that's the only data the in-page
    # archive viewer reads.
    if new_p is not None:
        old_eu_asof = current_eu_asof(web)
        if old_eu_asof and old_eu_asof != today_str:
            make_snapshot(web, old_eu_asof)
            pruned = update_archive_manifest(old_eu_asof, today)
            print(f"Archive: snapshot saved for {old_eu_asof}.")
            if pruned:
                print(f"Archive: pruned snapshots older than {ARCHIVE_RETENTION_DAYS}d: {', '.join(pruned)}")
        elif old_eu_asof == today_str:
            print("Archive: EU tracker is already dated today — skipping snapshot.")

    def apply_all(text):
        # Re-locate every block fresh against THIS text on every step --
        # index.html and mobile.html are different lengths, so offsets
        # computed against one file must never be reused on the other.
        if new_p is not None:
            _, ti, tj = extract_block(text, P_START, P_END_MARK)
            text = text[:ti] + new_p + text[tj:]
            _, i2, j2 = extract_block(text, ENP_START, ENP_END_MARK)
            text = text[:i2] + new_enp + text[j2:]
        if new_countries is not None:
            _, i3, j3 = extract_block(text, COUNTRIES_START, COUNTRIES_END_MARK)
            text = text[:i3] + new_countries + text[j3:]

        # EU-scoped "as of" markers move only when the EU tracker itself
        # changed -- the G20 tab must not silently look freshly verified.
        if new_p is not None:
            text = EU_ASOF_KO_PATTERN.sub(rf"\g<1>{today_ko}\g<3>", text, count=1)
            text = EU_ASOF_EN_PATTERN.sub(rf"\g<1>{today_en}\g<3>", text, count=1)
            text = HMETA_DATE_PATTERN.sub(rf"\g<1>{today_str}\g<3>", text, count=1)
            text = PROV_MONO_PATTERN.sub(rf"\g<1>{today_str}\g<3>", text)
            text = EU_AS_OF_JS_PATTERN.sub(rf"\g<1>{today_str}\g<3>", text)

        # G20-scoped "as of" markers move only when the countries block
        # changed -- same reasoning, the other direction.
        if new_countries is not None:
            text = G20_MENTION_KO_PATTERN.sub(rf"\g<1>{today_str}\g<3>", text, count=1)
            text = G20_MENTION_EN_PATTERN.sub(rf"\g<1>{today_str}\g<3>", text, count=1)
            text = G20_NOTE_KO_PATTERN.sub(rf"\g<1>{today_str}\g<3>", text, count=1)
            text = G20_NOTE_EN_PATTERN.sub(rf"\g<1>{today_str}\g<3>", text, count=1)
            text = G20_ASOF_JS_PATTERN.sub(rf"\g<1>{today_str}\g<3>", text)
        return text

    new_web = apply_all(web)
    write(WEB_FILE, new_web)

    if os.path.exists(MOBILE_FILE):
        mobile = read(MOBILE_FILE)
        new_mobile = apply_all(mobile)
        write(MOBILE_FILE, new_mobile)

    sections = []
    if eu_changelog:
        sections.append("## EU 정책 추적 (기관 경로 탭)\n\n" + eu_changelog)
    if g20_changelog:
        sections.append("## G20 법규 탭\n\n" + g20_changelog)
    changelog = "\n\n".join(sections)

    with open(CHANGELOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"# 주간 업데이트 — {today_str}\n\n{changelog}\n")

    print("Changes written. Changelog:")
    print(changelog)


if __name__ == "__main__":
    main()
