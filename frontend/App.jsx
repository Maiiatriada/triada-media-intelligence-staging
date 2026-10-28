import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

/* ==========================================================================
   TRIADA MEDIA INTELLIGENCE
   Step 1: Foundation (layout shell)
   Step 2: Mentions Library (database table)
   Step 3: Live data from the collector's mentions.json (auto-refresh + status filters)
   Step 4: Filter bar UI (Subject / Date Range / Sort By: selection only, no filtering yet)
   Step 5: Custom Range calendar popup in the Date Range filter
   Audit fixes: Platform spellings (Twitter -> X), Subject from categories, Date Range by calendar day,
                status chips and badges restored, "Show all mentions" resets every filter

   Structure (easy to extend in later steps):
   - NAV_ITEMS         -> add a tab here to add a section
   - VIEWS             -> map a tab id to the component that renders it
   - Mentions data layer -> fetchMentions(), useMentions()
   - <Header />        -> brand + navigation
   - <MainContent/>    -> renders the active view
   ========================================================================== */

/* ---------- Configuration ---------- */

const APP = {
  title: "TRIADA MEDIA INTELLIGENCE",
  subtitle: "Media Monitoring & Intelligence Platform",
};

const NAV_ITEMS = [
  { id: "overview", label: "Overview" },
  { id: "mentions", label: "Mentions Library" },
];

/* ---------- Mentions data layer ---------- */

// Public database written by the Python collector (GitHub Actions).
const MENTIONS_URL =
  "https://raw.githubusercontent.com/Maiiatriada/triada-media-intelligence/main/mentions_public.json";
const REFRESH_INTERVAL_MS = 60 * 1000; // Refresh updated Instagram records within one minute.
const REQUEST_TIMEOUT_MS = 20 * 1000;

// Relevance statuses the collector uses today, in display order.
// Any other status found in the data is kept and shown as well.
const KNOWN_STATUSES = ["relevant", "review", "irrelevant"];

/**
 * A record as stored in mentions_public.json:
 * { id, title, source, published_at, url, keyword, first_seen_at?, status,
 *   categories, platform?, subject?, source_type?, keywords?, urls?, legacy_ids? }
 * `platform` is missing on records written before it existed; those are Media.
 */

class LoadError extends Error {
  constructor(kind, message) {
    super(message);
    this.kind = kind; // "http" | "format" | "network" | "timeout"
  }
}

function isHttpUrl(value) {
  try {
    const { protocol } = new URL(value);
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

function toTime(value) {
  const time = Date.parse(value);
  return Number.isNaN(time) ? null : time;
}

// The platforms the Platform filter offers, in display order. The dropdown is built from this
// list only: it is strictly these entries. `value` is the name stored in the data; `aliases` are
// other spellings that mean the same platform (lower case), e.g. Twitter and "X / Twitter" are X.
const PLATFORMS = [
  { value: "Media", label: "Media", aliases: ["media"] },
  { value: "Facebook", label: "Facebook", aliases: ["facebook"] },
  { value: "Instagram", label: "Instagram", aliases: ["instagram"] },
  { value: "X", label: "X", aliases: ["x", "twitter", "x / twitter", "x/twitter"] },
  { value: "LinkedIn", label: "LinkedIn", aliases: ["linkedin"] },
  { value: "YouTube", label: "YouTube", aliases: ["youtube"] },
];
const DEFAULT_PLATFORM = "Media";

/**
 * The platform of a record. Records with no platform value are Media. Known spellings are
 * mapped to the official name; an unrecognised value is kept as written, so it is never
 * mistaken for Media (it is simply not selectable in the dropdown).
 */
function normalizePlatform(value) {
  const text = value === null || value === undefined ? "" : String(value).trim();
  if (!text) return DEFAULT_PLATFORM;
  const key = text.toLowerCase();
  const known = PLATFORMS.find((p) => p.value.toLowerCase() === key || p.aliases.includes(key));
  return known ? known.value : text;
}

/** Maps one raw record to the shape the UI uses. Returns null if unusable. */
function normalizeRecord(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const url = typeof raw.url === "string" ? raw.url.trim() : "";
  return {
    id: String(raw.id ?? url),
    title: String(raw.title ?? "").trim(),
    source: String(raw.source ?? "").trim(),
    publishedAt: typeof raw.published_at === "string" ? raw.published_at : "",
    url: isHttpUrl(url) ? url : "",
    status: raw.status ? String(raw.status).trim().toLowerCase() : "unknown",
    subject: raw.subject ?? null,
    sourceType: raw.source_type ?? "",
    platform: normalizePlatform(raw.platform),
    keyword: raw.keyword ?? "",
    categories: Array.isArray(raw.categories) ? raw.categories.map(String) : [],
    firstSeenAt: typeof raw.first_seen_at === "string" ? raw.first_seen_at : "",
  };
}

/** Newest publication date first; undated records last. */
function sortNewestFirst(records) {
  return [...records].sort((a, b) => {
    const ta = toTime(a.publishedAt);
    const tb = toTime(b.publishedAt);
    if (ta === null && tb !== null) return 1;
    if (ta !== null && tb === null) return -1;
    if (ta !== tb && ta !== null && tb !== null) return tb - ta;
    return (toTime(b.firstSeenAt) ?? 0) - (toTime(a.firstSeenAt) ?? 0);
  });
}

/** Downloads mentions.json and returns every valid record, sorted. */
async function fetchMentions(signal) {
  // The timestamp makes each request unique, so GitHub's 5-minute cache is skipped.
  // GitHub's raw-content CDN can serve a stale JSON blob despite query cache busters.
  // Read the current branch through the GitHub Contents API first, then fall back to raw.
  let json;
  try {
    const apiUrl = "https://api.github.com/repos/Maiiatriada/triada-media-intelligence/contents/mentions_public.json?ref=main&t=" + Date.now();
    const apiResponse = await fetch(apiUrl, { cache: "no-store", signal, headers: { Accept: "application/vnd.github+json" } });
    if (!apiResponse.ok) throw new Error("GitHub API returned " + apiResponse.status);
    const file = await apiResponse.json();
    if (!file.content || file.encoding !== "base64") throw new Error("Unexpected GitHub content encoding");
    const bytes = Uint8Array.from(atob(file.content.replace(/\\s/g, "")), c => c.charCodeAt(0));
    json = JSON.parse(new TextDecoder("utf-8").decode(bytes));
  } catch (apiError) {
    if (signal?.aborted) throw apiError;
    const response = await fetch(`${MENTIONS_URL}?t=${Date.now()}`, { cache: "no-store", signal });
    if (!response.ok) throw new LoadError("http", `The data source answered with HTTP ${response.status}.`);
    try { json = await response.json(); }
    catch { throw new LoadError("format", "The data file is not valid JSON."); }
  }
  if (!Array.isArray(json)) {
    throw new LoadError("format", "Unexpected data format: expected a list of mentions.");
  }

  const records = json.map(normalizeRecord).filter(Boolean);
  return { records: sortNewestFirst(records), skipped: json.length - records.length };
}

function describeError(err, timedOut) {
  if (timedOut) {
    return { kind: "timeout", message: "The data source did not answer within 20 seconds." };
  }
  if (err instanceof LoadError) return { kind: err.kind, message: err.message };
  return { kind: "network", message: "The browser could not reach the data source." };
}

/**
 * Loads the database when the page opens and again every 5 minutes.
 * If a refresh fails, the last successful data stays on screen.
 */
function useMentions() {
  const [state, setState] = useState({
    phase: "loading", // "loading" | "ready" | "error"
    records: [],
    skipped: 0,
    error: null,
    lastUpdated: null,
    refreshing: true,
  });
  const requestId = useRef(0);
  const controllerRef = useRef(null);

  const refresh = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const id = ++requestId.current;

    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT_MS);

    setState((prev) => ({ ...prev, refreshing: true }));
    try {
      const { records, skipped } = await fetchMentions(controller.signal);
      if (id !== requestId.current) return; // a newer request replaced this one
      setState({
        phase: "ready",
        records,
        skipped,
        error: null,
        lastUpdated: new Date(),
        refreshing: false,
      });
    } catch (err) {
      if (id !== requestId.current) return;
      setState((prev) => ({
        ...prev,
        phase: prev.lastUpdated ? "ready" : "error",
        error: describeError(err, timedOut),
        refreshing: false,
      }));
    } finally {
      clearTimeout(timer);
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, REFRESH_INTERVAL_MS);
    return () => {
      clearInterval(interval);
      requestId.current += 1; // ignore anything still in flight
      controllerRef.current?.abort();
    };
  }, [refresh]);

  return { ...state, refresh };
}

