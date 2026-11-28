"""온라인 수시대관 오픈 규칙 예측 · 검증 · 학습 (날짜별 모델)

관측 결과 (2026-11-28): 달 전체가 한꺼번에 열리는 게 아니라, 12/1 하루만 '예약가능'이 되었다.
그래서 규칙을 "사용일 기준 N일 전에 열린다"로 표현한다.

  규칙 = 오늘 기준 [lead_min, lead_max]일 뒤 날짜만 온라인 신청 가능
         (lead_min == lead_max == 3 이면 '사용일 3일 전 하루만 신청 가능')

동작
1. 관측할 때마다 1번 코트의 날짜 상태(이번 달·다음 달)를 받아 '열린 날짜' 목록을 만든다.
2. 규칙으로 계산한 '열려야 할 날짜'(휴관일 제외)와 비교한다.
   - 같으면 적중. 서로 다른 2일 이상, 3회 이상 적중하면 '확정'.
   - 다르면 같은 패턴이 2번 연속 나올 때 새 규칙으로 교체한다. (날짜가 바뀌는 순간에 잘못 관측하는 경우를 거르기 위해)
3. 각 날짜가 '닫힘 → 열림'으로 바뀐 시각 범위를 모아 오픈 시각(예: 00:00~09:10 사이)을 좁혀 나간다.
"""
import json
import os
import threading
from datetime import date, datetime, timedelta

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
OBS_FILE = os.path.join(STATE_DIR, "observations.jsonl")
STATE_FILE = os.path.join(STATE_DIR, "opening_state.json")
STATE_VERSION = 2
_lock = threading.Lock()

DEFAULT_RULE = {
    "lead_min": 3,
    "lead_max": 3,
    "status": "예상",
    "source": "11/28 관측: 12/1만 예약가능으로 바뀜, 사이트 이용가능일 시작 = 오늘+3일",
    "since": None,
    "checks": 0,
    "hits": 0,
    "hit_days": [],
}
CONFIRM_HITS = 3
CONFIRM_DAYS = 2
SWITCH_AFTER = 2  # 같은 불일치 패턴이 연속 몇 번이면 규칙 교체


# ---------- 유틸 ----------
def ymd(d):
    return d.strftime("%Y%m%d")


def from_ymd(s):
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def ym_of(d):
    return f"{d.year}{d.month:02d}"


def add_month(ym, n):
    y, m = int(ym[:4]), int(ym[4:]) - 1 + n
    return f"{y + m // 12}{m % 12 + 1:02d}"


def describe(rule):
    a, b = rule["lead_min"], rule["lead_max"]
    if a == b:
        return f"사용일 {a}일 전 하루만 온라인 신청 가능"
    return f"사용일 {b}일 전 ~ {a}일 전 온라인 신청 가능"


def expected_open(rule, today, months, closed_dates):
    """규칙상 지금 열려 있어야 할 날짜 (관측한 달 범위 안, 휴관일 제외)."""
    out = set()
    for k in range(rule["lead_min"], rule["lead_max"] + 1):
        d = ymd(today + timedelta(days=k))
        if d[:6] in months and d not in closed_dates:
            out.add(d)
    return out


