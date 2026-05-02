(function () {
  const toggle = document.getElementById("mobileSearchToggle");
  const panel = document.getElementById("mobileSearchPanel");
  const input = document.getElementById("mobileSearchInput");
  if (!toggle || !panel) return;

  function setOpen(open) {
    panel.hidden = !open;
    toggle.classList.toggle("active", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      window.setTimeout(() => input?.focus(), 50);
    }
  }

  toggle.addEventListener("click", () => {
    setOpen(panel.hidden);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
  });

  document.addEventListener("click", (event) => {
    if (panel.hidden) return;
    if (panel.contains(event.target) || toggle.contains(event.target)) return;
    setOpen(false);
  });
})();
