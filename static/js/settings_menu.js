(function () {
  const toggle = document.getElementById("settingsMenuToggle");
  const menu = document.getElementById("settingsMenu");
  if (!toggle || !menu) return;

  function setOpen(open) {
    menu.hidden = !open;
    toggle.classList.toggle("active", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  toggle.addEventListener("click", () => {
    setOpen(menu.hidden);
  });

  document.addEventListener("click", (event) => {
    if (menu.hidden) return;
    if (menu.contains(event.target) || toggle.contains(event.target)) return;
    setOpen(false);
  });

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      setOpen(false);
      toggle.focus();
    }
  });
})();
