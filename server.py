"""양벌 테니스 돔 빈 코트 찾기 - 서버

외부 패키지 없이 Python 표준 라이브러리만 사용한다.

  python server.py [--open]     # http://localhost:8765 에서 웹앱 실행 (--open: 브라우저 자동 열기)
  python server.py --dump       # public/data/YYYYMM.json 생성 (정적 호스팅용, 이번 달+다음 달)
  python server.py --observe    # 예약 오픈 여부를 한 번 관측하고 예측/검증 결과 출력 (작업 스케줄러용)
"""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import opening

BASE = "https://gjcenter.gjcs.or.kr/"
COMPANY = "GJCS03"  # 시민체육관&양벌 테니스 돔
PART = "10"  # 양벌 테니스 돔 대관
RENT_TYPE = "1001"  # 체육경기
BOOKING_URL = BASE + "fmcs/3"
CACHE_TTL = 180  # 초
KST = timezone(timedelta(hours=9))
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

OPEN_DAY_STATES = {"10", "11", "15"}  # 예약가능 / (기타 가능) / 추첨접수
CLOSED_DAY_STATE = "30"  # 휴관일

_cache = {}
_cache_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4)


UPSTREAM_TIMEOUT = 10  # 초. 예약 사이트가 응답하지 않을 때 오래 붙잡혀 있지 않도록
UPSTREAM_TRIES = 2


def fetch_json(path, timeout=UPSTREAM_TIMEOUT, tries=UPSTREAM_TRIES, **params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (yangbeol-court-viewer)"})
    last_err = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # 네트워크 일시 오류 재시도
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(f"{path} 요청 실패: {type(last_err).__name__}: {last_err}")


def diagnose():
    """서버에서 예약 사이트에 접속되는지 확인 (배포 환경 점검용)."""
    out = {"target": BASE, "checkedAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")}
    t = time.time()
    try:
        data = fetch_json("rest/common/company", timeout=15, tries=1, type="F")
        out.update(ok=True, seconds=round(time.time() - t, 2), sample=len(data) if isinstance(data, list) else data)
    except Exception as e:
        out.update(ok=False, seconds=round(time.time() - t, 2), error=str(e),
                   hint="이 서버에서 예약 사이트에 접속하지 못합니다. 해외 접속 차단일 가능성이 큽니다.")
    return out


def get_courts():
    data = fetch_json("rest/common/place", company_code=COMPANY, group_cd="", part_code=PART)
    return [{"cd": c["place_cd"], "name": c["place_nm"]} for c in data]


def fetch_court_month(court_cd, ym):
    base_date = ym + "01"
    common = dict(company_code=COMPANY, part_code=PART, place_code=court_cd,
                  base_date=base_date, rent_type=RENT_TYPE, mem_no="")
    day_states = fetch_json("rest/facilities/place_month_state_list", **common)
    slots = fetch_json("rest/facilities/place_month_time_state_list", **common)
    if isinstance(day_states, dict) or isinstance(slots, dict):  # {"error": true, ...}
        raise RuntimeError(f"코트 {court_cd} 데이터 오류")
    states = {d["date"].replace("-", ""): (d.get("state_cd", ""), d.get("state_nm", "")) for d in day_states}
    return states, slots


def slot_status(day_state, slot):
    if day_state == CLOSED_DAY_STATE:
        return "closed"
    if slot.get("rent_no"):
        return "booked"
    if slot.get("use_yn") == "N" and day_state in OPEN_DAY_STATES:
        return "open"
    return "empty"  # 예약자 없음, 하지만 지금은 신청 불가(오픈 전/마감/지난 시간)


def is_past(ymd, start_time, now):
    today = now.strftime("%Y%m%d")
    return ymd < today or (ymd == today and start_time <= now.strftime("%H:%M"))


ONLINE_LEAD_DAYS = 3  # 수시대관 온라인: 사용 3일 전까지 신청


def get_site_period():
    """사이트에 설정된 온라인 신청 이용기간/접수기간 (참고용)."""
    try:
        d = fetch_json("rest/facilities/place_detail", company_code=COMPANY, part_code=PART, place_code="25")
        return {"period": d.get("period", ""), "receipt": d.get("receipt_period", "")}
    except Exception:
        return {"period": "", "receipt": ""}