/* ---------- Views ---------- */

function DashboardPlaceholder() {
  return (
    <section className="tmi-panel" aria-live="polite">
      <p className="tmi-panel-text">
        Your media intelligence dashboard is being built.
      </p>
    </section>
  );
}

/* Mentions Library */

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

function formatDateTimeUtc(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  })} UTC`;
}

function formatClock(date) {
  return date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

function statusLabel(status) {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function StatusBadge({ status }) {
  const known = KNOWN_STATUSES.includes(status) ? status : "other";
  return <span className={`tmi-badge tmi-badge-${known}`}>{statusLabel(status)}</span>;
}

// Column definitions drive the table, so adding or reordering columns is one edit.
const MENTION_COLUMNS = [
  {
    key: "publishedAt",
    label: "Publication Date",
    className: "tmi-col-date",
    render: (m) => formatDate(m.publishedAt) || "Unknown",
  },
  {
    key: "platform",
    label: "Platform",
    className: "tmi-col-platform",
    render: (m) => m.platform,
  },
  {
    key: "source",
    label: "Media Source",
    className: "tmi-col-source",
    render: (m) => m.source || "Unknown",
  },
  {
    key: "title",
    label: "Article Title",
    className: "tmi-col-title",
    render: (m) => <span>{m.title || "Untitled"}</span>,
  },
  {
    key: "url",
    label: "Original Article Link",
    className: "tmi-col-link",
    render: (m) =>
      m.url ? (
        <a
          className="tmi-link"
          href={m.url}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Open article: ${m.title}`}
        >
          Open article
        </a>
      ) : (
        <span className="tmi-muted">No link</span>
      ),
  },
];

function buildFilterOptions(records) {
  const counts = {};
  for (const record of records) counts[record.status] = (counts[record.status] ?? 0) + 1;

  const extra = Object.keys(counts)
    .filter((status) => !KNOWN_STATUSES.includes(status))
    .sort();

  return [
    { value: "all", label: "All", count: records.length },
    ...[...KNOWN_STATUSES, ...extra].map((status) => ({
      value: status,
      label: statusLabel(status),
      count: counts[status] ?? 0,
    })),
  ];
}

function latestDiscovery(records) {
  let latest = null;
  for (const record of records) {
    const time = toTime(record.firstSeenAt);
    if (time !== null && (latest === null || time > latest)) latest = time;
  }
  return latest;
}

function StateBlock({ title, text, tone, children }) {
  return (
    <div className="tmi-empty" role={tone === "error" ? "alert" : "status"}>
      <p className={`tmi-empty-title${tone === "error" ? " tmi-error-text" : ""}`}>{title}</p>
      {text && <p className="tmi-empty-text">{text}</p>}
      {children}
    </div>
  );
}

/* Filter bar: Subject / Platform / Date Range / Sort By (applied by applyFilters below) */

// The first option of each list is the default.
const SUBJECT_OPTIONS = [
  { value: "all", label: "All Mentions" },
  { value: "triada-robotics", label: "TRIADA Robotics" },
  { value: "artem-veselov", label: "Artem Veselov" },
];

// Strictly "All Platforms" plus the PLATFORMS list; nothing is added from the data.
const PLATFORM_OPTIONS = [
  { value: "all", label: "All Platforms" },
  ...PLATFORMS.map((p) => ({ value: p.value, label: p.label })),
];

const DATE_RANGE_OPTIONS = [
  { value: "all-time", label: "All Time" },
  { value: "last-7-days", label: "Last 7 Days" },
  { value: "last-30-days", label: "Last 30 Days" },
  { value: "custom", label: "Custom Range" },
];

const SORT_OPTIONS = [
  { value: "newest", label: "Newest First" },
  { value: "oldest", label: "Oldest First" },
];

