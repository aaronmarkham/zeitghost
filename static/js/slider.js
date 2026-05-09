// Combined date-range filter + bias slider + live snapshot stats.
//
// Filters: an article is visible iff it passes BOTH the date range filter
// (from the toolbar buttons) AND the bias slider's tolerance window.
//
// Snapshot stats (count / date range / avg bias) reflect the currently
// visible set, so they update live when either filter changes.
(function () {
    const slider = document.getElementById("bias-slider");
    const status = document.getElementById("slider-status");
    const rangeButtons = Array.from(document.querySelectorAll(".range-btn"));
    const snapCount = document.getElementById("snap-count");
    const snapRange = document.getElementById("snap-range");
    const snapAvg = document.getElementById("snap-avg");
    const snapAvgLabel = document.getElementById("snap-avg-label");

    if (!slider) return;

    const cards = Array.from(document.querySelectorAll("article.card"));

    const VARIANT_NAME = {
        left: "Left-leaning rewrite",
        right: "Right-leaning rewrite",
        original: "Original framing",
    };
    const VARIANT_POSITION = { left: 0.20, right: 0.80 };
    const VARIANT_SHORT = { left: "left", right: "right", original: "original" };

    // Mirror analytics.HISTOGRAM_BUCKETS so the JS-calculated avg label
    // matches the labels Python writes onto individual cards. Keep in sync.
    function biasLabelFor(score) {
        if (score < 0.2) return "left";
        if (score < 0.4) return "center-left";
        if (score < 0.6) return "center";
        if (score < 0.8) return "center-right";
        return "right";
    }

    let activeRangeDays = 0; // 0 = no date filter

    function variantForValue(v) {
        if (v < 35) return "left";
        if (v > 65) return "right";
        return "original";
    }

    function visibilityWindow(v) {
        const target = v / 100;
        const distFromCenter = Math.abs(target - 0.5);
        const tolerance = 0.55 - distFromCenter * 0.7;
        return { target, tolerance };
    }

    function statusText(name, visible, total) {
        const subject = VARIANT_NAME[name].toLowerCase();
        if (visible === total) return `Showing ${subject} for all ${total} articles`;
        return `Showing ${subject} for ${visible} of ${total} articles`;
    }

    function fmtDate(iso) {
        if (!iso) return "?";
        const d = new Date(iso);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
    }

    function updateSnapshot(visibleCards) {
        const n = visibleCards.length;
        if (snapCount) snapCount.textContent = n;
        if (n === 0) {
            if (snapRange) snapRange.textContent = "no articles in range";
            if (snapAvg) snapAvg.textContent = "—";
            if (snapAvgLabel) snapAvgLabel.textContent = "";
            return;
        }
        let minD = "9999-12-31", maxD = "0000-01-01", sum = 0, count = 0;
        for (const c of visibleCards) {
            const p = c.dataset.published || "";
            if (p && p < minD) minD = p;
            if (p && p > maxD) maxD = p;
            const b = parseFloat(c.dataset.bias);
            if (!isNaN(b)) { sum += b; count += 1; }
        }
        if (snapRange) snapRange.textContent = `${fmtDate(minD)} → ${fmtDate(maxD)}`;
        const avg = count > 0 ? sum / count : 0;
        if (snapAvg) snapAvg.textContent = avg.toFixed(2);
        if (snapAvgLabel) snapAvgLabel.textContent = `(${biasLabelFor(avg)})`;
    }

    function apply() {
        const v = parseInt(slider.value, 10);
        const variant = variantForValue(v);
        const { target, tolerance } = visibilityWindow(v);

        // Cutoff in ms-since-epoch for the date filter, or null = no filter.
        const cutoff = activeRangeDays > 0
            ? Date.now() - activeRangeDays * 86400000
            : null;

        const visibleCards = [];

        cards.forEach((card) => {
            // Date filter
            let inDateRange = true;
            if (cutoff !== null) {
                const p = card.dataset.published;
                if (p) {
                    const d = new Date(p);
                    inDateRange = !isNaN(d.getTime()) && d.getTime() >= cutoff;
                }
            }

            // Bias filter (slider tolerance window)
            const bias = parseFloat(card.dataset.bias);
            const inBiasWindow =
                isNaN(bias) || Math.abs(bias - target) <= tolerance;

            const visible = inDateRange && inBiasWindow;
            card.classList.toggle("filter-hidden", !visible);
            if (visible) visibleCards.push(card);

            // Variant swap (always — even hidden cards, in case they reappear)
            card.querySelectorAll(".variant").forEach((el) => { el.hidden = true; });
            const target_el = card.querySelector(".variant-" + variant);
            if (target_el) target_el.hidden = false;

            // Bias chart: move Current marker, refresh legend text
            const chart = card.querySelector(".bias-chart");
            if (chart) {
                const sourcePos = parseFloat(chart.dataset.sourcePos);
                const currentPos =
                    variant in VARIANT_POSITION
                        ? VARIANT_POSITION[variant]
                        : sourcePos;
                const currentMarker = chart.querySelector(".bias-bar-marker-current");
                if (currentMarker) currentMarker.style.left = (currentPos * 100).toFixed(1) + "%";
                const currentVal = chart.querySelector(".legend-current-value");
                if (currentVal) currentVal.textContent =
                    VARIANT_SHORT[variant] + " · " + currentPos.toFixed(2);
            }
        });

        if (status) status.textContent = statusText(variant, visibleCards.length, cards.length);
        updateSnapshot(visibleCards);
    }

    rangeButtons.forEach((btn) => {
        btn.addEventListener("click", () => {
            rangeButtons.forEach((b) => b.classList.remove("active"));
            btn.classList.add("active");
            activeRangeDays = parseInt(btn.dataset.days, 10) || 0;
            apply();
        });
    });

    slider.addEventListener("input", apply);
    apply();
})();
