import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ====================== ΡΥΘΜΙΣΕΙΣ (μπορείς να τις αλλάξεις) ======================
MIN_SCORE = 2        # ελάχιστη βαθμολογία για να εμφανιστεί μια αγγελία (πιο χαμηλά = περισσότερες αγγελίες)
DISPLAY_DAYS = 30    # πόσες μέρες πίσω δείχνει η σελίδα

PART_TIME_WORDS = [
    "part-time", "part time", "parttime", "flexible hours", "flexible schedule",
    "hours per week", "hours a week", "hours/week", "few hours", "set your own hours",
    "flexible working", "work whenever",
]
ENTRY_WORDS = [
    "no experience", "entry level", "entry-level", "junior", "beginner", "trainee",
    "no prior experience", "no previous experience", "will train", "we train",
]
# Λέξεις που ΑΠΟΡΡΙΠΤΟΥΝ την αγγελία (ελέγχονται στον τίτλο και στην αρχή της περιγραφής)
BAD_WORDS = [
    "sales", "call center", "call centre", "cold call", "cold calling", "telemarketing",
    "telesales", "outbound", "freelance", "freelancer", "independent contractor", "1099",
    "commission", "business development", "account executive", "sdr", "bdr",
    "phone support", "inbound calls", "lead generation",
]
# Τίτλοι που δεν ταιριάζουν σε αρχάριο
BAD_TITLE_WORDS = [
    "senior", "sr", "lead", "principal", "staff", "head", "director", "manager", "architect",
    "vp", "chief", "expert", "engineer", "developer", "devops", "scientist", "programmer",
]
# Περιοχές που δέχονται αιτήσεις από Ελλάδα
OK_LOCATION = ["worldwide", "anywhere", "global", "europe", "emea", "eu", "greece", "international"]
# =================================================================================

BAD_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in BAD_WORDS) + r")\b", re.I)
BAD_TITLE_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in BAD_TITLE_WORDS) + r")\b", re.I)
EXPERIENCE_RE = re.compile(
    r"\b([2-9]|1\d)\s*\+?\s*(?:years|yrs)\b[^.]{0,40}experience|experience[^.]{0,30}\b([2-9]|1\d)\s*\+?\s*(?:years|yrs)\b",
    re.I,
)
DATA_FILE = "data/jobs.json"


def s(x):
    return "" if x is None else str(x)


def strip_html(t):
    t = re.sub(r"<[^>]+>", " ", s(t))
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "job-bot/1.0 (personal use)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def job(source, id_, title, company, url, location, text, job_type="", salary=""):
    return dict(source=source, id=f"{source.lower()}-{id_}", title=s(title), company=s(company),
                url=s(url), location=s(location), text=strip_html(text), job_type=s(job_type), salary=s(salary))


# ---------------------------- ΠΗΓΕΣ ----------------------------
def from_remotive():
    data = json.loads(get("https://remotive.com/api/remote-jobs?limit=200"))
    return [job("Remotive", j["id"], j.get("title"), j.get("company_name"), j.get("url"),
                j.get("candidate_required_location"), j.get("description"), j.get("job_type"), j.get("salary"))
            for j in data.get("jobs", [])]


def from_jobicy():
    data = json.loads(get("https://jobicy.com/api/v2/remote-jobs?count=100"))
    out = []
    for j in data.get("jobs", []):
        jt = j.get("jobType")
        jt = ", ".join(jt) if isinstance(jt, list) else s(jt)
        out.append(job("Jobicy", j["id"], j.get("jobTitle"), j.get("companyName"), j.get("url"),
                       j.get("jobGeo"), s(j.get("jobExcerpt")) + " " + s(j.get("jobDescription")), jt))
    return out


def from_arbeitnow():
    data = json.loads(get("https://www.arbeitnow.com/api/job-board-api"))
    out = []
    for j in data.get("data", []):
        if not j.get("remote"):
            continue
        jt = ", ".join(j.get("job_types") or [])
        out.append(job("Arbeitnow", j.get("slug"), j.get("title"), j.get("company_name"), j.get("url"),
                       "", j.get("description"), jt))
    return out


def from_wwr():
    root = ET.fromstring(get("https://weworkremotely.com/remote-jobs.rss"))
    out = []
    for it in root.iter("item"):
        link = s(it.findtext("link"))
        title = s(it.findtext("title"))
        company = ""
        if ": " in title:
            company, title = title.split(": ", 1)
        out.append(job("WWR", link, title, company, link, it.findtext("region"),
                       it.findtext("description"), it.findtext("type")))
    return out


SOURCES = [("Remotive", from_remotive), ("Jobicy", from_jobicy),
           ("Arbeitnow", from_arbeitnow), ("WeWorkRemotely", from_wwr)]


# ---------------------------- ΦΙΛΤΡΑ ----------------------------
def location_ok(loc):
    loc = loc.strip().lower()
    if not loc:
        return True
    return any(re.search(r"\b" + w + r"\b", loc) for w in OK_LOCATION)


