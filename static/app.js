function fmtDate(d) {
  // Range dates are calendar days built by businessToday() and the date math
  // below, so format them by their own calendar fields — converting them to
  // another time zone would shift them a day for viewers outside Central Time.
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function businessToday() {
  // Business day is Central Time (matches server's BUSINESS_TZ), not the
  // viewer's clock — e.g. from Manila, "Today" must still be today in Chicago.
  const [y, m, d] = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Chicago" }).format(new Date()).split("-").map(Number);
  return new Date(y, m - 1, d);
}

function addDays(d, n) {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}

function daysBetween(a, b) {
  return Math.round((b - a) / 86400000);
}

function startOfWeek(d) {
  const day = d.getDay(); // 0 = Sunday
  const diff = (day === 0 ? -6 : 1) - day; // back to Monday
  return addDays(d, diff);
}

function startOfMonth(d) {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}

function startOfQuarter(d) {
  const qMonth = Math.floor(d.getMonth() / 3) * 3;
  return new Date(d.getFullYear(), qMonth, 1);
}

function startOfYear(d) {
  return new Date(d.getFullYear(), 0, 1);
}

// Earliest date "All Time" asks for: the oldest HighLevel contact is from
// Jan 13, 2026 and the first call from Mar 10, 2026. Asking for older empty
// years only makes the call export crawl through them.
const ALL_TIME_START = new Date(2026, 0, 1);

function previousPeriodFor(key, start, end) {
  switch (key) {
    case "today": {
      const y = addDays(start, -1);
      return { start: y, end: y };
    }
    case "yesterday": {
      const y = addDays(start, -1);
      return { start: y, end: y };
    }
    case "week":
    case "lastweek":
      return { start: addDays(start, -7), end: addDays(end, -7) };
    case "month": {
      const prevMonthStart = new Date(start.getFullYear(), start.getMonth() - 1, 1);
      const dayOfMonth = end.getDate();
      const daysInPrevMonth = new Date(prevMonthStart.getFullYear(), prevMonthStart.getMonth() + 1, 0).getDate();
      const prevEnd = new Date(prevMonthStart.getFullYear(), prevMonthStart.getMonth(), Math.min(dayOfMonth, daysInPrevMonth));
      return { start: prevMonthStart, end: prevEnd };
    }
    case "quarter": {
      const daysIn = daysBetween(start, end);
      const prevQuarterStart = new Date(start.getFullYear(), start.getMonth() - 3, 1);
      return { start: prevQuarterStart, end: addDays(prevQuarterStart, daysIn) };
    }
    case "year": {
      const daysIn = daysBetween(start, end);
      const prevYearStart = new Date(start.getFullYear() - 1, 0, 1);
      return { start: prevYearStart, end: addDays(prevYearStart, daysIn) };
    }
    case "custom":
    default: {
      const lengthDays = daysBetween(start, end) + 1;
      const prevEnd = addDays(start, -1);
      const prevStart = addDays(prevEnd, -(lengthDays - 1));
      return { start: prevStart, end: prevEnd };
    }
  }
}

function currency(n) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(n);
}

let currentRange = { start: null, end: null };
let currentRangeKey = "week";

function setActiveButton(range) {
  document.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
  const btn = document.querySelector(`.range-btn[data-range="${range}"]`);
  if (btn) btn.classList.add("active");
}

function applyRange(rangeKey, start, end) {
  currentRangeKey = rangeKey;
  currentRange = { start, end };
  updateCustomButtonLabel();
  updateRangeDates();
  loadData();
}

function updateRangeDates() {
  const { start, end } = currentRange;
  const fmt = (d) => d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  document.getElementById("range-dates").textContent = currentRangeKey === "all"
    ? `All time through ${fmt(end)}`
    : isSameDay(start, end) ? fmt(start) : `${fmt(start)} – ${fmt(end)}`;
}

// --- Custom range calendar popup ---
// Calendar dates are local noon so formatting in Central Time never slips a day.
let calendarViewDate = businessToday();
let calSelStart = null;
let calSelEnd = null;
let calHoverDay = null;

function isSameDay(a, b) {
  return a && b && a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function atNoon(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate(), 12);
}

function parseInputDate(value) {
  if (!value) return null;
  const [y, m, d] = value.split("-").map(Number);
  return new Date(y, m - 1, d, 12);
}

function shortDate(d) {
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function showCalendarPopup() {
  // Reopen on the last applied custom range so it can be tweaked.
  if (currentRangeKey === "custom" && currentRange.start) {
    calSelStart = atNoon(currentRange.start);
    calSelEnd = atNoon(currentRange.end);
  } else {
    calSelStart = null;
    calSelEnd = null;
  }
  calHoverDay = null;
  calendarViewDate = calSelStart ? new Date(calSelStart) : businessToday();
  document.getElementById("calendar-popup").classList.remove("hidden");
  renderCalendar();
}

function hideCalendarPopup() {
  document.getElementById("calendar-popup").classList.add("hidden");
}

function updateCustomButtonLabel() {
  const btn = document.querySelector('.range-btn[data-range="custom"]');
  if (currentRangeKey === "custom" && currentRange.start) {
    const { start, end } = currentRange;
    btn.textContent = isSameDay(start, end) ? shortDate(start) : `${shortDate(start)} – ${shortDate(end)}`;
  } else {
    btn.textContent = "Custom";
  }
}

// Day cells for the visible month. Hovering only repaints these in place:
// rebuilding the grid mid-click swallows the click that picks the end date.
let calCells = [];

function renderCalendar() {
  const label = document.getElementById("cal-month-label");
  const grid = document.getElementById("calendar-grid");
  const year = calendarViewDate.getFullYear();
  const month = calendarViewDate.getMonth();

  label.textContent = calendarViewDate.toLocaleDateString("en-US", { month: "long", year: "numeric" });

  const firstOfMonth = new Date(year, month, 1, 12);
  const gridStart = addDays(firstOfMonth, -firstOfMonth.getDay());

  grid.innerHTML = "";
  calCells = [];
  for (let i = 0; i < 42; i++) {
    const day = addDays(gridStart, i);
    const cell = document.createElement("div");
    cell.textContent = day.getDate();

    cell.addEventListener("click", (e) => {
      e.stopPropagation();
      selectCalendarDay(day);
    });
    cell.addEventListener("mouseenter", () => {
      if (calSelStart && !calSelEnd && !isSameDay(calHoverDay, day)) {
        calHoverDay = day;
        paintCalendar();
      }
    });
    grid.appendChild(cell);
    calCells.push({ cell, day });
  }
  paintCalendar();
}

function paintCalendar() {
  const hint = document.getElementById("calendar-hint");
  const month = calendarViewDate.getMonth();
  const today = businessToday();

  if (!calSelStart) hint.textContent = "Pick a start date";
  else if (!calSelEnd) hint.textContent = `From ${shortDate(calSelStart)} – now pick an end date`;
  else {
    const days = daysBetween(calSelStart, calSelEnd) + 1;
    hint.textContent = `${shortDate(calSelStart)} – ${shortDate(calSelEnd)} (${days} day${days === 1 ? "" : "s"})`;
  }

  document.getElementById("cal-start-input").value = calSelStart ? fmtDate(calSelStart) : "";
  document.getElementById("cal-end-input").value = calSelEnd ? fmtDate(calSelEnd) : "";
  document.getElementById("cal-apply").disabled = !(calSelStart && calSelEnd);

  // While only a start is picked, preview the range up to the hovered day.
  let previewFrom = null;
  let previewTo = null;
  if (calSelStart && !calSelEnd && calHoverDay) {
    [previewFrom, previewTo] = calHoverDay < calSelStart ? [calHoverDay, calSelStart] : [calSelStart, calHoverDay];
  }

  for (const { cell, day } of calCells) {
    cell.className = "calendar-day";
    if (day.getMonth() !== month) cell.classList.add("cal-outside");
    if (isSameDay(day, today)) cell.classList.add("cal-today");
    if (isSameDay(day, calSelStart)) cell.classList.add("cal-range-start");
    if (isSameDay(day, calSelEnd)) cell.classList.add("cal-range-end");
    if (calSelStart && calSelEnd && day > calSelStart && day < calSelEnd) cell.classList.add("cal-in-range");
    if (previewFrom && day >= previewFrom && day <= previewTo && !isSameDay(day, calSelStart)) cell.classList.add("cal-preview");
  }
}

// First click picks the start, second click picks the end and applies the
// range. If the second day is earlier, the two are swapped so start <= end.
function selectCalendarDay(day) {
  calHoverDay = null;
  if (!calSelStart || calSelEnd) {
    calSelStart = day;
    calSelEnd = null;
  } else if (day < calSelStart) {
    calSelEnd = calSelStart;
    calSelStart = day;
  } else {
    calSelEnd = day;
  }

  if (calSelStart && calSelEnd) {
    hideCalendarPopup();
    setActiveButton("custom");
    applyRange("custom", calSelStart, calSelEnd);
    return;
  }
  if (day.getMonth() !== calendarViewDate.getMonth()) {
    calendarViewDate = new Date(day.getFullYear(), day.getMonth(), 1);
    renderCalendar();
  } else {
    paintCalendar();
  }
}

function onCalendarInputChange() {
  let start = parseInputDate(document.getElementById("cal-start-input").value);
  let end = parseInputDate(document.getElementById("cal-end-input").value);
  if (start && end && end < start) [start, end] = [end, start];
  calSelStart = start;
  calSelEnd = start ? end : null;
  if (calSelStart) calendarViewDate = new Date(calSelStart);
  renderCalendar();
}

document.getElementById("cal-start-input").addEventListener("change", onCalendarInputChange);
document.getElementById("cal-end-input").addEventListener("change", onCalendarInputChange);

document.getElementById("calendar-grid").addEventListener("mouseleave", () => {
  if (calHoverDay) {
    calHoverDay = null;
    paintCalendar();
  }
});

document.getElementById("cal-apply").addEventListener("click", (e) => {
  e.stopPropagation();
  if (!(calSelStart && calSelEnd)) return;
  hideCalendarPopup();
  setActiveButton("custom");
  applyRange("custom", calSelStart, calSelEnd);
});

document.getElementById("cal-cancel").addEventListener("click", (e) => {
  e.stopPropagation();
  hideCalendarPopup();
  setActiveButton(currentRangeKey);
});

document.getElementById("cal-prev-month").addEventListener("click", (e) => {
  e.stopPropagation();
  calendarViewDate = new Date(calendarViewDate.getFullYear(), calendarViewDate.getMonth() - 1, 1);
  renderCalendar();
});

document.getElementById("cal-next-month").addEventListener("click", (e) => {
  e.stopPropagation();
  calendarViewDate = new Date(calendarViewDate.getFullYear(), calendarViewDate.getMonth() + 1, 1);
  renderCalendar();
});

document.addEventListener("click", (e) => {
  const wrap = document.querySelector(".custom-range-wrap");
  const popup = document.getElementById("calendar-popup");
  if (wrap && !wrap.contains(e.target) && !popup.classList.contains("hidden")) {
    hideCalendarPopup();
    setActiveButton(currentRangeKey);
  }
});

document.querySelectorAll(".range-btn").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    const range = btn.dataset.range;
    setActiveButton(range);
    if (range === "custom") {
      showCalendarPopup();
      e.stopPropagation();
      return;
    }
    hideCalendarPopup();
    const now = businessToday();
    if (range === "today") applyRange("today", now, now);
    if (range === "yesterday") applyRange("yesterday", addDays(now, -1), addDays(now, -1));
    if (range === "week") applyRange("week", startOfWeek(now), now);
    if (range === "lastweek") applyRange("lastweek", addDays(startOfWeek(now), -7), addDays(startOfWeek(now), -1));
    if (range === "month") applyRange("month", startOfMonth(now), now);
    if (range === "quarter") applyRange("quarter", startOfQuarter(now), now);
    if (range === "year") applyRange("year", startOfYear(now), now);
    if (range === "all") applyRange("all", ALL_TIME_START, now);
  });
});

