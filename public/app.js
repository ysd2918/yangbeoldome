(() => {
  const $ = (id) => document.getElementById(id);
  const DOW = ["일", "월", "화", "수", "목", "금", "토"];
  const STATUS_TEXT = { open: "가능", empty: "미예약", booked: "예약", closed: "휴관", past: "지남", none: "–" };
  const MIN_OFFSET = -1, MAX_OFFSET = 2; // 이번 달 기준 볼 수 있는 범위

  // 한국 시간 기준 오늘
  const kstNow = () => new Date(Date.now() + (9 * 60 + new Date().getTimezoneOffset()) * 60000);
  const pad = (n) => String(n).padStart(2, "0");
  const ymdOf = (d) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`;
  const today = ymdOf(kstNow());
  const baseYm = today.slice(0, 6);

  const addMonths = (ym, n) => {
    const d = new Date(+ym.slice(0, 4), +ym.slice(4) - 1 + n, 1);
    return `${d.getFullYear()}${pad(d.getMonth() + 1)}`;
  };
  const monthDiff = (a, b) => (+a.slice(0, 4) - +b.slice(0, 4)) * 12 + (+a.slice(4) - +b.slice(4));

  const state = {
    ym: baseYm,
    showEmpty: localStorage.getItem("showEmpty2") === "1",
    selected: null,
    data: null,
    opening: null,
  };
  $("showEmpty").checked = state.showEmpty;

  async function fetchMonth(ym) {
    try {
      const r = await fetch(`api/month?ym=${ym}`, { cache: "no-store" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
      return j;
    } catch (e) {
      // 서버 없이 정적 호스팅(GitHub Pages)일 때: 미리 만들어둔 JSON 사용
      const r = await fetch(`data/${ym}.json`, { cache: "no-store" });
      if (!r.ok) throw e;
      return r.json();
    }
  }

  async function fetchJson(api, file) {
    try {
      const r = await fetch(api, { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      return await r.json();
    } catch (e) {
      const r = await fetch(file, { cache: "no-store" });
      if (!r.ok) throw e;
      return r.json();
    }
  }

  const BADGE = {
    "확정": "ok", "대기": "wait", "당일 대기": "wait", "지켜보기 전": "wait",
    "일치(범위 넓음)": "wide", "틀림": "bad", "검증불가": "", "추적 안 함": "",
  };
  const fmtDate = (iso) => {
    const d = new Date(iso.slice(0, 10) + "T00:00:00");
    return `${d.getMonth() + 1}월 ${d.getDate()}일(${DOW[d.getDay()]})`;
  };
  const fmtAt = (iso) => iso ? iso.slice(5, 16).replace("-", "/").replace("T", " ") : "-";

  function renderOpening() {
    const op = state.opening, box = $("opening");
    if (!op) { box.hidden = true; return; }
    // 보고 있는 달 → 없으면 다음 달
    let m = op.months.find((x) => x.ym === state.ym && x.status !== "추적 안 함");
    if (!m) m = op.months.find((x) => x.ym === addMonths(baseYm, 1));
    if (!m) { box.hidden = true; return; }
    box.hidden = false;
    const mon = +m.ym.slice(4);
    const opened = !!m.firstOpenAt;
    $("opTitle").textContent = opened
      ? `${mon}월 온라인 예약 오픈 확인: ${fmtAt(m.firstOpenAt)} 관측`
      : `${mon}월 온라인 예약 오픈 예상: ${fmtDate(m.predicted)}${op.rule.time ? " " + op.rule.time : ""}`;
    const b = $("opBadge");
    b.textContent = m.status === "대기" ? "예상 · 검증 대기" : m.status;
    b.className = "op-badge " + (BADGE[m.status] ?? "");
    $("opNote").textContent = m.note ||
      `규칙 "${op.rule.desc}" 로 계산. 예상일이 되면 실제로 열렸는지 자동 확인해서 맞으면 확정, 틀리면 새 규칙을 찾습니다.`;

    const li = [];
    li.push(`<li><b>현재 규칙:</b> ${op.rule.desc} <span class="op-badge ${op.rule.status === "확정" ? "ok" : "wait"}">${op.rule.status}</span>` +
      (op.rule.matches ? ` · 적중 ${op.rule.matches}회` : "") + `<br><span class="muted">${op.rule.source}</span></li>`);
    if (op.lead) {
      const L = op.lead;
      li.push(`<li><b>신청 마감:</b> 사용 ${L.days}일 전까지 — 관측 ${L.count}/${L.total}회 일치` +
        (L.count < L.total ? ` <span class="muted">(다른 값: ${JSON.stringify(L.all)})</span>` : "") + "</li>");
    }
    op.months.filter((x) => x.status !== "추적 안 함").forEach((x) => {
      li.push(`<li>${x.ym.slice(0, 4)}.${x.ym.slice(4)} — 예상 ${fmtDate(x.predicted)} · <b>${x.status}</b>` +
        (x.note ? ` <span class="muted">${x.note}</span>` : "") + "</li>");
    });
    op.history.forEach((h) => {
      li.push(`<li class="muted">폐기된 규칙: ${h.desc} (${h.retired_by} 관측과 불일치)</li>`);
    });
    li.push(`<li class="muted">관측 ${op.obsCount}회 · 마지막 확인 ${fmtAt(op.lastObs?.at)}` +
      (op.lastObs?.period ? ` · 사이트 이용가능일 ${op.lastObs.period}` : "") + "</li>");
    $("opRules").innerHTML = li.join("");
  }

  async function loadOpening() {
    try {
      state.opening = await fetchJson("api/opening", "data/opening.json");
    } catch (e) {
      state.opening = null;
    }
    renderOpening();
  }

  async function load(force) {
    loadOpening();
    const ym = state.ym;
    $("monthTitle").textContent = `${ym.slice(0, 4)}.${ym.slice(4)}`;
    $("prev").disabled = monthDiff(ym, baseYm) <= MIN_OFFSET;
    $("next").disabled = monthDiff(ym, baseYm) >= MAX_OFFSET;
    const st = $("status");
    st.className = "status";
    st.textContent = "12개 코트 현황 불러오는 중…";
    if (!force) { state.data = null; renderCalendar(); }
    try {
      const data = await fetchMonth(ym);
      if (ym !== state.ym) return; // 그사이 달을 바꿈
      state.data = data;
      if (data.bookingUrl) $("bookLink").href = data.bookingUrl;
      const open = data.days.reduce((s, d) => s + d.openCount, 0);
      const p = data.sitePeriod || {};
      st.textContent = `업데이트 ${data.updated} · 이 달 온라인 예약가능 ${open}타임` +
        (p.period ? ` · 사이트 설정 이용가능일 ${p.period}` : "");
      if (!state.selected || !state.selected.startsWith(ym)) {
        const first = data.days.find((d) => d.date >= today && (d.openCount || (state.showEmpty && d.emptyCount)));
        state.selected = first ? first.date : null;
      }
      renderCalendar();
      renderDetail();
    } catch (e) {
      st.className = "status err";
      st.textContent = `불러오기 실패: ${e.message || e}`;
    }
  }

  function dayKind(d) {
    if (!d) return "blank";
    if (d.closed) return "closed";
    if (d.date < today) return "past";
    if (d.openCount > 0) return "has-open";
    if (state.showEmpty && d.emptyCount > 0) return "has-empty";
    return "full";
  }

  function freeHours(d) {
    const want = d.openCount > 0 ? "open" : "empty";
    return d.slots
      .filter((s) => (want === "open" ? s.openCount : s.emptyCount) > 0)
      .map((s) => s.start.slice(0, 2).replace(/^0/, ""));
  }

  function renderCalendar() {
    const cal = $("calendar");
    cal.innerHTML = "";
    DOW.forEach((w, i) => {
      const el = document.createElement("div");
      el.className = "dow" + (i === 0 ? " sun" : i === 6 ? " sat" : "");
      el.textContent = w;
      cal.appendChild(el);
    });
    const ym = state.ym;
    const y = +ym.slice(0, 4), m = +ym.slice(4);
    const firstDow = new Date(y, m - 1, 1).getDay();
    const lastDay = new Date(y, m, 0).getDate();
    const byDate = {};
    (state.data?.days || []).forEach((d) => (byDate[d.date] = d));

    for (let i = 0; i < firstDow; i++) {
      const b = document.createElement("div");
      b.className = "day blank";
      cal.appendChild(b);
    }
    for (let day = 1; day <= lastDay; day++) {
      const ymd = `${ym}${pad(day)}`;
      const d = byDate[ymd];
      const dow = (firstDow + day - 1) % 7;
      const kind = state.data ? dayKind(d) : "loading";
      const title = d && d.reason && kind === "has-empty" ? d.reason : "";
      const btn = document.createElement("button");
      btn.className = `day ${kind}` + (dow === 0 ? " sun" : dow === 6 ? " sat" : "") +
        (ymd === today ? " today" : "") + (ymd === state.selected ? " selected" : "");
      let badge = "", hours = "";
      if (d && kind !== "blank") {
        if (kind === "closed") badge = "휴관";
        else if (kind === "past") badge = "지남";
        else if (kind === "has-open") badge = `<span class="lbl">가능 </span>${d.openCount}`;
        else if (kind === "has-empty") badge = `<span class="lbl">미예약 </span>${d.emptyCount}`;
        else badge = d.emptyCount ? "신청불가" : "만석";
        if (kind === "has-open" || kind === "has-empty") {
          const h = freeHours(d);
          hours = h.join("·") + "시";
        }
      }
      btn.innerHTML = `<span class="n">${day}</span>` +
        (badge ? `<span class="badge">${badge}</span>` : "") +
        (hours ? `<span class="hours">${hours}</span>` : "");
      if (title) btn.title = title;
      btn.setAttribute("aria-label", `${m}월 ${day}일 ${badge.replace(/<[^>]+>/g, "")}`);
      if (d) btn.addEventListener("click", () => {
        state.selected = ymd;
        renderCalendar();
        renderDetail();
        $("detail").scrollIntoView({ behavior: "smooth", block: "start" });
      });
      cal.appendChild(btn);
    }
  }

  function renderDetail() {
    const box = $("detail");
    const d = state.data?.days.find((x) => x.date === state.selected);
    if (!d) { box.hidden = true; return; }
    box.hidden = false;
    const courts = state.data.courts;
    const dt = new Date(+d.date.slice(0, 4), +d.date.slice(4, 6) - 1, +d.date.slice(6));
    $("detailTitle").textContent =
      `${dt.getMonth() + 1}월 ${dt.getDate()}일 (${DOW[dt.getDay()]})` +
      (d.stateNames.length ? ` · 사이트 상태: ${d.stateNames.join(", ")}` : "");

    const rs = $("reason");
    rs.hidden = !(d.reason && d.openCount === 0 && state.showEmpty);
    rs.textContent = d.reason ? `ⓘ 미예약 자리가 있지만 ${d.reason}` : "";

    // 시간대별 요약
    const sum = $("summary");
    sum.innerHTML = "";
    const lines = d.slots.filter((s) => s.openCount || (state.showEmpty && s.emptyCount));
    if (!lines.length) {
      sum.innerHTML = `<p class="none">${d.closed ? "휴관일입니다." : d.emptyCount ? `온라인으로 예약 가능한 코트가 없습니다. (미예약 ${d.emptyCount}타임 — 위 체크박스로 보기)` : "비어있는 코트가 없습니다."}</p>`;
    }
    lines.forEach((s) => {
      const div = document.createElement("div");
      div.className = "slotline";
      let html = `<span class="t">${s.start}~${s.end}</span>`;
      courts.forEach((c) => {
        const v = s.courts[c.cd];
        if (v === "open" || (state.showEmpty && v === "empty"))
          html += `<span class="chip ${v}">${c.name.replace(" 코트", "")}</span>`;
      });
      div.innerHTML = html;
      sum.appendChild(div);
    });

    // 코트 × 시간 표
    let html = "<thead><tr><th></th>" +
      d.slots.map((s) => `<th>${s.start}<br><small>${s.label}</small></th>`).join("") + "</tr></thead><tbody>";
    courts.forEach((c) => {
      html += `<tr><th class="row">${c.name}</th>`;
      d.slots.forEach((s) => {
        let v = s.courts[c.cd] || "none";
        const shown = v === "empty" && !state.showEmpty ? "closed" : v;
        html += `<td class="${shown}" title="${c.name} ${s.start}~${s.end}">${STATUS_TEXT[v]}</td>`;
      });
      html += "</tr>";
    });
    $("grid").innerHTML = html + "</tbody>";
  }

  $("prev").addEventListener("click", () => { state.ym = addMonths(state.ym, -1); load(); });
  $("next").addEventListener("click", () => { state.ym = addMonths(state.ym, 1); load(); });
  $("refresh").addEventListener("click", () => load(true));
  $("showEmpty").addEventListener("change", (e) => {
    state.showEmpty = e.target.checked;
    localStorage.setItem("showEmpty2", state.showEmpty ? "1" : "0");
    renderCalendar();
    renderDetail();
  });
  // 화면을 켜두면 3분마다 자동 갱신
  setInterval(() => { if (document.visibilityState === "visible") load(true); }, 180000);

  load();
})();