def evaluate(j):
    """Επιστρέφει (βαθμολογία, ετικέτες) ή None αν η αγγελία απορρίπτεται."""
    jt = j["job_type"].lower().replace("_", " ").replace("-", " ")
    head = (j["title"] + " " + j["text"][:700])
    full = (j["title"] + " " + jt + " " + j["text"]).lower().replace("_", " ")
    if re.search(r"freelance|contract", jt):
        return None
    if BAD_TITLE_RE.search(j["title"]) or BAD_RE.search(head):
        return None
    if EXPERIENCE_RE.search(full):
        return None
    if not location_ok(j["location"]):
        return None
    score, tags = 0, []
    if any(w in full for w in PART_TIME_WORDS) or "part time" in jt:
        score += 2
        tags.append("part-time")
    if any(w in full for w in ENTRY_WORDS):
        score += 2
        tags.append("χωρίς εμπειρία")
    if "flexible" in full:
        score += 1
        tags.append("flexible")
    return (score, tags) if score >= MIN_SCORE else None


# ---------------------------- ΕΞΟΔΟΣ ----------------------------
def render_items(items):
    rows = []
    for r in items:
        tags = " ".join(f'<span class="tag">{html.escape(t)}</span>' for t in r["tags"])
        meta = " · ".join(html.escape(x) for x in [r["company"], r["location"] or "Remote", r["source"], r["salary"]] if x)
        rows.append(
            f'<div class="job"><a href="{html.escape(r["url"])}">{html.escape(r["title"])}</a>'
            f'<div class="meta">{meta}</div><div>{tags}</div></div>'
        )
    return "\n".join(rows) or "<p>Καμία αγγελία προς το παρόν.</p>"


CSS = """
body{font-family:Arial,sans-serif;max-width:760px;margin:0 auto;padding:16px;line-height:1.4;color:#222;background:#fff}
h1{font-size:22px}
.job{border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0}
.job a{font-weight:bold;font-size:16px;color:#1a56b0;text-decoration:none}
.meta{color:#666;font-size:13px;margin:4px 0}
.tag{background:#e8f0fe;color:#1a56b0;border-radius:10px;padding:2px 8px;font-size:12px;margin-right:4px}
.err{color:#a33;font-size:13px}
"""


def build_page(items, errors, updated):
    err = "".join(f"<p class='err'>Πρόβλημα με πηγή: {html.escape(e)}</p>" for e in errors)
    return (f"<!doctype html><html lang='el'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Αγγελίες εργασίας</title><style>{CSS}</style></head><body>"
            f"<h1>Αγγελίες εργασίας</h1><p class='meta'>Τελευταία ενημέρωση: {updated} (UTC) · "
            f"{len(items)} αγγελίες τις τελευταίες {DISPLAY_DAYS} μέρες</p>{err}{render_items(items)}</body></html>")


def send_email(new, page_url, errors):
    addr = os.environ.get("EMAIL_ADDRESS", "").strip()
    pw = os.environ.get("EMAIL_APP_PASSWORD", "").replace(" ", "")
    if not addr or not pw:
        print("Δεν βρέθηκαν στοιχεία email, παραλείπεται η αποστολή.")
        return
    err = "".join(f"<p class='err'>Πρόβλημα με πηγή: {html.escape(e)}</p>" for e in errors)
    body = (f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>"
            f"<h1>{len(new)} νέες αγγελίες</h1>{render_items(new)}{err}"
            f"<p><a href='{html.escape(page_url)}'>Όλες οι αγγελίες στη σελίδα</a></p></body></html>")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(f"Job bot: {len(new)} νέες αγγελίες", "utf-8")
    msg["From"] = addr
    msg["To"] = addr
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as srv:
        srv.login(addr, pw)
        srv.sendmail(addr, [addr], msg.as_string())
    print("Στάλθηκε email.")


def main():
    os.makedirs("data", exist_ok=True)
    os.makedirs("docs", exist_ok=True)
    store = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            store = json.load(f)

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    errors, new = [], []
    for name, fn in SOURCES:
        try:
            jobs = fn()
        except Exception as e:
            errors.append(f"{name}: {e}")
            print("ΣΦΑΛΜΑ", name, e)
            continue
        print(name, "->", len(jobs), "αγγελίες")
        for j in jobs:
            if j["id"] in store or not j["url"]:
                continue
            res = evaluate(j)
            if res is None:
                continue
            rec = {k: j[k] for k in ("source", "title", "company", "url", "location", "salary")}
            rec.update(score=res[0], tags=res[1], first_seen=today)
            store[j["id"]] = rec
            new.append(rec)

    keep_after = (now - timedelta(days=120)).strftime("%Y-%m-%d")
    store = {k: v for k, v in store.items() if v["first_seen"] >= keep_after}
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)

    show_after = (now - timedelta(days=DISPLAY_DAYS)).strftime("%Y-%m-%d")
    items = sorted((v for v in store.values() if v["first_seen"] >= show_after),
                   key=lambda v: (v["first_seen"], v["score"]), reverse=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(build_page(items, errors, now.strftime("%Y-%m-%d %H:%M")))

    repo = os.environ.get("GITHUB_REPOSITORY", "user/job-bot")
    owner, name = repo.split("/", 1)
    page_url = f"https://{owner}.github.io/{name}/"
    print(f"Νέες αγγελίες: {len(new)}")
    if new:
        try:
            send_email(new, page_url, errors)
        except Exception as e:
            print("ΣΦΑΛΜΑ email:", e)
            sys.exit(1)


if __name__ == "__main__":
    main()