document.getElementById("compare-checkbox").addEventListener("change", loadData);

document.getElementById("refresh-data").addEventListener("click", async () => {
  setLoading(true);
  try {
    await fetch("/api/refresh");
  } finally {
    loadData();
  }
});

function powerDialerCloseRate(d) {
  return d.called ? Math.round((d.closed / d.called) * 1000) / 10 : null;
}

async function loadPowerDialer(start, end) {
  const el = document.getElementById("power-dialer-row");
  const compareEnabled = document.getElementById("compare-checkbox").checked;
  try {
    let curr;
    let prev = null;
    if (compareEnabled) {
      const prevRange = previousPeriodFor(currentRangeKey, currentRange.start, currentRange.end);
      const [currRes, prevRes] = await Promise.all([
        fetchJSON(`/api/power-dialer?start=${start}&end=${end}`),
        fetchJSON(`/api/power-dialer?start=${fmtDate(prevRange.start)}&end=${fmtDate(prevRange.end)}`),
      ]);
      curr = currRes;
      prev = prevRes;
    } else {
      curr = await fetchJSON(`/api/power-dialer?start=${start}&end=${end}`);
    }

    const missingNote =
      curr.missingDays && curr.missingDays.length
        ? `<div class="kpi-small">${curr.missingDays.length} day(s) in range not tracked yet</div>`
        : "";
    const closeRate = powerDialerCloseRate(curr);
    const prevCloseRate = prev ? powerDialerCloseRate(prev) : null;

    const calledDelta = prev ? deltaHTML(pctChange(curr.called, prev.called)) : "";
    const closedDelta = prev ? deltaHTML(pctChange(curr.closed, prev.closed)) : "";
    const contractDelta = prev ? deltaHTML(pctChange(curr.contractValue, prev.contractValue)) : "";
    const closeRateDelta = prev && closeRate !== null && prevCloseRate !== null ? deltaHTML(closeRate - prevCloseRate) : "";

    el.innerHTML = `
      <div class="kpi-card">
        <div class="kpi-label">Leads Called</div>
        <div class="kpi-value">${curr.called}</div>
        ${calledDelta ? `<div class="kpi-delta">${calledDelta} vs previous period</div>` : ""}
        ${missingNote}
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Leads Closed</div>
        <div class="kpi-value">${curr.closed}</div>
        ${closedDelta ? `<div class="kpi-delta">${closedDelta} vs previous period</div>` : ""}
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Contract Amount</div>
        <div class="kpi-value">${currency(curr.contractValue)}</div>
        ${contractDelta ? `<div class="kpi-delta">${contractDelta} vs previous period</div>` : ""}
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Closed Rate</div>
        <div class="kpi-value">${closeRate !== null ? closeRate + "%" : "—"}</div>
        ${closeRateDelta ? `<div class="kpi-delta">${closeRateDelta} vs previous period</div>` : ""}
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="kpi-card"><div class="kpi-label">Power Dialer</div><div class="kpi-small">${e.message}</div></div>`;
  }
}

document.querySelectorAll(".view-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".view-tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const view = tab.dataset.view;
    ["sales", "leaderboard", "sales-overview", "commercial", "revenue"].forEach((v) => {
      document.getElementById(`view-${v}`).classList.toggle("hidden", view !== v);
    });
    document
      .getElementById("range-toolbar")
      .classList.toggle("hidden", !["sales", "sales-overview", "commercial"].includes(view));
    if (view === "leaderboard") loadLeaderboard();
    if (view === "sales-overview" && currentRange.start) loadSalesOverview(fmtDate(currentRange.start), fmtDate(currentRange.end));
    if (view === "commercial" && currentRange.start) loadCommercial(fmtDate(currentRange.start), fmtDate(currentRange.end));
    if (view === "revenue") loadRevenue();
  });
});

function sumRevenueRows(rows) {
  const t = { sold: 0, acv: 0, arv: 0, initial: 0, recurring: 0, multiyear: 0, m18: 0, m24: 0, cancelled: 0, cancelledAcv: 0, serviced: 0, servicedAcv: 0, servicedActive: 0, servicedActiveAcv: 0 };
  rows.forEach((r) => Object.keys(t).forEach((k) => (t[k] += r[k] || 0)));
  return t;
}

// `filter` ({rep, month, inbound, label}) makes the row clickable: clicking a
// count opens the matching sales in the modal, filtered by that column's status.
function revenueTableRow(label, t, filter, extraClass = "") {
  const attr = filter ? ` data-filter="${escapeHTML(JSON.stringify({ ...filter, label }))}"` : "";
  const cell = (status, html) => `<td class="num${filter ? " rev-cell" : ""}" data-status="${status}">${html}</td>`;
  return `<tr class="${filter ? "rev-click " : ""}${extraClass}"${attr}>
    <td>${escapeHTML(label)}</td>
    ${cell("all", t.sold)}
    ${cell("all", currency(t.acv))}
    ${cell("all", t.sold ? currency(t.acv / t.sold) : "—")}
    ${cell("serviced", t.serviced)}
    ${cell("serviced", currency(t.servicedAcv))}
    ${cell("serviced", t.serviced ? currency(t.servicedAcv / t.serviced) : "—")}
    ${cell("serviced", pctLabel(rate(t.serviced, t.sold)))}
    ${cell("servicedActive", `${t.servicedActive} (${currency(t.servicedActiveAcv)})`)}
    ${cell("notServiced", `${t.sold - t.serviced} (${currency(t.acv - t.servicedAcv)})`)}
    ${cell("multiyear", `${t.multiyear} (${pctLabel(rate(t.multiyear, t.recurring))})`)}
    ${cell("cancelled", `${t.cancelled} (${currency(t.cancelledAcv)})`)}
  </tr>`;
}

const SALES_STATUS_LABEL = {
  all: "Closed/Won",
  serviced: "Serviced",
  servicedActive: "Serviced & Active",
  notServiced: "Not Serviced",
  multiyear: "Multiyear",
  cancelled: "Cancelled",
};

function salesMatching(filter, status) {
  return revenueSales.filter((s) => {
    if (filter.rep && s.group !== filter.rep) return false;
    if (filter.month && !s.sold.startsWith(filter.month)) return false;
    if (filter.inbound && !s.inbound) return false;
    if (filter.channels && !filter.channels.includes(s.channel)) return false;
    if (filter.start && s.sold < filter.start) return false;
    if (filter.end && s.sold > filter.end) return false;
    if (status === "serviced") return s.serviced;
    if (status === "servicedActive") return s.serviced && !s.cancelled;
    if (status === "notServiced") return !s.serviced;
    if (status === "multiyear") return s.months >= 18;
    if (status === "cancelled") return s.cancelled;
    return true;
  });
}

function saleStatusText(s) {
  return s.serviced ? (s.cancelled ? "Serviced, then cancelled" : "Serviced & active") : s.cancelled ? "Cancelled before service" : `Not serviced (${s.initialStatus || "—"})`;
}

// Customer name → the HighLevel lead the sale was matched to (Mika, 2026-10-27).
let revenueLocations = {};
function saleCustomerLink(sale) {
  const loc = revenueLocations[sale.channel];
  if (!sale.leadContactId || !loc) return escapeHTML(sale.customer);
  return `<a class="lead-link" href="${ghlContactUrl(loc, sale.leadContactId)}" target="_blank" rel="noopener">${escapeHTML(sale.customer)}</a>`;
}

function salesTableHTML(list) {
  return `<table class="deal-table call-log-table">
    <thead><tr><th>Sold</th><th>Customer</th><th>Rep</th><th>Plan</th><th>City</th><th class="num">Contract Value</th><th class="num">ARV</th><th class="num">Months</th><th>Status</th></tr></thead>
    <tbody>${list
      .map(
        (s) => `<tr>
          <td class="nowrap">${formatDealDate(s.sold)}</td>
          <td>${saleCustomerLink(s)}${s.channel || s.inbound ? ` <span class="call-pill">${escapeHTML(s.channel || "Inbound")}</span>` : ""}</td>
          <td class="nowrap">${escapeHTML(s.rep)}</td>
          <td>${escapeHTML(s.plan || "")}</td>
          <td class="nowrap">${escapeHTML(s.city || "")}</td>
          <td class="num">${currency(s.contractValue)}</td>
          <td class="num">${currency(s.arv)}</td>
          <td class="num">${s.months ?? "—"}</td>
          <td class="nowrap">${saleStatusText(s)}</td>
        </tr>`
      )
      .join("")}</tbody>
  </table>`;
}

function openSalesModal(filter, status) {
  const list = salesMatching(filter, status);
  const total = list.reduce((sum, s) => sum + s.contractValue, 0);
  const title = [filter.label, filter.inbound && !/Inbound/.test(filter.label) ? "Inbound" : null, SALES_STATUS_LABEL[status] || "Closed/Won"].filter(Boolean).join(" — ");
  document.getElementById("rep-deals-title").textContent = title;
  document.getElementById("rep-deals-body").innerHTML = list.length
    ? `<p class="revenue-asof">${list.length} sales · ${currency(total)} total contract value</p>${salesTableHTML(list)}`
    : `<p class="deal-table-empty">No sales match.</p>`;
  document.getElementById("rep-deals-modal").classList.remove("hidden");
}

