// Live chapter status while a generation is running: poll the project's
// status JSON and update the table; reload once everything settles so the
// buttons re-enable with correct labels.
(function () {
  const table = document.getElementById("chapters");
  if (!table || table.dataset.generating !== "1") return;
  const projectId = table.dataset.projectId;

  const timer = setInterval(async () => {
    let data;
    try {
      const r = await fetch(`/projects/${projectId}/status.json`);
      if (!r.ok) return;
      data = await r.json();
    } catch {
      return; // transient network hiccup; try again next tick
    }
    let stillGenerating = false;
    for (const ch of data.chapters) {
      const row = table.querySelector(`tr[data-chapter-id="${ch.id}"]`);
      if (!row) continue;
      const busy = ch.status === "generating" || ch.status === "recasting";
      const label = busy
        ? `${ch.status} (${ch.chunks_done}/${ch.chunks_total})`
        : ch.status;
      row.querySelector(".js-status").textContent = label;
      if (busy) stillGenerating = true;
    }
    if (!stillGenerating && !data.assembling) {
      clearInterval(timer);
      location.reload();
    }
  }, 2000);
})();