def unbookable_reason(ymd, day_open, now):
    """예약자는 없는데 온라인 신청이 안 되는 이유."""
    lead_limit = (now + timedelta(days=ONLINE_LEAD_DAYS)).strftime("%Y%m%d")
    if ymd < lead_limit:
        return "3일 이내라 온라인 신청 불가 — 현장(시민체육관 안내데스크)·전화 문의"
    if not day_open:
        return "온라인 수시대관 접수 전 (사이트에서 아직 예약불가 상태)"
    return ""


def build_month(ym):
    now = datetime.now(KST)
    courts = get_courts()
    period_future = _pool.submit(get_site_period)
    results = list(_pool.map(lambda c: fetch_court_month(c["cd"], ym), courts))

    days = {}
    for court, (states, slots) in zip(courts, results):
        cd = court["cd"]
        for ymd, (st_cd, st_nm) in states.items():
            if not ymd.startswith(ym):
                continue
            day = days.setdefault(ymd, {"date": ymd, "states": {}, "slots": {}})
            day["states"][cd] = {"cd": st_cd, "nm": st_nm}
        for s in slots:
            ymd = s["date"]
            if not ymd.startswith(ym):
                continue
            day = days.setdefault(ymd, {"date": ymd, "states": {}, "slots": {}})
            st_cd = day["states"].get(cd, {}).get("cd", "")
            key = s["start_time"] + "-" + s["end_time"]
            slot = day["slots"].setdefault(key, {
                "start": s["start_time"], "end": s["end_time"],
                "label": s.get("time_nm", ""), "courts": {},
            })
            status = slot_status(st_cd, s)
            if status in ("open", "empty") and is_past(ymd, s["start_time"], now):
                status = "past"
            slot["courts"][cd] = status

    out_days = []
    for ymd in sorted(days):
        d = days[ymd]
        slots = sorted(d["slots"].values(), key=lambda x: x["start"])
        for sl in slots:
            sl["openCount"] = sum(1 for v in sl["courts"].values() if v == "open")
            sl["emptyCount"] = sum(1 for v in sl["courts"].values() if v == "empty")
        state_codes = {v["cd"] for v in d["states"].values()}
        state_names = sorted({v["nm"] for v in d["states"].values() if v["nm"]})
        day_open = bool(state_codes & OPEN_DAY_STATES)
        empty_total = sum(s["emptyCount"] for s in slots)
        out_days.append({
            "date": ymd,
            "closed": bool(state_codes) and state_codes == {CLOSED_DAY_STATE},
            "stateNames": state_names,
            "reason": unbookable_reason(ymd, day_open, now) if empty_total else "",
            "openCount": sum(s["openCount"] for s in slots),
            "emptyCount": sum(s["emptyCount"] for s in slots),
            "slots": slots,
        })

    return {
        "ym": ym,
        "updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "bookingUrl": BOOKING_URL,
        "sitePeriod": period_future.result(),
        "courts": courts,
        "days": out_days,
    }


def get_month_cached(ym):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(ym)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
        lock = _cache.setdefault("lock:" + ym, threading.Lock())
    with lock:  # 같은 달을 동시에 여러 번 긁지 않도록
        with _cache_lock:
            hit = _cache.get(ym)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
        data = build_month(ym)
        with _cache_lock:
            _cache[ym] = (time.time(), data)
        return data