document.addEventListener("click", (e) => {
  const row = e.target.closest("tr.rev-click");
  if (!row) return;
  const cell = e.target.closest("td[data-status]");
  openSalesModal(JSON.parse(row.dataset.filter), cell ? cell.dataset.status : "all");
});

function revenueTable(rowsHTML, totalHTML) {
  return `<table class="deal-table call-log-table">
    <thead><tr><th>&nbsp;</th><th class="num">Closed/Won</th><th class="num">Total Contract Value</th><th class="num">Avg Contract Value</th><th class="num">Serviced</th><th class="num">Serviced Total Contract Value</th><th class="num">Serviced Avg Contract Value</th><th class="num">Serviced %</th><th class="num">Serviced &amp; Active</th><th class="num">Not Serviced</th><th class="num">Multiyear</th><th class="num">Cancelled</th></tr></thead>
    <tbody>${rowsHTML}${totalHTML}</tbody>
  </table>`;
}

let revenueSales = [];

function renderSalesList() {
  const el = document.getElementById("sales-list-table");
  const rep = document.getElementById("sales-list-rep").value;
  const status = document.getElementById("sales-list-status").value;
  const list = revenueSales.filter((s) => {
    if (rep !== "all" && s.group !== rep) return false;
    if (status === "servicedActive") return s.serviced && !s.cancelled;
    if (status === "notServiced") return !s.serviced;
    if (status === "cancelled") return s.cancelled;
    if (status === "multiyear") return s.months >= 18;
    if (status === "inbound") return s.inbound;
    return true;
  });
  const total = list.reduce((sum, s) => sum + s.contractValue, 0);
  document.getElementById("sales-list-count").textContent = `${list.length} sales · ${currency(total)} total contract value`;
  if (!list.length) {
    el.innerHTML = `<p class="deal-table-empty">No sales match.</p>`;
    return;
  }
  const statusText = (s) =>
    s.serviced ? (s.cancelled ? "Serviced, then cancelled" : "Serviced & active") : s.cancelled ? "Cancelled before service" : `Not serviced (${s.initialStatus || "—"})`;
  el.innerHTML = `<table class="deal-table call-log-table">
    <thead><tr><th>Sold</th><th>Customer</th><th>Rep</th><th>Plan</th><th>City</th><th class="num">Contract Value</th><th class="num">ARV</th><th class="num">Months</th><th>Status</th></tr></thead>
    <tbody>${list
      .map(
        (s) => `<tr>
          <td class="nowrap">${formatDealDate(s.sold)}</td>
          <td>${escapeHTML(s.customer)}${s.inbound ? ' <span class="call-pill">Inbound</span>' : ""}</td>
          <td class="nowrap">${escapeHTML(s.rep)}</td>
          <td>${escapeHTML(s.plan || "")}</td>
          <td class="nowrap">${escapeHTML(s.city || "")}</td>
          <td class="num">${currency(s.contractValue)}</td>
          <td class="num">${currency(s.arv)}</td>
          <td class="num">${s.months ?? "—"}</td>
          <td class="nowrap">${statusText(s)}</td>
        </tr>`
      )
      .join("")}</tbody>
  </table>`;
}

document.getElementById("sales-list-rep").addEventListener("change", renderSalesList);
document.getElementById("sales-list-status").addEventListener("change", renderSalesList);

// Revenue-row totals (same fields as the snapshot's per rep/month rows) for
// an arbitrary list of sales, so the Sales Overview can follow the date range.
function sumSales(list) {
  const t = sumRevenueRows([]);
  list.forEach((s) => {
    const acv = s.contractValue || 0;
    t.sold += 1;
    t.acv += acv;
    t.arv += s.arv || 0;
    t.initial += s.initial || 0;
    if (s.frequency !== "-1" && (s.months || 0) > 1) t.recurring += 1;
    if ((s.months || 0) >= 18) t.multiyear += 1;
    if (s.months === 18) t.m18 += 1;
    if (s.months === 24) t.m24 += 1;
    if (s.cancelled) {
      t.cancelled += 1;
      t.cancelledAcv += acv;
    }
    if (s.serviced) {
      t.serviced += 1;
      t.servicedAcv += acv;
      if (!s.cancelled) {
        t.servicedActive += 1;
        t.servicedActiveAcv += acv;
      }
    }
  });
  return t;
}

function durationLabel(seconds) {
  // Same style as HighLevel's dashboard: 1h 4m 12s / 8m 12s / 45s.
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const sec = Math.round(seconds % 60);
  return [h ? `${h}h` : "", h || m ? `${m}m` : "", `${sec}s`].filter(Boolean).join(" ");
}

async function loadCallCount(start, end) {
  const banner = document.getElementById("call-count-error-banner");
  banner.classList.add("hidden");
  const el = document.getElementById("call-count-accounts");
  try {
    const data = await fetchJSON(`/api/call-count?start=${start}&end=${end}`);
    const row = (r, cls = "") =>
      `<tr class="${cls}"><td class="nowrap">${escapeHTML(r.name)}</td><td class="num">${r.total}</td><td class="num">${r.outbound}</td><td class="num">${r.inbound}</td><td class="num">${r.answered}</td><td class="num">${r.missed}</td><td class="num">${r.busyFailed}</td><td class="num nowrap">${durationLabel(r.durationSeconds)}</td></tr>`;
    el.innerHTML = data.accounts
      .map(
        (a, i) => `
      <div class="widget widget-wide">
        <h2 class="channel-title">${escapeHTML(a.name)}</h2>
        <div class="kpi-row">
          <div class="kpi-card"><div class="kpi-label">Total Calls</div><div class="kpi-value">${a.total}</div><div class="kpi-small">${a.uniqueContacts} unique contacts</div></div>
          <div class="kpi-card"><div class="kpi-label">Outgoing</div><div class="kpi-value">${a.outbound}</div><div class="kpi-small">${pctLabel(rate(a.outbound, a.total))} of calls</div></div>
          <div class="kpi-card"><div class="kpi-label">Incoming</div><div class="kpi-value">${a.inbound}</div><div class="kpi-small">${pctLabel(rate(a.inbound, a.total))} of calls</div></div>
          <div class="kpi-card"><div class="kpi-label">Answered</div><div class="kpi-value">${a.answered}</div><div class="kpi-small">${a.missed} missed/no answer · ${a.busyFailed} busy/failed</div></div>
          <div class="kpi-card"><div class="kpi-label">Recorded Call Duration</div><div class="kpi-value">${durationLabel(a.durationSeconds)}</div><div class="kpi-small">avg ${a.total ? durationLabel(a.durationSeconds / a.total) : "0s"} recorded per call</div></div>
        </div>
      </div>
      <div class="dashboard-row call-count-row">
        <div class="widget"><h2>Calls by Rep</h2><div id="call-count-chart-${i}" class="bar-wrap"></div></div>
        <div class="widget"><h2>By Rep (Call Attended By)</h2><div class="call-log-wrap"><table class="deal-table call-log-table">
          <thead><tr><th>&nbsp;</th><th class="num">Total</th><th class="num">Outgoing</th><th class="num">Incoming</th><th class="num">Answered</th><th class="num">Missed</th><th class="num">Busy/Failed</th><th class="num">Recorded Duration</th></tr></thead>
          <tbody>${a.byRep.map((r) => row(r)).join("")}${row({ ...a, name: "Total" }, "revenue-total")}</tbody>
        </table></div></div>
      </div>`
      )
      .join("");
    data.accounts.forEach((a, i) =>
      renderBarChart(`call-count-chart-${i}`, a.byRep.length ? a.byRep : [{ name: "No calls", total: 0 }], {
        valueOf: (r) => r.total,
        formatValue: (item) => `${item.value}`,
        onClick: (r) => r.name !== "No calls" && openChartLeads("cc-calls", r.name, a.name),
      })
    );
  } catch (e) {
    banner.classList.remove("hidden");
    banner.textContent = `Call count: ${e.message}`;
  }
}

const OUTBOUND_CHANNELS = ["Warm Outbound", "Cold Outbound"];

function closeRateLabel(rate) {
  // Over 100% means the qualification field isn't being filled in for this
  // channel, so the rate says nothing — show a dash instead.
  return rate === null || rate > 100 ? "—" : `${rate}%`;
}

// Latest requested range per section: a slow answer for an older range must not
// overwrite the newer one when the user switches dates quickly (Mika, 2026-10-27).
const latestFunnelRequest = {};

async function loadChannelFunnels(start, end) {
  const key = `${start}|${end}`;
  latestFunnelRequest.overview = key;
  const data = await fetchJSON(`/api/sales-overview?start=${start}&end=${end}`);
  if (latestFunnelRequest.overview !== key) return;
  renderChannelFunnels(document.getElementById("overview-channels"), data.channels, "overview");
}

async function loadCommercial(start, end) {
  const banner = document.getElementById("commercial-error-banner");
  banner.classList.add("hidden");
  try {
    const key = `${start}|${end}`;
    latestFunnelRequest.commercial = key;
    const data = await fetchJSON(`/api/commercial?start=${start}&end=${end}`);
    if (latestFunnelRequest.commercial !== key) return;
    document.getElementById("commercial-asof").textContent =
      `Main-account leads with Property Type = Commercial (${data.taggedCommercial} tagged so far). They're left out of every other tab. FieldRoutes data through ${formatDealDate(data.dataThrough)}.`;
    renderChannelFunnels(document.getElementById("commercial-channels"), data.channels, "commercial");
  } catch (e) {
    banner.classList.remove("hidden");
    banner.textContent = `Commercial: ${e.message}`;
  }
}