function FilterSelect({ id, label, value, options, onChange }) {
  // Highlighted in yellow whenever it differs from its default option.
  const isActive = value !== options[0].value;
  return (
    <div className="tmi-field">
      <label className="tmi-field-label" htmlFor={id}>
        {label}
      </label>
      <div className={`tmi-select-wrap${isActive ? " is-active" : ""}`}>
        <select
          id={id}
          className="tmi-select"
          value={value}
          onChange={(event) => onChange(event.target.value)}
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

/* Custom Range: calendar popup for the Date Range filter.
   Dates are plain "YYYY-MM-DD" strings (no time zones), so a chosen day is always the day that was clicked. */

const MONTH_NAMES = Array.from({ length: 12 }, (_, index) =>
  new Date(Date.UTC(2000, index, 1)).toLocaleDateString("en-GB", { month: "long", timeZone: "UTC" })
);
const WEEKDAY_LABELS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]; // weeks start on Monday

function isoFromParts(year, month, day) {
  return `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function parseIsoDate(iso) {
  const [year, month, day] = iso.split("-").map(Number);
  return { y: year, m: month - 1, d: day };
}

function todayIsoDate() {
  const now = new Date();
  return isoFromParts(now.getFullYear(), now.getMonth(), now.getDate());
}

function daysInMonthOf(year, month) {
  return new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
}

// Monday = 0 ... Sunday = 6
function weekdayIndex(iso) {
  const { y, m, d } = parseIsoDate(iso);
  return (new Date(Date.UTC(y, m, d)).getUTCDay() + 6) % 7;
}

function addDaysIso(iso, days) {
  const { y, m, d } = parseIsoDate(iso);
  const date = new Date(Date.UTC(y, m, d + days));
  return isoFromParts(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate());
}

function addMonths(year, month, delta) {
  const total = year * 12 + month + delta;
  return { y: Math.floor(total / 12), m: ((total % 12) + 12) % 12 };
}

function shiftMonthIso(iso, delta) {
  const { y, m, d } = parseIsoDate(iso);
  const target = addMonths(y, m, delta);
  return isoFromParts(target.y, target.m, Math.min(d, daysInMonthOf(target.y, target.m)));
}

function formatDayShort(iso, withYear) {
  const { y, m, d } = parseIsoDate(iso);
  return new Date(Date.UTC(y, m, d)).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    ...(withYear ? { year: "numeric" } : {}),
    timeZone: "UTC",
  });
}

function formatDayLong(iso) {
  const { y, m, d } = parseIsoDate(iso);
  return new Date(Date.UTC(y, m, d)).toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

// "05 Oct – 12 Oct 2026", "30 Dec 2025 – 02 Jan 2026", or "12 Oct 2026" for a single day
function formatPeriodLabel({ start, end }) {
  if (start === end) return formatDayShort(start, true);
  const sameYear = start.slice(0, 4) === end.slice(0, 4);
  return `${formatDayShort(start, !sameYear)} – ${formatDayShort(end, true)}`;
}

function CalChevron({ direction }) {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
      <path
        d={direction === "left" ? "M9 2.5 4.5 7 9 11.5" : "M5 2.5 9.5 7 5 11.5"}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function DateRangePopup({ initialRange, onApply, onCancel }) {
  const [today] = useState(todayIsoDate);
  const [start, setStart] = useState(initialRange ? initialRange.start : null);
  const [end, setEnd] = useState(initialRange ? initialRange.end : null);
  const [hover, setHover] = useState(null);

  const anchor = initialRange ? initialRange.start : today;
  const [view, setView] = useState(() => {
    const { y, m } = parseIsoDate(anchor);
    return { y, m };
  });
  const [focusIso, setFocusIso] = useState(anchor);

  const dayRefs = useRef(new Map());
  const focusPending = useRef(true); // move keyboard focus into the calendar when it opens

  const awaitingEnd = start !== null && end === null;
  const complete = start !== null && end !== null;

  // A hovered day previews the range while the end date is still being chosen.
  const previewEnd = !end && awaitingEnd && hover && hover >= start ? hover : null;
  const rangeEnd = end ?? previewEnd;

  // Only one day is a keyboard tab stop (roving tabindex).
  const monthPrefix = `${view.y}-${String(view.m + 1).padStart(2, "0")}`;
  const tabIso = focusIso.startsWith(monthPrefix) ? focusIso : isoFromParts(view.y, view.m, 1);

  useEffect(() => {
    if (!focusPending.current) return;
    focusPending.current = false;
    const button = dayRefs.current.get(tabIso);
    if (button) button.focus();
  });

  const pickDay = (iso) => {
    setFocusIso(iso);
    if (awaitingEnd) {
      if (iso < start) return; // an end date can never be earlier than the start date
      setEnd(iso);
    } else {
      setStart(iso); // first click, or a new range after a finished one
      setEnd(null);
    }
  };

  const clear = () => {
    setStart(null);
    setEnd(null);
    setHover(null);
  };

  const goToMonth = (delta) => setView((current) => addMonths(current.y, current.m, delta));

  const handleGridKeyDown = (event) => {
    const current = event.target.getAttribute("data-date") || tabIso;
    const moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
    let next = null;
    if (event.key in moves) next = addDaysIso(current, moves[event.key]);
    else if (event.key === "Home") next = addDaysIso(current, -weekdayIndex(current));
    else if (event.key === "End") next = addDaysIso(current, 6 - weekdayIndex(current));
    else if (event.key === "PageUp") next = shiftMonthIso(current, event.shiftKey ? -12 : -1);
    else if (event.key === "PageDown") next = shiftMonthIso(current, event.shiftKey ? 12 : 1);
    if (!next) return;
    event.preventDefault();
    focusPending.current = true;
    setFocusIso(next);
    const { y, m } = parseIsoDate(next);
    setView({ y, m });
  };

  const handleDialogKeyDown = (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onCancel({ restoreFocus: true });
    }
  };

  // Calendar cells for the visible month, padded to six weeks so the popup never changes height.
  const leading = weekdayIndex(isoFromParts(view.y, view.m, 1));
  const cells = Array(leading).fill(null);
  for (let day = 1; day <= daysInMonthOf(view.y, view.m); day += 1) {
    cells.push(isoFromParts(view.y, view.m, day));
  }
  while (cells.length < 42) cells.push(null);
  const weeks = [];
  for (let index = 0; index < 42; index += 7) weeks.push(cells.slice(index, index + 7));

  const thisYear = parseIsoDate(today).y;
  const years = [];
  for (let year = Math.min(thisYear - 10, view.y); year <= Math.max(thisYear + 1, view.y); year += 1) {
    years.push(year);
  }

  let summary = "Select a start date";
  if (complete) summary = formatPeriodLabel({ start, end });
  else if (awaitingEnd) summary = `Select an end date on or after ${formatDayShort(start, true)}`;

  return (
    <div
      className="tmi-cal"
      role="dialog"
      aria-label="Choose a custom date range"
      onKeyDown={handleDialogKeyDown}
    >
      <div className="tmi-cal-head">
        <button
          type="button"
          className="tmi-cal-nav"
          aria-label="Previous month"
          onClick={() => goToMonth(-1)}
        >
          <CalChevron direction="left" />
        </button>
        <div className="tmi-cal-selects">
          <select
            className="tmi-cal-select"
            aria-label="Month"
            value={view.m}
            onChange={(event) => setView((current) => ({ ...current, m: Number(event.target.value) }))}
          >
            {MONTH_NAMES.map((name, index) => (
              <option key={name} value={index}>
                {name}
              </option>
            ))}
          </select>
          <select
            className="tmi-cal-select"
            aria-label="Year"
            value={view.y}
            onChange={(event) => setView((current) => ({ ...current, y: Number(event.target.value) }))}
          >
            {years.map((year) => (
              <option key={year} value={year}>
                {year}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          className="tmi-cal-nav"
          aria-label="Next month"
          onClick={() => goToMonth(1)}
        >
          <CalChevron direction="right" />
        </button>
      </div>

      <table
        className="tmi-cal-grid"
        role="grid"
        aria-label={`${MONTH_NAMES[view.m]} ${view.y}`}
        onKeyDown={handleGridKeyDown}
        onMouseLeave={() => setHover(null)}
      >
        <thead>
          <tr>
            {WEEKDAY_LABELS.map((label) => (
              <th key={label} scope="col">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {weeks.map((week, weekIndex) => (
            <tr key={weekIndex}>
              {week.map((iso, cellIndex) => {
                if (!iso) return <td key={cellIndex} />;

                const isStart = iso === start;
                const isEnd = iso === end;
                const isPreviewEnd = !end && iso === previewEnd && iso !== start;
                const hasBand = start !== null && rangeEnd !== null && rangeEnd !== start;
                const inBand = hasBand && iso > start && iso < rangeEnd;
                const disabled = awaitingEnd && iso < start;

                const cellClass = inBand
                  ? "is-band"
                  : hasBand && isStart
                  ? "is-band-start"
                  : hasBand && iso === rangeEnd
                  ? "is-band-end"
                  : "";
                const dayClass = [
                  "tmi-day",
                  isStart || isEnd ? "is-selected" : "",
                  isPreviewEnd ? "is-preview" : "",
                  iso === today ? "is-today" : "",
                ]
                  .filter(Boolean)
                  .join(" ");
                const note = isStart && isEnd ? ", start and end date" : isStart ? ", start date" : isEnd ? ", end date" : "";

                return (
                  <td key={cellIndex} className={cellClass}>
                    <button
                      type="button"
                      className={dayClass}
                      data-date={iso}
                      tabIndex={iso === tabIso ? 0 : -1}
                      aria-label={`${formatDayLong(iso)}${note}`}
                      aria-pressed={isStart || isEnd}
                      aria-disabled={disabled || undefined}
                      onClick={() => {
                        if (!disabled) pickDay(iso);
                      }}
                      onMouseEnter={() => setHover(iso)}
                      ref={(element) => {
                        if (element) dayRefs.current.set(iso, element);
                        else dayRefs.current.delete(iso);
                      }}
                    >
                      {parseIsoDate(iso).d}
                    </button>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>

      <div className={`tmi-cal-summary${complete ? " is-complete" : ""}`} role="status">
        <span>{summary}</span>
        <button type="button" className="tmi-text-button" onClick={clear} disabled={start === null}>
          Clear
        </button>
      </div>

      <div className="tmi-cal-actions">
        <button type="button" className="tmi-cal-btn" onClick={() => onCancel({ restoreFocus: true })}>
          Cancel
        </button>
        <button
          type="button"
          className="tmi-cal-btn tmi-cal-btn-primary"
          onClick={() => onApply({ start, end })}
          disabled={!complete}
        >
          Apply
        </button>
      </div>
    </div>
  );
}

/**
 * The Date Range dropdown. Same look as the other filters, but choosing
 * "Custom Range" opens the calendar, and once a range is applied the field
 * shows the period itself (an "Edit" link reopens the calendar).
 */
function DateRangeFilter({ id, label, value, onChange, customRange, onCustomRangeChange }) {
  const [open, setOpen] = useState(false);
  const valueBeforeOpen = useRef(value); // what Cancel returns to
  const fieldRef = useRef(null);
  const selectRef = useRef(null);

  const isActive = value !== DATE_RANGE_OPTIONS[0].value;
  const showsPeriod = value === "custom" && customRange !== null;
  const options = DATE_RANGE_OPTIONS.map((option) =>
    option.value === "custom" && showsPeriod
      ? { ...option, label: formatPeriodLabel(customRange) }
      : option
  );

  const close = (restoreFocus) => {
    setOpen(false);
    if (restoreFocus && selectRef.current) selectRef.current.focus();
  };

  const handleSelect = (next) => {
    if (next === "custom") {
      valueBeforeOpen.current = value;
      onChange("custom");
      setOpen(true);
    } else {
      setOpen(false);
      onChange(next);
    }
  };

  const handleEdit = () => {
    valueBeforeOpen.current = value;
    setOpen(true);
  };

  const handleApply = (range) => {
    onCustomRangeChange(range);
    onChange("custom");
    close(true);
  };

  const handleCancel = ({ restoreFocus }) => {
    onChange(valueBeforeOpen.current);
    close(restoreFocus);
  };

  // Clicking anywhere outside the field closes the calendar like Cancel.
  useEffect(() => {
    if (!open) return undefined;
    const handleOutside = (event) => {
      if (fieldRef.current && !fieldRef.current.contains(event.target)) {
        onChange(valueBeforeOpen.current);
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handleOutside);
    document.addEventListener("touchstart", handleOutside);
    return () => {
      document.removeEventListener("mousedown", handleOutside);
      document.removeEventListener("touchstart", handleOutside);
    };
  }, [open, onChange]);

  return (
    <div className="tmi-field tmi-field-range" ref={fieldRef}>
      <div className="tmi-field-head">
        <label className="tmi-field-label" htmlFor={id}>
          {label}
        </label>
        {showsPeriod && !open && (
          <button type="button" className="tmi-text-button tmi-field-action" onClick={handleEdit}>
            Edit
          </button>
        )}
      </div>
      <div className={`tmi-select-wrap${isActive ? " is-active" : ""}`}>
        <select
          id={id}
          ref={selectRef}
          className="tmi-select"
          value={value}
          onChange={(event) => handleSelect(event.target.value)}
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
      {open && (
        <DateRangePopup initialRange={customRange} onApply={handleApply} onCancel={handleCancel} />
      )}
    </div>
  );
}

/* Filtering: every control in the Mentions Library is applied together (AND). */

// "YYYY-MM-DD" of a publication time in UTC, the same day the table shows.
function publicationDay(record) {
  const time = toTime(record.publishedAt);
  return time === null ? null : new Date(time).toISOString().slice(0, 10);
}

// Inclusive { from, to } day limits for the Date Range filter, or null for no limit.
// "Last 7 Days" is today plus the six days before it (UTC); "Last 30 Days" likewise.
// Whole days are compared, so a mention published today counts whatever its time of day.
function dateBounds(dateRange, customRange, now) {
  const today = now.toISOString().slice(0, 10);
  if (dateRange === "last-7-days") return { from: addDaysIso(today, -6), to: null };
  if (dateRange === "last-30-days") return { from: addDaysIso(today, -29), to: null };
  if (dateRange === "custom" && customRange) return { from: customRange.start, to: customRange.end };
  return null; // All Time, or a custom range that has not been applied yet
}

// Subject reads the record's categories (Company / CEO). A record tagged with both
// appears under both subjects.
const SUBJECT_CATEGORY = { "triada-robotics": "company", "artem-veselov": "ceo" };

function matchesSubject(record, subject) {
  const category = SUBJECT_CATEGORY[subject];
  if (!category) return true; // All Mentions
  return record.categories.some((c) => c.toLowerCase() === category);
}

function applyFilters(
  records,
  { status, subject, platform, dateRange, customRange, sortBy },
  now = new Date()
) {
  const bounds = dateBounds(dateRange, customRange, now);
  const kept = records.filter((record) => {
    if (status !== "all" && record.status !== status) return false;
    if (platform !== "all" && record.platform !== platform) return false;
    if (!matchesSubject(record, subject)) return false;
    if (bounds) {
      const day = publicationDay(record);
      if (day === null) return false; // an undated record cannot be placed in a range
      if (bounds.from && day < bounds.from) return false;
      if (bounds.to && day > bounds.to) return false;
    }
    return true;
  });
  const sorted = sortNewestFirst(kept);
  return sortBy === "oldest" ? sorted.reverse() : sorted;
}

function MentionsLibrary({ data }) {
  const { phase, records, skipped, error, lastUpdated, refreshing, refresh } = data;
  const [filter, setFilter] = useState("all");
  const [subject, setSubject] = useState(SUBJECT_OPTIONS[0].value);
  const [platform, setPlatform] = useState(PLATFORM_OPTIONS[0].value);
  const [dateRange, setDateRange] = useState(DATE_RANGE_OPTIONS[0].value);
  const [sortBy, setSortBy] = useState(SORT_OPTIONS[0].value);
  // { start, end } as "YYYY-MM-DD", set when a custom range is applied
  const [customRange, setCustomRange] = useState(null);

  const options = useMemo(() => buildFilterOptions(records), [records]);
  const visible = useMemo(
    () => applyFilters(records, { status: filter, subject, platform, dateRange, customRange, sortBy }),
    [records, filter, subject, platform, dateRange, customRange, sortBy]
  );
  // Clears every filter (the sort order is not a filter and is kept).
  const resetFilters = () => {
    setFilter("all");
    setSubject(SUBJECT_OPTIONS[0].value);
    setPlatform(PLATFORM_OPTIONS[0].value);
    setDateRange(DATE_RANGE_OPTIONS[0].value);
    setCustomRange(null);
  };
  const discovered = useMemo(() => latestDiscovery(records), [records]);

  const isLoading = phase === "loading";
  const isError = phase === "error";
  const isReady = phase === "ready";

  return (
    <section className="tmi-library" aria-labelledby="mentions-heading">
      <div className="tmi-page-header">
        <h2 id="mentions-heading" className="tmi-page-title">
          Mentions Library
        </h2>
        <p className="tmi-page-subtitle">
          Centralized archive of media coverage and online mentions. {isReady ? `${visible.length} of ${records.length} mentions shown.` : ""}
        </p>
      </div>

      <div className="tmi-filterbar" role="group" aria-label="Mention filters">
        <FilterSelect
          id="filter-subject"
          label="Subject"
          value={subject}
          options={SUBJECT_OPTIONS}
          onChange={setSubject}
        />
        <FilterSelect
          id="filter-platform"
          label="Platform"
          value={platform}
          options={PLATFORM_OPTIONS}
          onChange={setPlatform}
        />
        <DateRangeFilter
          id="filter-date-range"
          label="Date Range"
          value={dateRange}
          onChange={setDateRange}
          customRange={customRange}
          onCustomRangeChange={setCustomRange}
        />
        <FilterSelect
          id="filter-sort-by"
          label="Sort By"
          value={sortBy}
          options={SORT_OPTIONS}
          onChange={setSortBy}
        />
      </div>

      <div className="tmi-card" aria-busy={refreshing}>
        {refreshing && <div className="tmi-progress" aria-hidden="true" />}

        {isReady && error && (
          <div className="tmi-banner" role="alert">
            <span>
              Couldn’t refresh the data. Showing the last successful update
              {lastUpdated ? ` (${formatClock(lastUpdated)})` : ""}. {error.message}
            </span>
            <button type="button" className="tmi-text-button" onClick={refresh}>
              Try again
            </button>
          </div>
        )}

        <div className="tmi-table-scroll">
          <table className="tmi-table">
            <caption className="tmi-sr-only">Media mentions</caption>
            <thead>
              <tr>
                {MENTION_COLUMNS.map((col) => (
                  <th key={col.key} scope="col" className={col.className}>
                    {col.label}
                  </th>
                ))}
              </tr>
            </thead>
            {visible.length > 0 && (
              <tbody>
                {visible.map((mention, index) => (
                  <tr key={`${mention.id}-${index}`}>
                    {MENTION_COLUMNS.map((col) => (
                      <td key={col.key} className={col.className}>
                        {col.render(mention)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            )}
          </table>
        </div>

        {isLoading && (
          <StateBlock
            title="Loading mentions…"
            text="Fetching the latest data from the media database."
          />
        )}

        {isError && (
          <StateBlock tone="error" title="Couldn’t load the mentions database." text={error.message}>
            {error.kind === "network" && (
              <p className="tmi-empty-text">
                If this page is running inside a Claude Artifact, the Artifact sandbox may be
                blocking outside requests. The same file works unchanged on a normal website.
              </p>
            )}
            <button type="button" className="tmi-button" onClick={refresh}>
              Try again
            </button>
          </StateBlock>
        )}

        {isReady && records.length === 0 && (
          <StateBlock
            title="No mentions collected yet."
            text="New mentions will appear here after the collector’s next run."
          />
        )}

        {isReady && records.length > 0 && visible.length === 0 && (
          <StateBlock title="No mentions match this filter.">
            <button type="button" className="tmi-text-button" onClick={resetFilters}>
              Show all mentions
            </button>
          </StateBlock>
        )}

        {isReady && records.length > 0 && (
          <div className="tmi-table-footer">
            Showing {visible.length} of {records.length} mentions
            {discovered !== null && ` · Latest discovered ${formatDateTimeUtc(discovered)}`}
            {skipped > 0 && ` · ${skipped} unreadable ${skipped === 1 ? "record" : "records"} skipped`}
          </div>
        )}
      </div>
    </section>
  );
}

// Each view will get its own component as we build it out.

function ConnectorsView() {
  const [state, setState] = useState({loading:true, data:null, error:null});
  const load = useCallback(async () => {
    setState(s => ({...s, loading:true, error:null}));
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 20000);
      let response;
      try {
        response = await fetch("https://raw.githubusercontent.com/Maiiatriada/triada-media-intelligence/main/collector_status.json?t=" + Date.now(), {cache:"no-store",signal:controller.signal});
      } finally { clearTimeout(timeout); }
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      if (!data || typeof data.connectors !== "object") throw new Error("Connector diagnostics are missing");
      setState({loading:false,data,error:null});
    } catch(e) { setState(s=>({...s,loading:false,error:e.message})); }
  },[]);
  useEffect(()=>{load();const timer=setInterval(load,300000);return()=>clearInterval(timer)},[load]);
  const entries=Object.entries(state.data?.connectors||{});
  const color={ok:"#53c88a",partial:"#facc15",error:"#ff6b6b",not_configured:"#8c8c86",not_run:"#8c8c86"};
  return <section style={{padding:"48px 0 80px"}}>
    <h2 style={{fontSize:24,margin:"0 0 8px"}}>Connectors</h2>
    <p style={{color:"#8c8c86",marginBottom:24}}>Live diagnostics from the monitoring collector. A successful request does not guarantee complete coverage.</p>
    <button onClick={load} disabled={state.loading} style={{background:"transparent",border:"1px solid #facc15",color:"#facc15",padding:"10px 16px",cursor:"pointer",marginBottom:24}}>{state.loading?"Refreshing…":"Refresh diagnostics"}</button>
    {state.error && <p role="alert" style={{color:"#ff6b6b"}}>Could not refresh diagnostics: {state.error}{state.data?" · Showing previous data":""}</p>}
    {state.data && <p style={{color:"#8c8c86",marginBottom:24}}>Last run: {state.data.last_run_utc||"unknown"} · Confirmed: {state.data.confirmed_total??"—"} · Pending review: {state.data.pending_internal??"—"}</p>}
    <div style={{display:"grid",gap:12}}>
    {entries.map(([id,c])=><article key={id} style={{border:"1px solid #292929",padding:20,background:"#111"}}>
      <div style={{display:"flex",justifyContent:"space-between",gap:12,flexWrap:"wrap"}}>
        <strong>{c.label||id}</strong><span style={{color:color[c.status]||"#aaa",fontWeight:700}}>{(c.status||"unknown").replaceAll("_"," ").toUpperCase()}</span>
      </div>
      <p style={{color:"#a5a5a0",fontSize:14}}>Requests: {c.requests??0} · Failed: {c.failed_requests??0} · Items fetched: {c.items_fetched??0} · New confirmed: {c.new_confirmed??0}</p>
      {c.last_error&&<p style={{color:"#ff9b91",fontSize:13,overflowWrap:"anywhere"}}>Last error: {c.last_error}</p>}
      {Array.isArray(c.feeds)&&<details><summary style={{cursor:"pointer",color:"#facc15"}}>Publisher feeds ({c.feeds.length})</summary><div style={{paddingTop:12}}>{c.feeds.map(f=><p key={f.name} style={{fontSize:13,color:color[f.status]||"#aaa"}}>{f.name}: {f.status} · {f.items_fetched??0} items {f.last_error?"· "+f.last_error:""}</p>)}</div></details>}
    </article>)}
    </div>
    {!state.loading&&!state.data&&<p>No connector data available.</p>}
  </section>;
}

const VIEWS = {
  overview: DashboardPlaceholder,
  mentions: MentionsLibrary,
};

/* ---------- Layout components ---------- */

function Header({ activeView, onNavigate }) {
  return (
    <header className="tmi-header">
      <div className="tmi-container">
        <div className="tmi-brand">
          <span className="tmi-brand-bar" aria-hidden="true" />
          <div>
            <h1 className="tmi-title">{APP.title}</h1>
            <p className="tmi-subtitle">{APP.subtitle}</p>
          </div>
        </div>

        <nav className="tmi-nav" aria-label="Primary">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.id}
              type="button"
              className="tmi-tab"
              aria-current={activeView === item.id ? "page" : undefined}
              onClick={() => onNavigate(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>
      </div>
    </header>
  );
}

function MainContent({ activeView, mentions }) {
  const View = VIEWS[activeView];
  return (
    <main className="tmi-main">
      <div className="tmi-container">
        {/* key replays the one-time scan line when the section changes */}
        <View key={activeView} data={mentions} />
      </div>
    </main>
  );
}

/* ---------- App ---------- */

export default function App() {
  const [activeView, setActiveView] = useState(NAV_ITEMS[0].id);
  // Fetches on page load and every 5 minutes, whichever tab is open.
  const mentions = useMentions();

  return (
    <div className="tmi-app">
      <style>{STYLES}</style>
      <Header activeView={activeView} onNavigate={setActiveView} />
      <MainContent activeView={activeView} mentions={mentions} />
    </div>
  );
}

/* ---------- Styles ---------- */

const STYLES = `
.tmi-app {
  --bg: #0B0B0B;
  --surface: #111111;
  --line: #242424;
  --text: #F2F2EE;
  --muted: #8C8C86;
  --accent: #FACC15;

  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg);
  color: var(--text);
  font-family: "Helvetica Neue", "Segoe UI", Inter, Roboto, Arial, system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

.tmi-app *, .tmi-app *::before, .tmi-app *::after { box-sizing: border-box; }
.tmi-app ::selection { background: var(--accent); color: #0B0B0B; }

.tmi-container {
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 32px;
}

/* Header */
.tmi-header {
  border-bottom: 1px solid var(--line);
  padding-top: 32px;
}

.tmi-brand {
  display: flex;
  align-items: stretch;
  gap: 16px;
}

.tmi-brand-bar {
  flex: none;
  width: 4px;
  background: var(--accent);
}

.tmi-title {
  margin: 0;
  font-size: clamp(1.25rem, 2.6vw, 1.75rem);
  font-weight: 700;
  letter-spacing: 0.16em;
  line-height: 1.15;
}

.tmi-subtitle {
  margin: 8px 0 0;
  font-size: 0.875rem;
  letter-spacing: 0.02em;
  color: var(--muted);
}

/* Navigation */
.tmi-nav {
  display: flex;
  gap: 32px;
  margin-top: 28px;
  overflow-x: auto;
  scrollbar-width: none;
}
.tmi-nav::-webkit-scrollbar { display: none; }

.tmi-tab {
  position: relative;
  flex: none;
  padding: 0 0 14px;
  margin-bottom: -1px;
  background: none;
  border: 0;
  border-bottom: 2px solid transparent;
  color: var(--muted);
  font: inherit;
  font-size: 0.9375rem;
  font-weight: 500;
  cursor: pointer;
  transition: color 160ms ease, border-color 160ms ease;
}
.tmi-tab:hover { color: var(--text); }
.tmi-tab[aria-current="page"] {
  color: var(--text);
  border-bottom-color: var(--accent);
}
.tmi-tab:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 4px;
  border-radius: 2px;
}

/* Main */
.tmi-main {
  flex: 1;
  padding: 48px 0 64px;
}

.tmi-panel {
  position: relative;
  overflow: hidden;
  display: flex;
  align-items: center;
  min-height: min(52vh, 420px);
  padding: 56px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 6px;
}

.tmi-panel-text {
  margin: 0;
  max-width: 20ch;
  font-size: clamp(1.625rem, 4vw, 2.75rem);
  font-weight: 300;
  line-height: 1.18;
  letter-spacing: -0.01em;
}

/* One-time scan line when a section opens */
.tmi-panel::before {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent 0%, var(--accent) 50%, transparent 100%);
  opacity: 0;
  animation: tmi-scan 1.5s ease-out 0.15s 1 both;
  pointer-events: none;
}

@keyframes tmi-scan {
  0%   { top: 0;    opacity: 0.9; }
  85%  { opacity: 0.35; }
  100% { top: 100%; opacity: 0; }
}

/* Responsive */
@media (max-width: 640px) {
  .tmi-container { padding: 0 20px; }
  .tmi-header { padding-top: 24px; }
  .tmi-nav { gap: 24px; margin-top: 22px; }
  .tmi-main { padding: 28px 0 40px; }
  .tmi-panel { padding: 32px 24px; min-height: 280px; }
}

@media (prefers-reduced-motion: reduce) {
  .tmi-panel::before { animation: none; opacity: 0; }
  .tmi-tab { transition: none; }
}

/* ---- Mentions Library ---- */
.tmi-library { --danger: #F2766B; }

.tmi-sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.tmi-page-header { margin-bottom: 24px; }

.tmi-page-title {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 600;
  letter-spacing: -0.005em;
  line-height: 1.2;
}

.tmi-page-subtitle {
  margin: 6px 0 0;
  font-size: 0.875rem;
  color: var(--muted);
}

/* Filter bar: Subject / Date Range / Sort By */
.tmi-filterbar {
  display: flex;
  flex-wrap: wrap;
  gap: 16px 20px;
  margin-bottom: 16px;
  padding: 14px 16px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 6px;
}

.tmi-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex: 1 1 200px;
  max-width: 280px;
}

.tmi-field-label {
  color: var(--muted);
  font-size: 0.75rem;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.tmi-select-wrap { position: relative; }

.tmi-select-wrap::after {
  content: "";
  position: absolute;
  right: 14px;
  top: 50%;
  width: 6px;
  height: 6px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  color: var(--muted);
  transform: translateY(-70%) rotate(45deg);
  pointer-events: none;
  transition: color 160ms ease;
}

.tmi-select {
  width: 100%;
  padding: 9px 36px 9px 12px;
  appearance: none;
  -webkit-appearance: none;
  background: #0E0E0E;
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--text);
  color-scheme: dark;
  font: inherit;
  font-size: 0.8125rem;
  cursor: pointer;
  transition: border-color 160ms ease, color 160ms ease;
}
.tmi-select:hover { border-color: #3A3A3A; }
.tmi-select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tmi-select option { background: #111111; color: var(--text); }

.tmi-select-wrap.is-active .tmi-select {
  border-color: rgba(250, 204, 21, 0.6);
  color: var(--accent);
}
.tmi-select-wrap.is-active::after { color: var(--accent); }

/* Custom Range: Date Range field + calendar popup */
.tmi-field-range { position: relative; }

/* Same height as the other filters' labels; the Edit link floats at the right without adding height */
.tmi-field-head { position: relative; display: flex; }
.tmi-field-action {
  position: absolute;
  top: 50%;
  right: 0;
  transform: translateY(-50%);
  font-size: 0.75rem;
  line-height: 1;
}

.tmi-cal {
  position: absolute;
  top: calc(100% + 8px);
  left: 0;
  z-index: 40;
  width: 316px;
  max-width: calc(100vw - 48px);
  padding: 14px;
  background: #121212;
  border: 1px solid #2E2E2E;
  border-radius: 8px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.65);
  color: var(--text);
  font-size: 0.8125rem;
}

.tmi-cal-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
}

.tmi-cal-nav {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  padding: 0;
  background: transparent;
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--muted);
  cursor: pointer;
  transition: color 160ms ease, border-color 160ms ease;
}
.tmi-cal-nav:hover { color: var(--accent); border-color: rgba(250, 204, 21, 0.5); }

