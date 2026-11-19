/* Render the issue from bundled data or pipeline JSON. */
(() => {
  const DATA_URL = "data/world_covered.json";
  const BUILT_URL = "data/built.json";
  const SECTION_URL = "data/world_tracked.json";
  const SPORTS_URL = "data/sports.json";
  const TECH_URL = "data/tech.json";
  const LIETUVA_URL = "data/lietuva.json";
  const EUROPE_URL = "data/europe.json";
  const MAX_STORIES = 5; // "the five most important topics"
  const MAX_STORIES_NOTABLE = 8;
  // A single-outlet story has no lean label.
  const NOTABLE_OUTLETS = 3;
  const MAX_SECTION_STORIES = 5;
  const MAX_PER_LEAN = 2; // frames shown per lean; keeps the arrows from feeling endless
  const SECTIONS = [
    { container: "worldStories", title: "World", url: DATA_URL },
    {
      container: "europeStories",
      title: "Europe",
      url: EUROPE_URL,
      presorted: true,
    },
    {
      container: "lietuvaStories",
      title: "Lietuva",
      url: LIETUVA_URL,
      presorted: true,
    },
    {
      container: "techStories",
      title: "Tech",
      url: TECH_URL,
      presorted: true,
    },
    {
      container: "sportsStories",
      title: "Sports",
      url: SPORTS_URL,
      presorted: true,
    },
    {
      container: "otherStories",
      title: "Other",
      url: SECTION_URL,
      category: "Other",
    },
  ];

  const LEAN_LABEL = { left: "Left", center: "Centre", right: "Right" };

  const FLIP_ORDER = ["left", "center", "right"];


  /* Use the pipeline's rank so the page and notification agree. */
  function rankStories(rows) {
    return rows
      .filter((row) => Number.isInteger(row.briefing_rank))
      .sort((a, b) => a.briefing_rank - b.briefing_rank);
  }

  const SIGNIFICANCE_RANK = { high: 0, medium: 1, low: 2 };

  function rankSections(rows, category) {
    return rows
      .filter((row) => (row.classification || {}).category === category)
      .filter((row) => row.title && row.url)
      .sort((a, b) => {
        const tier =
          (SIGNIFICANCE_RANK[(a.classification || {}).significance] ?? 3) -
          (SIGNIFICANCE_RANK[(b.classification || {}).significance] ?? 3);
        if (tier !== 0) return tier;
        return new Date(b.publishedAt || 0) - new Date(a.publishedAt || 0);
      })
      .slice(0, MAX_SECTION_STORIES);
  }

  function buildFrames(story) {
    // Beat sections supply their own frames.
    if (Array.isArray(story.frames) && story.frames.length) {
      return story.frames.filter(
        (frame) => frame && (frame.title || frame.url),
      );
    }

    const coverage = story.coverage || {};
    const frames = [];

    for (const lean of FLIP_ORDER) {
      const items = coverage[lean] || [];
      for (
        let depth = 0;
        depth < MAX_PER_LEAN && depth < items.length;
        depth += 1
      ) {
        frames.push({ ...items[depth], lean });
      }
    }

    if (!frames.length) frames.push({ ...story, lean: null });

    return frames;
  }


  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function formatClock(iso) {
    const stamp = new Date(iso);
    if (Number.isNaN(stamp.getTime())) return "";
    return stamp.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  async function loadJson(url) {
    const bundled = window.__BRIEF__;
    if (bundled && Object.hasOwn(bundled, url)) {
      return bundled[url];
    }
    try {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } catch {
      return null;
    }
  }


  function buildCard(story, frames, storyIndex) {
    const card = el("article", "news-card");
    card.dataset.storyIndex = String(storyIndex);

    const image = el("div", "card-image");
    const img = el("img", "card-image-img");
    img.alt = "";
    img.loading = "lazy";
    // Avoid sending a referrer to image hosts that reject hotlinks.
    img.referrerPolicy = "no-referrer";
    image.appendChild(img);

    // Ignore a delayed image error after the reader has changed frames.
    let pendingSrc = "";
    img.addEventListener("error", () => {
      if (pendingSrc && img.getAttribute("src") === pendingSrc) {
        image.classList.remove("card-image--filled");
      }
    });

    const history = story.history || {};
    let developing = null;
    if (history.is_developing) {
      developing = el("div", "card-developing");
      const ordinal = String(history.update_ordinal || "").trim();
      const days = Number(history.days_running) || 0;
      // Only use the weekly label for recent stories.
      const elapsed = Date.now() - new Date(history.first_seen || "").getTime();
      const weekly = elapsed >= 0 && elapsed <= 7 * 864e5 ? " this week" : "";
      developing.textContent = `Developing — ${ordinal} update${weekly}`;
      if (days) {
        developing.title =
          `Running for ${days} day(s), first seen ` +
          String(history.first_seen || "").slice(0, 10);
      }
    }

    const headline = el("h2", "card-headline");
    const headlineLink = el("a", "card-headline-link");
    headline.appendChild(headlineLink);

    const summary = el("p", "card-summary");

    const footer = el("div", "card-footer");
    const sourceRow = el("div", "card-source-row");
    const source = el("span", "card-source");
    const leanPill = el("span", "card-lean");
    sourceRow.append(source, leanPill);

    // Show framing controls only for stories with multiple frames.
    let prev = null;
    let next = null;
    let dots = null;
    if (frames.length > 1) {
      const nav = el("div", "card-nav");
      prev = el("button", "card-nav-btn", "\u2039");
      prev.type = "button";
      prev.setAttribute("aria-label", "Previous framing");
      next = el("button", "card-nav-btn", "\u203A");
      next.type = "button";
      next.setAttribute("aria-label", "Next framing");
      dots = el("span", "card-nav-dots");
      nav.append(prev, dots, next);
      footer.append(sourceRow, nav);
    } else {
      footer.append(sourceRow);
    }

    card.append(image);
    if (developing) card.appendChild(developing);
    card.append(headline, summary, footer);

    const dotNodes = dots
      ? frames.map(() => {
          const dot = el("span", "card-nav-dot");
          dots.appendChild(dot);
          return dot;
        })
      : [];


    let index = 0;

    function paint() {
      const frame = frames[index] || {};
      const lead = story;
      const lean = frame.lean;

      headlineLink.textContent = frame.title || lead.title || "";
      const url = String(frame.url || lead.url || "");
      headlineLink.href = /^https?:\/\//i.test(url) ? url : "#";
      headlineLink.target = "_blank";
      headlineLink.rel = "noopener noreferrer";

      summary.textContent = frame.description || lead.description || "";

      source.textContent = frame.source || lead.source || "";

      leanPill.textContent = LEAN_LABEL[lean] || "";
      leanPill.dataset.lean = lean || "";
      leanPill.hidden = !lean;


      const src = frame.urlToImage || lead.urlToImage || "";
      if (src) {
        pendingSrc = src;
        img.src = src;
        img.alt = "";
        image.classList.add("card-image--filled");
      } else {
        pendingSrc = "";
        img.removeAttribute("src");
        image.classList.remove("card-image--filled");
      }

      dotNodes.forEach((dot, i) => {
        dot.classList.toggle("active", i === index);
      });
    }

    function step(delta) {
      index = (index + delta + frames.length) % frames.length;
      paint();
    }

    if (prev && next) {
      prev.addEventListener("click", () => step(-1));
      next.addEventListener("click", () => step(1));
    }
    card.addEventListener("keydown", (event) => {
      if (frames.length > 1 && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
        event.preventDefault();
        event.stopPropagation();
        step(event.key === "ArrowLeft" ? -1 : 1);
      }
    });

    if (frames.length) paint();
    return card;
  }


  function showMessage(container, title, detail) {
    const box = el("div", "page-empty");
    box.append(
      el("p", "page-empty-title", title),
      el("p", "page-empty-detail", detail),
    );
    container.appendChild(box);
  }

  function paintTitle(container, stories) {
    const section = container.closest("section");
    const title = section && section.querySelector(".page-title");
    if (!title) return;

    const candidates = [];
    for (const story of stories) {
      const src = buildFrames(story)[0]?.urlToImage;
      if (src && !candidates.includes(src)) candidates.push(src);
    }

    if (!candidates.length) {
      title.style.backgroundImage = ""; // back to the stylesheet's gradient
      return;
    }

    // Try each header image until one loads.
    const accent = section.dataset.accentRgb || "14,116,144";
    let attempt = 0;

    const tryNext = () => {
      if (attempt >= candidates.length) return;
      const src = candidates[attempt++];
      const probe = new Image();
      probe.referrerPolicy = "no-referrer";
      probe.addEventListener("load", () => {
        title.style.backgroundImage =
          `linear-gradient(rgba(${accent},0.26), rgba(${accent},0.26)), ` +
          `linear-gradient(rgba(24,20,16,0.34), rgba(24,20,16,0.34)), ` +
          `url("${src}")`;
        title.style.backgroundSize = "cover";
        title.style.backgroundPosition = "center";
      });
      probe.addEventListener("error", tryNext);
      probe.src = src;
    };

    tryNext();
  }

  function capStories(stories) {
    if (stories.length <= MAX_STORIES) return stories;

    const kept = stories.slice(0, MAX_STORIES);
    const interests = new Set(
      kept.map((story) => story.interest).filter(Boolean),
    );

    for (const story of stories.slice(MAX_STORIES)) {
      if (kept.length >= MAX_STORIES_NOTABLE) break;
      const interest = story.interest;
      const unreached = interest && !interests.has(interest);
      if (unreached || isNotable(story)) {
        kept.push(story);
        if (interest) interests.add(interest);
      }
    }

    return kept;
  }

  function isNotable(story) {
    const outlets = Number(
      story.outlet_count || (story.outlets || []).length || 0,
    );
    return outlets >= NOTABLE_OUTLETS;
  }

  function renderSection(section, rows, container) {
    let stories;
    if (section.category) {
      stories = rankSections(rows, section.category);
    } else if (section.presorted) {
      // World renders the ranked list without the beat cap.
      stories = rows.filter((row) => row.title && row.url);
    } else {
      stories = rankStories(rows);
    }

    if (section.presorted || section.category) stories = capStories(stories);

    if (!stories.length) {
      showMessage(
        container,
        `Nothing in ${section.title} today`,
        "No stories met this section's selection criteria for this issue.",
      );
      return 0;
    }

    const feed = el("div", "feed");
    container.appendChild(feed);

    stories.forEach((story, i) => {
      const node = buildCard(story, buildFrames(story), i);
      if (section.title === "Lietuva") node.lang = "lt";
      node.classList.add("feed-card");
      feed.appendChild(node);
      if (i < stories.length - 1) feed.appendChild(el("hr", "divider"));
    });

    paintTitle(container, stories);
    return stories.length;
  }

  async function loadBuildStamp() {
    const stamp = document.getElementById("worldUpdated");
    const warnings = document.querySelectorAll("[data-stale]");
    if (!stamp) return false;

    let builtAt;
    let payloadDemo = false;
    try {
      const payload = await loadJson(BUILT_URL);
      // A missing build date must not parse as the Unix epoch.
      if (!payload || !payload.built_at) throw new Error("no stamp");
      payloadDemo = payload.demo === true;
      builtAt = new Date(payload.built_at);
      if (Number.isNaN(builtAt.getTime())) throw new Error("bad timestamp");
    } catch {
      return false;
    }

    const weekday = builtAt.toLocaleDateString("en-GB", { weekday: "long" });
    const dayMonth = builtAt.toLocaleDateString("en-GB", {
      day: "numeric",
      month: "long",
    });
    stamp.textContent = payloadDemo ? "Sample issue · fictional headlines" : `${weekday}, ${dayMonth}`;

    document.querySelectorAll("[data-stamp]").forEach((node) => {
      node.textContent = payloadDemo ? "Sample issue · fictional headlines" : `${weekday}, ${dayMonth}`;
    });
    const hours = (Date.now() - builtAt.getTime()) / 36e5;
    const stale = !payloadDemo && hours >= 0 && builtAt.toDateString() !== new Date().toDateString();
    warnings.forEach(warning => {
      warning.textContent = `Previous issue · compiled ${dayMonth}`;
      warning.hidden = !stale;
    });

    return true;
  }

  async function main() {
    const stamp = document.getElementById("worldUpdated");

    document.querySelectorAll("[data-stamp]").forEach(node => {
      node.textContent = "Issue date unavailable";
    });
    const stamped = await loadBuildStamp();

    // Fail one section at a time if its source is missing.
    const urls = [...new Set(SECTIONS.map((s) => s.url))];
    const loaded = await Promise.all(urls.map((url) => loadJson(url)));
    const byUrl = new Map(urls.map((url, i) => [url, loaded[i]]));

    let shown = 0;
    for (const section of SECTIONS) {
      const container = document.getElementById(section.container);
      if (!container) continue;

      const rows = byUrl.get(section.url);
      if (!Array.isArray(rows) || rows.some(row => !row || typeof row !== "object")) {
        showMessage(
          container,
          "Could not load today's brief",
          "This section is unavailable in this copy. Please open the most recent paper from your chat.",
        );
        continue;
      }
      shown += renderSection(section, rows, container);
    }

    if (stamp && !stamped) {
      const covered = byUrl.get(DATA_URL);
      if (shown) {
        const newest = (Array.isArray(covered) ? covered : [])
          .filter(row => row && typeof row === "object")
          .map((s) => new Date(s.publishedAt || 0))
          .filter((d) => !Number.isNaN(d.getTime()))
          .sort((a, b) => b - a)[0];
        stamp.textContent = newest
          ? `Updated ${formatClock(newest.toISOString())}`
          : `${shown} stories`;
      } else {
        stamp.textContent = covered ? "no data" : "unavailable";
      }
    }
  }

  document.addEventListener("DOMContentLoaded", main);
})();
