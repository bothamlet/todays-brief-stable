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

    // Remove in-view from old page
    pages[currentPage].classList.remove("in-view");

    currentPage = index;

    const offset = -currentPage * PAGE_WIDTH();
    if (animate) {
      wrapper.style.transition = `transform ${SNAP_DURATION}ms cubic-bezier(0.25, 0.1, 0.25, 1)`;
    } else {
      wrapper.style.transition = "none";
    }
    wrapper.style.transform = `translateX(${offset}px)`;

    // Update dots
    dots.forEach((d, i) => d.classList.toggle("active", i === currentPage));

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
  wrapper.addEventListener("touchcancel", onPointerUp);

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
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      goToPage(currentPage + 1);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
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