function renderChannelFunnels(el, channels, prefix) {
  // Inbound (and Commercial) track qualified leads; Warm Outbound shows leads
  // received by lead type; Cold Outbound shows calls made (Mika, 2026-10-26).
  const kpi = (label, value, small) => `<div class="kpi-card"><div class="kpi-label">${label}</div><div class="kpi-value">${value}</div><div class="kpi-small">${small}</div></div>`;
  const byType = (ch, f) => (ch.leadTypes || []).map((t) => `${escapeHTML(t.name)}: ${f(t)}`).join("<br>");
  // Inbound, Warm and Cold Outbound titles get a full-width green band (Mika, 2026-10-27).
  const titleClass = (ch) => ["Inbound", "Warm Outbound", "Cold Outbound"].includes(ch.name) ? "channel-title channel-title-band" : "channel-title";
  const block = (ch, i) => {
    if (ch.name === "Warm Outbound") {
      return `
      <div class="widget widget-wide">
        <h2 class="${titleClass(ch)}">${escapeHTML(ch.name)}</h2>
        <div class="kpi-row">
          ${kpi("Leads Received", ch.leads, byType(ch, (t) => t.leads))}
          ${kpi("Sales", ch.frCount, byType(ch, (t) => t.frCount))}
          ${kpi("Contract Value", currency(ch.frAcv), byType(ch, (t) => currency(t.frAcv)))}
        </div>
      </div>
      <div class="dashboard-row dashboard-row-3">
        <div class="widget"><h2>Leads Received by Type</h2><div id="${prefix}-types-${i}" class="bar-wrap"></div></div>
        <div class="widget"><h2>Sales by Lead Type</h2><div id="${prefix}-typesales-${i}" class="bar-wrap"></div></div>
        <div class="widget"><h2>Sold Contracts by Sales Rep</h2><div id="${prefix}-sold-${i}" class="bar-wrap"></div></div>
      </div>`;
    }
    if (ch.name === "Cold Outbound") {
      return `
      <div class="widget widget-wide">
        <h2 class="${titleClass(ch)}">${escapeHTML(ch.name)}</h2>
        <div class="kpi-row">
          ${kpi("Calls Made", ch.callsMade ?? "—", "outgoing calls in this period")}
          ${kpi("Sales", ch.frCount, "sold in this period")}
          ${kpi("Contract Value", currency(ch.frAcv), "total contract value")}
        </div>
      </div>
      <div class="dashboard-row">
        <div class="widget"><h2>Calls Made by Rep</h2><div id="${prefix}-calls-${i}" class="bar-wrap"></div></div>
        <div class="widget"><h2>Sold Contracts by Sales Rep</h2><div id="${prefix}-sold-${i}" class="bar-wrap"></div></div>
      </div>`;
    }
    const fixed = ch.qualified < ch.frCount;
    return `
      <div class="widget widget-wide">
        <h2 class="${titleClass(ch)}">${escapeHTML(ch.name)}</h2>
        ${ch.olderSources && ch.olderSources.count ? `<p class="revenue-asof">Plus ${ch.olderSources.count} sales (${currency(ch.olderSources.acv)}) from leads with older inbound sources (Yelp, GMB, LSA, Field Routes, truck numbers, no source) — not counted in the close rate.</p>` : ""}
        ${fixed ? `<p class="revenue-asof">Fewer leads are marked Qualified than were sold, so close rates aren't shown — the qualification field isn't being filled in for most ${escapeHTML(ch.name)} leads.</p>` : ""}
        <div class="kpi-row">
          ${kpi("Leads", ch.leads, "added in this period")}
          ${kpi("Qualified Leads", pctLabel(rate(ch.qualified, ch.leads)), `${ch.qualified} qualified`)}
          ${kpi("Sales", ch.frCount, "sold in this period")}
          ${kpi("Contract Value", currency(ch.frAcv), "total contract value")}
          ${kpi("Close Rate", closeRateLabel(ch.frCloseRate), `${ch.frCount} sales / ${ch.qualified} qualified`)}
        </div>
      </div>
      <div class="dashboard-row dashboard-row-3">
        <div class="widget"><h2>Sold Contracts by Sales Rep</h2><div id="${prefix}-sold-${i}" class="bar-wrap"></div></div>
        <div class="widget"><h2>Qualified Leads by Rep</h2><div id="${prefix}-qualified-${i}" class="bar-wrap"></div></div>
        <div class="widget"><h2>Closed / Qualified by Sales Rep</h2><div id="${prefix}-close-${i}" class="bar-wrap"></div></div>
      </div>`;
  };
  el.innerHTML = channels.map(block).join("");
  channels.forEach((ch, i) => {
    renderBarChart(`${prefix}-sold-${i}`, ch.reps, {
      onClick: (r) => openChartLeads("ov-sold", r.name, ch.name),
      valueOf: (r) => r.frCount,
      formatValue: (item) => `${item.value}`,
    });
    if (ch.name === "Warm Outbound") {
      renderBarChart(`${prefix}-types-${i}`, ch.leadTypes, {
        valueOf: (t) => t.leads,
        formatValue: (item) => `${item.value}`,
        onClick: (t) => openChartLeads("ov-types", t.name, ch.name),
      });
      renderBarChart(`${prefix}-typesales-${i}`, ch.leadTypes, {
        onClick: (t) => openChartLeads("ov-typesales", t.name, ch.name),
        valueOf: (t) => t.frCount,
        formatValue: (item) => `${item.value}`,
      });
      return;
    }
    if (ch.name === "Cold Outbound") {
      renderBarChart(`${prefix}-calls-${i}`, ch.callsByRep && ch.callsByRep.length ? ch.callsByRep : [{ name: "No calls", calls: 0 }], {
        valueOf: (r) => r.calls,
        onClick: (r) => r.name !== "No calls" && openChartLeads("ov-calls", r.name, ch.name),
        formatValue: (item) => `${item.value}`,
      });
      return;
    }
    renderBarChart(`${prefix}-qualified-${i}`, ch.reps, {
      onClick: (r) => openChartLeads("ov-qualified", r.name, ch.name),
      valueOf: (r) => r.qualified,
      formatValue: (item) => `${item.value}`,
    });
    renderBarChart(`${prefix}-close-${i}`, ch.reps, {
      onClick: (r) => openChartLeads("ov-close", r.name, ch.name),
      valueOf: (r) => (r.frCloseRate !== null && r.frCloseRate <= 100 ? r.frCloseRate : 0),
      formatValue: (item) => closeRateLabel(item.rep.frCloseRate),
      referenceLineAt: 100,
      referenceLabel: "100%",
    });
  });
}

async function loadSalesOverview(start, end) {
  const banner = document.getElementById("overview-error-banner");
  banner.classList.add("hidden");
  loadChannelFunnels(start, end).catch((e) => {
    banner.classList.remove("hidden");
    banner.textContent = `Sales overview: ${e.message}`;
  });
  try {
    const key = `${start}|${end}`;
    latestFunnelRequest.overviewTables = key;
    const data = await fetchJSON("/api/revenue-snapshot");
    revenueLocations = data.locations || revenueLocations;
    if (latestFunnelRequest.overviewTables !== key) return;
    revenueSales = data.sales || [];
    // Commercial sales live on the Commercial tab only.
    const inRange = revenueSales.filter((s) => s.sold >= start && s.sold <= end && s.channel !== "Commercial");
    const of = (list, channels) => list.filter((s) => channels.includes(s.channel));
    const all = sumSales(inRange);
    const groups = [
      { label: "Inbound", channels: ["Inbound"] },
      { label: "Outbound", channels: OUTBOUND_CHANNELS, subtotal: true },
      { label: "Outbound — Warm", channels: ["Warm Outbound"] },
      { label: "Outbound — Cold", channels: ["Cold Outbound"] },
      { label: "Not matched to a lead", channels: ["Not matched"] },
    ];
    const notes = [`${data.reps.join(" + ")} · FieldRoutes sales credited by Sold By`];
    if (data.othersNote) notes.push(data.othersNote);
    notes.push(
      "Channel = the HighLevel account of the most recent lead with the same phone/email that came in on or before the sale: Inbound (main account, any source), Warm Outbound or Cold Outbound (those sub-accounts)"
    );
    if (start < "2026-05-01") notes.push("FieldRoutes sales start May 1, 2026");
    notes.push(`FieldRoutes data through ${formatDealDate(data.dataThrough)}`);
    document.getElementById("overview-asof").textContent = notes.join(" · ");
    const card = (label, t) =>
      `<div class="kpi-card"><div class="kpi-label">${label}</div><div class="kpi-value">${t.sold}</div><div class="kpi-small">${currency(t.acv)} total contract value<br>${t.sold ? currency(t.acv / t.sold) : "$0"} avg contract value${label === "All Sales" ? "" : ` · ${pctLabel(rate(t.sold, all.sold))} of sales`}</div></div>`;
    document.getElementById("overview-kpi-row").innerHTML =
      card("All Sales", all) +
      groups.map((g) => card(g.label, sumSales(of(inRange, g.channels)))).join("");
    const range = { start, end };
    const rowsFor = (list, extra, prefix) =>
      groups
        .map((g) =>
          revenueTableRow(prefix ? `${prefix} — ${g.label}` : g.label, sumSales(of(list, g.channels)), { ...range, ...extra, channels: g.channels }, g.subtotal ? "revenue-subtotal" : "")
        )
        .join("");
    document.getElementById("overview-channel-table").innerHTML = revenueTable(rowsFor(inRange, {}), revenueTableRow("Total", all, range, "revenue-total"));
    const repRows = data.reps
      .map((rep) => {
        const mine = inRange.filter((s) => s.group === rep);
        return rowsFor(mine, { rep }, rep) + revenueTableRow(`${rep} — Total`, sumSales(mine), { ...range, rep }, "revenue-total");
      })
      .join("");
    document.getElementById("overview-rep-table").innerHTML = revenueTable(repRows, revenueTableRow("Team", all, range, "revenue-total"));
  } catch (e) {
    banner.classList.remove("hidden");
    banner.textContent = `Sales overview: ${e.message}`;
  }
}

