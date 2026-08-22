(() => {
  const selector = document.getElementById("odooctl-version-selector");
  const banner = document.querySelector(".odooctl-version-banner");
  if (!selector || !banner) return;

  const currentChannel = banner.dataset.docsChannel;
  const currentPath = window.location.pathname.replace(/^\/docs\/(?:[^/]+)\/?/, "");
  fetch("/docs/versions.json", { credentials: "same-origin" })
    .then((response) => response.ok ? response.json() : Promise.reject(response))
    .then((manifest) => {
      for (const entry of manifest.versions) {
        const option = document.createElement("option");
        option.value = entry.canonical_url;
        option.textContent = `${entry.version} (${entry.channel})`;
        option.selected = entry.channel === currentChannel;
        selector.append(option);
      }
      selector.addEventListener("change", () => {
        const target = new URL(selector.value, window.location.origin);
        target.pathname += currentPath;
        window.location.assign(target);
      });
    })
    .catch(() => { selector.hidden = true; });
})();
