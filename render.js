/**
 * render.js — wires every page to the pipeline output.
 *
 * Two sources, because the pages need different things:
 *
 *   World    data/world_covered.json — the briefing, with lean coverage gathered
 *            for it. Each card's arrows flip through how the left, centre and
 *            right outlets actually headlined the same story.
 *   Lietuva  data/world_tracked.json — the same stories grouped by the category
 *   Tech     classify_articles.py assigned. One outlet per story, so there are no
 *   Sports   arrows and no lean pill: a single outlet's lean is not recorded on
 *   Other    the story, and deriving one from the outlet's name would be a
 *            judgement call presented as a fact.
 *
 * Needs to be served over HTTP — `fetch()` is blocked on file:// URLs. Run:
 *     python3 -m http.server 8000
 * then open http://localhost:8000
 */
(() => {
  const DATA_URL = "data/world_covered.json";
  const DESK_URL = "data/desk.json";
  const BUILT_URL = "data/built.json";
  // The category pages read the tracked list: same classification as
  // world_filtered.json, plus the history the developing cue needs.
  const SECTION_URL = "data/world_tracked.json";
  // The sports page has its own beat and its own file — see fetch_sports.py. It
  // arrives already ranked and capped, so the page renders what it is handed.
  const SPORTS_URL = "data/sports.json";
  // Results and standings, above the sports cards — see fetch_stats.py. Separate
  // from the news file because it is a different shape: tables, not articles.
  const STATS_URL = "data/stats.json";
  // The tech page has its own beat too — see fetch_tech.py. It reads the world
  // roster for its policy coverage and its own sources for everything else,
  // because a page built only from general news desks is a page about AI
  // legislation rather than about AI.
  const TECH_URL = "data/tech.json";
  // The Lietuva page has its own beat too — Lithuanian newsrooms, not Lithuania as
  // the world roster happens to report it. See fetch_lietuva.py.
  const LIETUVA_URL = "data/lietuva.json";
  // The weather for Kaunas, drawn above the Lietuva news — see fetch_weather.py.
  // A panel rather than a page: it is one map and four numbers, and breakfast is
  // the only time of day anybody looks at it.
  const WEATHER_URL = "data/weather.json";
  // Europe is the odd one out: it has no beat of its own, because the world
  // pipeline already classifies a region on every story and nothing was reading
  // it. See fetch_europe.py.
  const EUROPE_URL = "data/europe.json";
  const MAX_STORIES = 5; // "the five most important topics"
  // At most five stories a page, and no more than eight however good the day is.
  // See capStories for what earns a slot past the fifth.
  const MAX_STORIES_NOTABLE = 8;
  // How many newsrooms have to have carried a story independently for it to be
  // worth a sixth slot. Three is a low bar and still rare: across the whole last
  // run only three stories cleared it, because most reach the pipeline through a
  // single feed. See isNotable for why coverage and not significance.
  const NOTABLE_OUTLETS = 3;
  const MAX_SECTION_STORIES = 5;
  const MAX_PER_LEAN = 2; // frames shown per lean; keeps the arrows from feeling endless
  // Rows shown per statistics table. A championship table is twenty rows nobody
  // scrolls on a phone; five is the shape of the thing and the leader is the point.
  const MAX_STAT_ROWS = 5;

  // PARKED: the Sports dashboard.
  //
  // The results panels — final games, team spotlight, fixtures, the F1 feature and
  // the standings tables — are out of the design. Sports is a news page again.
  //
  // A flag rather than a deleted call or a commented-out one, so that everything
  // stays wired: renderDashboard keeps its definition, STATS_URL keeps being
  // fetched, `stats: true` keeps marking the section, fetch_stats.py keeps running
  // in run.sh and data/stats.json keeps being current. Setting this to false is
  // the whole of the restoration.
  //
  // The filter tabs live inside renderDashboard, so they are parked with it — and
  // the league tags they read are still written by the pipeline, so they come
  // back working rather than back empty.
  const DASHBOARD_PARKED = true;

  // A morning paper is out of date once it has missed a morning. Thirty rather
  // than twenty-four so a slightly late 07:00 run never trips it by accident.
  const STALE_AFTER_HOURS = 30;

  // How much the map is magnified beyond its native tiles.
  //
  // RainViewer's radar tops out at zoom seven, where a pixel is about 700 metres.
  // There is no closer radar to fetch, so "closer" means showing less of what we
  // have — the layer is drawn this much larger and the window therefore takes in
  // that much less of it. Past about two the tiles are visibly soft, and the rain
  // cells are the only thing that stays readable because they were never sharp.
  const RADAR_MAGNIFY = 1.7;

  // How long each radar frame holds before the loop moves on. Six frames at this
  // speed is a three-second loop: long enough to read a shape moving, short enough
  // that nobody waits for the frame they want.
  const RADAR_FRAME_MS = 500;

  // Below this, an hourly chance of rain is not worth a line in the weather row.
  // A column of 0%s and 3%s down it is noise, and noise is what stops the 80%
  // three cells along from being seen at all.
  const WX_RAIN_WORTH_SHOWING = 15;

  // The chance at which the radar footer calls the next hour wet rather than
  // merely damp. Higher than the line above, because "next rain 16:00" is a claim
  // about the day and a 20% hour is not one — the threshold that decides whether
  // to draw a number is not the threshold that decides whether to promise rain.
  const WX_RAIN_CERTAIN = 50;

  /**
   * The pages, and where each one's stories come from.
   *
   * World is the only page with lean coverage gathered for it, so it reads the
   * covered file and defers to the pipeline's briefing_rank. Europe and the two
   * beats read files that are already ordered. The rest read the tracked file and
   * take their own category. The `category` values must match what
   * classify_articles.py emits — see CATEGORIES there.
   */
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
      // Panels drawn above the news, before the cards. The weather file holds a
      // list of them, the same shape fetch_stats.py writes, so the same builders
      // render both.
      panels: WEATHER_URL,
    },
    {
      container: "techStories",
      title: "Tech",
      url: TECH_URL,
      // Presorted, like Sports and Lietuva: fetch_tech.py already balanced the
      // page across interests with a round-robin, and re-capping it here at five
      // would undo that — the narrow interests are exactly the ones that lose a
      // fight for a slot.
      presorted: true,
    },
    {
      container: "sportsStories",
      title: "Sports",
      url: SPORTS_URL,
      presorted: true,
      stats: true,
    },
    {
      container: "otherStories",
      title: "Other",
      url: SECTION_URL,
      category: "Other",
    },
  ];

  const LEAN_LABEL = { left: "Left", center: "Centre", right: "Right" };

  //
  // The sports page's filter tabs. Every panel and every headline on it carries
  // one of these, written by the pipeline, and the tabs show one at a time.
  //
  // EuroLeague is in the list and has no results panel behind it: the competition
  // publishes nothing this pipeline can reach, so that tab filters the news and
  // finds no scoreboard — which the page shows by drawing nothing above it, rather
  // than by inventing a table.
  //
  const LEAGUE_TABS = [
    { key: "ALL", label: "All", icon: "grid" },
    { key: "NBA", label: "NBA", icon: "ball" },
    { key: "EUROLEAGUE", label: "EuroLeague", icon: "euro" },
    { key: "F1", label: "F1", icon: "flag" },
  ];

  // Inline rather than a sprite or a font: there are six of them, they never
  // change, and a page that is served from a file has no business making six more
  // requests. Stroked with currentColor, so a tab's colour comes from its CSS.
  const ICONS = {
    grid:
      '<svg viewBox="0 0 16 16"><g class="ico-dots"><circle cx="3" cy="3" r="1.5"/>' +
      '<circle cx="8" cy="3" r="1.5"/><circle cx="13" cy="3" r="1.5"/>' +
      '<circle cx="3" cy="8" r="1.5"/><circle cx="8" cy="8" r="1.5"/>' +
      '<circle cx="13" cy="8" r="1.5"/><circle cx="3" cy="13" r="1.5"/>' +
      '<circle cx="8" cy="13" r="1.5"/><circle cx="13" cy="13" r="1.5"/></g></svg>',
    ball:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><circle cx="8" cy="8" r="6.3"/>' +
      '<path d="M1.7 8h12.6M8 1.7v12.6"/>' +
      '<path d="M3.6 3.6c2.5 2.5 2.5 6.3 0 8.8M12.4 3.6c-2.5 2.5-2.5 6.3 0 8.8"/></g></svg>',
    euro:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><path d="M2.4 5.6h11.2L8 13.4z"/>' +
      '<path d="M1.9 3.2h12.2"/></g></svg>',
    flag:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><path d="M3.4 1.4v13.2"/>' +
      '<path d="M3.4 2.8h9.2l-1.6 2.4 1.6 2.4H3.4"/></g>' +
      '<g class="ico-dots"><circle cx="5.2" cy="8.9" r="0.85"/><circle cx="7.2" cy="8.9" r="0.85"/>' +
      '<circle cx="9.2" cy="8.9" r="0.85"/><circle cx="11.2" cy="8.9" r="0.85"/>' +
      '<circle cx="4.2" cy="11" r="0.85"/><circle cx="6.2" cy="11" r="0.85"/>' +
      '<circle cx="8.2" cy="11" r="0.85"/><circle cx="10.2" cy="11" r="0.85"/>' +
      '<circle cx="12.2" cy="11" r="0.85"/></g></svg>',
    cal:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><rect x="2" y="3.2" width="12" height="10.6" rx="1.6"/>' +
      '<path d="M2 6.6h12M5.2 1.8v2.8M10.8 1.8v2.8"/></g></svg>',
    chart:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><path d="M2.6 12.6v-4.2M6 12.6v-7.2M9.4 12.6v-3M12.8 12.6v-8.4"/></g></svg>',
    cloud:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><path d="M4.9 12.1h6.4a2.6 2.6 0 0 0 .4-5.2 3.5 3.5 0 0 0-6.7-.9 2.8 2.8 0 0 0-.1 6.1z"/></g></svg>',
    play: '<svg viewBox="0 0 16 16"><path class="ico-solid" d="M5.4 3.2l7 4.8-7 4.8z"/></svg>',
    pause:
      '<svg viewBox="0 0 16 16"><path class="ico-solid" d="M5 3.4h2v9.2H5zM9 3.4h2v9.2H9z"/></svg>',
    // The weather rows. Drawn small and stroked, because they sit in a line of
    // small capitals and a filled shape at this size reads as a smudge.
    drop:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><path d="M8 2.4s4 4.4 4 7a4 4 0 0 1-8 0c0-2.6 4-7 4-7z"/></g></svg>',
    thermometer:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><path d="M9.4 9.4V3.6a1.4 1.4 0 0 0-2.8 0v5.8a2.6 2.6 0 1 0 2.8 0z"/></g></svg>',
    wind:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><path d="M1.8 6.2h8.4a2 2 0 1 0-2-2"/>' +
      '<path d="M1.8 9.6h10.6a2 2 0 1 1-2 2"/></g></svg>',
    gauge:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><path d="M2.4 12.2a6.6 6.6 0 1 1 11.2 0z"/>' +
      '<path d="M8 12.2l3-3.6"/></g></svg>',
    sunrise:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><path d="M2.2 13.4h11.6"/>' +
      '<path d="M4.6 10.4a3.4 3.4 0 0 1 6.8 0"/><path d="M8 1.8v2.4M3.4 4.2l1.4 1.4M12.6 4.2l-1.4 1.4"/></g></svg>',
    clock:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><circle cx="8" cy="8" r="6"/>' +
      '<path d="M8 4.4V8l2.4 1.6"/></g></svg>',
    map:
      '<svg viewBox="0 0 16 16"><g class="ico-line"><path d="M1.8 4l4.2-2 4 2 4.2-2v10l-4.2 2-4-2-4.2 2z"/>' +
      '<path d="M6 2v10M10 4v10"/></g></svg>',
  };

  // Left first, then centre, then right. Grouped rather than interleaved: the
  // arrows walk all the left framing, then all the centre, then all the right,
  // so you can read one perspective through before moving to the next.
  const FLIP_ORDER = ["left", "center", "right"];

  // ---------------------------------------------------------------- ranking

  /**
   * Stories ordered by the pipeline — see select_briefing in fetch_coverage.py.
   *
   * The ranking lives there, not here, so this page and the desk line cannot
   * disagree about what the briefing contains. That rule used to be duplicated
   * in two languages, and drifting apart is what made the desk line open with a
   * ferry disaster that never reached the page.
   */
  function rankStories(rows) {
    return rows
      .filter((row) => Number.isInteger(row.briefing_rank))
      .sort((a, b) => a.briefing_rank - b.briefing_rank);
  }

  /**
   * Stories for one category page: most significant first, newest first within a
   * tier.
   *
   * This rule is restated here rather than published by the pipeline, unlike the
   * World page's briefing_rank. It is a two-key sort with no cross-story
   * constraints — no topic caps, no lean balance — so the failure that made
   * briefing_rank necessary cannot arise here: there is nothing for two
   * implementations to drift apart about. If it ever grows a cap or a balance
   * rule, it belongs in the pipeline beside select_briefing.
   */
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

  /** Flatten a story's coverage into the frames the arrows step through. */
  function buildFrames(story) {
    // A beat that gathered several newsrooms onto one event supplies its frames
    // directly. Those are not political leanings — they are separate outlets
    // agreeing on a fact — so there is no lean to label and no pill to show.
    if (Array.isArray(story.frames) && story.frames.length) {
      return story.frames.filter(
        (frame) => frame && (frame.title || frame.url),
      );
    }

    const coverage = story.coverage || {};
    const frames = [];

    // Lean-major: every left frame, then every centre frame, then every right.
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

    // Category pages have no gathered coverage, so the story's own outlet is the
    // only frame. No lean, and so no pill: see the note at the top of this file.
    if (!frames.length) frames.push({ ...story, lean: null });

    return frames;
  }

  // ------------------------------------------------------------ dom helpers

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  /**
   * When a fixture starts, as far as it is known.
   *
   * Dates arrive from three sources with three habits: ESPN sends a full
   * timestamp, Jolpica sends a bare date and sometimes a time beside it. Anything
   * that parses becomes a short date; anything that does not is shown as it came,
   * which beats an "Invalid Date" where the fixture should be.
   */
  function formatFixture(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    const when = new Date(text);
    if (Number.isNaN(when.getTime())) return text;
    const day = when.toLocaleDateString("en-GB", {
      weekday: "short",
      day: "numeric",
      month: "short",
    });
    // Only show a clock when the source carried one. ESPN's timestamps do and
    // Jolpica's bare dates do not, and midnight is not a kick-off time.
    return /\d{2}:\d{2}/.test(text) ? `${day} · ${formatClock(text)}` : day;
  }

  function formatClock(iso) {
    const stamp = new Date(iso);
    if (Number.isNaN(stamp.getTime())) return "";
    return stamp.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  /**
   * One of the page's data files, from wherever this copy of the page got it.
   *
   * Two ways, because the paper now travels two ways. Served over HTTP it
   * fetches, exactly as it always has. Delivered as a single file — which is how
   * it reaches a phone now, as a Telegram attachment — there is nothing to
   * fetch: every browser refuses `fetch()` on a file:// URL, so a bundle carrying
   * its own data would otherwise open to a masthead with no news under it.
   *
   * bundle.py writes the files into window.__BRIEF__ while it inlines everything
   * else. A hosted page never defines that object, so the whole cost there is one
   * property lookup that misses.
   */
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

  // ------------------------------------------------------------- card build

  function buildCard(story, frames, storyIndex) {
    const card = el("article", "news-card");
    card.dataset.storyIndex = String(storyIndex);

    // Image — falls back to a neutral gradient when the feed carried none.
    const image = el("div", "card-image");
    const img = el("img", "card-image-img");
    img.alt = "";
    img.loading = "lazy";
    // Several sports CDNs refuse to serve an image to a page on another origin.
    // motorsport.com, sportando.basketball, espncdn.com and talkbasket.net all
    // returned the file to a bare request and nothing at all to a browser that
    // named a referrer — the card reserved its 16:9 box and painted an empty
    // rectangle into it. Asking for no referrer gets the image and gives nothing
    // away: the request still goes straight to the publisher's own CDN.
    img.referrerPolicy = "no-referrer";
    image.appendChild(img);

    // A feed can declare an image that then fails to load — hotlink protection, an
    // expired signed URL, a 404. Without this the card reserves a 16:9 box and
    // paints nothing into it, which reads as a broken page rather than as a story
    // that has no picture. Collapsing makes the two cases identical.
    //
    // pendingSrc guards the arrows: stepping to a frame with a good image must not
    // be undone by the previous frame's image failing a moment later. The check is
    // against the src the element currently holds, so a late error for a src we
    // have already replaced is ignored.
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
      const weekly = days && days <= 7 ? " this week" : "";
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

    // Arrows only when there is more than one framing to step through. Category
    // pages carry a single outlet per story, and a lone arrow pair would be a
    // control that does nothing when pressed.
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

    // No meta line: "WORLD — 1 outlet" repeated on every card, and the count was
    // misleading anyway — it tallied DISCOVERY outlets (a 21-feed roster) while
    // the coverage below comes from the 6-outlet covering roster, so a card
    // could read "1 outlet" above seven outlets' worth of framing.
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

    // ------------------------------------------------------------ rendering

    let index = 0;

    function paint() {
      const frame = frames[index] || {};
      const lead = story;
      const lean = frame.lean;

      headlineLink.textContent = frame.title || lead.title || "";
      headlineLink.href = frame.url || lead.url || "#";
      headlineLink.target = "_blank";
      headlineLink.rel = "noopener noreferrer";

      summary.textContent = frame.description || lead.description || "";

      source.textContent = frame.source || lead.source || "";

      leanPill.textContent = LEAN_LABEL[lean] || "";
      leanPill.dataset.lean = lean || "";
      leanPill.hidden = !lean;

      // No interest tag on the card. fetch_sports.py still writes interest_label —
      // the run log uses it to show why a story was picked — but on the card it
      // read as clutter: two grey labels side by side, the second dotted twice
      // ("· NBA · THUNDER"), saying what the headline already said.

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
      if (event.key === "ArrowLeft") step(-1);
      if (event.key === "ArrowRight") step(1);
    });

    if (frames.length) paint();
    return card;
  }

  // ------------------------------------------------------------------- boot

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

    // Try each picture in turn and take the first that actually loads. One attempt
    // was not enough: the lead story usually has no picture at all — on Sports the
    // first three interests come from Google, which cannot supply one — and the
    // next candidate is often hotlink-protected (talkbasket.net answers 403), so a
    // single try landed on the gradient most days. On failure nothing is set, so
    // the stylesheet's gradient remains the floor.
    const accent = section.dataset.accentRgb || "14,116,144";
    let attempt = 0;

    const tryNext = () => {
      if (attempt >= candidates.length) return;
      const src = candidates[attempt++];
      const probe = new Image();
      probe.referrerPolicy = "no-referrer";
      probe.addEventListener("load", () => {
        // Two layers over the photograph: the page's accent for colour and
        // identity, and a plain darkening for contrast. The darkening is the one
        // that matters. A title is legible when it is darker than the cream page,
        // and the lead picture is often pale — a satellite view, an overcast
        // street — which left LIETUVA as a washed grey ghost. Lightening the wash
        // instead would help those and ruin the dark lead photos, so the bias is
        // deliberately towards dark.
        //
        // Adapting per image would be better and is not possible: reading the
        // pixels taints the canvas, because the photograph comes from another
        // origin.
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

  // Parsed once and cloned, rather than assigned as markup at each use. The
  // strings are constants so neither form can carry a script, but a clone is
  // cheaper than re-parsing the same markup for every tab and every panel, and it
  // keeps the one operation that turns text into elements in a single place.
  const iconCache = new Map();

  /**
   * An SVG element from a markup string, parsed once and cloned after.
   *
   * Keyed by the markup rather than by a name, because two callers now want two
   * different things from it: the page's fixed icon set asks by name, and the
   * weather asks with the drawing itself. Passing a weather drawing to a lookup
   * that reads ICONS[name] finds nothing and returns null — silently, since
   * nothing is null in a way that renders as no icon rather than as an error.
   *
   * Parsed as HTML, NOT as "image/svg+xml". Chromium's XML parser returns the
   * root element with a null namespace, and an element in no namespace is not an
   * SVG: every icon sat in the DOM at the right size, reserving its space, and
   * painted nothing. The tabs and panel headings were blank for days while the
   * boxes around them measured correctly. HTML parsing puts <svg> in the SVG
   * namespace, which is the only thing that makes it draw.
   */
  function svgFrom(markup) {
    if (!markup) return null;
    if (!iconCache.has(markup)) {
      const parsed = new DOMParser().parseFromString(markup, "text/html");
      const svg = parsed.body && parsed.body.firstElementChild;
      if (!svg || svg.nodeName !== "svg") return null;
      iconCache.set(markup, document.importNode(svg, true));
    }
    return iconCache.get(markup).cloneNode(true);
  }

  /** One of the page's own icons, by name. */
  function icon(name) {
    return svgFrom(ICONS[name]);
  }

  /**
   * One dashboard panel's frame: heading, icon and the row of cards inside.
   *
   * Every panel-card carries its own `data-league`, not just the panel, because
   * the strips mix competitions — FINAL GAMES holds an NBA game and a grand prix
   * side by side, and tapping NBA should leave one of them standing rather than
   * hiding the whole strip.
   *
   * The second parameter is the icon's name, not the icon: calling it `icon`
   * shadowed the icon() helper with a string and every panel threw.
   */
  function panelShell(panel, glyphName) {
    const node = el("section", "panel");
    node.dataset.league = panel.league || "";
    // A panel can name the tabs it belongs to (see the EuroLeague news panel,
    // which exists because its tab has no results and is redundant under ALL,
    // where the same headlines are already in the feed below it). Absent means
    // every tab.
    if (panel.tabs && panel.tabs.length)
      node.dataset.tabs = panel.tabs.join(" ");

    const head = el("div", "panel-head");
    const label = el("div", "panel-label");
    const glyph = icon(glyphName);
    if (glyph) label.appendChild(glyph);
    label.appendChild(document.createTextNode(panel.title || ""));
    head.appendChild(label);
    if (panel.note) head.appendChild(el("span", "panel-note", panel.note));
    node.appendChild(head);
    return node;
  }

  function card(league) {
    const node = el("div", "panel-card");
    node.dataset.league = league || "";
    return node;
  }

  /** One finished game: crest, name, score, per side. */
  function gameCard(game) {
    const node = card(game.league);
    node.appendChild(el("p", "card-league", game.league || ""));

    if (game.teams) {
      for (const team of game.teams) {
        const line = el("div", "team-line");
        if (team.winner) line.classList.add("is-winner");
        if (team.logo) {
          const crest = el("img", "team-crest");
          crest.src = team.logo;
          crest.alt = "";
          crest.loading = "lazy";
          // A crest that will not load leaves a gap the width of a crest.
          crest.addEventListener("error", () => crest.remove());
          line.appendChild(crest);
        }
        line.appendChild(el("span", "team-name", team.name || ""));
        line.appendChild(el("span", "team-score", team.score || ""));
        node.appendChild(line);
      }
    }

    // A race is not two teams with a score, so it comes as an ordered list of
    // finishers instead: position, driver, and the time or the gap behind.
    if (game.lines) {
      if (game.title) node.appendChild(el("p", "card-title", game.title));
      for (const line of game.lines) {
        const row = el("div", "race-line");
        row.appendChild(el("span", "race-place", `${line.place}.`));
        row.appendChild(el("span", "race-name", line.name || ""));
        row.appendChild(el("span", "race-value", line.value || ""));
        node.appendChild(row);
      }
    }

    node.appendChild(el("p", "card-status", game.status || ""));
    return node;
  }

  /** One team: crest and identity on the left, season averages on the right. */
  function spotlightCard(team) {
    const node = card(team.league);
    // Wider than a result card — see the stylesheet — because it carries a table.
    node.classList.add("spot-card");

    const top = el("div", "spot-top");
    const id = el("div", "spot-id");
    if (team.logo) {
      const crest = el("img", "spot-crest");
      crest.src = team.logo;
      crest.alt = "";
      crest.loading = "lazy";
      crest.addEventListener("error", () => crest.remove());
      id.appendChild(crest);
    }
    // Split so the place reads small and the nickname reads large: "OKLAHOMA CITY"
    // above "THUNDER", which is how a team is said out loud.
    const words = String(team.team || "").split(" ");
    const nickname = words.length > 1 ? words.pop() : "";
    if (nickname) id.appendChild(el("p", "spot-city", words.join(" ")));
    id.appendChild(el("p", "spot-name", nickname || team.team || ""));
    if (team.record) id.appendChild(el("p", "spot-record", team.record));
    if (team.standing) id.appendChild(el("p", "spot-standing", team.standing));
    top.appendChild(id);

    if (team.stats && team.stats.length) {
      const stats = el("div", "spot-stats");
      stats.appendChild(el("p", "spot-label", "Team stats"));
      for (const [name, value] of team.stats) {
        const row = el("div", "spot-stat");
        row.appendChild(el("span", "spot-stat-name", name));
        row.appendChild(el("b", "spot-stat-value", value));
        stats.appendChild(row);
      }
      top.appendChild(stats);
    }
    node.appendChild(top);

    const player = team.player;
    if (player && player.name) {
      const block = el("div", "spot-player");
      const glyph = icon("chart");
      if (glyph) block.appendChild(glyph);
      block.appendChild(el("p", "spot-label", "Key player (team leader)"));
      block.appendChild(el("p", "spot-player-name", player.name));
      const stats = el("div", "spot-player-stats");
      for (const [value, name] of player.stats || []) {
        const cell = el("div", "spot-player-stat");
        cell.appendChild(el("b", "", value));
        cell.appendChild(el("span", "", name));
        stats.appendChild(cell);
      }
      block.appendChild(stats);
      node.appendChild(block);
    }
    return node;
  }

  /** One fixture that has not been played. */
  function upcomingCard(game) {
    const node = card(game.league);
    node.appendChild(el("p", "card-league", game.league || ""));
    for (const team of game.teams || []) {
      const line = el("div", "team-line");
      if (team.logo) {
        const crest = el("img", "team-crest");
        crest.src = team.logo;
        crest.alt = "";
        crest.loading = "lazy";
        crest.addEventListener("error", () => crest.remove());
        line.appendChild(crest);
      }
      line.appendChild(el("span", "team-name", team.name || ""));
      node.appendChild(line);
    }
    node.appendChild(el("p", "card-status", formatFixture(game.time)));
    return node;
  }

  /** One headline, as a card: no picture and no summary, because it is an index. */
  function storyCard(story, league) {
    // The league is passed in rather than left blank: the card is filtered by the
    // same rule as every other card, and a blank one matched no tab at all — so
    // the EuroLeague panel hid itself under the one tab it exists for.
    const node = card(league || "");
    const title = el("a", "story-title", story.title || "");
    title.href = story.url || "#";
    title.rel = "noopener noreferrer";
    node.appendChild(title);
    if (story.source) node.appendChild(el("p", "story-source", story.source));
    return node;
  }

  /** Columns and rows, used by the standings panels and the feature card alike. */
  function buildTable(columns, rows, label) {
    const wrap = el("div", "table");
    if (label) wrap.appendChild(el("p", "table-label", label));

    // Which column should absorb the leftover width.
    //
    // It is the one holding the longest text: a player's name in one table, a
    // driver's in another, a position in a third. Every other column takes exactly
    // what it needs. A fixed index cannot work — the F1 tables lead with "1" and
    // the player table leads with a name, so "whichever column is first" pushed
    // every driver hard against the right margin with their positions stranded on
    // the far left, in one table or the other, always.
    const heads = columns || [];
    const body = (rows || []).slice(0, MAX_STAT_ROWS);
    let widest = 0;
    let longest = -1;
    heads.forEach((column, index) => {
      const width = Math.max(
        String(column).length,
        ...body.map((row) => String(row[index] ?? "").length),
      );
      if (width > longest) {
        longest = width;
        widest = index;
      }
    });
    wrap.style.setProperty(
      "--table-cols",
      heads
        .map((_, index) =>
          index === widest ? "minmax(0, 1fr)" : "max-content",
        )
        .join(" "),
    );

    const header = el("div", "table-row table-row--head");
    for (const column of columns || []) {
      header.appendChild(el("span", "table-cell", column));
    }
    wrap.appendChild(header);

    for (const row of (rows || []).slice(0, MAX_STAT_ROWS)) {
      const line = el("div", "table-row");
      row.forEach((cell, index) => {
        const node = el("span", "table-cell", String(cell));
        // The first column is the position or the player's name. Either way it is
        // the one the eye lands on, so it carries the emphasis.
        if (index === 0) node.classList.add("table-cell--lead");
        line.appendChild(node);
      });
      wrap.appendChild(line);
    }
    return wrap;
  }

  /** The feature card: a story, its picture, and a championship table beside it. */
  function featureCard(panel) {
    const node = card(panel.league);
    node.classList.add("feature");

    if (panel.image) {
      const shot = el("img", "feature-img");
      shot.src = panel.image;
      shot.alt = "";
      shot.loading = "lazy";
      // A feature is a story first. If the photograph is hotlink-protected the
      // card closes up rather than holding an empty frame where a picture was.
      shot.addEventListener("error", () => shot.remove());
      node.appendChild(shot);
    }

    const body = el("div", "feature-body");
    body.appendChild(el("p", "feature-kicker", panel.league || ""));
    const title = el("h3", "feature-title");
    const link = el("a", "feature-link", panel.title || "");
    link.href = panel.url || "#";
    link.rel = "noopener noreferrer";
    title.appendChild(link);
    body.appendChild(title);
    // Not truncated here. The stylesheet clamps it to three lines, which cuts
    // between words rather than through one and does not spend a length constant
    // pretending to know how wide the phone is.
    if (panel.summary) {
      body.appendChild(el("p", "feature-summary", panel.summary));
    }
    if (panel.source) body.appendChild(el("p", "feature-source", panel.source));
    const more = el("a", "feature-more", "Full story →");
    more.href = panel.url || "#";
    more.rel = "noopener noreferrer";
    body.appendChild(more);
    node.appendChild(body);

    if (panel.table) {
      const side = el("div", "feature-side");
      side.appendChild(
        buildTable(panel.table.columns, panel.table.rows, panel.table.title),
      );
      node.appendChild(side);
    }
    return node;
  }

  /**
   * The weather: what it is now, what the radar says is coming, and four days.
   *
   * The radar is two grids of nine tiles stacked, not one image: the tiles are
   * transparent overlays and the base map has to sit under them. Inline as data
   * URIs, which is why this works with no network — see fetch_weather.py.
   */
  // Weather art, one per kind of weather.
  //
  // Drawn rather than imported: nine shapes is cheaper than a font or a sprite,
  // and they take their colours from the stylesheet the way every other icon on
  // the page does. The pipeline turns a WMO code into words because a number is
  // not a forecast; this turns it into a shape because a word is not a glance.
  // Neither decides anything about the other's job.
  const WEATHER_ART = {
    clear:
      '<circle class="wi-sun" cx="12" cy="12" r="4.6"/>' +
      '<g class="wi-stroke"><path d="M12 1.6v2.8M12 19.6v2.8M1.6 12h2.8M19.6 12h2.8' +
      'M4.7 4.7l2 2M17.3 17.3l2 2M19.3 4.7l-2 2M6.7 17.3l-2 2"/></g>',
    partly:
      '<circle class="wi-sun" cx="9.4" cy="8.4" r="3.6"/>' +
      '<g class="wi-stroke"><path d="M9.4 1.8v2.2M1.8 8.4h2.2M4 3.1l1.6 1.6"/></g>' +
      '<path class="wi-cloud" d="M9.6 20.2h8.6a3.6 3.6 0 0 0 .5-7.2 4.9 4.9 0 0 0-9.2-1.2 4 4 0 0 0 .1 8.4z"/>',
    cloudy:
      '<path class="wi-cloud" d="M5.8 17.6h12.4a4.1 4.1 0 0 0 .5-8.2 5.6 5.6 0 0 0-10.6-1.4 4.5 4.5 0 0 0-.3 9.6z"/>',
    fog:
      '<path class="wi-cloud" d="M5.8 14.4h12.4a4.1 4.1 0 0 0 .5-8.2 5.6 5.6 0 0 0-10.6-1.4 4.5 4.5 0 0 0-.3 9.6z"/>' +
      '<g class="wi-stroke"><path d="M4.4 17.8h15.2M6.4 20.8h11.2"/></g>',
    drizzle:
      '<path class="wi-cloud" d="M5.8 14.4h12.4a4.1 4.1 0 0 0 .5-8.2 5.6 5.6 0 0 0-10.6-1.4 4.5 4.5 0 0 0-.3 9.6z"/>' +
      '<g class="wi-stroke"><path d="M10 17.6v2.2M14 17.6v2.2"/></g>',
    rain:
      '<path class="wi-cloud" d="M5.8 14.4h12.4a4.1 4.1 0 0 0 .5-8.2 5.6 5.6 0 0 0-10.6-1.4 4.5 4.5 0 0 0-.3 9.6z"/>' +
      '<g class="wi-stroke"><path d="M8.4 17.4v3M12 17.4v3M15.6 17.4v3"/></g>',
    heavy:
      '<path class="wi-cloud" d="M5.8 14.4h12.4a4.1 4.1 0 0 0 .5-8.2 5.6 5.6 0 0 0-10.6-1.4 4.5 4.5 0 0 0-.3 9.6z"/>' +
      '<g class="wi-stroke wi-heavy"><path d="M7.8 17.2l-1 4M12 17.2l-1 4M16.2 17.2l-1 4"/></g>',
    snow:
      '<path class="wi-cloud" d="M5.8 14.4h12.4a4.1 4.1 0 0 0 .5-8.2 5.6 5.6 0 0 0-10.6-1.4 4.5 4.5 0 0 0-.3 9.6z"/>' +
      '<g class="wi-dot"><circle cx="8.8" cy="18.6" r="1.1"/><circle cx="12" cy="21" r="1.1"/><circle cx="15.2" cy="18.6" r="1.1"/></g>',
    storm:
      '<path class="wi-cloud" d="M4.4 14.2h12.4a4.1 4.1 0 0 0 .5-8.2 5.6 5.6 0 0 0-10.6-1.4 4.5 4.5 0 0 0-.3 9.6z"/>' +
      '<path class="wi-bolt" d="M13.6 15.4l-3.6 4.4h2.4l-.8 3.4 3.6-4.6h-2.4z"/>',
  };

  /**
   * The drawing for one of the pipeline's kinds, as an element.
   *
   * Takes the kind, not the WMO code: the mapping from code to shape lives in
   * fetch_weather.py beside the mapping from code to words, so that the renderer
   * decides no more about the shape of the weather than it does about the
   * wording of it. The fallback is for a data file written before `kind` existed
   * — a cloudy drawing is wrong in a way nobody minds, where a blank square at
   * the top of the panel reads as a rendering fault.
   */
  function weatherIcon(kind, className) {
    const art = WEATHER_ART[kind] || WEATHER_ART.cloudy;
    const span = el("span", `wx-icon${className ? ` ${className}` : ""}`);
    // svgFrom, not icon(): icon() looks the drawing up by name in ICONS, and the
    // weather drawings are not in it.
    //
    // And the wrapper goes on here rather than being stored nine times. The
    // drawings above are fragments — a circle, a cloud, some drops — and svgFrom
    // wants a complete element: handed a bare <circle> it checks nodeName, finds
    // "circle", and returns null. That failure is silent, since a null icon
    // renders as nothing at all, and it cost two rounds of checking the DOM.
    const glyph = svgFrom(`<svg viewBox="0 0 24 24">${art}</svg>`);
    if (glyph) span.appendChild(glyph);
    return span;
  }
  /**
   * One row of small facts, each with an icon, divided by hairlines.
   *
   * The dividers are borders on the cells rather than elements between them: a
   * rule that is part of the cell cannot end up as the last thing in the row, and
   * a trailing rule is the sort of detail that only shows up in a screenshot.
   */
  function wxFactRow(facts, className) {
    const row = el("div", `wx-facts${className ? ` ${className}` : ""}`);
    row.style.setProperty("--wx-cols", String(Math.max(1, facts.length)));
    for (const fact of facts) {
      const cell = el("div", "wx-fact");
      const glyph = icon(fact.icon);
      if (glyph) cell.appendChild(glyph);
      const body = el("div", "wx-fact-body");
      body.appendChild(el("p", "wx-fact-label", fact.label));
      const value = el("p", "wx-fact-value", fact.value);
      if (fact.sub) value.appendChild(el("span", "wx-fact-sub", fact.sub));
      body.appendChild(value);
      cell.appendChild(body);
      row.appendChild(cell);
    }
    return row;
  }

  /**
   * The weather: what it is doing, four facts, the hours ahead, the radar, and
   * three days.
   *
   * Laid out after the weather mock. Its two typographic habits are the ones
   * worth copying, because they are what the rest of this paper already does:
   * every figure is set in the display face and every label is a small capital in
   * the body face. A temperature in Inter beside a headline in Fraunces was the
   * thing making this panel look like it came from somewhere else.
   */
  function weatherPanel(panel) {
    const node = card(panel.league || "");
    node.classList.add("weather");

    const now = panel.current || {};
    const today = (panel.days && panel.days[0]) || {};

    // The head is centred and two lines deep, which reads as a masthead for the
    // block rather than as a label with a note beside it.
    const head = el("div", "wx-head");
    head.appendChild(el("p", "wx-place", panel.title || ""));
    head.appendChild(el("p", "wx-kicker", "Today's weather"));
    node.appendChild(head);

    // The hero, in two halves with a rule between: what it is doing, and what to
    // expect. The split earns its place — the left answers "what is it like out"
    // and the right answers "do I need a coat".
    const hero = el("div", "wx-hero");

    const main = el("div", "wx-hero-main");
    const glyph = weatherIcon(now.kind, "wx-icon--lead");
    if (glyph) main.appendChild(glyph);
    const figure = el("div", "wx-hero-figure");
    figure.appendChild(
      el("p", "wx-temp", `${Math.round(Number(now.temperature) || 0)}°`),
    );
    figure.appendChild(el("p", "wx-desc", now.description || ""));
    main.appendChild(figure);
    hero.appendChild(main);

    const range = el("div", "wx-hero-range");
    if (today.max != null) {
      range.appendChild(el("p", "wx-range-line", `High ${Math.round(today.max)}°`));
    }
    if (today.min != null) {
      range.appendChild(
        el("p", "wx-range-line", `Low ${Math.round(today.min)}°`),
      );
    }
    if (today.sunrise) {
      const sun = el("p", "wx-sun");
      const sunGlyph = icon("sunrise");
      if (sunGlyph) sun.appendChild(sunGlyph);
      sun.appendChild(
        document.createTextNode(`${today.sunrise} – ${today.sunset}`),
      );
      range.appendChild(sun);
    }
    hero.appendChild(range);
    node.appendChild(hero);

    // The four facts. Pressure is the one that needed fetching; the rest were
    // already in the response and were being dropped on the floor.
    node.appendChild(
      wxFactRow([
        {
          icon: "thermometer",
          label: "Feels like",
          value:
            now.feels_like == null
              ? "—"
              : `${Math.round(Number(now.feels_like))}°`,
        },
        {
          icon: "drop",
          label: "Humidity",
          value: now.humidity == null ? "—" : `${now.humidity}%`,
        },
        {
          icon: "wind",
          label: "Wind",
          value: now.wind == null ? "—" : `${now.wind} km/h`,
          sub: now.wind_direction || "",
        },
        {
          icon: "gauge",
          label: "Pressure",
          value: now.pressure == null ? "—" : `${now.pressure} hPa`,
        },
      ]),
    );

    // The hours, with a heading of their own. Six columns, which is the reach of
    // a short forecast and the most that fits at a readable size.
    if (panel.hours && panel.hours.length) {
      const section = el("div", "wx-section");
      section.appendChild(el("p", "wx-label", "Hourly forecast"));
      if (panel.updated) section.appendChild(el("p", "wx-note", panel.updated));
      node.appendChild(section);

      const strip = el("div", "wx-hours");
      strip.style.setProperty("--wx-hours", String(panel.hours.length));
      for (const hour of panel.hours) {
        const cell = el("div", "wx-hour");
        cell.appendChild(el("p", "wx-hour-time", hour.time || ""));
        const art = weatherIcon(hour.kind, "wx-icon--hour");
        if (art) cell.appendChild(art);
        cell.appendChild(
          el("p", "wx-hour-temp", `${Math.round(Number(hour.temperature) || 0)}°`),
        );
        const chance = Number(hour.probability);
        if (Number.isFinite(chance) && chance >= WX_RAIN_WORTH_SHOWING) {
          const rain = el("p", "wx-hour-rain");
          const rainGlyph = icon("drop");
          if (rainGlyph) rain.appendChild(rainGlyph);
          rain.appendChild(document.createTextNode(`${chance}%`));
          cell.appendChild(rain);
        } else {
          cell.appendChild(el("p", "wx-hour-rain wx-hour-rain--quiet", ""));
        }
        strip.appendChild(cell);
      }
      node.appendChild(strip);
    }

    // The map.
    const radar = panel.radar;
    if (radar && radar.base) {
      const geo = el("div", "radar");
      const cols = String(radar.grid || 2);
      geo.style.setProperty("--radar-cols", cols);
      geo.style.setProperty("--radar-magnify", String(RADAR_MAGNIFY));

      const shift = radar.shift || [0, 0];
      geo.style.setProperty("--radar-shift-x", `${(shift[0] || 0) * 100}%`);
      geo.style.setProperty("--radar-shift-y", `${(shift[1] || 0) * 100}%`);

      const layer = (tiles) => {
        const grid = el("div", "radar-layer");
        for (const tile of tiles) {
          if (!tile) {
            // An empty cell rather than no cell: the grid has to keep its shape
            // or the whole map shifts by one tile and puts Kaunas in the sea.
            grid.appendChild(el("div", "radar-gap"));
            continue;
          }
          const img = el("img", "radar-tile");
          img.src = tile;
          img.alt = "";
          grid.appendChild(img);
        }
        return grid;
      };

      const base = layer(radar.base);
      base.classList.add("radar-base");
      geo.appendChild(base);

      // One layer per frame, stacked and cross-faded by a class. The class is
      // what the stylesheet holds at zero opacity — without it every frame sits
      // at full opacity on top of the last, which looks like a single working
      // image and hides the fact that the animation is doing nothing at all.
      const frames = (radar.frames || []).map((frame) => {
        const node2 = layer(frame.tiles);
        node2.classList.add("radar-frame");
        return node2;
      });
      for (const frame of frames) geo.appendChild(frame);

      // The marker, in the window rather than in the layer: the city is at the
      // middle of the view by construction — see build_radar — so this is a fixed
      // dot, and it is what makes the map a map of somewhere.
      if (Number.isFinite(Number(shift[0]))) {
        geo.appendChild(el("span", "radar-marker"));
      }
      node.appendChild(geo);

      const times = (radar.frames || []).map((frame) => frame.time || "");
      const stamp = el("span", "radar-time");

      const bar = el("div", "radar-bar");
      const scrub = el("input", "radar-scrub");
      scrub.type = "range";
      scrub.min = "0";
      scrub.max = String(Math.max(0, frames.length - 1));
      scrub.step = "1";
      scrub.value = "0";
      scrub.setAttribute("aria-label", "Radar frame");

      const play = el("button", "radar-play");
      play.type = "button";

      if (frames.length > 1) {
        let current = 0;
        let running = false;
        let timer = 0;

        const show = (index) => {
          current = index;
          frames.forEach((frame, i) => {
            frame.classList.toggle("is-shown", i === index);
          });
          scrub.value = String(index);
          stamp.textContent = times[index] || "";
        };

        const stop = () => {
          running = false;
          if (timer) window.clearInterval(timer);
          timer = 0;
          const glyph2 = svgFrom(ICONS.play);
          play.replaceChildren();
          if (glyph2) play.appendChild(glyph2);
          play.setAttribute("aria-label", "Play the radar loop");
          play.classList.remove("is-playing");
        };

        const start = () => {
          if (running) return;
          running = true;
          const glyph2 = svgFrom(ICONS.pause);
          play.replaceChildren();
          if (glyph2) play.appendChild(glyph2);
          play.setAttribute("aria-label", "Pause the radar loop");
          play.classList.add("is-playing");
          timer = window.setInterval(() => {
            show((current + 1) % frames.length);
          }, RADAR_FRAME_MS);
        };

        play.addEventListener("click", () => (running ? stop() : start()));

        // Dragging stops it. The point of a scrubber is to look at one frame, and
        // an animation that carries on underneath the hand doing the looking is
        // fighting the reader.
        scrub.addEventListener("input", () => {
          stop();
          show(Number(scrub.value) || 0);
        });

        show(0);

        // Respecting the setting rather than animating regardless: a looping map
        // is exactly the kind of motion the preference exists for. The scrubber
        // stays, so the earlier frames are still reachable.
        if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
          show(frames.length - 1);
          stop();
        } else {
          start();
        }
      } else {
        const glyph2 = svgFrom(ICONS.play);
        if (glyph2) play.appendChild(glyph2);
        play.disabled = true;
        scrub.disabled = true;
        stamp.textContent = radar.time || "";
      }

      bar.appendChild(play);
      bar.appendChild(scrub);
      node.appendChild(bar);

      const wetIndex = (panel.hours || []).findIndex(
        (hour) => Number(hour.probability) >= WX_RAIN_CERTAIN,
      );
      // The row starts at the current hour, so finding rain there means it is
      // raining now — and "next rain 12:00" at half past twelve is a claim about
      // the future that the present has already contradicted.
      const wet = !panel.hours || wetIndex < 0
        ? "None today"
        : wetIndex === 0
          ? "Now"
          : panel.hours[wetIndex].time;
      const foot = el("div", "wx-facts radar-foot");
      foot.style.setProperty("--wx-cols", "3");
      const cells = [
        { icon: "clock", label: "Radar", value: stamp },
        {
          icon: "drop",
          label: "Next rain",
          value: wet,
        },
        {
          icon: "map",
          label: radar.attribution || "",
          value: "",
        },
      ];
      for (const item of cells) {
        const cell = el("div", "wx-fact");
        const glyph2 = icon(item.icon);
        if (glyph2) cell.appendChild(glyph2);
        const body = el("div", "wx-fact-body");
        if (item.label) body.appendChild(el("p", "wx-fact-label", item.label));
        if (typeof item.value === "string") {
          if (item.value) body.appendChild(el("p", "wx-fact-value", item.value));
        } else {
          const line = el("p", "wx-fact-value");
          line.appendChild(item.value);
          body.appendChild(line);
        }
        cell.appendChild(body);
        foot.appendChild(cell);
      }
      node.appendChild(foot);
    } else {
      node.appendChild(el("p", "radar-absent", "Radar unavailable this morning."));
    }

    if (panel.days && panel.days.length > 1) {
      // In a section wrapper like the hourly heading, so the two labels sit the
      // same distance from what is above them.
      const section = el("div", "wx-section");
      section.appendChild(el("p", "wx-label", "Three days"));
      node.appendChild(section);

      const row = el("div", "wx-days");
      for (const day of panel.days.slice(1, 4)) {
        const cell = el("div", "wx-day");
        const art = weatherIcon(day.kind, "wx-icon--day");
        if (art) cell.appendChild(art);
        cell.appendChild(el("p", "wx-day-name", day.weekday || ""));
        cell.appendChild(el("p", "wx-day-desc", day.description || ""));
        cell.appendChild(
          el("p", "wx-day-temp", `${Math.round(day.min)}°/${Math.round(day.max)}°`),
        );
        if (day.probability != null) {
          const rain = el("p", "wx-day-rain");
          const rainGlyph = icon("drop");
          if (rainGlyph) rain.appendChild(rainGlyph);
          rain.appendChild(document.createTextNode(`${day.probability}%`));
          cell.appendChild(rain);
        }
        row.appendChild(cell);
      }
      node.appendChild(row);
    }

    return node;
  }

  /** Draw one panel in whichever of the shapes it asked for. */
  function buildPanel(panel) {
    switch (panel.kind) {
      case "games": {
        const node = panelShell(panel, "ball");
        const row = el("div", "panel-row");
        for (const game of panel.cards || []) row.appendChild(gameCard(game));
        node.appendChild(row);
        return node;
      }
      case "spotlight": {
        const node = panelShell(panel, "ball");
        const row = el("div", "panel-row");
        for (const team of panel.cards || [])
          row.appendChild(spotlightCard(team));
        node.appendChild(row);
        return node;
      }
      case "upcoming": {
        const node = panelShell(panel, "cal");
        const row = el("div", "panel-row");
        for (const game of panel.cards || [])
          row.appendChild(upcomingCard(game));
        node.appendChild(row);
        return node;
      }
      case "stories": {
        const node = panelShell(panel, "euro");
        const row = el("div", "panel-row");
        for (const story of panel.cards || []) {
          row.appendChild(storyCard(story, panel.league));
        }
        node.appendChild(row);
        return node;
      }
      case "weather": {
        const node = el("section", "panel panel--bare");
        node.dataset.league = panel.league || "";
        node.appendChild(weatherPanel(panel));
        return node;
      }
      case "feature": {
        const node = el("section", "panel panel--bare");
        node.dataset.league = panel.league || "";
        node.appendChild(featureCard(panel));
        return node;
      }
      case "table": {
        const node = panelShell(panel, "chart");
        // Wrapped like every other card, so the tab filter can count it: a panel
        // whose cards cannot be found reads as empty and gets hidden.
        const body = card(panel.league);
        body.classList.add("table-card");
        // No label: the panel heading already carries the note, and passing it
        // here too printed the season twice, once at each end of the panel.
        body.appendChild(buildTable(panel.columns, panel.rows));
        node.appendChild(body);
        return node;
      }
      default:
        return null;
    }
  }

  /**
   * Show one competition and hide the rest, panels and headlines together.
   *
   * Cards are filtered inside their panel rather than the panel being filtered as
   * a unit, and then a panel left holding nothing is hidden — which is what makes
   * EUROLEAGUE honest: this page carries EuroLeague news and no EuroLeague
   * results, because the competition publishes no feed this pipeline can reach,
   * so that tab shows its headlines and no empty scoreboard above them.
   */
  function applyLeague(scope, league) {
    const matches = (node) =>
      league === "ALL" || (node.dataset.league || "") === league;

    for (const node of scope.querySelectorAll(".feed-card")) {
      node.classList.toggle("is-hidden", !matches(node));
    }

    for (const panel of scope.querySelectorAll(".panel")) {
      // Asked for by name: a panel that lists the tabs it belongs to is shown on
      // those and nowhere else, not even under ALL.
      const named = panel.dataset.tabs;
      if (named && !named.split(" ").includes(league)) {
        panel.classList.add("is-hidden");
        continue;
      }

      let shown = 0;
      for (const node of panel.querySelectorAll(".panel-card")) {
        const ok = matches(node);
        node.classList.toggle("is-hidden", !ok);
        if (ok) shown += 1;
      }
      panel.classList.toggle("is-hidden", shown === 0);
    }

    // Dividers sit between news cards and belong to the card above them.
    for (const rule of scope.querySelectorAll("hr.divider")) {
      const previous = rule.previousElementSibling;
      rule.classList.toggle(
        "is-hidden",
        !previous || previous.classList.contains("is-hidden"),
      );
    }

    const feed = scope.querySelector(".feed");
    if (feed) {
      const any = [...feed.querySelectorAll(".feed-card")].some(
        (node) => !node.classList.contains("is-hidden"),
      );
      feed.classList.toggle("is-empty", !any);
    }
  }

  /**
   * Panels above a page's news, with no tabs.
   *
   * Split out of renderDashboard, which is the sports one and comes with a tab
   * strip. The weather needs the panels without the tabs — it is a single panel,
   * there is nothing to filter, and a filter row above a map would be furniture.
   */
  function renderPanels(container, panels) {
    if (!Array.isArray(panels) || !panels.length) return;

    const dash = el("div", "dash");
    let drawn = 0;
    for (const panel of panels) {
      const node = buildPanel(panel);
      if (!node) continue;
      dash.appendChild(node);
      drawn += 1;
    }
    if (drawn) container.appendChild(dash);
  }

  /**
   * The dashboard above the sports news: tabs, then the panels.
   *
   * Nothing is drawn when there is nothing to draw — a page with no statistics is
   * the news page it was before, not a dashboard full of empty frames.
   */
  function renderDashboard(container, panels) {
    if (!Array.isArray(panels) || !panels.length) return;

    const dash = el("div", "dash");
    const tabs = el("div", "dash-tabs");
    const buttons = [];
    for (const tab of LEAGUE_TABS) {
      const button = el("button", "dash-tab");
      button.type = "button";
      button.dataset.league = tab.key;
      const glyph = icon(tab.icon);
      if (glyph) button.appendChild(glyph);
      button.appendChild(document.createTextNode(tab.label));
      if (tab.key === "ALL") button.classList.add("is-active");
      button.addEventListener("click", () => {
        for (const other of buttons) {
          other.classList.toggle("is-active", other === button);
        }
        applyLeague(container, tab.key);
      });
      buttons.push(button);
      tabs.appendChild(button);
    }
    dash.appendChild(tabs);

    let drawn = 0;
    for (const panel of panels) {
      const node = buildPanel(panel);
      if (!node) continue;
      dash.appendChild(node);
      drawn += 1;
    }
    if (!drawn) return;

    container.appendChild(dash);
    // The tabs start on ALL, so ALL has to be applied rather than assumed. Not
    // calling this left every panel visible on load, including the ones that name
    // their own tab and should never appear under ALL at all.
    applyLeague(container, "ALL");
  }

  /**
   * At most five stories, and a few more only for the ones that earn it.
   *
   * A page is meant to be read. Ten cards is a scroll, and the thing being built
   * here is a briefing — if it takes longer to get through than a coffee, it is
   * the wrong length and gets skipped, which is the one outcome worse than
   * missing a story.
   *
   * Two things extend it past five, and the second matters more than it looks:
   *
   *   Notability. A story the classifier called high significance, or that three
   *   or more newsrooms carried independently, is worth a slot.
   *
   *   Representation. The last story of an interest is never dropped to make the
   *   count. Not a nicety: capping Sports at five once silently deleted EuroLeague
   *   and the international game, because a flat cap takes whatever comes first
   *   and the interests at the end of the round-robin vanish. That was a real bug
   *   and it is why the beats are ordered the way they are. A page that hides a
   *   whole competition to save three cards is not shorter, it is missing.
   *
   * The beats are built so that five can cover everything: their round-robins
   * walk a handful of coarse buckets rather than every interest they tag with, so
   * the first five stories already represent each part of the subject.
   */
  function capStories(stories) {
    if (stories.length <= MAX_STORIES) return stories;

    const kept = stories.slice(0, MAX_STORIES);
    const interests = new Set(
      kept.map((story) => story.interest).filter(Boolean),
    );

    for (const story of stories.slice(MAX_STORIES)) {
      if (kept.length >= MAX_STORIES_NOTABLE) break;
      const interest = story.interest;
      // Only ever the *first* story of an interest not yet on the page. A second
      // rescue of the same interest would not be representation, it would be a
      // loophole that lets any interest exceed the cap by publishing more.
      const unreached = interest && !interests.has(interest);
      if (unreached || isNotable(story)) {
        kept.push(story);
        if (interest) interests.add(interest);
      }
    }

    return kept;
  }

  /**
   * Did this one earn a slot past the fifth?
   *
   * Coverage only, and deliberately not significance. The rule was meant to be
   * "very good ones or much covered ones" and the first attempt used the
   * classifier's `high` tier for the first half of that. It does not work: 70 of
   * 231 stories come back high, and fetch_europe pre-filters its own rows to high,
   * so on that page every story was exceptional and the cap moved from five to
   * eight without anyone deciding anything.
   *
   * Three newsrooms independently carrying the same story is a fact about the
   * world rather than an opinion about it, and it is rare: three stories in the
   * whole of the last run. On a normal morning this page shows five stories.
   */
  function isNotable(story) {
    // Two shapes for the same idea: Europe carries `outlet_count` and the beats
    // carry the outlets themselves.
    const outlets = Number(
      story.outlet_count || (story.outlets || []).length || 0,
    );
    return outlets >= NOTABLE_OUTLETS;
  }

  /** Render one page's cards into its container. Returns how many it drew. */
  function renderSection(section, rows, container, extras) {
    // Two shapes of extra content, and a section may have either, both or
    // neither. Passed as one object rather than as two positional arguments
    // because the call site had three booleans' worth of meaning in two slots.
    const statsBlocks = (extras && extras.stats) || [];
    const panels = (extras && extras.panels) || null;

    let stories;
    if (section.category) {
      stories = rankSections(rows, section.category);
    } else if (section.presorted) {
      // Uncapped here on purpose, then capped below like everything else. The
      // beats already ranked these and balanced them across their buckets, so the
      // order is the pipeline's business; how many of them a page shows is this
      // file's. Applying a flat slice here instead is what once deleted
      // EuroLeague from Sports — see capStories.
      stories = rows.filter((row) => row.title && row.url);
    } else {
      stories = rankStories(rows);
    }

    stories = capStories(stories);

    if (!stories.length) {
      showMessage(
        container,
        `Nothing in ${section.title} today`,
        section.category
          ? `Nothing was classified as ${section.category} in the latest run. ` +
              "The page is wired — it fills as soon as a story lands in the category."
          : `The latest run found nothing for this page. Check logs/run-*.log, ` +
              "or run:  ./run.sh",
      );
      return 0;
    }

    // A beat's own panels, above its news. The weather for Lietuva; the
    // dashboard for Sports, which is parked.
    if (panels && panels.length) renderPanels(container, panels);

    if (section.stats && !DASHBOARD_PARKED) {
      renderDashboard(container, statsBlocks);
    }

    // The feed is wrapped so the tabs can tell an empty filter from a page with
    // no news on it: hiding every card under NBA should say so, not look broken.
    //
    // The wrapper and the empty message stay although the tabs are parked with the
    // dashboard above: they cost nothing, and a filter that comes back to find its
    // scaffolding missing is a filter nobody re-enables.
    const feed = el("div", "feed");
    feed.appendChild(
      el("p", "feed-empty", "Nothing for this competition today."),
    );
    container.appendChild(feed);

    stories.forEach((story, i) => {
      const node = buildCard(story, buildFrames(story), i);
      node.dataset.league = story.league || "";
      node.classList.add("feed-card");
      feed.appendChild(node);
      if (i < stories.length - 1) feed.appendChild(el("hr", "divider"));
    });

    paintTitle(container, stories);
    return stories.length;
  }

  /**
   * When this page was compiled, from data/built.json (written by run.sh).
   *
   * Returns true when the date line was set, so the caller knows whether to fall
   * back. The stamp comes from the run rather than from the newest story: a
   * story's publish time is when a newsroom filed it, which is neither when this
   * page was built nor any guide to whether the page is out of date.
   */
  async function loadBuildStamp() {
    const stamp = document.getElementById("worldUpdated");
    const warning = document.getElementById("worldStale");
    if (!stamp) return false;

    let builtAt;
    try {
      const payload = await loadJson(BUILT_URL);
      // Checked before constructing the date: `new Date(null)` is the epoch, not
      // an invalid date, so a missing file would have dated the paper 1970 and
      // tripped the stale badge rather than falling back.
      if (!payload || !payload.built_at) throw new Error("no stamp");
      builtAt = new Date(payload.built_at);
      if (Number.isNaN(builtAt.getTime())) throw new Error("bad timestamp");
    } catch {
      return false;
    }

    // Two calls because en-GB omits the comma between weekday and date, and the
    // masthead wants one: "Sunday, 13 September".
    const weekday = builtAt.toLocaleDateString("en-GB", { weekday: "long" });
    const dayMonth = builtAt.toLocaleDateString("en-GB", {
      day: "numeric",
      month: "long",
    });
    stamp.textContent = `${weekday}, ${dayMonth}`;

    // Every wired page carries the same build date. Only World owns the stale
    // badge, because one page being out of date means they all are.
    document.querySelectorAll("[data-stamp]").forEach((node) => {
      // The sports masthead sets the date opposite its title, where there is room
      // for two short lines and not for "Wednesday, 16 September" — which wrapped
      // to three in a seventy-five pixel column. Pages opt in with
      // data-stamp="compact"; the rest keep the long form under the masthead.
      if (node.dataset.stamp === "compact") {
        // en-US for the month specifically: British English abbreviates September
        // to four characters ("16 Sept") where the masthead has room for three.
        const short = builtAt.toLocaleDateString("en-GB", { day: "numeric" });
        const month = builtAt.toLocaleDateString("en-US", { month: "short" });
        const weekdayShort = builtAt.toLocaleDateString("en-GB", {
          weekday: "short",
        });
        // One string, two lines: the stylesheet sets white-space: pre-line.
        node.textContent = `${short} ${month}\n${weekdayShort}`.toUpperCase();
        return;
      }
      node.textContent = `${weekday}, ${dayMonth}`;
    });
    // Only speaks up when the paper is not today's; silence is the normal case.
    if (warning) {
      const hours = (Date.now() - builtAt.getTime()) / 36e5;
      if (hours >= STALE_AFTER_HOURS) {
        const age =
          hours >= 48
            ? `${Math.floor(hours / 24)} days ago`
            : `${Math.floor(hours)} hours ago`;
        warning.textContent = `⚠ Compiled ${age}`;
        warning.hidden = false;
      } else {
        warning.hidden = true;
      }
    }

    return true;
  }

  /**
   * Fill the desk quote, if the page still has a block for it.
   *
   * PARKED, but wired: the quote is out of the design, so #deskBlock is commented
   * out in index.html and the guard at the call site skips this. Kept live rather
   * than deleted because it encodes something easy to get wrong — a missing quote
   * must never blank the page, so it loads separately from the story list and
   * hides its own block instead of failing.
   */
  async function loadDeskQuote() {
    const block = document.getElementById("deskBlock");
    const target = document.getElementById("deskQuote");
    if (!block || !target) return;

    try {
      const payload = await loadJson(DESK_URL);
      if (!payload || !payload.quote) throw new Error("no quote in file");
      target.textContent = payload.quote;
    } catch {
      block.hidden = true;
    }
  }

  async function main() {
    const stamp = document.getElementById("worldUpdated");

    // The desk quote is parked: its markup is commented out in index.html, so
    // there is no #deskBlock and this is a no-op costing one lookup. Guarded
    // rather than commented out so the wiring stays live — restoring the quote is
    // then a markup change alone, with no JS to remember to uncomment.
    if (document.getElementById("deskBlock")) loadDeskQuote();

    const stamped = await loadBuildStamp();

    // One fetch per source file, not per page: three of the five pages read the
    // same tracked list. A missing file is not fatal to the others — each page
    // says so on its own. Panels are fetched alongside but never gate a page:
    // a dead results source should cost the reader a table, not a page, and a
    // dead weather service should cost them the weather.
    const urls = [...new Set(SECTIONS.map((s) => s.url))];
    // Panel sources, in a fixed order, so the indices below stay honest. A set
    // because two beats could one day share one — they do not today.
    const panelUrls = [STATS_URL, WEATHER_URL];
    const loaded = await Promise.all([
      ...urls.map((url) => loadJson(url)),
      ...panelUrls.map((url) => loadJson(url)),
    ]);
    const byUrl = new Map(urls.map((url, i) => [url, loaded[i]]));
    const panelsByUrl = new Map(
      panelUrls.map((url, i) => [url, loaded[urls.length + i] || []]),
    );
    const statsBlocks = panelsByUrl.get(STATS_URL);

    let shown = 0;
    for (const section of SECTIONS) {
      const container = document.getElementById(section.container);
      if (!container) continue;

      const rows = byUrl.get(section.url);
      if (!rows) {
        showMessage(
          container,
          "Could not load today's brief",
          `Tried ${section.url}. Serve this folder over HTTP — opening the file ` +
            "directly blocks fetch(). Try:  python3 -m http.server 8000",
        );
        continue;
      }
      shown += renderSection(section, rows, container, {
        stats: statsBlocks,
        panels: section.panels ? panelsByUrl.get(section.panels) : null,
      });
    }

    if (stamp && !stamped) {
      // No build stamp to show: the best available signal is the newest filing.
      const covered = byUrl.get(DATA_URL);
      if (shown) {
        const newest = (covered || [])
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