async function loadRevenue() {
  const banner = document.getElementById("revenue-error-banner");
  banner.classList.add("hidden");
  try {
    const data = await fetchJSON("/api/revenue-snapshot");
    revenueLocations = data.locations || revenueLocations;
    const team = sumRevenueRows(data.rows);
    document.getElementById("revenue-title").textContent = `${data.periodLabel || "2026 Year to Date"} — Inside Sales`;
    document.getElementById("revenue-asof").textContent =
      `${data.reps.join(" + ")} · all lead sources${data.othersNote ? ` · ${data.othersNote}` : ""} · FieldRoutes data through ${formatDealDate(data.dataThrough)} (snapshot ${formatDealDate(data.asOf)})`;
    document.getElementById("revenue-kpi-row").innerHTML = `
      <div class="kpi-card"><div class="kpi-label">Closed/Won</div><div class="kpi-value">${team.sold}</div><div class="kpi-small">${currency(team.acv)} total contract value<br>${currency(team.acv / team.sold)} avg contract value</div></div>
      <div class="kpi-card"><div class="kpi-label">Serviced</div><div class="kpi-value">${team.serviced}</div><div class="kpi-small">${currency(team.servicedAcv)} total contract value<br>${currency(team.servicedAcv / team.serviced)} avg contract value · ${pctLabel(rate(team.serviced, team.sold))} of closed/won</div></div>
      <div class="kpi-card"><div class="kpi-label">Serviced &amp; Active</div><div class="kpi-value">${team.servicedActive}</div><div class="kpi-small">${currency(team.servicedActiveAcv)} total contract value · ${pctLabel(rate(team.servicedActive, team.sold))} of closed/won</div></div>
      <div class="kpi-card"><div class="kpi-label">Not Serviced</div><div class="kpi-value">${team.sold - team.serviced}</div><div class="kpi-small">${currency(team.acv - team.servicedAcv)} ACV</div></div>
      <div class="kpi-card"><div class="kpi-label">ARV</div><div class="kpi-value">${currency(team.arv)}</div><div class="kpi-small">${currency(team.initial)} initial revenue</div></div>
      <div class="kpi-card"><div class="kpi-label">Multiyear</div><div class="kpi-value">${pctLabel(rate(team.multiyear, team.recurring))}</div><div class="kpi-small">${team.multiyear} of ${team.recurring} recurring (${team.m24}×24mo, ${team.m18}×18mo)</div></div>
      <div class="kpi-card"><div class="kpi-label">Still Active</div><div class="kpi-value">${team.sold - team.cancelled}</div><div class="kpi-small">${team.cancelled} cancelled (${currency(team.cancelledAcv)} ACV)</div></div>
    `;
    const repRows = data.reps.map((rep) => revenueTableRow(rep, sumRevenueRows(data.rows.filter((r) => r.rep === rep)), { rep })).join("");
    document.getElementById("revenue-rep-table").innerHTML = revenueTable(repRows, revenueTableRow("Team", team, {}, "revenue-total"));
    const months = [...new Set(data.rows.map((r) => r.month))].sort();
    const monthLabel = (m) => new Date(`${m}-15T12:00:00`).toLocaleDateString("en-US", { month: "long", year: "numeric" });
    const monthRows = months.map((m) => revenueTableRow(monthLabel(m), sumRevenueRows(data.rows.filter((r) => r.month === m)), { month: m })).join("");
    document.getElementById("revenue-month-table").innerHTML = revenueTable(monthRows, revenueTableRow("Total", team, {}, "revenue-total"));

    revenueSales = data.sales || [];
    const repSelect = document.getElementById("sales-list-rep");
    if (repSelect.options.length === 1) {
      data.reps.forEach((r) => repSelect.add(new Option(r, r)));
    }
    renderSalesList();

    const inbound = data.inbound;
    if (inbound) {
      const it = sumRevenueRows(inbound.rows);
      document.getElementById("revenue-inbound-note").textContent = `${inbound.label}. ${inbound.othersNote}.`;
      document.getElementById("revenue-inbound-kpi-row").innerHTML = `
        <div class="kpi-card"><div class="kpi-label">Closed/Won</div><div class="kpi-value">${it.sold}</div><div class="kpi-small">${currency(it.acv)} total contract value<br>${currency(it.acv / it.sold)} avg contract value</div></div>
        <div class="kpi-card"><div class="kpi-label">Serviced</div><div class="kpi-value">${it.serviced}</div><div class="kpi-small">${currency(it.servicedAcv)} total contract value<br>${currency(it.servicedAcv / it.serviced)} avg contract value · ${pctLabel(rate(it.serviced, it.sold))}</div></div>
        <div class="kpi-card"><div class="kpi-label">Serviced &amp; Active</div><div class="kpi-value">${it.servicedActive}</div><div class="kpi-small">${currency(it.servicedActiveAcv)} total contract value · ${pctLabel(rate(it.servicedActive, it.sold))}</div></div>
        <div class="kpi-card"><div class="kpi-label">Multiyear</div><div class="kpi-value">${pctLabel(rate(it.multiyear, it.recurring))}</div><div class="kpi-small">${it.multiyear} of ${it.recurring} recurring</div></div>
        <div class="kpi-card"><div class="kpi-label">Cancelled</div><div class="kpi-value">${it.cancelled}</div><div class="kpi-small">${currency(it.cancelledAcv)} ACV</div></div>
      `;
      const inRepRows = inbound.reps.map((rep) => revenueTableRow(rep, sumRevenueRows(inbound.rows.filter((r) => r.rep === rep)), { rep, inbound: true })).join("");
      document.getElementById("revenue-inbound-rep-table").innerHTML = revenueTable(inRepRows, revenueTableRow("Total", it, { inbound: true }, "revenue-total"));
      const inMonths = [...new Set(inbound.rows.map((r) => r.month))].sort();
      const inMonthRows = inMonths.map((m) => revenueTableRow(monthLabel(m), sumRevenueRows(inbound.rows.filter((r) => r.month === m)), { month: m, inbound: true })).join("");
      document.getElementById("revenue-inbound-month-table").innerHTML = revenueTable(inMonthRows, revenueTableRow("Total", it, { inbound: true }, "revenue-total"));
    }
  } catch (e) {
    banner.classList.remove("hidden");
    banner.textContent = `Revenue data: ${e.message}`;
  }
}

function showError(msg) {
  const banner = document.getElementById("error-banner");
  if (!msg) {
    banner.classList.add("hidden");
    banner.textContent = "";
    return;
  }
  banner.classList.remove("hidden");
  banner.textContent = msg;
}

function pctChange(curr, prev) {
  if (prev === null || prev === undefined || prev === 0) return null;
  return ((curr - prev) / prev) * 100;
}

function deltaHTML(change) {
  if (change === null || change === undefined || !isFinite(change)) return "";
  const rounded = Math.round(change * 10) / 10;
  const cls = rounded > 0 ? "up" : rounded < 0 ? "down" : "flat";
  const arrow = rounded > 0 ? "▲" : rounded < 0 ? "▼" : "▬";
  return `<span class="delta ${cls}">${arrow} ${Math.abs(rounded)}%</span>`;
}

function pctLabel(v) {
  return v !== null && v !== undefined && isFinite(v) ? `${Math.round(v * 10) / 10}%` : "—";
}

function rate(numerator, denominator) {
  return denominator ? (numerator / denominator) * 100 : null;
}

function withPrev(currReps, prevReps) {
  const map = {};
  (prevReps || []).forEach((r) => {
    map[r.name] = r;
  });
  return currReps.map((r) => ({ ...r, _prev: map[r.name] || null }));
}

function renderTopKPIRow(curr, prev) {
  const el = document.getElementById("kpi-row-top");
  el.innerHTML = "";
  const items = [
    {
      label: "Total Sales",
      big: `${curr.totals.closedCount}`,
      small: "",
      delta: prev ? deltaHTML(pctChange(curr.totals.closedCount, prev.totals.closedCount)) : "",
    },
    {
      label: "Contract Value",
      big: currency(curr.totals.closedValue),
      small: "",
      delta: prev ? deltaHTML(pctChange(curr.totals.closedValue, prev.totals.closedValue)) : "",
    },
  ];
  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "kpi-card";
    card.innerHTML = `
      <div class="kpi-label">${item.label}</div>
      <div class="kpi-value">${item.big}</div>
      ${item.delta ? `<div class="kpi-delta">${item.delta} vs previous period</div>` : ""}
    `;
    el.appendChild(card);
  });
}

function renderKPIRow(curr, prev, elId = "kpi-row", { includeTotalClosedRate = true } = {}) {
  const el = document.getElementById(elId);
  el.innerHTML = "";

  const leads = curr.totals.leads;
  const qualified = curr.totals.qualifiedLeads;
  const sales = curr.totals.closedCount;
  const qualRate = rate(qualified, leads);
  const saleRate = rate(sales, leads);
  const closeRate = curr.totals.closeRate;

  const prevQualRate = prev ? rate(prev.totals.qualifiedLeads, prev.totals.leads) : null;
  const prevSaleRate = prev ? rate(prev.totals.closedCount, prev.totals.leads) : null;

  const items = [
    {
      label: "Leads",
      big: `${leads}`,
      small: "",
      delta: prev ? deltaHTML(pctChange(leads, prev.totals.leads)) : "",
    },
    {
      label: "Qualified Leads",
      big: pctLabel(qualRate),
      small: `${qualified} qualified`,
      delta: prev && qualRate !== null && prevQualRate !== null ? deltaHTML(qualRate - prevQualRate) : "",
    },
    {
      label: "Sales",
      big: pctLabel(saleRate),
      small: `${sales} sales`,
      delta: prev && saleRate !== null && prevSaleRate !== null ? deltaHTML(saleRate - prevSaleRate) : "",
    },
    {
      label: "Contract Value",
      big: currency(curr.totals.closedValue),
      small: "",
      delta: prev ? deltaHTML(pctChange(curr.totals.closedValue, prev.totals.closedValue)) : "",
    },
    {
      label: "Close Rate",
      big: closeRate !== null ? `${closeRate}%` : "—",
      small: `${sales} sales / ${qualified} qualified`,
      delta:
        prev && closeRate !== null && prev.totals.closeRate !== null
          ? deltaHTML(closeRate - prev.totals.closeRate)
          : "",
    },
  ];

  if (includeTotalClosedRate) {
    items.push({
      label: "Total Closed Rate",
      big: curr.totals.totalClosedRate !== null ? `${curr.totals.totalClosedRate}%` : "—",
      small: `${curr.totals.closedLeads} closed / ${qualified} qualified`,
      delta:
        prev && curr.totals.totalClosedRate !== null && prev.totals.totalClosedRate !== null
          ? deltaHTML(curr.totals.totalClosedRate - prev.totals.totalClosedRate)
          : "",
    });
  }

  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "kpi-card";
    card.innerHTML = `
      <div class="kpi-label">${item.label}</div>
      <div class="kpi-value">${item.big}</div>
      ${item.small ? `<div class="kpi-small">${item.small}</div>` : ""}
      ${item.delta ? `<div class="kpi-delta">${item.delta} vs previous period</div>` : ""}
    `;
    el.appendChild(card);
  });
}

function renderSources(curr, prev) {
  const order = ["Inbound"];
  const items = order.map((name) => ({
    name,
    _prev: prev ? { count: prev.totals.sources[name] || 0 } : null,
    count: curr.totals.sources[name] || 0,
  }));
  renderBarChart("sources-chart", items, {
    onClick: (r) => openChartLeads("team-sources", r.name),
    valueOf: (r) => r.count,
    formatValue: (item) => `${item.value}`,
  });
}

