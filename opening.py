"""온라인 수시대관 오픈일 예측 · 검증 · 규칙 학습

동작 방식
1. 가설 규칙으로 다음 달의 '예상 오픈일'을 계산한다. (기본: 해당 월 1일)
2. 주기적으로 사이트를 관측한다. (place_detail 의 이용가능일 + 1번 코트 날짜 상태)
   - 아직 안 열렸으면 '마지막으로 닫혀 있던 시각'을 기록
   - 처음 열린 걸 보면 '처음 열린 시각'을 기록
3. 실제 오픈 시점 범위 [마지막 닫힘, 처음 열림] 과 예상일을 비교
   - 예상일과 맞고 범위가 하루 이내 → 규칙 '확정'
   - 예상일이 범위 안이지만 범위가 넓음 → '일치(범위 넓음)' (모순은 없지만 확정은 보류)
   - 예상일이 범위 밖 → '틀림' → 관측된 실제 오픈일로 새 규칙을 만들고 다음 달에 다시 검증
4. 이용가능일(period) 시작일 - 오늘 = '사용 N일 전까지' 규칙도 매번 집계한다.
"""
import calendar
import json
import os
import threading
from datetime import date, datetime, timedelta

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
OBS_FILE = os.path.join(STATE_DIR, "observations.jsonl")
STATE_FILE = os.path.join(STATE_DIR, "opening_state.json")
_lock = threading.Lock()

DEFAULT_RULE = {
    "id": "default-1st",
    "month_delta": 0,  # 0 = 해당 월, -1 = 전월
    "day": 1,
    "status": "예상",
    "source": "가설: 현장 신청 '당월 상시' 안내 + 사이트 이용가능일 설정으로 추정",
    "time": None,
    "matches": 0,
}


# ---------- 날짜 유틸 ----------
def ym_of(d):
    return f"{d.year}{d.month:02d}"


def add_month(ym, n):
    y, m = int(ym[:4]), int(ym[4:]) - 1 + n
    return f"{y + m // 12}{m % 12 + 1:02d}"


def month_first(ym):
    return date(int(ym[:4]), int(ym[4:]), 1)


def month_last(ym):
    y, m = int(ym[:4]), int(ym[4:])
    return date(y, m, calendar.monthrange(y, m)[1])


def parse_period(p):
    try:
        a, b = p.split("~")
        return date.fromisoformat(a.strip()), date.fromisoformat(b.strip())
    except Exception:
        return None


def describe_rule(rule):
    where = "해당 월" if rule["month_delta"] == 0 else "전월" if rule["month_delta"] == -1 else f"{rule['month_delta']}개월"
    s = f"{where} {rule['day']}일 오픈"
    if rule.get("time"):
        s += f" ({rule['time']})"
    return s


def predicted_open(rule, ym):
    base = add_month(ym, rule["month_delta"])
    last = month_last(base).day
    return month_first(base).replace(day=min(rule["day"], last))


# ---------- 상태 저장 ----------
def new_state():
    return {"rule": dict(DEFAULT_RULE), "history": [], "months": {}, "lead": {}, "end": {},
            "last_obs": None, "obs_count": 0}


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return new_state()


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


# ---------- 관측 ----------
def collect(fetch_json, company, part, open_states, now):
    """사이트에서 관측값 하나를 만든다 (요청 3번)."""
    detail = fetch_json("rest/facilities/place_detail", company_code=company, part_code=part, place_code="25")
    cur = ym_of(now.date())
    obs = {"at": now.isoformat(timespec="seconds"), "period": detail.get("period", ""),
           "receipt": detail.get("receipt_period", ""), "open_days": {}}
    for ym in (cur, add_month(cur, 1)):
        days = fetch_json("rest/facilities/place_month_state_list", company_code=company, part_code=part,
                          place_code="25", base_date=ym + "01", rent_type="1001", mem_no="")
        if not isinstance(days, list):
            days = []
        obs["open_days"][ym] = sum(1 for d in days
                                   if d["date"].replace("-", "").startswith(ym) and d.get("state_cd") in open_states)
    return obs


def month_is_open(obs, ym):
    if obs["open_days"].get(ym, 0) > 0:
        return True
    pr = parse_period(obs.get("period", ""))
    return bool(pr and pr[0] <= pr[1] and pr[1] >= month_first(ym) and pr[0] <= month_last(ym))


