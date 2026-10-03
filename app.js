/**
 * Today's Brief — Swipe Navigation
 * Handles horizontal page swiping, dot navigation, and touch support.
 */
(() => {
  const wrapper = document.getElementById("pagesWrapper");
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

  const PAGE_WIDTH = () => pages[0].getBoundingClientRect().width;
  const THRESHOLD = 50; // minimum px to trigger page change
  const SNAP_DURATION = 500;

  // --- Core navigation ---

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
      wrapper.style.transition = `transform ${duration}ms cubic-bezier(0.25, 0.1, 0.25, 1)`;
    } else {
      wrapper.style.transition = "none";
    }
    wrapper.style.transform = `translateX(${offset}px)`;

    // Update dots
    dots.forEach((dot, i) => {
      dot.classList.toggle("active", i === currentPage);
      if (i === currentPage) dot.setAttribute("aria-current", "page");
      else dot.removeAttribute("aria-current");
    });

    // Trigger entrance animation
    requestAnimationFrame(() => {
      pages[currentPage].classList.add("in-view");
    });

    // Update CSS accent variables for the current page
    const page = pages[currentPage];
    const accent = page.dataset.accent;
    const accentRgb = page.dataset.accentRgb;
    document.documentElement.style.setProperty("--accent", accent);
    document.documentElement.style.setProperty("--accent-rgb", accentRgb);
  }

  // --- Touch / pointer handling ---

  function onPointerDown(e) {
    if (e.touches && e.touches.length !== 1) return;
    // Ignore touches on interactive elements
    if (e.target.closest("button, a, input")) return;

    isDragging = true;
    isVerticalScroll = false;
    directionDecided = false;
    startX = e.type.startsWith("touch") ? e.touches[0].clientX : e.clientX;
    startY = e.type.startsWith("touch") ? e.touches[0].clientY : e.clientY;
    dragOffset = 0;
    wrapper.style.transition = "none";
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

    // Decide scroll direction on first significant movement
    if (!directionDecided && (Math.abs(diffX) > 5 || Math.abs(diffY) > 5)) {
      directionDecided = true;
      isVerticalScroll = Math.abs(diffY) > Math.abs(diffX);
    }

    if (isVerticalScroll) return; // let the page scroll vertically

    e.preventDefault(); // prevent vertical scroll while swiping horizontally

    // Apply resistance at edges
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

    if (dragOffset < -THRESHOLD && currentPage < pages.length - 1) {
      goToPage(currentPage + 1);
    } else if (dragOffset > THRESHOLD && currentPage > 0) {
      goToPage(currentPage - 1);
    } else {
      goToPage(currentPage); // snap back
    }
  }

  // Touch events
  wrapper.addEventListener("touchstart", onPointerDown, { passive: true });
  wrapper.addEventListener("touchmove", onPointerMove, { passive: false });
  wrapper.addEventListener("touchend", onPointerUp);
  wrapper.addEventListener("touchcancel", () => {
    isDragging = false;
    goToPage(currentPage);
  });

  // Mouse events (for desktop testing)
  wrapper.addEventListener("mousedown", onPointerDown);
  window.addEventListener("mousemove", onPointerMove);
  window.addEventListener("mouseup", onPointerUp);

  // --- Dot click navigation ---
  dots.forEach((dot, i) => {
    dot.addEventListener("click", () => goToPage(i));
    dot.style.cursor = "pointer";
  });

  // --- Swipe hint click ---
  document.querySelectorAll(".page-swipe-hint").forEach((hint) => {
    hint.addEventListener("click", () => {
      if (currentPage < pages.length - 1) {
        goToPage(currentPage + 1);
      }
    });
  });

  // --- Keyboard navigation ---
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

  // --- Window resize: re-snap ---
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => goToPage(currentPage, false), 100);
  });

  // --- Init ---
  goToPage(0);
})();