function renderClosedSources(curr, prev) {
  const order = ["Inbound"];
  const items = order.map((name) => ({
    name,
    _prev: prev ? { count: prev.totals.closedSources[name] || 0 } : null,
    count: curr.totals.closedSources[name] || 0,
  }));
  renderBarChart("closed-sources-chart", items, {
    onClick: (r) => openChartLeads("team-closed-sources", r.name),
    valueOf: (r) => r.count,
    formatValue: (item) => `${item.value} sold`,
    showDelta: true,
  });
}

function rangeForLeaderboard(key) {
  const now = businessToday();
  if (key === "week") return { start: startOfWeek(now), end: now };
  if (key === "month") return { start: startOfMonth(now), end: now };
  return { start: now, end: now };
}

function renderLeaderboardList(containerId, reps) {
  const el = document.getElementById(containerId);
  const ranked = reps
    .slice()
    .sort((a, b) => (b.leaderboardCloseRate ?? -1) - (a.leaderboardCloseRate ?? -1));

  if (!ranked.length || !ranked.some((r) => r.closedValue > 0)) {
    el.innerHTML = `<p class="deal-table-empty">No closed contracts yet.</p>`;
    return;
  }

  el.innerHTML = ranked
    .map((rep, i) => {
      const rank = i + 1;
      return `
        <div class="leaderboard-row${rank === 1 ? " leaderboard-first" : ""}">
          <div class="leaderboard-rank">${rank}</div>
          <div class="leaderboard-name">${rep.name}</div>
          <div class="leaderboard-value">${currency(rep.closedValue)}</div>
          <div class="leaderboard-rate">${rep.leaderboardCloseRate !== null ? rep.leaderboardCloseRate + "%" : "—"}</div>
        </div>
      `;
    })
    .join("");
}

async function loadLeaderboardPeriod(key, containerId) {
  const el = document.getElementById(containerId);
  el.innerHTML = `<p class="deal-table-empty">Loading…</p>`;
  try {
    const { start, end } = rangeForLeaderboard(key);
    const data = await fetchJSON(`/api/performance?start=${fmtDate(start)}&end=${fmtDate(end)}`);
    renderLeaderboardList(containerId, data.reps);
  } catch (e) {
    el.innerHTML = `<p class="deal-table-empty">Failed to load: ${e.message}</p>`;
  }
}

function loadLeaderboard() {
  loadLeaderboardPeriod("today", "leaderboard-today-body");
  loadLeaderboardPeriod("week", "leaderboard-week-body");
  loadLeaderboardPeriod("month", "leaderboard-month-body");
}

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function wrapBarLabel(text, maxChars) {
  if (text.length <= maxChars || !text.includes(" ")) return [text];
  const words = text.split(" ");
  const lines = [];
  let current = "";
  words.forEach((w) => {
    const candidate = current ? `${current} ${w}` : w;
    if (candidate.length > maxChars && current) {
      lines.push(current);
      current = w;
    } else {
      current = candidate;
    }
  });
  if (current) lines.push(current);
  return lines;
}

// Charts drawn inside a hidden tab measure 0px wide and render shrunken, so
// redraw each chart whenever its container's width actually changes.
// Small width jitter (e.g. a scrollbar appearing as charts redraw) is ignored
// so redraws can't feed back into each other.
const barChartState = new Map();
const barChartObserver = new ResizeObserver((entries) => {
  requestAnimationFrame(() => {
    for (const entry of entries) {
      const state = barChartState.get(entry.target.id);
      const w = Math.round(entry.target.clientWidth);
      if (state && w > 0 && Math.abs(w - state.width) > 24) {
        renderBarChart(entry.target.id, state.reps, state.opts);
      }
    }
  });
});

function renderHorizontalBars(el, items, opts, width) {
  const rowH = 30, gap = 8, top = items.some((i) => i.previousValue !== null && i.value > 0) ? 26 : 6;
  // Label column sized to the longest name (≈7px per character at 13px), capped at 45% of the width.
  const CHAR_W = 7.1;
  const longest = Math.max(...items.map((i) => i.label.length));
  const labelW = Math.round(Math.min(width * 0.45, Math.max(70, longest * CHAR_W + 8)));
  const maxChars = Math.max(6, Math.floor((labelW - 6) / CHAR_W));
  const valueW = 70;
  const barX = labelW + 12;
  const barMax = Math.max(40, width - barX - valueW - 8);
  const height = top + items.length * (rowH + gap) + 4;
  const rawMax = Math.max(1, ...items.map((i) => i.value), ...items.map((i) => i.previousValue || 0), opts.referenceLineAt || 0);
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, class: "bar-chart bar-chart-h" });
  if (opts.referenceLineAt) {
    const rx = barX + (opts.referenceLineAt / rawMax) * barMax;
    svg.appendChild(svgEl("line", { x1: rx, x2: rx, y1: top - 4, y2: height - 4, class: "bar-ref-line" }));
  }
  items.forEach((item, i) => {
    const y = top + i * (rowH + gap);
    const g = svgEl("g", { class: "bar-group" });
    const label = svgEl("text", { x: labelW, y: y + rowH / 2 + 5, "text-anchor": "end", class: "bar-name-label" });
    label.textContent = item.label.length > maxChars ? item.label.slice(0, maxChars - 1) + "…" : item.label;
    const title = svgEl("title", {});
    title.textContent = item.label;
    label.appendChild(title);
    g.appendChild(label);
    if (item.previousValue !== null && item.value > 0) {
      g.appendChild(svgEl("rect", { x: barX, y: y + 2, width: Math.max((item.previousValue / rawMax) * barMax, 0), height: rowH - 4, rx: 6, class: "bar-ghost-rect" }));
    }
    const w = (item.value / rawMax) * barMax;
    g.appendChild(svgEl("rect", { x: barX, y: y + 4, width: Math.max(w, item.value > 0 ? 3 : 0), height: rowH - 8, rx: 6, fill: "#2f9e3a", class: "bar-fill-rect" }));
    const val = svgEl("text", { x: barX + Math.max(w, (item.previousValue !== null && item.value > 0 ? (item.previousValue / rawMax) * barMax : 0)) + 8, y: y + rowH / 2 + 5, class: "bar-value-label bar-value-label-h" });
    val.textContent = opts.formatValue(item);
    g.appendChild(val);
    if (opts.onClick) {
      const hit = svgEl("rect", { x: 0, y, width, height: rowH, fill: "transparent", class: "bar-hit-rect" });
      hit.addEventListener("click", () => opts.onClick(item.rep));
      g.appendChild(hit);
    }
    svg.appendChild(g);
  });
  if (items.some((i) => i.previousValue !== null && i.value > 0)) {
    const lx = width - 110;
    svg.appendChild(svgEl("rect", { x: lx, y: 4, width: 12, height: 12, rx: 3, class: "bar-ghost-rect" }));
    const lt = svgEl("text", { x: lx + 18, y: 14, class: "bar-ref-label" });
    lt.textContent = "Previous period";
    svg.appendChild(lt);
  }
  el.appendChild(svg);
}

function renderBarChart(containerId, reps, opts) {
  const el = document.getElementById(containerId);
  el.innerHTML = "";
  if (!barChartState.has(containerId)) barChartObserver.observe(el);
  barChartState.set(containerId, { reps, opts, width: Math.round(el.clientWidth) });

  const items = reps
    .map((r) => ({
      rep: r,
      label: r.name,
      value: opts.valueOf(r) || 0,
      previousValue: r._prev ? opts.valueOf(r._prev) || 0 : null,
    }))
    .sort((a, b) => b.value - a.value);

  // Many bars or long names overlap as columns, so those charts are drawn as
  // horizontal rows instead (Mika: "fix the visuals", 2026-10-27).
  const probeWidth = Math.max(320, Math.round(el.clientWidth) || 640);
  const probeBar = (probeWidth - 40 - 32 * (items.length - 1)) / Math.max(1, items.length);
  const crowded =
    items.length > 5 ||
    (probeWidth < 460 && items.length > 3) ||
    items.some((it) => wrapBarLabel(it.label, Math.max(6, Math.floor(probeBar / 8))).length > 2);
  if (crowded && !opts.vertical) {
    renderHorizontalBars(el, items, opts, probeWidth);
    return;
  }

  const width = Math.max(320, Math.round(el.clientWidth) || 640);
  const padding = { top: opts.showDelta ? 58 : 36, right: 20, bottom: 40, left: 20 };
  const chartWidth = width - padding.left - padding.right;
  const barGap = 32;
  const barWidth = (chartWidth - barGap * (items.length - 1)) / items.length;

  const maxCharsPerLine = Math.max(6, Math.floor(barWidth / 8));
  const labelLines = items.map((item) => wrapBarLabel(item.label, maxCharsPerLine));
  const maxLabelLines = Math.max(1, ...labelLines.map((lines) => lines.length));
  padding.bottom += (maxLabelLines - 1) * 16;

  const height = 300 + (maxLabelLines - 1) * 16;
  const chartHeight = height - padding.top - padding.bottom;
  const baselineY = padding.top + chartHeight;

  const rawMax = Math.max(1, ...items.map((i) => i.value), ...items.map((i) => i.previousValue || 0), opts.referenceLineAt || 0);
  const maxValue = rawMax * 1.15;

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, class: "bar-chart" });

  [0.25, 0.5, 0.75].forEach((frac) => {
    const gy = baselineY - frac * chartHeight;
    svg.appendChild(svgEl("line", { x1: padding.left, x2: width - padding.right, y1: gy, y2: gy, class: "bar-grid-line" }));
  });

  items.forEach((item, i) => {
    const x = padding.left + i * (barWidth + barGap);
    const group = svgEl("g", { class: "bar-group" });

    if (item.previousValue !== null && item.value > 0) {
      const ghostHeight = (item.previousValue / maxValue) * chartHeight;
      group.appendChild(
        svgEl("rect", { x, y: baselineY - ghostHeight, width: barWidth, height: Math.max(ghostHeight, 0), rx: 6, class: "bar-ghost-rect" })
      );
    }

    const barHeight = (item.value / maxValue) * chartHeight;
    const y = baselineY - barHeight;
    const bar = svgEl("rect", { x, y, width: barWidth, height: Math.max(barHeight, 0), rx: 8, fill: "#2f9e3a", class: "bar-fill-rect" });
    group.appendChild(bar);

    // Sit labels above whichever is taller — this period's bar or last period's outline.
    const ghostTop = item.previousValue !== null && item.value > 0 ? baselineY - (item.previousValue / maxValue) * chartHeight : baselineY;
    const labelY = Math.min(y, ghostTop);
    const valueLabel = svgEl("text", { x: x + barWidth / 2, y: labelY - 10, "text-anchor": "middle", class: "bar-value-label" });
    valueLabel.textContent = opts.formatValue(item);
    group.appendChild(valueLabel);

    if (opts.showDelta && item.previousValue !== null && item.previousValue !== 0) {
      const change = ((item.value - item.previousValue) / item.previousValue) * 100;
      const rounded = Math.round(change * 10) / 10;
      const cls = rounded > 0 ? "bar-delta-up" : rounded < 0 ? "bar-delta-down" : "bar-delta-flat";
      const arrow = rounded > 0 ? "▲" : rounded < 0 ? "▼" : "▬";
      const deltaLabel = svgEl("text", {
        x: x + barWidth / 2,
        y: labelY - 32,
        "text-anchor": "middle",
        class: `bar-delta-label ${cls}`,
      });
      deltaLabel.textContent = `${arrow} ${Math.abs(rounded)}% vs prev`;
      group.appendChild(deltaLabel);
    }

    const nameLabel = svgEl("text", { x: x + barWidth / 2, y: baselineY + 22, "text-anchor": "middle", class: "bar-name-label" });
    const lines = labelLines[i];
    if (lines.length === 1) {
      nameLabel.textContent = lines[0];
    } else {
      lines.forEach((line, lineIdx) => {
        const tspan = svgEl("tspan", { x: x + barWidth / 2, dy: lineIdx === 0 ? 0 : 16 });
        tspan.textContent = line;
        nameLabel.appendChild(tspan);
      });
    }
    group.appendChild(nameLabel);

    if (opts.onClick) {
      const hitRect = svgEl("rect", {
        x,
        y: padding.top,
        width: barWidth,
        height: chartHeight,
        fill: "transparent",
        class: "bar-hit-rect",
      });
      hitRect.addEventListener("click", () => opts.onClick(item.rep));
      group.appendChild(hitRect);
    }
    svg.appendChild(group);
  });

  svg.appendChild(svgEl("line", { x1: padding.left, x2: width - padding.right, y1: baselineY, y2: baselineY, class: "bar-axis-line" }));

  if (items.some((item) => item.previousValue !== null && item.value > 0)) {
    const legendX = width - padding.right - 104;
    svg.appendChild(svgEl("rect", { x: legendX, y: 4, width: 12, height: 12, rx: 3, class: "bar-ghost-rect" }));
    const legendLabel = svgEl("text", { x: legendX + 18, y: 14, class: "bar-ref-label" });
    legendLabel.textContent = "Previous period";
    svg.appendChild(legendLabel);
  }

  if (opts.referenceLineAt) {
    const refY = baselineY - (opts.referenceLineAt / maxValue) * chartHeight;
    svg.appendChild(svgEl("line", { x1: padding.left, x2: width - padding.right, y1: refY, y2: refY, class: "bar-ref-line" }));
    const refLabel = svgEl("text", { x: width - padding.right, y: refY - 4, "text-anchor": "end", class: "bar-ref-label" });
    refLabel.textContent = opts.referenceLabel || "";
    svg.appendChild(refLabel);
  }

  el.appendChild(svg);
}

