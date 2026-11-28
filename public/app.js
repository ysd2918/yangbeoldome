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
      const r = await fetch(`api/month?ym=${ym}`, { cache: "no-store", signal: AbortSignal.timeout(90000) });
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

  const fmtDate = (iso) => {
    const d = new Date(iso.slice(0, 10) + "T00:00:00");
    return `${d.getMonth() + 1}/${d.getDate()}(${DOW[d.getDay()]})`;
  };
  const fmtYmd = (k) => fmtDate(`${k.slice(0, 4)}-${k.slice(4, 6)}-${k.slice(6)}`);
  const fmtAt = (iso) => iso ? iso.slice(5, 16).replace("-", "/").replace("T", " ") : "-";
  const isoToday = `${today.slice(0, 4)}-${today.slice(4, 6)}-${today.slice(6)}`;

  // 사용일(YYYYMMDD) → 규칙상 온라인 오픈일(YYYYMMDD)
  function openDayOf(ymd) {
    const r = state.opening?.rule;
    if (!r) return null;
    const d = new Date(+ymd.slice(0, 4), +ymd.slice(4, 6) - 1, +ymd.slice(6) - r.lead_max);
    return ymdOf(d);
  }

  function renderOpening() {
    const op = state.opening, box = $("opening");
    if (!op) { box.hidden = true; return; }
    box.hidden = false;
    const r = op.rule, tw = op.timeWindow;
    $("opTitle").textContent = `온라인 예약 규칙: ${r.desc}`;
    const b = $("opBadge");
    b.textContent = r.status === "확정" ? `확정 · 적중 ${r.hits}회` : `${r.status} · 적중 ${r.hits}/${r.checks}`;
    b.className = "op-badge " + (r.status === "확정" ? "ok" : r.status.startsWith("학습") ? "wide" : "wait");

    const parts = [];
    if (op.lastError) parts.push(`⚠ 관측 실패 (${op.lastError.at}): ${op.lastError.error}`);
    const lo = op.lastObs;
    if (lo && lo.at.slice(0, 10) === isoToday) {
      parts.push(`지금 열린 날: ${lo.open_dates.length ? lo.open_dates.map(fmtYmd).join(", ") : "없음"}`);
    }
    const tm = op.upcoming.find((u) => u.openDay > isoToday);
    if (tm) parts.push(`${fmtDate(tm.openDay)} 오픈 예정: ${tm.targets.map(fmtYmd).join(", ")} 사용분`);
    parts.push(tw ? `오픈 시각 ${tw.lo}~${tw.hi} 사이 (${tw.n}건으로 좁힘)` : "오픈 시각 관측 중");
    $("opNote").textContent = parts.join(" · ");

    const li = [];
    li.push(`<li><b>근거:</b> ${r.source}<br><span class="muted">관측할 때마다 실제 열린 날짜와 비교해서 서로 다른 2일 이상, 3회 적중하면 확정합니다. 같은 불일치가 2번 연속 나오면 새 규칙으로 바꿉니다.</span></li>`);
    if (op.pending) li.push(`<li><b>새 규칙 후보:</b> 사용일 ${op.pending.lead_max}일 전 ~ ${op.pending.lead_min}일 전 (연속 ${op.pending.count}회 관측)</li>`);
    li.push("<li><b>앞으로 오픈 예정:</b> " + op.upcoming.filter((u) => u.openDay >= isoToday)
      .map((u) => `${fmtDate(u.openDay)} → ${u.targets.map(fmtYmd).join(", ")}`).join(" · ") + "</li>");
    if (op.recent.length) {
      li.push("<li><b>날짜별 오픈 확인:</b><ul>" + op.recent.map((x) =>
        `<li>${fmtYmd(x.date)} 사용분 — ${x.status || ""}${x.window ? ` <span class="muted">(${x.window} 사이 열림)</span>` : ""}</li>`).join("") + "</ul></li>");
    }
    op.history.forEach((h) => li.push(`<li class="muted">폐기된 규칙: ${h.desc} (${fmtAt(h.retired_at)})</li>`));
    if (op.log.length) li.push("<li><b>기록:</b><ul>" + op.log.map((l) => `<li class="muted">${l}</li>`).join("") + "</ul></li>");
    li.push(`<li class="muted">관측 ${op.obsCount}회 · 마지막 확인 ${fmtAt(lo?.at)}` +
      (lo?.period ? ` · 사이트 이용가능일 ${lo.period}` : "") + " · 서버가 켜져 있으면 30분마다 확인</li>");
    $("opRules").innerHTML = li.join("");
  }

  async function loadOpening() {
    try {
      state.opening = await fetchJson("api/opening", "data/opening.json");
    } catch (e) {
      state.opening = null;
    }
    renderOpening();
    if (state.data) renderCalendar();
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
      st.innerHTML = "";
      st.append(`불러오기 실패: ${e.name === "TimeoutError" ? "90초 안에 응답 없음" : (e.message || e)} · `);
      const a = document.createElement("a");
      a.href = "api/diag"; a.target = "_blank"; a.textContent = "접속 진단 열기";
      st.append(a);
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
      let badge = "", hours = "", opens = "";
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
        const od = openDayOf(ymd);
        if (od && od > today && kind !== "has-open" && kind !== "closed" && kind !== "past") {
          opens = `${+od.slice(4, 6)}/${+od.slice(6)} 오픈`;
        }
      }
      btn.innerHTML = `<span class="n">${day}</span>` +
        (badge ? `<span class="badge">${badge}</span>` : "") +
        (hours ? `<span class="hours">${hours}</span>` : "") +
        (opens ? `<span class="opens">${opens}</span>` : "");
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