def valid_ym(ym):
    if not (isinstance(ym, str) and len(ym) == 6 and ym.isdigit()):
        return False
    y, m = int(ym[:4]), int(ym[4:])
    today = datetime.now(KST).date()
    first = date(today.year, today.month, 1)
    target = date(y, m, 1) if 1 <= m <= 12 else None
    if not target:
        return False
    diff = (target.year - first.year) * 12 + target.month - first.month
    return -3 <= diff <= 3  # 앞뒤 3개월만 허용


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=PUBLIC_DIR, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now(KST).strftime("%H:%M:%S"), fmt % args))

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/month":
            ym = urllib.parse.parse_qs(u.query).get("ym", [""])[0]
            if not valid_ym(ym):
                return self.send_json(400, {"error": "ym 형식은 YYYYMM, 앞뒤 3개월까지만 가능"})
            try:
                return self.send_json(200, get_month_cached(ym))
            except Exception as e:
                return self.send_json(502, {"error": f"예약 사이트 조회 실패: {e}"})
        if u.path == "/api/opening":
            with opening._lock:
                state = opening.load_state()
            sm = opening.summary(state, datetime.now(KST).replace(tzinfo=None))
            sm["lastError"] = LAST_OBS_ERROR if LAST_OBS_ERROR["error"] else None
            return self.send_json(200, sm)
        if u.path == "/api/diag":
            return self.send_json(200, diagnose())
        if u.path == "/healthz":
            return self.send_json(200, {"ok": True})
        return super().do_GET()


def month_list(n=2):
    t = datetime.now(KST).date().replace(day=1)
    out = []
    for i in range(n):
        y, m = t.year + (t.month - 1 + i) // 12, (t.month - 1 + i) % 12 + 1
        out.append(f"{y}{m:02d}")
    return out


def observe_once():
    now = datetime.now(KST).replace(tzinfo=None)
    return opening.observe(fetch_json, COMPANY, PART, OPEN_DAY_STATES, now)


LAST_OBS_ERROR = {"error": None, "at": None}


def observer_loop():
    """서버가 켜져 있는 동안 주기적으로 오픈 여부를 관측."""
    while True:
        wait = 3600
        try:
            state = observe_once()
            LAST_OBS_ERROR.update(error=None, at=None)
            wait = opening.poll_interval(state, datetime.now(KST).replace(tzinfo=None))
        except Exception as e:
            sys.stderr.write(f"[관측 실패] {e}\n")
            LAST_OBS_ERROR.update(error=str(e), at=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"))
            wait = 600
        time.sleep(wait)


def print_opening(state):
    sm = opening.summary(state, datetime.now(KST).replace(tzinfo=None))
    r = sm["rule"]
    print(f"현재 규칙: {r['desc']} [{r['status']}] — {r['source']}")
    for m in sm["months"]:
        print(f"  {m['ym'][:4]}.{m['ym'][4:]}  예상 오픈 {m['predicted']}  [{m['status']}] {m['note']}")
    if sm["lead"]:
        L = sm["lead"]
        print(f"사용 {L['days']}일 전까지: 관측 {L['count']}/{L['total']}회 일치 {L['all']}")
    print(f"관측 {sm['obsCount']}회, 마지막: {sm['lastObs']['at'] if sm['lastObs'] else '-'}")


def dump():
    out_dir = os.path.join(PUBLIC_DIR, "data")
    os.makedirs(out_dir, exist_ok=True)
    for ym in month_list(2):
        data = build_month(ym)
        with open(os.path.join(out_dir, ym + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print(ym, "저장 완료:", sum(d["openCount"] for d in data["days"]), "개 예약가능 슬롯")
    state = observe_once()
    with open(os.path.join(out_dir, "opening.json"), "w", encoding="utf-8") as f:
        json.dump(opening.summary(state, datetime.now(KST).replace(tzinfo=None)), f, ensure_ascii=False)


if __name__ == "__main__":
    if "--dump" in sys.argv:
        dump()
    elif "--observe" in sys.argv:
        print_opening(observe_once())
    else:
        threading.Thread(target=observer_loop, daemon=True).start()
        port = int(os.environ.get("PORT", "8765"))
        host = os.environ.get("HOST", "127.0.0.1")  # 이 PC에서만 접속
        srv = ThreadingHTTPServer((host, port), Handler)
        print(f"양벌 테니스 돔 빈 코트 찾기: http://localhost:{port}  (창을 닫으면 종료)")
        if "--open" in sys.argv:
            import webbrowser
            threading.Timer(1.0, webbrowser.open, args=(f"http://localhost:{port}",)).start()
        srv.serve_forever()