.tmi-cal-selects { flex: 1; display: flex; gap: 6px; min-width: 0; }

.tmi-cal-select {
  flex: 1;
  min-width: 0;
  padding: 6px 22px 6px 8px;
  appearance: none;
  -webkit-appearance: none;
  background-color: #0E0E0E;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%238C8C86' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 8px center;
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--text);
  color-scheme: dark;
  font: inherit;
  font-size: 0.8125rem;
  cursor: pointer;
}
.tmi-cal-select:hover { border-color: #3A3A3A; }
.tmi-cal-select option { background: #111111; color: var(--text); }

.tmi-cal-grid {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  table-layout: fixed;
}
.tmi-cal-grid th {
  padding: 4px 0 6px;
  color: var(--muted);
  font-size: 0.6875rem;
  font-weight: 600;
  text-align: center;
}
.tmi-cal-grid td {
  height: 36px;
  padding: 0;
  text-align: center;
}

/* The range band runs behind the days from the start date to the end date */
.tmi-cal-grid td.is-band { background: rgba(250, 204, 21, 0.14); }
.tmi-cal-grid td.is-band-start {
  background: rgba(250, 204, 21, 0.14);
  border-radius: 8px 0 0 8px;
}
.tmi-cal-grid td.is-band-end {
  background: rgba(250, 204, 21, 0.14);
  border-radius: 0 8px 8px 0;
}

.tmi-day {
  display: block;
  width: calc(100% - 4px);
  height: 32px;
  margin: 2px;
  padding: 0;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--text);
  font: inherit;
  font-size: 0.8125rem;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
}
.tmi-day:hover { background: rgba(255, 255, 255, 0.07); }
.tmi-day.is-today { border-color: #3A3A3A; }
.tmi-day.is-selected,
.tmi-day.is-selected:hover {
  background: var(--accent);
  border-color: var(--accent);
  color: #0B0B0B;
  font-weight: 600;
}
.tmi-day.is-preview { border-color: var(--accent); }
.tmi-day[aria-disabled="true"],
.tmi-day[aria-disabled="true"]:hover {
  background: transparent;
  color: #4A4A46;
  cursor: not-allowed;
}

.tmi-cal-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 28px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 0.8125rem;
}
.tmi-cal-summary.is-complete { color: var(--text); }

.tmi-cal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}

