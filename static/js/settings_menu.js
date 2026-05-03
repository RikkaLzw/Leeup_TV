(function () {
  const toggle = document.getElementById("settingsMenuToggle");
  const menu = document.getElementById("settingsMenu");
  const aboutOpen = document.getElementById("aboutDialogOpen");
  const aboutDialog = document.getElementById("aboutDialog");
  const copyButtons = document.querySelectorAll("[data-copy-text]");
  if (!toggle || !menu) return;

  function setOpen(open) {
    menu.hidden = !open;
    toggle.classList.toggle("active", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  toggle.addEventListener("click", () => {
    setOpen(menu.hidden);
  });

  function openAbout() {
    if (!aboutDialog) return;
    setOpen(false);
    aboutDialog.hidden = false;
    document.body.classList.add("modal-open");
    aboutDialog.querySelector("[data-about-close]")?.focus?.();
  }

  function closeAbout() {
    if (!aboutDialog) return;
    aboutDialog.hidden = true;
    document.body.classList.remove("modal-open");
    toggle.focus();
  }

  aboutOpen?.addEventListener("click", openAbout);
  aboutDialog?.addEventListener("click", (event) => {
    if (event.target.closest("[data-about-close]")) closeAbout();
  });

  copyButtons.forEach((button) => {
    const defaultText = button.textContent || "复制";
    button.addEventListener("click", async () => {
      const value = button.getAttribute("data-copy-text") || "";
      if (!value) return;
      try {
        await navigator.clipboard.writeText(value);
      } catch (_error) {
        const input = document.createElement("textarea");
        input.value = value;
        input.setAttribute("readonly", "");
        input.style.position = "fixed";
        input.style.left = "-9999px";
        document.body.appendChild(input);
        input.select();
        document.execCommand("copy");
        input.remove();
      }
      button.textContent = "已复制";
      window.setTimeout(() => {
        button.textContent = defaultText;
      }, 1600);
    });
  });

  document.addEventListener("click", (event) => {
    if (menu.hidden) return;
    if (menu.contains(event.target) || toggle.contains(event.target)) return;
    setOpen(false);
  });

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && aboutDialog && !aboutDialog.hidden) {
      closeAbout();
      return;
    }
    if (event.key === "Escape" && !menu.hidden) {
      setOpen(false);
      toggle.focus();
    }
  });
})();
