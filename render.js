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
  // The Lietuva page has its own beat too — Lithuanian newsrooms, not Lithuania as
  // the world roster happens to report it. See fetch_lietuva.py.
  const LIETUVA_URL = "data/lietuva.json";
  // Europe is the odd one out: it has no beat of its own, because the world
  // pipeline already classifies a region on every story and nothing was reading
  // it. See fetch_europe.py.
  const EUROPE_URL = "data/europe.json";
  const MAX_STORIES = 5; // "the five most important topics"
  const MAX_SECTION_STORIES = 5;
  const MAX_PER_LEAN = 2; // frames shown per lean; keeps the arrows from feeling endless
  // Rows shown per statistics table. A championship table is twenty rows nobody
  // scrolls on a phone; five is the shape of the thing and the leader is the point.
  const MAX_STAT_ROWS = 5;

  // A morning paper is out of date once it has missed a morning. Thirty rather
  // than twenty-four so a slightly late 07:00 run never trips it by accident.
  const STALE_AFTER_HOURS = 30;

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
    },
    {
      container: "techStories",
      title: "Tech",
      url: SECTION_URL,
      category: "Tech",
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
  function icon(name) {
    const markup = ICONS[name];
    if (!markup) return null;
    if (!iconCache.has(name)) {
      // Parsed as HTML, NOT as "image/svg+xml". Chromium's XML parser returns the
      // root element with a null namespace, and an element in no namespace is not
      // an SVG: every icon sat in the DOM at the right size, reserving its space,
      // and painted nothing. The tabs and panel headings were blank for days
      // while the boxes around them measured correctly. HTML parsing puts <svg>
      // in the SVG namespace, which is the only thing that makes it draw.
      const parsed = new DOMParser().parseFromString(markup, "text/html");
      const svg = parsed.body && parsed.body.firstElementChild;
      if (!svg || svg.nodeName !== "svg") return null;
      iconCache.set(name, document.importNode(svg, true));
    }
    return iconCache.get(name).cloneNode(true);
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

  /** Draw one panel in whichever of the four shapes it asked for. */
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

  /** Render one page's cards into its container. Returns how many it drew. */
  function renderSection(section, rows, container, statsBlocks) {
    let stories;
    if (section.category) {
      stories = rankSections(rows, section.category);
    } else if (section.presorted) {
      // Deliberately no cap. fetch_sports.py already ranked this page and capped
      // it, and it is the better judge: its round-robin balances the interests, so
      // applying a second, smaller cap here silently deleted whole interests —
      // EuroLeague and the international game were what got cut when this page
      // capped at 5 and the file held 8.
      stories = rows.filter((row) => row.title && row.url);
    } else {
      stories = rankStories(rows).slice(0, MAX_STORIES);
    }

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

    if (section.stats) renderDashboard(container, statsBlocks);

    // The feed is wrapped so the tabs can tell an empty filter from a page with
    // no news on it: hiding every card under NBA should say so, not look broken.
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
    // says so on its own. Statistics are fetched alongside but never gate a page:
    // a dead results source should cost the reader a table, not a page.
    const urls = [...new Set(SECTIONS.map((s) => s.url))];
    const loaded = await Promise.all([
      ...urls.map((url) => loadJson(url)),
      loadJson(STATS_URL),
    ]);
    const byUrl = new Map(urls.map((url, i) => [url, loaded[i]]));
    const statsBlocks = loaded[urls.length] || [];

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
      shown += renderSection(section, rows, container, statsBlocks);
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