.tmi-cal-btn {
  padding: 7px 14px;
  background: transparent;
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--text);
  font: inherit;
  font-size: 0.8125rem;
  font-weight: 500;
  cursor: pointer;
  transition: border-color 160ms ease, background-color 160ms ease;
}
.tmi-cal-btn:hover { border-color: #3A3A3A; }
.tmi-cal-btn-primary { color: var(--accent); border-color: rgba(250, 204, 21, 0.55); }
.tmi-cal-btn-primary:hover:not(:disabled) {
  background: rgba(250, 204, 21, 0.1);
  border-color: var(--accent);
}
.tmi-cal-btn:disabled { opacity: 0.4; cursor: default; }

.tmi-cal-nav:focus-visible,
.tmi-cal-select:focus-visible,
.tmi-cal-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.tmi-day:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

/* Toolbar: status filters + refresh info */
.tmi-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px 24px;
  margin-bottom: 14px;
}

.tmi-filters { display: flex; flex-wrap: wrap; gap: 8px; }

.tmi-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  background: transparent;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--muted);
  font: inherit;
  font-size: 0.8125rem;
  font-weight: 500;
  cursor: pointer;
  transition: color 160ms ease, border-color 160ms ease, background-color 160ms ease;
}
.tmi-chip:hover { color: var(--text); border-color: #3A3A3A; }
.tmi-chip[aria-pressed="true"] {
  color: var(--accent);
  border-color: rgba(250, 204, 21, 0.6);
  background: rgba(250, 204, 21, 0.08);
}
.tmi-chip-count { font-size: 0.75rem; opacity: 0.75; font-variant-numeric: tabular-nums; }

.tmi-sync {
  display: flex;
  align-items: center;
  gap: 12px;
  color: var(--muted);
  font-size: 0.8125rem;
}

.tmi-text-button {
  padding: 0;
  background: none;
  border: 0;
  border-bottom: 1px solid rgba(250, 204, 21, 0.35);
  color: var(--accent);
  font: inherit;
  font-size: 0.8125rem;
  cursor: pointer;
}
.tmi-text-button:hover:not(:disabled) { border-bottom-color: var(--accent); }
.tmi-text-button:disabled { opacity: 0.45; cursor: default; }

.tmi-button {
  margin-top: 18px;
  padding: 8px 16px;
  background: transparent;
  border: 1px solid rgba(250, 204, 21, 0.55);
  border-radius: 4px;
  color: var(--accent);
  font: inherit;
  font-size: 0.8125rem;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 160ms ease;
}
.tmi-button:hover { background: rgba(250, 204, 21, 0.08); }

.tmi-chip:focus-visible,
.tmi-text-button:focus-visible,
.tmi-button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}

