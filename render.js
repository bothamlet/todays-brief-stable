/** Today's Brief — render the six phone sections from pipeline output or an offline bundle. */
(() => {
  const DATA_URL = "data/world_covered.json";
  const BUILT_URL = "data/built.json";
  // The category pages read the tracked list: same classification as
  // world_filtered.json, plus the history the developing cue needs.
  const SECTION_URL = "data/world_tracked.json";
  // The sports page has its own beat and its own file — see fetch_sports.py. It
  // arrives already ranked and capped, so the page renders what it is handed.
  const SPORTS_URL = "data/sports.json";
  // The tech page has its own beat too — see fetch_tech.py. It reads the world
  // roster for its policy coverage and its own sources for everything else,
  // because a page built only from general news desks is a page about AI
  // legislation rather than about AI.
  const TECH_URL = "data/tech.json";
  // The Lietuva page has its own beat too — Lithuanian newsrooms, not Lithuania as
  // the world roster happens to report it. See fetch_lietuva.py.
  const LIETUVA_URL = "data/lietuva.json";
  // Europe is the odd one out: it has no beat of its own, because the world
  // pipeline already classifies a region on every story and nothing was reading
  // it. See fetch_europe.py.
  const EUROPE_URL = "data/europe.json";
  const MAX_STORIES = 5; // "the five most important topics"
  // Beat pages show five to eight stories; World uses the pipeline's ranked set.
  // See capStories for what earns a slot past the fifth.
  const MAX_STORIES_NOTABLE = 8;
  // How many newsrooms have to have carried a story independently for it to be
  // worth a sixth slot. Three is a low bar and still rare: across the whole last
  // run only three stories cleared it, because most reach the pipeline through a
  // single feed. See isNotable for why coverage and not significance.
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
    },
    {
      container: "otherStories",
      title: "Other",
      url: SECTION_URL,
      category: "Other",
    },
  ];

  const LEAN_LABEL = { left: "Left", center: "Centre", right: "Right" };

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
      const url = String(frame.url || lead.url || "");
      headlineLink.href = /^https?:\/\//i.test(url) ? url : "#";
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
      if (frames.length > 1 && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
        event.preventDefault();
        event.stopPropagation();
        step(event.key === "ArrowLeft" ? -1 : 1);
      }
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
  function renderSection(section, rows, container) {
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
    const warnings = document.querySelectorAll("[data-stale]");
    if (!stamp) return false;

    let builtAt;
    let payloadDemo = false;
    try {
      const payload = await loadJson(BUILT_URL);
      // Checked before constructing the date: `new Date(null)` is the epoch, not
      // an invalid date, so a missing file would have dated the paper 1970 and
      // tripped the stale badge rather than falling back.
      if (!payload || !payload.built_at) throw new Error("no stamp");
      payloadDemo = payload.demo === true;
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
    stamp.textContent = payloadDemo ? "Sample issue · fictional headlines" : `${weekday}, ${dayMonth}`;

    // Every section carries the same edition date and freshness warning.
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

    // Load each section independently so a missing source costs one section.
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
      // No build stamp to show: the best available signal is the newest filing.
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