async function fetchJSON(url) {
  const res = await fetch(url);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

function formatDealDate(isoDate) {
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

// --- Chart drill-down: click a bar → the leads behind it, each linking to HighLevel.
const ghlContactUrl = (locationId, contactId) =>
  `https://app.gohighlevel.com/v2/location/${locationId}/contacts/detail/${contactId}`;

function renderLeadRows(rows, emptyText) {
  if (!rows.length) return `<p class="deal-table-empty">${escapeHTML(emptyText || "No leads for this bar in this date range.")}</p>`;
  const body = rows
    .map((r) => {
      const name = escapeHTML(r.name || "Unnamed");
      const link = r.contactId && r.locationId
        ? `<a class="lead-link" href="${ghlContactUrl(r.locationId, r.contactId)}" target="_blank" rel="noopener">${name}</a>`
        : name;
      const detail = r.detail ? `<div class="lead-detail">${escapeHTML(r.detail)}</div>` : "";
      return `<tr><td>${link}${detail}</td><td class="nowrap lead-date">${r.date ? formatDealDate(r.date) : ""}</td></tr>`;
    })
    .join("");
  return `<p class="lead-count">${rows.length} lead${rows.length === 1 ? "" : "s"} · click a name to open it in HighLevel</p>
    <table class="deal-table lead-table"><thead><tr><th>Lead</th><th class="lead-date">Date</th></tr></thead><tbody>${body}</tbody></table>`;
}

function showLeadsModal(title, html) {
  document.getElementById("rep-deals-title").textContent = title;
  document.getElementById("rep-deals-body").innerHTML = html;
  document.getElementById("rep-deals-modal").classList.remove("hidden");
}

let chartLeadsToken = 0;
// period: "current" or "previous" (the outline bars when "Compare to previous period" is on).
async function openChartLeads(chart, key, channel, period = "current") {
  const token = ++chartLeadsToken;
  const compare = document.getElementById("compare-checkbox").checked;
  let range = { start: currentRange.start, end: currentRange.end };
  if (period === "previous") range = previousPeriodFor(currentRangeKey, currentRange.start, currentRange.end);
  const fmtShort = (d) => d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  const label = (r) => (isSameDay(r.start, r.end) ? fmtShort(r.start) : `${fmtShort(r.start)} – ${fmtShort(r.end)}`);
  let toggle = "";
  if (compare) {
    const prev = previousPeriodFor(currentRangeKey, currentRange.start, currentRange.end);
    const btn = (p, text, r) =>
      `<button class="period-btn${p === period ? " active" : ""}" data-period="${p}">${text}<span>${label(r)}</span></button>`;
    toggle = `<div class="period-toggle">${btn("current", "This period", currentRange)}${btn("previous", "Previous period", prev)}</div>`;
  }
  const wire = () =>
    document.querySelectorAll("#rep-deals-body .period-btn").forEach((b) =>
      b.addEventListener("click", () => openChartLeads(chart, key, channel, b.dataset.period))
    );
  showLeadsModal(`${key}`, `${toggle}<p class="deal-table-empty">Loading…</p>`);
  wire();
  try {
    const q = new URLSearchParams({ chart, key, start: fmtDate(range.start), end: fmtDate(range.end) });
    if (channel) q.set("channel", channel);
    const data = await fetchJSON(`/api/chart-leads?${q}`);
    if (token !== chartLeadsToken) return;
    showLeadsModal(data.title + (period === "previous" ? " (previous period)" : ""), toggle + renderLeadRows(data.rows));
    wire();
  } catch (e) {
    if (token !== chartLeadsToken) return;
    showLeadsModal(key, `${toggle}<p class="deal-table-empty">Failed to load: ${escapeHTML(e.message)}</p>`);
    wire();
  }
}

// Inbound call log charts: the calls are already on the page, so filter them here.
function openCallLogLeads(field, label, title) {
  if (!callLogData) return;
  // Same rule as the chart: only audited calls are counted.
  const calls = callLogData.calls.filter((c) => c.audited && ((c[field] ?? null) === null ? "—" : c[field]) === label);
  const seen = new Map();
  calls.forEach((c) => {
    const k = c.contactId || c.name;
    if (!seen.has(k)) seen.set(k, { name: c.name, contactId: c.contactId, locationId: callLogData.locationId, detail: [c.callType, c.outcome].filter((x) => x && x !== "N/A").join(" · "), date: c.date, n: 0 });
    seen.get(k).n += 1;
  });
  const rows = [...seen.values()].map((r) => ({ ...r, detail: `${r.n} call${r.n === 1 ? "" : "s"}${r.detail ? " · " + r.detail : ""}` }));
  showLeadsModal(`${title} — ${label}`, renderLeadRows(rows));
}

function closeRepDealsModal() {
  document.getElementById("rep-deals-modal").classList.add("hidden");
}

async function openRepDealsModal(repName, endpoint = "/api/rep-deals", titleSuffix = "Sold Contracts") {
  const modal = document.getElementById("rep-deals-modal");
  const body = document.getElementById("rep-deals-body");
  document.getElementById("rep-deals-title").textContent = `${repName} — ${titleSuffix}`;
  body.innerHTML = `<p class="deal-table-empty">Loading…</p>`;
  modal.classList.remove("hidden");

  try {
    const start = fmtDate(currentRange.start);
    const end = fmtDate(currentRange.end);
    const data = await fetchJSON(`${endpoint}?rep=${encodeURIComponent(repName)}&start=${start}&end=${end}`);
    if (!data.deals.length) {
      body.innerHTML = `<p class="deal-table-empty">No closed contracts in this date range.</p>`;
      return;
    }
    const rows = data.deals
      .map(
        (d) => `<tr>
          <td>${d.name}</td>
          <td>${currency(d.value)}</td>
          <td>${formatDealDate(d.closedDate)}</td>
        </tr>`
      )
      .join("");
    body.innerHTML = `
      <table class="deal-table">
        <thead><tr><th>Lead</th><th>Contract Value</th><th>Closed Date</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  } catch (e) {
    body.innerHTML = `<p class="deal-table-empty">Failed to load: ${e.message}</p>`;
  }
}

async function openQualifiedLeadsModal(repName) {
  const modal = document.getElementById("rep-deals-modal");
  const body = document.getElementById("rep-deals-body");
  document.getElementById("rep-deals-title").textContent = `${repName} — Qualified Leads`;
  body.innerHTML = `<p class="deal-table-empty">Loading…</p>`;
  modal.classList.remove("hidden");

  try {
    const start = fmtDate(currentRange.start);
    const end = fmtDate(currentRange.end);
    const data = await fetchJSON(
      `/api/inbound-qualified-leads?rep=${encodeURIComponent(repName)}&start=${start}&end=${end}`
    );
    if (!data.leads.length) {
      body.innerHTML = `<p class="deal-table-empty">No qualified leads in this date range.</p>`;
      return;
    }
    const rows = data.leads
      .map(
        (l) => `<tr>
          <td>${l.name}</td>
          <td>${formatDealDate(l.dateAdded)}</td>
        </tr>`
      )
      .join("");
    body.innerHTML = `
      <table class="deal-table">
        <thead><tr><th>Lead</th><th>Date Added</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  } catch (e) {
    body.innerHTML = `<p class="deal-table-empty">Failed to load: ${e.message}</p>`;
  }
}

document.getElementById("rep-deals-close").addEventListener("click", closeRepDealsModal);
document.getElementById("rep-deals-modal").addEventListener("click", (e) => {
  if (e.target.id === "rep-deals-modal") closeRepDealsModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeRepDealsModal();
    hideCalendarPopup();
  }
});

function renderColdOutbound(data) {
  const counts = data.callerCounts.counts || {};
  const callers = Object.keys(counts).map((name) => ({ name, count: counts[name] }));
  const totalCalls = callers.reduce((sum, c) => sum + c.count, 0);

  const kpiEl = document.getElementById("cold-outbound-kpi-row");
  kpiEl.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">Total Contacts Called</div>
      <div class="kpi-value">${totalCalls}</div>
    </div>
  `;

  renderBarChart("cold-outbound-chart", callers, {
    onClick: (r) => openChartLeads("co-callers", r.name),
    valueOf: (r) => r.count,
    formatValue: (item) => `${item.value}`,
  });

  const dispoCounts = data.dispositionCounts.counts || {};
  const dispositions = Object.keys(dispoCounts).map((name) => ({ name, count: dispoCounts[name] }));
  renderBarChart("cold-outbound-disposition-chart", dispositions, {
    onClick: (r) => openChartLeads("co-dispositions", r.name),
    valueOf: (r) => r.count,
    formatValue: (item) => `${item.value}`,
  });
}

async function loadColdOutbound(start, end) {
  const banner = document.getElementById("cold-outbound-error-banner");
  banner.classList.add("hidden");
  banner.textContent = "";
  try {
    const data = await fetchJSON(`/api/cold-outbound?start=${start}&end=${end}`);
    renderColdOutbound(data);
  } catch (e) {
    banner.classList.remove("hidden");
    banner.textContent = `Cold Outbound data: ${e.message}`;
  }
}

async function loadInboundCalls(start, end) {
  const banner = document.getElementById("inbound-calls-error-banner");
  banner.classList.add("hidden");
  banner.textContent = "";
  try {
    const data = await fetchJSON(`/api/inbound-calls?start=${start}&end=${end}`);
    renderKPIRow(data, null, "inbound-kpi-row", { includeTotalClosedRate: false });
    renderBarChart("inbound-sold-chart", data.reps, {
      valueOf: (r) => r.closedCount,
      formatValue: (item) => `${item.value} sold`,
      onClick: (rep) => openChartLeads("inbound-deals", rep.name),
    });
    renderBarChart("inbound-qualified-chart", data.reps, {
      valueOf: (r) => r.qualifiedLeads,
      formatValue: (item) => `${item.value}`,
      onClick: (rep) => openChartLeads("inbound-qualified", rep.name),
    });
    renderBarChart("inbound-close-rate-chart", data.reps, {
      onClick: (rep) => openChartLeads("inbound-close", rep.name),
      valueOf: (r) => r.qualifiedCloseRate || 0,
      formatValue: (item) => (item.rep.qualifiedCloseRate !== null ? `${item.rep.qualifiedCloseRate}%` : "—"),
      referenceLineAt: 100,
      referenceLabel: "100%",
    });
  } catch (e) {
    banner.classList.remove("hidden");
    banner.textContent = `Inbound Calls data: ${e.message}`;
  }
}

function escapeHTML(text) {
  return String(text ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
}

function countsToBars(counts) {
  return Object.keys(counts || {}).map((name) => ({ name: name === "null" ? "—" : name, count: counts[name] }));
}

function formatCallDuration(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

let callLogData = null;

function renderCallLogTable() {
  const el = document.getElementById("call-log-table");
  if (!callLogData) return;
  const filter = document.getElementById("call-log-filter").value;
  const calls = callLogData.calls.filter((c) => {
    if (filter === "Sales lead") return c.callType === "Sales lead";
    if (filter === "conversations") return c.callType && c.callType !== "No conversation";
    return true;
  });
  if (!calls.length) {
    el.innerHTML = `<p class="deal-table-empty">No inbound calls in this date range.</p>`;
    return;
  }
  const contactUrl = (id) => `https://app.gohighlevel.com/v2/location/${callLogData.locationId}/contacts/detail/${id}`;
  const pill = (cls, text) => (text && text !== "N/A" ? `<span class="call-pill ${cls}">${escapeHTML(text)}</span>` : "");
  const rows = calls
    .map(
      (c) => `<tr>
        <td class="nowrap">${formatDealDate(c.date)}<br><span class="kpi-sub">${escapeHTML(c.time)}</span></td>
        <td>${c.contactId ? `<a href="${contactUrl(c.contactId)}" target="_blank" rel="noopener">${escapeHTML(c.name)}</a>` : escapeHTML(c.name)}</td>
        <td class="num">${formatCallDuration(c.duration)}</td>
        <td class="nowrap">${c.audited ? escapeHTML(c.handledBy) : `<span class="kpi-sub">Not audited</span>`}</td>
        <td class="nowrap">${escapeHTML(c.callType || "")}${c.pest ? `<br><span class="kpi-sub">${escapeHTML(c.pest)}</span>` : ""}</td>
        <td>${pill(`q-${c.qualified}`, c.qualified)}</td>
        <td>${pill(`o-${c.outcome}`, c.outcome)}${c.price ? `<br><span class="kpi-sub">${escapeHTML(c.price)}</span>` : ""}</td>
        <td class="call-summary">${escapeHTML(c.summary || "")}${c.notes ? `<span class="call-notes">${escapeHTML(c.notes)}</span>` : ""}</td>
      </tr>`
    )
    .join("");
  el.innerHTML = `
    <table class="deal-table call-log-table">
      <thead><tr><th>When</th><th>Caller</th><th class="num">Length</th><th>Handled By</th><th>Type</th><th>Qualified</th><th>Outcome</th><th>Summary</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}


async function loadInboundCallLog(start, end) {
  const banner = document.getElementById("inbound-calls-error-banner");
  try {
    const data = await fetchJSON(`/api/inbound-call-log?start=${start}&end=${end}`);
    callLogData = data;
    const auditNote =
      data.audited < data.totalCalls ? `<div class="kpi-small">${data.totalCalls - data.audited} not audited yet</div>` : "";
    document.getElementById("call-log-kpi-row").innerHTML = `
      <div class="kpi-card"><div class="kpi-label">Inbound Calls</div><div class="kpi-value">${data.totalCalls}</div><div class="kpi-small">${data.uniqueCallers} unique callers</div></div>
      <div class="kpi-card"><div class="kpi-label">Connected</div><div class="kpi-value">${data.connected}</div><div class="kpi-small">${data.talkMinutes} min talk time</div></div>
      <div class="kpi-card"><div class="kpi-label">Sales Lead Calls</div><div class="kpi-value">${data.salesLeads}</div>${auditNote}</div>
      <div class="kpi-card"><div class="kpi-label">Qualified</div><div class="kpi-value">${data.qualified}</div><div class="kpi-small">${pctLabel(rate(data.qualified, data.salesLeads))} of sales leads</div></div>
      <div class="kpi-card"><div class="kpi-label">Sold on the Call</div><div class="kpi-value">${data.sold}</div><div class="kpi-small">${pctLabel(rate(data.sold, data.qualified))} of qualified</div></div>
    `;
    const countChart = (id, counts, field, title) =>
      renderBarChart(id, countsToBars(counts), {
        valueOf: (r) => r.count,
        formatValue: (item) => `${item.value}`,
        onClick: (r) => openCallLogLeads(field, r.name, title),
      });
    countChart("call-handler-chart", data.byHandler, "handledBy", "Handled By");
    countChart("call-type-chart", data.byCallType, "callType", "Call Type");
    countChart("call-outcome-chart", data.byOutcome, "outcome", "Outcome");
    renderCallLogTable();
  } catch (e) {
    banner.classList.remove("hidden");
    banner.textContent = `Inbound call log: ${e.message}`;
  }
}

let loadToken = 0;

function setLoading(isLoading) {
  document.getElementById("loading-indicator").classList.toggle("hidden", !isLoading);
  document.querySelectorAll(".range-btn, #compare-checkbox").forEach((el) => {
    el.disabled = isLoading;
  });
}

async function loadData() {
  if (!currentRange.start) return;
  const token = ++loadToken;
  showError(null);
  setLoading(true);
  const start = fmtDate(currentRange.start);
  const end = fmtDate(currentRange.end);
  const compareEnabled = document.getElementById("compare-checkbox").checked;

  loadPowerDialer(start, end); // fire independently — can be slow on a cold cache, shouldn't block the rest
  loadSalesOverview(start, end); // fire independently — same reasoning
  loadCommercial(start, end);

  try {
    let curr;
    let prev = null;
    if (compareEnabled) {
      const prevRange = previousPeriodFor(currentRangeKey, currentRange.start, currentRange.end);
      const combined = await fetchJSON(
        `/api/performance-compare?start=${start}&end=${end}&prevStart=${fmtDate(prevRange.start)}&prevEnd=${fmtDate(prevRange.end)}`
      );
      curr = combined.current;
      prev = combined.previous;
    } else {
      curr = await fetchJSON(`/api/performance?start=${start}&end=${end}`);
    }

    if (token !== loadToken) return; // superseded by a newer request

    renderTopKPIRow(curr, prev);
    renderKPIRow(curr, prev);
    renderSources(curr, prev);
    renderClosedSources(curr, prev);

    const repsWithPrev = withPrev(curr.reps, prev ? prev.reps : null);

    renderBarChart("sold-services-chart", repsWithPrev, {
      valueOf: (r) => r.closedCount,
      formatValue: (item) => `${item.value} sold`,
      showDelta: true,
      onClick: (rep) => openChartLeads("team-deals", rep.name),
    });

    renderBarChart("sales-share", repsWithPrev, {
      valueOf: (r) => r.closedValue,
      formatValue: (item) => `${currency(item.value)} (${item.rep.pctOfTotal}%)`,
      showDelta: true,
      onClick: (rep) => openChartLeads("team-deals", rep.name),
    });

    renderBarChart("qualified-leads-chart", repsWithPrev, {
      valueOf: (r) => r.qualifiedLeads,
      formatValue: (item) => `${item.value}`,
      onClick: (rep) => openChartLeads("team-qualified", rep.name),
    });

    renderBarChart("close-rate-chart", repsWithPrev, {
      onClick: (rep) => openChartLeads("team-close", rep.name),
      valueOf: (r) => r.closeRate || 0,
      formatValue: (item) => (item.rep.closeRate !== null ? `${item.rep.closeRate}%` : "—"),
      referenceLineAt: 100,
      referenceLabel: "100%",
    });

  } catch (e) {
    if (token === loadToken) showError(`Performance data: ${e.message}`);
  } finally {
    if (token === loadToken) setLoading(false);
  }
}

// default to today on load
setActiveButton("today");
applyRange("today", businessToday(), businessToday());
loadLeaderboard();