/* Card + table */
.tmi-card {
  position: relative;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 6px;
  overflow: hidden;
}

.tmi-progress {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  overflow: hidden;
  z-index: 1;
}
.tmi-progress::before {
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  width: 35%;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  animation: tmi-indeterminate 1.3s ease-in-out infinite;
}
@keyframes tmi-indeterminate {
  from { transform: translateX(-100%); }
  to   { transform: translateX(300%); }
}

.tmi-banner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 16px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--line);
  color: var(--danger);
  font-size: 0.8125rem;
}

.tmi-table-scroll {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

.tmi-table {
  width: 100%;
  min-width: 720px;
  border-collapse: collapse;
  font-size: 0.8125rem;
  line-height: 1.45;
  text-align: left;
}

.tmi-table th {
  padding: 11px 16px;
  background: #0E0E0E;
  border-bottom: 1px solid rgba(250, 204, 21, 0.35);
  color: var(--muted);
  font-size: 0.75rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

.tmi-table td {
  padding: 12px 16px;
  border-bottom: 1px solid var(--line);
  color: var(--text);
  vertical-align: top;
}

.tmi-table tbody tr:last-child td { border-bottom: 0; }
.tmi-table tbody tr:hover td { background: rgba(250, 204, 21, 0.04); }

.tmi-col-date { width: 128px; white-space: nowrap; }
.tmi-col-source { width: 190px; }
.tmi-col-title { min-width: 300px; }
.tmi-col-link { width: 150px; white-space: nowrap; }

/* Relevance status badge, shown under the article title */
.tmi-badge {
  display: inline-block;
  margin-top: 8px;
  padding: 2px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--muted);
  font-size: 0.6875rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  line-height: 1.5;
}
.tmi-col-title span:first-child { display: block; }
.tmi-badge-relevant {
  color: var(--accent);
  border-color: rgba(250, 204, 21, 0.5);
  background: rgba(250, 204, 21, 0.08);
}
.tmi-badge-review { color: var(--text); border-color: #3A3A3A; }
.tmi-badge-irrelevant { color: #6F6F6A; border-color: var(--line); }

.tmi-muted { color: var(--muted); }

.tmi-link {
  color: var(--accent);
  text-decoration: none;
  border-bottom: 1px solid rgba(250, 204, 21, 0.35);
  transition: border-color 160ms ease;
}
.tmi-link:hover { border-bottom-color: var(--accent); }
.tmi-link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  border-radius: 2px;
}

/* Loading / empty / error states */
.tmi-empty { padding: 44px 16px 48px; }
.tmi-empty-title {
  margin: 0;
  font-size: 0.9375rem;
  font-weight: 500;
}
.tmi-empty-text {
  margin: 6px 0 0;
  max-width: 62ch;
  font-size: 0.8125rem;
  color: var(--muted);
}
.tmi-error-text { color: var(--danger); }

.tmi-table-footer {
  padding: 11px 16px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 0.75rem;
}

@media (max-width: 640px) {
  .tmi-page-header { margin-bottom: 20px; }
  .tmi-page-title { font-size: 1.125rem; }
  .tmi-filterbar { padding: 12px; gap: 14px; }
  .tmi-field { flex-basis: 100%; max-width: none; }
  .tmi-cal { width: min(316px, calc(100vw - 48px)); padding: 12px; }
  .tmi-empty { padding: 32px 16px 36px; }
}

@media (prefers-reduced-motion: reduce) {
  .tmi-link, .tmi-chip, .tmi-button, .tmi-select, .tmi-select-wrap::after, .tmi-cal-nav, .tmi-cal-btn { transition: none; }
  .tmi-progress::before { animation: none; width: 100%; opacity: 0.5; }
}
`;