# ---------- 상태 ----------
def new_state():
    return {"version": STATE_VERSION, "rule": dict(DEFAULT_RULE), "history": [], "pending": None,
            "dates": {}, "time_window": None, "lead_period": {}, "offsets": {},
            "last_obs": None, "obs_count": 0, "log": []}


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        if s.get("version") == STATE_VERSION:
            return s
    except FileNotFoundError:
        pass
    return rebuild_state()


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def rebuild_state():
    """관측 기록 파일을 처음부터 다시 적용해서 상태를 만든다."""
    state = new_state()
    try:
        with open(OBS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    obs = json.loads(line)
                    if "open_dates" in obs:
                        apply(state, obs)
    except FileNotFoundError:
        pass
    return state


# ---------- 관측 ----------
def collect(fetch_json, company, part, open_states, now):
    detail = fetch_json("rest/facilities/place_detail", company_code=company, part_code=part, place_code="25")
    cur = ym_of(now.date())
    months = [cur, add_month(cur, 1)]
    open_dates, closed_dates = [], []
    for ym in months:
        days = fetch_json("rest/facilities/place_month_state_list", company_code=company, part_code=part,
                          place_code="25", base_date=ym + "01", rent_type="1001", mem_no="")
        for d in days if isinstance(days, list) else []:
            key = d["date"].replace("-", "")
            if key[:6] != ym:
                continue
            if d.get("state_cd") in open_states:
                open_dates.append(key)
            elif d.get("state_cd") == "30":
                closed_dates.append(key)
    return {"at": now.isoformat(timespec="seconds"), "period": detail.get("period", ""),
            "receipt": detail.get("receipt_period", ""), "months": months,
            "open_dates": sorted(open_dates), "closed_dates": sorted(closed_dates)}


def _log(state, now, msg):
    state["log"].append(f"{now:%m/%d %H:%M} {msg}")
    state["log"] = state["log"][-30:]


def apply(state, obs):
    now = datetime.fromisoformat(obs["at"])
    today = now.date()
    state["obs_count"] += 1
    state["last_obs"] = obs
    months = set(obs.get("months") or [ym_of(today), add_month(ym_of(today), 1)])
    opened = set(obs["open_dates"])
    closed = set(obs.get("closed_dates", []))

    # 사이트 이용가능일 시작 = 오늘 + N  (참고 지표)
    try:
        start = date.fromisoformat(obs["period"].split("~")[0].strip())
        k = str((start - today).days)
        state["lead_period"][k] = state["lead_period"].get(k, 0) + 1
    except Exception:
        pass
    for d in opened:
        k = str((from_ymd(d) - today).days)
        state["offsets"][k] = state["offsets"].get(k, 0) + 1

    check_rule(state, now, today, months, opened, closed)
    track_dates(state, now, today, months, opened)


def check_rule(state, now, today, months, opened, closed):
    rule = state["rule"]
    expected = expected_open(rule, today, months, closed)
    if not expected and not opened:
        return  # 휴관일 등으로 비교할 게 없음
    if opened == expected:
        rule["checks"] += 1
        rule["hits"] += 1
        if today.isoformat() not in rule["hit_days"]:
            rule["hit_days"] = (rule["hit_days"] + [today.isoformat()])[-10:]
        state["pending"] = None
        if rule["status"] != "확정" and rule["hits"] >= CONFIRM_HITS and len(rule["hit_days"]) >= CONFIRM_DAYS:
            rule["status"] = "확정"
            _log(state, now, f"규칙 확정: {describe(rule)} (적중 {rule['hits']}회)")
        return

    if not opened:
        # 아직 오늘 오픈 시각 전이거나 점검/전부 마감 → 판정 보류 (적중/불일치에 넣지 않음)
        return
    rule["checks"] += 1
    offs = sorted((from_ymd(d) - today).days for d in opened)
    cand = {"lead_min": offs[0], "lead_max": offs[-1]}
    _log(state, now, f"불일치: 예상 {sorted(expected)} / 실제 {sorted(opened)} (오늘+{offs}일)")
    p = state["pending"]
    if p and p["lead_min"] == cand["lead_min"] and p["lead_max"] == cand["lead_max"]:
        p["count"] += 1
    else:
        p = state["pending"] = {**cand, "count": 1, "first_seen": now.isoformat(timespec="seconds")}
    if p["count"] >= SWITCH_AFTER:
        old = dict(rule)
        old["retired_at"] = now.isoformat(timespec="seconds")
        old["desc"] = describe(old)
        state["history"].append(old)
        state["rule"] = {**DEFAULT_RULE, **cand, "status": "학습됨(검증 대기)",
                         "source": f"{p['first_seen'][5:16].replace('T', ' ')}부터 {p['count']}회 연속 관측: 오늘+{offs}일 날짜가 열림",
                         "since": now.isoformat(timespec="seconds"), "checks": 0, "hits": 0, "hit_days": []}
        state["pending"] = None
        state["time_window"] = None
        _log(state, now, f"새 규칙 학습: {describe(state['rule'])}")


def track_dates(state, now, today, months, opened):
    """각 날짜가 닫힘→열림으로 바뀐 시각을 기록하고 오픈 시각 범위를 좁힌다."""
    rule = state["rule"]
    horizon = today + timedelta(days=rule["lead_max"] + 3)
    d = today
    while d <= horizon:
        key = ymd(d)
        if key[:6] in months:
            rec = state["dates"].get(key)
            if key in opened:
                if not rec:
                    state["dates"][key] = {"first_open_at": now.isoformat(timespec="seconds"),
                                           "status": "관측 전에 이미 열림"}
                elif rec.get("last_closed_at") and not rec.get("first_open_at"):
                    rec["first_open_at"] = now.isoformat(timespec="seconds")
                    resolve_date(state, key, rec)
            elif d >= today + timedelta(days=rule["lead_min"]):
                # 아직 열리기 전인 날짜 (이미 신청 기간이 지난 날짜는 제외)
                rec = state["dates"].setdefault(key, {})
                if not rec.get("first_open_at"):
                    rec["last_closed_at"] = now.isoformat(timespec="seconds")
        d += timedelta(days=1)
    # 오래된 기록 정리
    cutoff = ymd(today - timedelta(days=40))
    state["dates"] = {k: v for k, v in state["dates"].items() if k >= cutoff}


def resolve_date(state, key, rec):
    rule = state["rule"]
    lc = datetime.fromisoformat(rec["last_closed_at"])
    fo = datetime.fromisoformat(rec["first_open_at"])
    pred = from_ymd(key) - timedelta(days=rule["lead_max"])
    rec["predicted"] = pred.isoformat()
    rec["window"] = f"{lc:%m/%d %H:%M} ~ {fo:%m/%d %H:%M}"
    if not (lc.date() <= pred <= fo.date()):
        rec["status"] = "예상일과 다름"
        return
    if fo.date() != pred:
        rec["status"] = "일치(범위 넓음)"
        return
    rec["status"] = "예상일에 오픈"
    if lc.date() == pred:
        lo = lc.strftime("%H:%M")
    elif lc.date() == pred - timedelta(days=1):
        lo = "00:00"
    else:
        return  # 관측 공백이 길어 시각은 못 좁힘
    hi = fo.strftime("%H:%M")
    tw = state["time_window"]
    if tw:
        nlo, nhi = max(tw["lo"], lo), min(tw["hi"], hi)
        if nlo <= nhi:
            state["time_window"] = {"lo": nlo, "hi": nhi, "n": tw["n"] + 1}
        else:  # 서로 모순되면 최신 값으로 다시 시작
            state["time_window"] = {"lo": lo, "hi": hi, "n": 1}
    else:
        state["time_window"] = {"lo": lo, "hi": hi, "n": 1}


def observe(fetch_json, company, part, open_states, now):
    obs = collect(fetch_json, company, part, open_states, now)
    with _lock:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(OBS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(obs, ensure_ascii=False) + "\n")
        state = load_state()
        apply(state, obs)
        save_state(state)
    return state


def poll_interval(state, now):
    """오픈 시각을 좁히기 위해 30분 간격으로 관측."""
    return 1800


def summary(state, now):
    today = now.date()
    rule = state["rule"]
    tw = state["time_window"]
    upcoming = []
    for i in range(0, 4):
        day = today + timedelta(days=i)
        targets = [ymd(day + timedelta(days=k)) for k in range(rule["lead_min"], rule["lead_max"] + 1)]
        upcoming.append({"openDay": day.isoformat(), "targets": targets})
    recent = sorted(((k, v) for k, v in state["dates"].items() if v.get("first_open_at")), reverse=True)[:6]
    lp = state["lead_period"]
    return {
        "rule": {**rule, "desc": describe(rule)},
        "timeWindow": tw,
        "pending": state["pending"],
        "history": state["history"],
        "upcoming": upcoming,
        "recent": [{"date": k, **v} for k, v in recent],
        "offsets": state["offsets"],
        "leadPeriod": lp,
        "log": state["log"][-8:],
        "lastObs": state["last_obs"],
        "obsCount": state["obs_count"],
    }