def apply(state, obs):
    """관측값 하나를 상태에 반영 (예측 검증 / 규칙 학습)."""
    now = datetime.fromisoformat(obs["at"])
    today = now.date()
    cur = ym_of(today)
    nxt = add_month(cur, 1)
    state["obs_count"] += 1
    state["last_obs"] = obs

    # '사용 N일 전까지' / 신청 가능 마지막 날 규칙 집계
    pr = parse_period(obs.get("period", ""))
    if pr:
        lead = str((pr[0] - today).days)
        state["lead"][lead] = state["lead"].get(lead, 0) + 1
        if pr[0] <= pr[1]:
            key = "말일" if pr[1] == month_last(ym_of(pr[1])) else f"오늘+{(pr[1] - today).days}일"
            state["end"][key] = state["end"].get(key, 0) + 1

    for ym in (cur, nxt):
        m = state["months"].get(ym)
        if m is None:
            if ym != nxt:
                continue  # 미리 지켜보기 시작한 달만 검증 대상
            P = predicted_open(state["rule"], ym)
            m = state["months"][ym] = {
                "ym": ym, "predicted": P.isoformat(), "rule_id": state["rule"]["id"],
                "status": "대기", "last_closed_at": None, "first_open_at": None, "note": "",
            }
        if m["first_open_at"]:
            continue
        P = date.fromisoformat(m["predicted"])
        if not month_is_open(obs, ym):
            m["last_closed_at"] = obs["at"]
            if today > P:
                m["status"] = "틀림"
                m["note"] = f"예상일({P:%m/%d})이 지났는데 아직 안 열림 — 열리는 시점을 계속 관측 중"
            elif today == P:
                m["status"] = "당일 대기"
                m["note"] = "예상일 당일, 아직 안 열림 (오픈 시각 미정)"
            else:
                m["status"] = "대기"
                m["note"] = ""
        else:
            m["first_open_at"] = obs["at"]
            evaluate(state, m)


def evaluate(state, m):
    P = date.fromisoformat(m["predicted"])
    fo = datetime.fromisoformat(m["first_open_at"])
    if not m["last_closed_at"]:
        m["status"] = "검증불가"
        m["note"] = "관측을 시작했을 때 이미 열려 있었음"
        return
    lc = datetime.fromisoformat(m["last_closed_at"])
    lo, hi = lc.date(), fo.date()
    window = f"{lc:%m/%d %H:%M} ~ {fo:%m/%d %H:%M} 사이 오픈"
    rule = state["rule"]

    if lo <= P <= hi:
        # 예상일 당일에 열린 걸 봤고, 마지막 닫힘이 당일 또는 전날 18시 이후면 하루 단위로 확정
        if hi == P and (lo == P or (lo == P - timedelta(days=1) and lc.hour >= 18)):
            m["status"] = "확정"
            m["note"] = window + " — 예상과 일치"
            if rule["id"] == m["rule_id"]:
                rule["matches"] += 1
                rule["status"] = "확정"
                if lo == hi:
                    rule["time"] = f"{lc:%H:%M}~{fo:%H:%M} 사이"
        else:
            m["status"] = "일치(범위 넓음)"
            m["note"] = window + " — 예상과 모순은 없지만 하루 단위로 확인 못 함"
        return

    # 틀림 → 새 규칙 학습
    m["status"] = "틀림"
    m["note"] = window + f" — 예상일({P:%m/%d})과 다름, 새 규칙 학습"
    D = hi
    approx = lo != hi and not (hi - lo).days <= 1
    delta = (D.year - int(m["ym"][:4])) * 12 + D.month - int(m["ym"][4:])
    old = dict(rule)
    old["status"] = "틀림"
    old["retired_by"] = m["ym"]
    state["history"].append(old)
    new = {
        "id": f"learned-{m['ym']}",
        "month_delta": delta,
        "day": D.day,
        "status": "학습됨(다음 달 검증 대기)" + (" · 날짜 추정" if approx else ""),
        "source": f"{m['ym'][:4]}.{m['ym'][4:]} 실제 오픈 관측: {window}",
        "time": f"{lc:%H:%M}~{fo:%H:%M} 사이" if lo == hi else None,
        "matches": 0,
    }
    state["rule"] = new
    for other in state["months"].values():  # 아직 안 열린 달 다시 예측
        if not other["first_open_at"] and other["ym"] != m["ym"]:
            other["predicted"] = predicted_open(new, other["ym"]).isoformat()
            other["rule_id"] = new["id"]
            other["status"] = "대기"
            other["note"] = ""


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
    """예상일 전후에는 10분, 평소에는 1시간 간격."""
    today = now.date()
    for m in state["months"].values():
        if m["first_open_at"]:
            continue
        P = date.fromisoformat(m["predicted"])
        if P - timedelta(days=1) <= today or m["status"] == "틀림":
            return 600
    return 3600


def summary(state, now):
    """화면용 요약."""
    today = now.date()
    rule = state["rule"]
    cur = ym_of(today)
    upcoming = []
    for i in range(0, 3):
        ym = add_month(cur, i)
        m = state["months"].get(ym)
        upcoming.append({
            "ym": ym,
            "predicted": m["predicted"] if m else predicted_open(rule, ym).isoformat(),
            "status": m["status"] if m else ("지켜보기 전" if i else "추적 안 함"),
            "note": m["note"] if m else "",
            "firstOpenAt": m["first_open_at"] if m else None,
            "lastClosedAt": m["last_closed_at"] if m else None,
        })
    total = sum(state["lead"].values())
    lead = None
    if total:
        top, cnt = max(state["lead"].items(), key=lambda kv: kv[1])
        lead = {"days": int(top), "count": cnt, "total": total, "all": state["lead"]}
    return {
        "rule": {**rule, "desc": describe_rule(rule)},
        "history": [{**h, "desc": describe_rule(h)} for h in state["history"]],
        "months": upcoming,
        "lead": lead,
        "end": state["end"],
        "lastObs": state["last_obs"],
        "obsCount": state["obs_count"],
    }
