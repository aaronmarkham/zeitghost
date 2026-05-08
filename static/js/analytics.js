// All-sources filter — show/hide rows based on free-text match against the
// source name. Updates the count badge so users see how many match.
(function () {
    const input = document.getElementById("source-filter-input");
    const table = document.getElementById("all-sources-table");
    const countEl = document.getElementById("source-filter-count");
    if (!input || !table) return;

    const rows = Array.from(table.querySelectorAll("tbody tr"));
    const total = rows.length;

    function applyFilter(term) {
        const t = term.trim().toLowerCase();
        let visible = 0;
        rows.forEach((row) => {
            const nameCell = row.querySelector(".source-name");
            const name = nameCell ? nameCell.textContent.toLowerCase() : "";
            const match = !t || name.includes(t);
            row.hidden = !match;
            if (match) visible += 1;
        });
        if (countEl) {
            countEl.textContent =
                t === ""
                    ? `${total} sources`
                    : `${visible} of ${total} sources`;
        }
    }

    input.addEventListener("input", (e) => applyFilter(e.target.value));
})();
