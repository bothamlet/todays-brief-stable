/* Swipe, keyboard, and dot navigation. */
(() => {
  const wrapper = document.getElementById("pagesWrapper");
  const frame = wrapper.closest(".phone-frame");
  const dotsContainer = document.getElementById("pageDots");
  const pages = document.querySelectorAll(".page");
  const dots = dotsContainer.querySelectorAll(".dot");

  let currentPage = 0;
  let startX = 0;
  let startY = 0;
  let isDragging = false;
  let dragOffset = 0;
  let isVerticalScroll = false;
  let directionDecided = false;
  let startedAt = 0;
  let suppressClickUntil = 0;

  const PAGE_WIDTH = () => pages[0].getBoundingClientRect().width;
  const THRESHOLD = 36;
  const SNAP_DURATION = 280;


  function goToPage(index, animate = true) {
    if (index < 0 || index >= pages.length) return;

    const oldPage = pages[currentPage];
    const movingFocus = oldPage.contains(document.activeElement) && index !== currentPage;
    currentPage = index;
    pages.forEach((page, i) => {
      const active = i === currentPage;
      page.classList.toggle("in-view", active);
      page.inert = !active;
      page.setAttribute("aria-hidden", String(!active));
    });
    if (movingFocus) dots[currentPage].focus({ preventScroll: true });

    const offset = -currentPage * PAGE_WIDTH();
    if (animate) {
      const duration = matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : SNAP_DURATION;
      wrapper.style.transition = `transform ${duration}ms cubic-bezier(0.22, 0.61, 0.36, 1)`;
    } else {
      wrapper.style.transition = "none";
    }
    wrapper.style.transform = `translateX(${offset}px)`;

    dots.forEach((dot, i) => {
      dot.classList.toggle("active", i === currentPage);
      if (i === currentPage) dot.setAttribute("aria-current", "page");
      else dot.removeAttribute("aria-current");
    });

    requestAnimationFrame(() => {
      pages[currentPage].classList.add("in-view");
    });

    const page = pages[currentPage];
    const accent = page.dataset.accent;
    const accentRgb = page.dataset.accentRgb;
    document.documentElement.style.setProperty("--accent", accent);
    document.documentElement.style.setProperty("--accent-rgb", accentRgb);
  }


  function onPointerDown(e) {
    if (e.touches && e.touches.length !== 1) return;
    // Movement distinguishes a swipe from a tap on a headline or control.
    if (!e.touches && e.target.closest("button, a, input")) return;
    if (e.target.closest("input, textarea, select, [contenteditable]")) return;

    isDragging = true;
    isVerticalScroll = false;
    directionDecided = false;
    startX = e.type.startsWith("touch") ? e.touches[0].clientX : e.clientX;
    startY = e.type.startsWith("touch") ? e.touches[0].clientY : e.clientY;
    dragOffset = 0;
    startedAt = performance.now();
    suppressClickUntil = 0;
  }

  function onPointerMove(e) {
    if (!isDragging) return;
    if (e.touches && e.touches.length !== 1) {
      isDragging = false;
      goToPage(currentPage);
      return;
    }

    const currentX = e.type.startsWith("touch")
      ? e.touches[0].clientX
      : e.clientX;
    const currentY = e.type.startsWith("touch")
      ? e.touches[0].clientY
      : e.clientY;
    const diffX = currentX - startX;
    const diffY = currentY - startY;

    // Wait for a clear axis before taking over native scrolling.
    const x = Math.abs(diffX);
    const y = Math.abs(diffY);
    if (!directionDecided && Math.max(x, y) >= 10 &&
        (x > y * 1.2 || y > x * 1.2 || Math.max(x, y) >= 20)) {
      directionDecided = true;
      isVerticalScroll = y >= x;
      if (!isVerticalScroll) wrapper.style.transition = "none";
    }

    if (!directionDecided || isVerticalScroll) return;

    if (e.cancelable) e.preventDefault();
    suppressClickUntil = performance.now() + 500;

    dragOffset = diffX;
    if (
      (currentPage === 0 && dragOffset > 0) ||
      (currentPage === pages.length - 1 && dragOffset < 0)
    ) {
      dragOffset *= 0.3; // rubber-band effect
    }

    const baseOffset = -currentPage * PAGE_WIDTH();
    wrapper.style.transform = `translateX(${baseOffset + dragOffset}px)`;
  }

  function onPointerUp() {
    if (!isDragging) return;
    isDragging = false;

    if (isVerticalScroll || !directionDecided) return;

    suppressClickUntil = performance.now() + 500;
    const distance = Math.abs(dragOffset);
    const speed = distance / Math.max(performance.now() - startedAt, 1);
    const shouldMove = distance >= THRESHOLD || (distance >= 18 && speed >= 0.4);
    if (shouldMove && dragOffset < 0 && currentPage < pages.length - 1) {
      goToPage(currentPage + 1);
    } else if (shouldMove && dragOffset > 0 && currentPage > 0) {
      goToPage(currentPage - 1);
    } else {
      goToPage(currentPage); // snap back
    }
  }

  // Listen on navigation controls as well as the story area.
  frame.addEventListener("touchstart", onPointerDown, { passive: true });
  frame.addEventListener("touchmove", onPointerMove, { passive: false });
  frame.addEventListener("touchend", onPointerUp);
  frame.addEventListener("touchcancel", () => {
    isDragging = false;
    goToPage(currentPage);
  });
  frame.addEventListener("click", (event) => {
    if (performance.now() < suppressClickUntil) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);

  wrapper.addEventListener("mousedown", onPointerDown);
  window.addEventListener("mousemove", onPointerMove);
  window.addEventListener("mouseup", onPointerUp);

  dots.forEach((dot, i) => {
    dot.addEventListener("click", () => goToPage(i));
    dot.style.cursor = "pointer";
  });

  document.querySelectorAll(".page-swipe-hint").forEach((hint) => {
    hint.addEventListener("click", () => {
      if (currentPage < pages.length - 1) {
        goToPage(currentPage + 1);
      }
    });
  });

  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.target.closest("input, textarea, select, [contenteditable]")) return;
    if (e.key === "ArrowRight") {
      e.preventDefault();
      goToPage(currentPage + 1);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      goToPage(currentPage - 1);
    }
  });

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => goToPage(currentPage, false), 100);
  });

  goToPage(0);
})();
