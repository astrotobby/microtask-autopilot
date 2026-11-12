const TYPE_LABELS = {
  ai_training: "AI training",
  microtask: "Microtasks",
  professional_marketplace: "Professional marketplace",
  user_research: "User research",
  bounty_marketplace: "Bounty marketplace",
};

const safeText = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
}[char]));

function officialHref(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

function typeLabel(type) {
  return TYPE_LABELS[type] || "Other platform";
}

function statusClass(platform) {
  const state = `${platform.tracking_status} ${platform.kenya_status}`.toLowerCase();
  if (state.includes("disabled") || state.includes("waitlist") || state.includes("restricted") || state.includes("unsupported")) {
    return "state-restricted";
  }
  if (state.includes("public api") || state.includes("policy monitor")) {
    return "state-active";
  }
  return "state-human";
}

function initials(name) {
  const words = name.trim().split(/\s+/).filter(Boolean);
  return words.slice(0, 2).map((part) => part[0]).join("").toUpperCase();
}

function renderCard(platform) {
  const href = officialHref(platform.official_url);
  const status = safeText(platform.tracking_status);
  return `
    <article class="platform-card">
      <div class="platform-card-head">
        <div class="platform-dot" aria-hidden="true">${safeText(initials(platform.name))}</div>
        <div>
          <h3>${safeText(platform.name)}</h3>
          <span class="platform-type">${safeText(typeLabel(platform.type))}</span>
        </div>
        <span class="state-pill ${statusClass(platform)}">${status}</span>
      </div>
      <div class="platform-meta">
        <div class="platform-meta-row"><span>Kenya eligibility</span><strong>${safeText(platform.kenya_status)}</strong></div>
        <div class="platform-meta-row"><span>Discovery</span><strong>${safeText(platform.discovery)}</strong></div>
      </div>
      <div class="platform-foot">
        ${href ? `<a href="${safeText(href)}" target="_blank" rel="noopener noreferrer">Official source ↗</a>` : `<span>Official identity needs review</span>`}
        <span>Not a live job feed</span>
      </div>
    </article>`;
}

function setSnapshotDate(date) {
  const dateLabel = new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${date}T00:00:00Z`));
  document.querySelectorAll("#snapshot-date, #footer-date").forEach((node) => {
    node.textContent = dateLabel;
  });
}

async function startDashboard() {
  const grid = document.querySelector("#platform-grid");
  const count = document.querySelector("#directory-count");
  const search = document.querySelector("#platform-search");
  const category = document.querySelector("#platform-category");

  try {
    const response = await fetch("./data/platforms.json", { cache: "no-store" });
    if (!response.ok) throw new Error("Registry could not be loaded");
    const registry = await response.json();
    if (!Array.isArray(registry.platforms) || registry.platforms.length !== registry.platform_count) {
      throw new Error("Registry snapshot is incomplete");
    }

    document.querySelector("#metric-platforms").textContent = String(registry.platform_count);
    document.querySelector("#metric-discovery").textContent = String(registry.automated_public_discovery_sources);
    document.querySelector("#metric-autonomous").textContent = String(registry.fully_autonomous_earning_sources);
    setSnapshotDate(registry.updated_at);

    const categories = [...new Set(registry.platforms.map((item) => item.type))]
      .sort((a, b) => typeLabel(a).localeCompare(typeLabel(b)));
    categories.forEach((key) => {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = typeLabel(key);
      category.append(option);
    });

    const openTrain = registry.platforms.find((item) => item.id === "opentrain_ai");
    const sampleLink = document.querySelector("#sample-official-link");
    const sampleUrl = officialHref(openTrain?.official_url);
    if (sampleUrl) sampleLink.href = sampleUrl;

    function updateDirectory() {
      const term = search.value.trim().toLocaleLowerCase();
      const selectedType = category.value;
      const visible = registry.platforms.filter((item) => {
        const matchesType = selectedType === "all" || item.type === selectedType;
        const searchText = [item.name, typeLabel(item.type), item.kenya_status, item.discovery, item.tracking_status]
          .join(" ").toLocaleLowerCase();
        return matchesType && (!term || searchText.includes(term));
      });
      grid.innerHTML = visible.length
        ? visible.map(renderCard).join("")
        : '<div class="empty-state">No tracked platforms match this search. Try a different term or category.</div>';
      count.textContent = `${visible.length} of ${registry.platform_count} tracked platforms`;
      grid.setAttribute("aria-busy", "false");
    }

    search.addEventListener("input", updateDirectory);
    category.addEventListener("change", updateDirectory);
    updateDirectory();
  } catch {
    grid.innerHTML = '<div class="load-error">The public registry snapshot could not be loaded. No live account or opportunity data is requested.</div>';
    count.textContent = "Registry unavailable";
    grid.setAttribute("aria-busy", "false");
  }
}

document.addEventListener("DOMContentLoaded", startDashboard);
